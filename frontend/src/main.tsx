import { FormEvent, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type User = { id: string; email: string; role: string }
type Company = { id: string; display_name: string; summary?: string; primary_industry: string; job_count: number; updated_at: string }
type Job = { id: string; canonical_title: string; recruitment_type: string; employment_type: string; status: string; locations_json?: string; locations?: string[]; company_name?: string; updated_at?: string }
type Application = Job & { state: string; favorite: number; updated_at: string }
type CompanyDetail = Company & { aliases: string[]; jobs: Job[]; evidences: { source_url?: string; source_type: string; excerpt?: string }[] }
type Notification = { id: string; title: string; body: string; read_at?: string | null }
type Invitation = { id: string; email: string; role: string; expires_at: string; used_at?: string | null; created_at: string }
type ReviewItem = { id: string; kind: string; entity_type?: string; entity_id?: string; payload: Record<string, any> }

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const isForm = options.body instanceof FormData
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || response.statusText)
  }
  return response.json()
}

function App() {
  const [user, setUser] = useState<User | null>(null)
  const [initialized, setInitialized] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [page, setPage] = useState<'companies' | 'applications' | 'import' | 'admin' | 'settings' | 'review'>('companies')
  const [companies, setCompanies] = useState<Company[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [applications, setApplications] = useState<Application[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<CompanyDetail | null>(null)
  const [notice, setNotice] = useState('')
  const [notifications, setNotifications] = useState<Notification[]>([])

  const loadData = async () => {
    const [companyData, jobData, applicationData, notificationData] = await Promise.all([
      api<Company[]>(`/companies?q=${encodeURIComponent(query)}`),
      api<Job[]>('/jobs'),
      api<Application[]>('/me/applications'),
      api<Notification[]>('/notifications'),
    ])
    setCompanies(companyData)
    setJobs(jobData)
    setApplications(applicationData)
    setNotifications(notificationData)
  }

  useEffect(() => {
    Promise.all([
      api<{ user: User }>('/auth/me'),
      api<{ initialized: boolean }>('/bootstrap/status'),
    ]).then(([me, status]) => {
      setUser(me.user)
      setInitialized(status.initialized)
      return loadData()
    }).catch(async () => {
      const status = await api<{ initialized: boolean }>('/bootstrap/status').catch(() => ({ initialized: true }))
      setInitialized(status.initialized)
    }).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!user) return
    const source = new EventSource('/api/v1/events')
    const refresh = () => loadData().catch(() => undefined)
    source.addEventListener('job.created', refresh)
    source.addEventListener('job.updated', refresh)
    source.addEventListener('company.created', refresh)
    source.addEventListener('sync.completed', refresh)
    return () => source.close()
  }, [user, query])

  const flash = (message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(''), 3000)
  }

  if (loading) return <div className="loading-screen"><div className="spinner" />正在加载 JobPostings…</div>
  if (!user) return initialized ? <Login onLoggedIn={setUser} /> : <Bootstrap onLoggedIn={setUser} />

  const sync = async () => {
    try { const result = await api<{ fetched: number }>('/admin/sync', { method: 'POST' }); flash(`同步完成，新增 ${result.fetched} 条消息`); await loadData() }
    catch (e) { setError((e as Error).message) }
  }
  const openCompany = async (id: string) => {
    try { setSelected(await api<CompanyDetail>(`/companies/${id}`)) }
    catch (e) { setError((e as Error).message) }
  }
  const updateState = async (jobId: string, state: string, favorite?: boolean) => {
    try { await api(`/me/jobs/${jobId}/state`, { method: 'PUT', body: JSON.stringify({ state, ...(favorite === undefined ? {} : { favorite }) }) }); flash('求职进度已更新'); await loadData() }
    catch (e) { setError((e as Error).message) }
  }
  const followCompany = async (companyId: string, followed = true) => {
    try { await api(`/me/companies/${companyId}/follow`, { method: 'PUT', body: JSON.stringify({ followed }) }); flash(followed ? '已关注企业' : '已取消关注') }
    catch (e) { setError((e as Error).message) }
  }
  const exportJobs = async (format: 'xlsx' | 'csv' | 'json') => {
    try { const result = await api<{ download_url: string }>('/exports?fmt=' + format, { method: 'POST' }); window.open(result.download_url, '_blank', 'noopener,noreferrer'); flash('导出文件已生成') }
    catch (e) { setError((e as Error).message) }
  }
  const markNotificationRead = async (id: string) => {
    await api(`/notifications/${id}/read`, { method: 'POST' })
    setNotifications(current => current.map(item => item.id === id ? { ...item, read_at: new Date().toISOString() } : item))
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="logo"><span className="logo-mark">J</span><span>JobPostings</span></div>
      <div className="side-caption">招聘信息工作台</div>
      <nav>
        <NavButton active={page === 'companies'} onClick={() => { setPage('companies'); setSelected(null) }} icon="⌂">企业与岗位</NavButton>
        <NavButton active={page === 'applications'} onClick={() => { setPage('applications'); setSelected(null) }} icon="✓">求职进度</NavButton>
        <NavButton active={page === 'import'} onClick={() => { setPage('import'); setSelected(null) }} icon="＋">导入信息</NavButton>
        {user.role === 'admin' && <NavButton active={page === 'admin'} onClick={() => { setPage('admin'); setSelected(null) }} icon="▦">管理台</NavButton>}
        {user.role === 'admin' && <NavButton active={page === 'settings'} onClick={() => { setPage('settings'); setSelected(null) }} icon="⚙">系统设置</NavButton>}
        {user.role === 'admin' && <NavButton active={page === 'review'} onClick={() => { setPage('review'); setSelected(null) }} icon="!">待审核</NavButton>}
      </nav>
      <div className="sidebar-bottom"><div className="user-avatar">{user.email.slice(0, 1).toUpperCase()}</div><div><strong>{user.email}</strong><small>{user.role === 'admin' ? '管理员' : '受邀用户'}</small></div><button className="logout" onClick={async () => { await api('/auth/logout', { method: 'POST' }); location.reload() }}>↗</button></div>
    </aside>
    <main className="content">
      {error && <div className="error-banner">{error}<button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="notice">{notice}</div>}
      {!selected && notifications.filter(item => !item.read_at).slice(0, 3).map(item => <div className="notification-strip" key={item.id}><div><strong>{item.title}</strong><span>{item.body}</span></div><button onClick={() => markNotificationRead(item.id)}>知道了</button></div>)}
      {selected ? <CompanyView company={selected} onBack={() => setSelected(null)} onState={updateState} onFollow={followCompany} /> : page === 'companies' ? <CompaniesPage companies={companies} jobs={jobs} query={query} setQuery={setQuery} onSearch={() => loadData()} onSync={user.role === 'admin' ? sync : undefined} onOpen={openCompany} onExport={exportJobs} onImport={() => setPage('import')} /> : page === 'applications' ? <ApplicationsPage applications={applications} onState={updateState} /> : page === 'import' ? <ImportPage onImported={async () => { flash('已加入处理队列'); await loadData() }} /> : page === 'admin' ? <AdminPage onNavigate={target => { setPage(target); setSelected(null) }} /> : page === 'review' ? <ReviewPage onResolved={async () => { flash('审核项已处理'); await loadData() }} /> : <SettingsPage onSaved={flash} />}
    </main>
  </div>
}

function NavButton({ active, onClick, icon, children }: { active: boolean; onClick: () => void; icon: string; children: string }) {
  return <button className={`nav-button ${active ? 'active' : ''}`} onClick={onClick}><span>{icon}</span>{children}</button>
}

function Login({ onLoggedIn }: { onLoggedIn: (user: User) => void }) {
  const [email, setEmail] = useState('')
  const [challenge, setChallenge] = useState('')
  const [code, setCode] = useState('')
  const [debug, setDebug] = useState('')
  const [message, setMessage] = useState('')
  const send = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ challenge_id: string; debug_code?: string }>('/auth/request-code', { method: 'POST', body: JSON.stringify({ email }) }); setChallenge(result.challenge_id); setDebug(result.debug_code || '') } catch (e) { setMessage((e as Error).message) } }
  const verify = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ user: User }>('/auth/verify-code', { method: 'POST', body: JSON.stringify({ challenge_id: challenge, code }) }); onLoggedIn(result.user) } catch (e) { setMessage((e as Error).message) } }
  return <AuthFrame title="欢迎回来" subtitle="使用受邀邮箱进入招聘信息工作台"><form onSubmit={challenge ? verify : send}>{!challenge && <input autoFocus type="email" required placeholder="受邀邮箱" value={email} onChange={e => setEmail(e.target.value)} />}{challenge && <><div className="sent-hint">验证码已发送至 <strong>{email}</strong></div><input autoFocus inputMode="numeric" required minLength={6} maxLength={6} placeholder="6 位验证码" value={code} onChange={e => setCode(e.target.value)} />{debug && <div className="debug-code">开发验证码：{debug}</div>}</>}<button className="primary full" type="submit">{challenge ? '进入工作台' : '发送验证码'}</button>{message && <div className="form-error">{message}</div>}</form></AuthFrame>
}

function Bootstrap({ onLoggedIn }: { onLoggedIn: (user: User) => void }) {
  const [email, setEmail] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ user: User }>('/bootstrap', { method: 'POST', body: JSON.stringify({ email }) }); onLoggedIn(result.user) } catch (e) { alert((e as Error).message) } }
  return <AuthFrame title="创建本机管理员" subtitle="首次启动仅允许从本机创建管理员账户"><form onSubmit={submit}><input autoFocus type="email" required placeholder="管理员邮箱" value={email} onChange={e => setEmail(e.target.value)} /><button className="primary full" type="submit">初始化 JobPostings</button></form></AuthFrame>
}

function AuthFrame({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) { return <div className="auth-page"><div className="auth-glow" /><div className="auth-card"><div className="auth-logo"><span className="logo-mark">J</span>JobPostings</div><h1>{title}</h1><p>{subtitle}</p>{children}</div></div> }

function CompaniesPage({ companies, jobs, query, setQuery, onSearch, onSync, onOpen, onExport, onImport }: { companies: Company[]; jobs: Job[]; query: string; setQuery: (value: string) => void; onSearch: () => void; onSync?: () => void; onOpen: (id: string) => void; onExport: (format: 'xlsx' | 'csv' | 'json') => void; onImport: () => void }) {
  return <><PageHeader eyebrow="招聘知识库" title="企业与岗位" description="把分散在群聊、公众号和文件里的招聘信息，整理成可以行动的机会。">{onSync && <button className="secondary" onClick={onSync}>↻ 立即同步</button>}<button className="secondary" onClick={() => onExport('csv')}>导出 CSV</button><button className="secondary" onClick={() => onExport('xlsx')}>导出 Excel</button><button className="primary" onClick={onImport}>＋ 快速导入</button></PageHeader><div className="metrics"><Metric label="企业" value={companies.length} tone="blue" /><Metric label="岗位" value={jobs.length} tone="violet" /><Metric label="有效岗位" value={jobs.filter(j => j.status === 'active').length} tone="green" /><Metric label="最近更新" value={jobs[0]?.updated_at?.slice(5, 10) || '—'} tone="orange" /></div><div className="toolbar"><div className="search"><span>⌕</span><input className="search-input" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && onSearch()} placeholder="搜索企业、岗位、地点或专业" /></div><button className="filter">筛选　⌄</button><button className="filter">排序：最近更新　⌄</button></div>{companies.length ? <div className="company-grid">{companies.map(company => <CompanyCard key={company.id} company={company} onClick={() => onOpen(company.id)} />)}</div> : <EmptyState />}</>
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone: string }) { return <div className="metric"><div className={`metric-icon ${tone}`}>{tone === 'blue' ? '◈' : tone === 'violet' ? '▣' : tone === 'green' ? '✓' : '◷'}</div><div><small>{label}</small><strong>{value}</strong></div></div> }
function CompanyCard({ company, onClick }: { company: Company; onClick: () => void }) { return <button className="company-card" onClick={onClick}><div className="company-top"><div className="company-avatar">{company.display_name.slice(0, 1)}</div><span className="more">···</span></div><h3>{company.display_name}</h3><div className="chips"><span>{company.primary_industry}</span><span>{company.job_count} 个岗位</span></div><p>{company.summary || '企业介绍将在联网检索或审核后补充。'}</p><div className="card-footer"><span>最近更新</span><time>{company.updated_at?.replace('T', ' ').slice(0, 16) || '—'}</time><span className="arrow">→</span></div></button> }
function EmptyState() { return <div className="empty-state"><div className="empty-icon">✦</div><h3>知识库还在等待第一条招聘信息</h3><p>从“导入信息”粘贴群消息或公开链接，系统会自动识别企业和岗位。</p></div> }

function CompanyView({ company, onBack, onState, onFollow }: { company: CompanyDetail; onBack: () => void; onState: (id: string, state: string, favorite?: boolean) => void; onFollow: (id: string, followed?: boolean) => void }) { return <><button className="back" onClick={onBack}>← 返回企业列表</button><PageHeader eyebrow="企业详情" title={company.display_name} description={`${company.primary_industry} · ${company.jobs.length} 个岗位`}><button className="secondary" onClick={() => onFollow(company.id)}>☆ 关注企业</button></PageHeader><div className="detail-layout"><section><div className="detail-card intro"><div className="large-avatar">{company.display_name.slice(0, 1)}</div><div><h2>企业概览</h2><p>{company.summary || '暂无企业介绍。完成公开网页检索后将在这里显示带来源的企业介绍。'}</p><div className="chips">{company.aliases.map(alias => <span key={alias}>{alias}</span>)}</div></div></div><div className="detail-card"><div className="section-title"><h2>招聘岗位</h2><span>{company.jobs.length} 个岗位</span></div>{company.jobs.length ? company.jobs.map(job => <JobRow key={job.id} job={job} onState={onState} />) : <div className="empty-inline">暂无岗位</div>}</div></section><aside><div className="detail-card"><div className="section-title"><h2>来源与证据</h2><span>{company.evidences.length}</span></div>{company.evidences.slice(0, 8).map((e, index) => <div className="evidence" key={`${e.source_url}-${index}`}><span className="evidence-dot" /><div><strong>{e.source_type}</strong><p>{e.excerpt || '已保存来源证据'}</p>{e.source_url && <a href={e.source_url} target="_blank">打开来源 ↗</a>}</div></div>)}</div></aside></div></> }
function JobRow({ job, onState }: { job: Job; onState: (id: string, state: string, favorite?: boolean) => void }) { const locations = job.locations || (() => { try { return JSON.parse(job.locations_json || '[]') } catch { return [] } })(); return <div className="job-row"><div className="job-symbol">⌁</div><div className="job-main"><strong>{job.canonical_title}</strong><div><span>{job.recruitment_type}</span><span>{job.employment_type}</span><span>{locations.join('、') || '地点待确认'}</span></div></div><span className={`status ${job.status}`}>{job.status === 'active' ? '有效' : job.status === 'possibly_expired' ? '可能过期' : job.status}</span><button className="tiny-action" onClick={() => onState(job.id, 'interested', true)}>☆</button></div> }

function ApplicationsPage({ applications, onState }: { applications: Application[]; onState: (id: string, state: string, favorite?: boolean) => void }) { const columns = [['interested', '感兴趣'], ['applied', '已投递'], ['interview', '面试中'], ['offer', 'Offer']] as const; return <><PageHeader eyebrow="我的行动" title="求职进度" description="把感兴趣的岗位，从看到变成投递和面试。" /><div className="kanban">{columns.map(([state, title]) => <ApplicationColumn key={state} title={title} state={state} jobs={applications.filter(job => job.state === state)} onState={onState} />)}</div>{!applications.length && <div className="empty-state compact"><div className="empty-icon">✓</div><h3>收藏岗位后，它们会出现在这里</h3><p>在企业详情中点击星标，即可开始记录求职进度。</p></div>}</> }
function ApplicationColumn({ title, state, jobs, onState }: { title: string; state: string; jobs: Job[]; onState: (id: string, state: string, favorite?: boolean) => void }) { return <div className="kanban-column"><div className="column-head"><strong>{title}</strong><span>{jobs.length}</span></div>{jobs.map(job => <button className="application-card" key={job.id} onClick={() => onState(job.id, state)}><strong>{job.canonical_title}</strong><small>{job.company_name}</small></button>)}<button className="add-card">＋ 添加岗位</button></div> }

function AdminPage({ onNavigate }: { onNavigate: (page: 'settings' | 'review') => void }) {
  const [invitations, setInvitations] = useState<Invitation[]>([])
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState('')

  const load = async () => {
    try {
      setInvitations(await api<Invitation[]>('/admin/invitations'))
      setMessage('')
    } catch (e) {
      setMessage((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const invite = async (event: FormEvent) => {
    event.preventDefault()
    if (!email.trim()) return
    setBusy(true)
    try {
      await api('/admin/invitations', { method: 'POST', body: JSON.stringify({ email: email.trim(), role }) })
      setEmail('')
      setRole('member')
      await load()
      setMessage('邀请已创建；如果已配置 SMTP，邀请邮件会发送到该邮箱。')
    } catch (e) {
      setMessage((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const activeCount = invitations.filter(item => !item.used_at && new Date(item.expires_at).getTime() > Date.now()).length
  const usedCount = invitations.filter(item => Boolean(item.used_at)).length
  const expiredCount = invitations.filter(item => !item.used_at && new Date(item.expires_at).getTime() <= Date.now()).length

  return <><PageHeader eyebrow="管理员" title="管理台" description="管理访问权限，并从这里进入系统配置和审核队列。"><button className="secondary" onClick={() => onNavigate('settings')}>系统设置</button><button className="secondary" onClick={() => onNavigate('review')}>待审核</button></PageHeader><div className="metrics"><Metric label="邀请总数" value={invitations.length} tone="blue" /><Metric label="待登录" value={activeCount} tone="green" /><Metric label="已使用" value={usedCount} tone="violet" /><Metric label="已过期" value={expiredCount} tone="orange" /></div><div className="admin-grid"><section className="detail-card setting-section"><h2>邀请用户</h2><p className="setting-help">邀请有效期为 72 小时。受邀用户使用该邮箱申请登录验证码，默认角色为受邀用户。</p><form className="invite-form" onSubmit={invite}><label>邮箱<input type="email" required value={email} onChange={event => setEmail(event.target.value)} placeholder="name@example.com" /></label><label>角色<select value={role} onChange={event => setRole(event.target.value as 'member' | 'admin')}><option value="member">受邀用户</option><option value="admin">管理员</option></select></label><button className="primary" disabled={busy || !email.trim()} type="submit">{busy ? '创建中…' : '创建邀请'}</button></form>{message && <p className="setting-help admin-message">{message}</p>}</section><section className="detail-card setting-section"><h2>管理员工作流</h2><div className="admin-guide"><div><strong>1. 配置数据源</strong><p>在“系统设置”连接 TraceMemo，读取并选择招聘群。</p><button className="secondary" onClick={() => onNavigate('settings')}>打开系统设置 →</button></div><div><strong>2. 处理异常信息</strong><p>低置信度识别、字段冲突和失败任务会进入“待审核”。</p><button className="secondary" onClick={() => onNavigate('review')}>打开待审核 →</button></div><div><strong>3. 同步和导入</strong><p>回到“企业与岗位”点击“立即同步”，或使用“导入信息”手工补录。</p></div></div></section></div><section className="detail-card invitation-section"><div className="section-title"><h2>邀请记录</h2><div className="section-actions"><span>{invitations.length} 条</span><button className="secondary" onClick={load} disabled={loading}>↻ 刷新</button></div></div>{loading ? <div className="loading-inline">加载邀请记录…</div> : invitations.length ? <div className="invitation-list">{invitations.map(item => <InvitationRow key={item.id} invitation={item} />)}</div> : <div className="empty-inline">还没有邀请记录</div>}</section></>
}

function InvitationRow({ invitation }: { invitation: Invitation }) {
  const expired = !invitation.used_at && new Date(invitation.expires_at).getTime() <= Date.now()
  const status = invitation.used_at ? '已使用' : expired ? '已过期' : '待登录'
  const statusClass = invitation.used_at ? 'used' : expired ? 'expired' : 'active'
  const role = invitation.role === 'admin' ? '管理员' : '受邀用户'
  return <div className="invitation-row"><div className="invitation-avatar">{invitation.email.slice(0, 1).toUpperCase()}</div><div className="invitation-main"><strong>{invitation.email}</strong><div><span>{role}</span><span>创建于 {formatDate(invitation.created_at)}</span><span>{invitation.used_at ? `使用于 ${formatDate(invitation.used_at)}` : `有效至 ${formatDate(invitation.expires_at)}`}</span></div></div><span className={`status ${statusClass}`}>{status}</span></div>
}

function formatDate(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ').slice(0, 16)
  return date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function ReviewPage({ onResolved }: { onResolved: () => Promise<void> }) {
  const [items, setItems] = useState<ReviewItem[]>([])
  const [message, setMessage] = useState('')
  const load = async () => {
    try { setItems(await api<ReviewItem[]>('/admin/review-items')); setMessage('') }
    catch (e) { setMessage((e as Error).message) }
  }
  useEffect(() => { load() }, [])
  const resolve = async (id: string, action: 'resolved' | 'rejected') => {
    try { await api(`/admin/review-items/${id}/resolve`, { method: 'POST', body: JSON.stringify({ action }) }); setItems(current => current.filter(item => item.id !== id)); await onResolved() }
    catch (e) { setMessage((e as Error).message) }
  }
  return <><PageHeader eyebrow="管理员" title="待审核" description="低置信度识别、字段冲突和处理失败会在这里保留原始上下文。"><button className="secondary" onClick={load}>↻ 刷新</button></PageHeader>{message && <div className="setting-help">{message}</div>}{items.length ? <div className="review-list">{items.map(item => <div className="detail-card review-card" key={item.id}><div className="review-head"><div><strong>{item.kind}</strong><span>{item.entity_type || 'unknown'} / {item.entity_id || '—'}</span></div><div><button className="secondary" onClick={() => resolve(item.id, 'rejected')}>保留待查</button><button className="primary" onClick={() => resolve(item.id, 'resolved')}>标记已处理</button></div></div><pre>{JSON.stringify(item.payload, null, 2)}</pre></div>)}</div> : <div className="empty-state compact"><div className="empty-icon">✓</div><h3>当前没有待审核项</h3><p>自动识别产生低置信度结果或字段冲突后，会在这里显示。</p></div>}</>
}

function ImportPage({ onImported }: { onImported: () => Promise<void> }) { const [text, setText] = useState(''); const [url, setUrl] = useState(''); const [busy, setBusy] = useState(false); const submitText = async () => { if (!text.trim()) return; setBusy(true); try { await api('/imports/text', { method: 'POST', body: JSON.stringify({ text }) }); setText(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitUrl = async () => { if (!url.trim()) return; setBusy(true); try { await api('/imports/url', { method: 'POST', body: JSON.stringify({ url }) }); setUrl(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitFile = async (file: File) => { setBusy(true); try { const form = new FormData(); form.append('file', file); await api('/imports/files', { method: 'POST', body: form }); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; return <><PageHeader eyebrow="数据入口" title="导入招聘信息" description="先把信息放进来，系统会自动解析、分类、去重并保留来源。" /><div className="import-grid"><div className="detail-card import-card"><div className="import-icon blue">✎</div><h2>粘贴群聊文字</h2><p>适合复制微信群中的招聘消息、合并转发和招聘说明。</p><textarea value={text} onChange={e => setText(e.target.value)} rows={10} placeholder="粘贴招聘群消息……" /><button className="primary" disabled={busy || !text.trim()} onClick={submitText}>{busy ? '提交中…' : '加入处理队列 →'}</button></div><div className="detail-card import-card"><div className="import-icon violet">↗</div><h2>公开链接与文件</h2><p>公众号文章、企业官网、招聘平台和其他公开页面均可。</p><input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/recruitment" /><div className="dropzone">将网页 URL 粘贴到上方<br /><span>验证码、登录页和小程序请手工补录</span></div><button className="secondary" disabled={busy || !url.trim()} onClick={submitUrl}>抓取并加入队列 →</button><label className="file-input">选择 PDF、DOCX、XLSX、CSV、TXT 或图片<input type="file" accept=".pdf,.docx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.webp" onChange={e => e.target.files?.[0] && submitFile(e.target.files[0])} /></label></div></div></> }

function SettingsPage({ onSaved }: { onSaved: (message: string) => void }) {
  const [settings, setSettings] = useState<Record<string, any>>({})
  const [trace, setTrace] = useState({ base_url: 'http://127.0.0.1:6131/api/v1', enabled: false })
  const [traceToken, setTraceToken] = useState('')
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    Promise.all([api<Record<string, any>>('/admin/settings'), api<any[]>('/admin/connectors')]).then(([value, connectors]) => {
      setSettings(value)
      const connector = connectors.find(item => item.kind === 'tracememo')
      if (connector) setTrace({ base_url: connector.base_url, enabled: Boolean(connector.enabled) })
      setLoaded(true)
    }).catch(() => undefined)
  }, [])
  const set = (key: string, value: any) => setSettings(current => ({ ...current, [key]: value }))
  const save = async () => {
    await api('/admin/settings', { method: 'PUT', body: JSON.stringify({ values: settings }) })
    await api('/admin/connectors/tracememo', { method: 'PUT', body: JSON.stringify({ ...trace, ...(traceToken ? { token: traceToken } : {}) }) })
    onSaved('设置已保存')
  }
  if (!loaded) return <div className="loading-inline">加载设置…</div>
  const provider = settings.llm_provider || {}
  const backup = settings.backup || {}
  const smtp = settings.smtp || {}
  return <><PageHeader eyebrow="管理员" title="系统设置" description="连接 TraceMemo、通用模型、SMTP 和 AList WebDAV。"><button className="primary" onClick={save}>保存设置</button></PageHeader><div className="settings-grid"><section className="detail-card setting-section"><h2>同步与隐私</h2><label>同步间隔（分钟）<input type="number" min="1" value={settings.sync_interval_minutes || 10} onChange={e => set('sync_interval_minutes', Number(e.target.value))} /></label><label>首次导入天数<input type="number" min="1" value={settings.initial_import_days || 30} onChange={e => set('initial_import_days', Number(e.target.value))} /></label><label className="check"><input type="checkbox" checked={Boolean(settings.redaction_enabled)} onChange={e => set('redaction_enabled', e.target.checked)} />发送云模型前启用脱敏（默认关闭）</label></section><section className="detail-card setting-section"><h2>TraceMemo</h2><label className="check"><input type="checkbox" checked={Boolean(trace.enabled)} onChange={e => setTrace({ ...trace, enabled: e.target.checked })} />启用自动同步</label><label>API 地址<input value={trace.base_url} onChange={e => setTrace({ ...trace, base_url: e.target.value })} /></label><label>Bearer Token<input type="password" value={traceToken} placeholder="已配置时留空保持不变" onChange={e => setTraceToken(e.target.value)} /></label><p className="setting-help">保存后可在后端接口读取群聊列表并手工选择招聘群，默认每 10 分钟轮询。</p><TraceMemoGroups /></section><section className="detail-card setting-section"><h2>通用模型 API</h2><label className="check"><input type="checkbox" checked={Boolean(provider.enabled)} onChange={e => set('llm_provider', { ...provider, enabled: e.target.checked })} />启用云模型处理</label><label>API 风格<select value={provider.api_style || 'chat_completions'} onChange={e => set('llm_provider', { ...provider, api_style: e.target.value })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label><label>Base URL<input value={provider.base_url || ''} onChange={e => set('llm_provider', { ...provider, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label><label>模型名称<input value={provider.model || provider.text_model || ''} onChange={e => set('llm_provider', { ...provider, model: e.target.value, text_model: e.target.value })} /></label><label>API Key<input type="password" placeholder={provider.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={e => set('llm_provider', { ...provider, api_key: e.target.value })} /></label><button className="secondary" onClick={async () => { try { await api('/admin/models/test', { method: 'POST' }); onSaved('模型连接成功') } catch (e) { alert((e as Error).message) } }}>测试模型连接</button></section><section className="detail-card setting-section"><h2>AList WebDAV 备份</h2><label className="check"><input type="checkbox" checked={Boolean(backup.enabled)} onChange={e => set('backup', { ...backup, enabled: e.target.checked })} />启用每日备份</label><label>WebDAV URL<input value={backup.webdav_url || ''} onChange={e => set('backup', { ...backup, webdav_url: e.target.value })} placeholder="https://alist.example.com/dav/" /></label><label>用户名<input value={backup.username || ''} onChange={e => set('backup', { ...backup, username: e.target.value })} /></label><label>远端目录<input value={backup.remote_directory || '/JobPostings'} onChange={e => set('backup', { ...backup, remote_directory: e.target.value })} /></label><label>WebDAV 密码<input type="password" placeholder={backup.webdav_password_configured ? '已配置，留空保持不变' : '输入 WebDAV 密码'} onChange={e => set('backup', { ...backup, webdav_password: e.target.value })} /></label><label>备份密码<input type="password" placeholder={backup.backup_password_configured ? '已配置，留空保持不变' : '输入独立备份密码'} onChange={e => set('backup', { ...backup, backup_password: e.target.value })} /></label><div className="button-row"><button className="secondary" onClick={async () => { try { await api('/admin/backups/test', { method: 'POST' }); onSaved('WebDAV 连接成功') } catch (e) { alert((e as Error).message) } }}>测试 WebDAV</button><button className="secondary" onClick={async () => { try { await api('/admin/backups/run', { method: 'POST' }); onSaved('备份已完成') } catch (e) { alert((e as Error).message) } }}>立即备份</button></div></section><section className="detail-card setting-section"><h2>SMTP 邮件</h2><label className="check"><input type="checkbox" checked={Boolean(smtp.enabled)} onChange={e => set('smtp', { ...smtp, enabled: e.target.checked })} />启用邀请和验证码邮件</label><label>SMTP 主机<input value={smtp.host || ''} onChange={e => set('smtp', { ...smtp, host: e.target.value })} placeholder="smtp.example.com" /></label><label>端口<input type="number" value={smtp.port || 587} onChange={e => set('smtp', { ...smtp, port: Number(e.target.value) })} /></label><label>发件人邮箱<input type="email" value={smtp.from_email || ''} onChange={e => set('smtp', { ...smtp, from_email: e.target.value })} /></label><label>用户名<input value={smtp.username || ''} onChange={e => set('smtp', { ...smtp, username: e.target.value })} /></label><label>SMTP 密码<input type="password" placeholder={smtp.password_configured ? '已配置，留空保持不变' : '输入 SMTP 密码'} onChange={e => set('smtp', { ...smtp, password: e.target.value })} /></label><label className="check"><input type="checkbox" checked={smtp.starttls !== false} onChange={e => set('smtp', { ...smtp, starttls: e.target.checked })} />使用 STARTTLS</label></section><section className="detail-card setting-section"><h2>Agent API</h2><label className="check"><input type="checkbox" checked={Boolean(settings.agent_api_enabled)} onChange={e => set('agent_api_enabled', e.target.checked)} />允许创建受限 Agent Token</label><p className="setting-help">Token 默认只允许读取招聘目录和操作自己的求职进度，不能读取原始群消息或系统密钥。</p></section></div></>
}

function TraceMemoGroups() {
  const [groups, setGroups] = useState<Array<{ id: string; name: string; selected: boolean }>>([])
  const [message, setMessage] = useState('')
  const refresh = async () => {
    try { setGroups(await api<Array<{ id: string; name: string; selected: boolean }>>('/admin/connectors/tracememo/groups')); setMessage('') }
    catch (e) { setMessage((e as Error).message) }
  }
  const save = async () => {
    try { await api('/admin/source-groups', { method: 'PUT', body: JSON.stringify({ groups }) }); setMessage('群组选择已保存') }
    catch (e) { setMessage((e as Error).message) }
  }
  return <div className="group-picker"><div className="group-picker-head"><strong>招聘群选择</strong><span>{groups.filter(group => group.selected).length}/20</span></div><div className="group-picker-actions"><button className="secondary" onClick={refresh}>读取群列表</button><button className="secondary" onClick={save} disabled={!groups.length}>保存选择</button></div>{groups.map(group => <label className="check" key={group.id}><input type="checkbox" checked={group.selected} onChange={event => setGroups(current => current.map(item => item.id === group.id ? { ...item, selected: event.target.checked } : item))} />{group.name}</label>)}{message && <p className="setting-help">{message}</p>}</div>
}

function PageHeader({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: React.ReactNode }) { return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div><div className="header-actions">{children}</div></header> }

export default App

createRoot(document.getElementById('root')!).render(<App />)
