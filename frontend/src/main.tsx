import { FormEvent, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import './queue.css'

type User = { id: string; email: string; role: string; password_configured?: boolean }
type Company = { id: string; display_name: string; legal_name?: string; website?: string; summary?: string; primary_industry: string; job_count: number; updated_at: string; company_nature?: string; founded_at?: string; company_size?: string; headquarters?: string; businesses?: string[]; highlights?: string[]; official_channels?: string[] }
type Job = { id: string; canonical_title: string; recruitment_type: string; employment_type: string; status: string; locations_json?: string; locations?: string[]; company_name?: string; updated_at?: string; department?: string; headcount?: string; education?: string[]; majors?: string[]; experience_requirement?: string; salary?: Record<string, any>; responsibilities?: string; requirements?: string; benefits?: string[]; application_methods?: string[]; contacts?: string[]; explicit_deadline?: string }
type Application = Job & { state: string; favorite: number; updated_at: string }
type Evidence = { id: string; source_url?: string; source_type: string; excerpt?: string; artifact_id?: string; artifact_ids?: string[]; qr_values?: string[]; observed_at?: string; raw_text?: string; ocr_text?: string; sender?: string; sent_at?: string; source_group_name?: string; filename?: string; mime_type?: string; metadata?: Record<string, any> }
type RecruitmentEvent = { id: string; company_id: string; company_name: string; batch_name?: string; title: string; event_type: string; start_at?: string; end_at?: string; timezone: string; format: string; city?: string; campus?: string; location?: string; application_url?: string; audience?: string; notes?: string; job_ids: string[]; evidence_ids: string[]; status: string }
type CompanyDetail = Company & { aliases: string[]; jobs: Job[]; evidences: Evidence[]; events: RecruitmentEvent[] }
type Notification = { id: string; title: string; body: string; read_at?: string | null }
type Invitation = { id: string; email: string; role: string; expires_at: string; used_at?: string | null; created_at: string }
type ReviewItem = { id: string; kind: string; entity_type?: string; entity_id?: string; created_at?: string; payload: Record<string, any> }
type TraceMemoGroup = { id: string; external_id: string; name: string; avatar?: string | null; selected: boolean; enabled?: boolean }
type ProcessingLog = { id: string; stage: string; level: string; message: string; details: Record<string, any>; created_at: string }
type ProcessingQueueItem = { id: string; kind: string; raw_message_id?: string | null; company_id?: string | null; status: string; stage: string; attempts: number; lease_until?: string | null; next_attempt_at?: string | null; processor?: string | null; error?: string | null; created_at: string; updated_at: string; connector_id?: string | null; source_group_id?: string | null; source_group_name?: string | null; message_type?: string | null; sender?: string | null; sent_at?: string | null; text_preview?: string | null }
type ProcessingQueue = { state: 'running' | 'paused'; stats: Record<string, number>; items: ProcessingQueueItem[]; total: number }

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
  const [page, setPage] = useState<'companies' | 'timeline' | 'applications' | 'import' | 'admin' | 'queue' | 'settings' | 'security' | 'review'>('companies')
  const [companies, setCompanies] = useState<Company[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [applications, setApplications] = useState<Application[]>([])
  const [timeline, setTimeline] = useState<RecruitmentEvent[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<CompanyDetail | null>(null)
  const [notice, setNotice] = useState('')
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [syncing, setSyncing] = useState(false)

  const loadData = async () => {
    const [companyData, jobData, applicationData, notificationData, timelineData] = await Promise.all([
      api<Company[]>(`/companies?q=${encodeURIComponent(query)}`),
      api<Job[]>('/jobs'),
      api<Application[]>('/me/applications'),
      api<Notification[]>('/notifications'),
      api<RecruitmentEvent[]>('/recruitment-events'),
    ])
    setCompanies(companyData)
    setJobs(jobData)
    setApplications(applicationData)
    setNotifications(notificationData)
    setTimeline(timelineData)
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
    source.addEventListener('processing.updated', refresh)
    return () => source.close()
  }, [user, query])

  const flash = (message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(''), 3000)
  }

  if (loading) return <div className="loading-screen"><div className="spinner" />正在加载 JobPostings…</div>
  if (!user) return initialized ? <Login onLoggedIn={setUser} /> : <Bootstrap onLoggedIn={setUser} />

  const sync = async (force = false) => {
    if (syncing) return
    setSyncing(true)
    try {
      const result = await api<{ fetched: number; added: number; created: number; updated: number; duplicates: number; filtered_system?: number; media_attached?: number; media_failed?: number; reset?: { backup_path?: string; deleted?: Record<string, number> } }>('/admin/sync', { method: 'POST', ...(force ? { body: JSON.stringify({ force: true }) } : {}) })
      const cleared = Object.values(result.reset?.deleted || {}).reduce((total, value) => total + value, 0)
      const resetText = force ? `；已备份并清理 ${cleared} 条旧记录` : ''
      const mediaText = result.media_attached || result.media_failed ? `；图片处理成功 ${result.media_attached || 0}，失败 ${result.media_failed || 0}` : ''
      flash(`同步完成：读取 ${result.fetched} 条，新增 ${result.created} 条，更新 ${result.updated} 条，重复 ${result.duplicates} 条，过滤系统消息 ${result.filtered_system || 0} 条${mediaText}${resetText}`)
      await loadData()
    }
    catch (e) { setError((e as Error).message) }
    finally { setSyncing(false) }
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
        <NavButton active={page === 'timeline'} onClick={() => { setPage('timeline'); setSelected(null) }} icon="◷">招聘时间轴</NavButton>
        <NavButton active={page === 'applications'} onClick={() => { setPage('applications'); setSelected(null) }} icon="✓">求职进度</NavButton>
        <NavButton active={page === 'import'} onClick={() => { setPage('import'); setSelected(null) }} icon="＋">导入信息</NavButton>
        {user.role === 'admin' && <NavButton active={page === 'admin'} onClick={() => { setPage('admin'); setSelected(null) }} icon="▦">管理台</NavButton>}
        {user.role === 'admin' && <NavButton active={page === 'queue'} onClick={() => { setPage('queue'); setSelected(null) }} icon="≋">处理队列</NavButton>}
        {user.role === 'admin' && <NavButton active={page === 'settings'} onClick={() => { setPage('settings'); setSelected(null) }} icon="⚙">系统设置</NavButton>}
        <NavButton active={page === 'security'} onClick={() => { setPage('security'); setSelected(null) }} icon="⌑">账户安全</NavButton>
        {user.role === 'admin' && <NavButton active={page === 'review'} onClick={() => { setPage('review'); setSelected(null) }} icon="!">待审核</NavButton>}
      </nav>
      <div className="sidebar-bottom"><div className="user-avatar">{user.email.slice(0, 1).toUpperCase()}</div><div><strong>{user.email}</strong><small>{user.role === 'admin' ? '管理员' : '受邀用户'}</small></div><button className="logout" onClick={async () => { await api('/auth/logout', { method: 'POST' }); location.reload() }}>↗</button></div>
    </aside>
    <main className="content">
      {error && <div className="error-banner">{error}<button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="notice">{notice}</div>}
      {!selected && notifications.filter(item => !item.read_at).slice(0, 3).map(item => <div className="notification-strip" key={item.id}><div><strong>{item.title}</strong><span>{item.body}</span></div><button onClick={() => markNotificationRead(item.id)}>知道了</button></div>)}
      {selected ? <CompanyView company={selected} onBack={() => setSelected(null)} onState={updateState} onFollow={followCompany} /> : page === 'companies' ? <CompaniesPage companies={companies} jobs={jobs} query={query} setQuery={setQuery} onSearch={() => loadData()} onSync={user.role === 'admin' ? sync : undefined} syncing={syncing} onOpen={openCompany} onExport={exportJobs} onImport={() => setPage('import')} /> : page === 'timeline' ? <TimelinePage events={timeline} onOpenCompany={openCompany} /> : page === 'applications' ? <ApplicationsPage applications={applications} onState={updateState} /> : page === 'import' ? <ImportPage onImported={async () => { flash('已加入处理队列'); await loadData(); setPage('queue') }} /> : page === 'admin' ? <AdminPage onNavigate={target => { setPage(target); setSelected(null) }} /> : page === 'queue' ? <QueuePage onSync={sync} syncing={syncing} /> : page === 'security' ? <AccountSecurityPage /> : page === 'review' ? <ReviewPage onResolved={async () => { await loadData() }} /> : <SettingsPage onSaved={flash} onSync={sync} syncing={syncing} />}
    </main>
  </div>
}

function NavButton({ active, onClick, icon, children }: { active: boolean; onClick: () => void; icon: string; children: string }) {
  return <button className={`nav-button ${active ? 'active' : ''}`} onClick={onClick}><span>{icon}</span>{children}</button>
}

function Login({ onLoggedIn }: { onLoggedIn: (user: User) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [method, setMethod] = useState<'password' | 'otp'>('password')
  const [challenge, setChallenge] = useState('')
  const [code, setCode] = useState('')
  const [debug, setDebug] = useState('')
  const [otpEnabled, setOtpEnabled] = useState(false)
  const [initialPasswordRequired, setInitialPasswordRequired] = useState(false)
  const [localPasswordSetupAllowed, setLocalPasswordSetupAllowed] = useState(false)
  const [optionsLoaded, setOptionsLoaded] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => { api<{ otp_login_enabled: boolean; initial_admin_password_required: boolean; local_password_setup_allowed: boolean }>('/auth/options').then(value => { setOtpEnabled(value.otp_login_enabled); setInitialPasswordRequired(value.initial_admin_password_required); setLocalPasswordSetupAllowed(value.local_password_setup_allowed) }).catch(() => undefined).finally(() => setOptionsLoaded(true)) }, [])
  const login = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ user: User }>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }); onLoggedIn(result.user) } catch (e) { setMessage((e as Error).message) } }
  const send = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ challenge_id: string; debug_code?: string }>('/auth/request-code', { method: 'POST', body: JSON.stringify({ email }) }); setChallenge(result.challenge_id); setDebug(result.debug_code || '') } catch (e) { setMessage((e as Error).message) } }
  const verify = async (event: FormEvent) => { event.preventDefault(); try { const result = await api<{ user: User }>('/auth/verify-code', { method: 'POST', body: JSON.stringify({ challenge_id: challenge, code }) }); onLoggedIn(result.user) } catch (e) { setMessage((e as Error).message) } }
  const useOtp = () => { setMessage(''); setMethod('otp'); setChallenge(''); setCode('') }
  if (optionsLoaded && initialPasswordRequired) return <InitialAdminPassword localAllowed={localPasswordSetupAllowed} onLoggedIn={onLoggedIn} />
  return <AuthFrame title="欢迎回来" subtitle="使用账号密码进入招聘信息工作台"><form onSubmit={method === 'password' ? login : challenge ? verify : send}><input autoFocus type="email" required placeholder="账号邮箱" value={email} onChange={e => setEmail(e.target.value)} />{method === 'password' && <input type="password" required minLength={8} maxLength={128} placeholder="密码" value={password} onChange={e => setPassword(e.target.value)} />}{method === 'otp' && challenge && <><div className="sent-hint">验证码已发送至 <strong>{email}</strong></div><input autoFocus inputMode="numeric" required minLength={6} maxLength={6} placeholder="6 位验证码" value={code} onChange={e => setCode(e.target.value)} />{debug && <div className="debug-code">开发验证码：{debug}</div>}</>}<button className="primary full" type="submit">{method === 'password' ? '登录' : challenge ? '进入工作台' : '发送验证码'}</button>{method === 'password' ? <button className="auth-link" type="button" disabled={!otpEnabled} onClick={useOtp}>{otpEnabled ? '使用邮箱验证码登录' : '邮箱验证码登录（当前关闭）'}</button> : <button className="auth-link" type="button" onClick={() => { setMethod('password'); setChallenge(''); setMessage('') }}>返回密码登录</button>}{message && <div className="form-error">{message}</div>}</form></AuthFrame>
}

function InitialAdminPassword({ localAllowed, onLoggedIn }: { localAllowed: boolean; onLoggedIn: (user: User) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [message, setMessage] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (password !== confirm) { setMessage('两次输入的密码不一致'); return }
    try {
      const result = await api<{ user: User }>('/auth/initial-password', { method: 'POST', body: JSON.stringify({ email, password }) })
      onLoggedIn(result.user)
    } catch (e) { setMessage((e as Error).message) }
  }
  if (!localAllowed) return <AuthFrame title="请在本机完成管理员设置" subtitle="当前管理员账号尚未设置密码"><p className="setting-help">为保护管理员账号，初始密码只能在运行 JobPostings 的电脑上设置。请在该电脑打开 <strong>http://127.0.0.1:17879</strong>，完成一次初始密码设置。</p></AuthFrame>
  return <AuthFrame title="设置管理员密码" subtitle="这是旧账号迁移后的首次密码设置"><form onSubmit={submit}><input autoFocus type="email" required placeholder="管理员邮箱" value={email} onChange={e => setEmail(e.target.value)} /><input type="password" required minLength={8} maxLength={128} placeholder="设置密码（至少 8 位）" value={password} onChange={e => setPassword(e.target.value)} /><input type="password" required minLength={8} maxLength={128} placeholder="再次输入密码" value={confirm} onChange={e => setConfirm(e.target.value)} /><button className="primary full" type="submit">设置并进入工作台</button>{message && <div className="form-error">{message}</div>}</form></AuthFrame>
}

function Bootstrap({ onLoggedIn }: { onLoggedIn: (user: User) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [message, setMessage] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); if (password !== confirm) { setMessage('两次输入的密码不一致'); return } try { const result = await api<{ user: User }>('/bootstrap', { method: 'POST', body: JSON.stringify({ email, password }) }); onLoggedIn(result.user) } catch (e) { setMessage((e as Error).message) } }
  return <AuthFrame title="创建本机管理员" subtitle="首次启动仅允许从本机创建管理员账户"><form onSubmit={submit}><input autoFocus type="email" required placeholder="管理员邮箱" value={email} onChange={e => setEmail(e.target.value)} /><input type="password" required minLength={8} maxLength={128} placeholder="设置密码（至少 8 位）" value={password} onChange={e => setPassword(e.target.value)} /><input type="password" required minLength={8} maxLength={128} placeholder="再次输入密码" value={confirm} onChange={e => setConfirm(e.target.value)} /><button className="primary full" type="submit">初始化 JobPostings</button>{message && <div className="form-error">{message}</div>}</form></AuthFrame>
}

function AuthFrame({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) { return <div className="auth-page"><div className="auth-glow" /><div className="auth-card"><div className="auth-logo"><span className="logo-mark">J</span>JobPostings</div><h1>{title}</h1><p>{subtitle}</p>{children}</div></div> }

function CompaniesPage({ companies, jobs, query, setQuery, onSearch, onSync, syncing, onOpen, onExport, onImport }: { companies: Company[]; jobs: Job[]; query: string; setQuery: (value: string) => void; onSearch: () => void; onSync?: (force?: boolean) => void; syncing?: boolean; onOpen: (id: string) => void; onExport: (format: 'xlsx' | 'csv' | 'json') => void; onImport: () => void }) {
  return <><PageHeader eyebrow="招聘知识库" title="企业与岗位" description="把分散在群聊、公众号和文件里的招聘信息，整理成可以行动的机会。">{onSync && <button className="secondary" disabled={syncing} onClick={() => onSync()}>{syncing ? '同步中…' : '↻ 从微信群同步'}</button>}<button className="secondary" onClick={() => onExport('csv')}>导出 CSV</button><button className="secondary" onClick={() => onExport('xlsx')}>导出 Excel</button><button className="primary" onClick={onImport}>＋ 快速导入</button></PageHeader><div className="metrics"><Metric label="企业" value={companies.length} tone="blue" /><Metric label="岗位" value={jobs.length} tone="violet" /><Metric label="有效岗位" value={jobs.filter(j => j.status === 'active').length} tone="green" /><Metric label="最近更新" value={jobs[0]?.updated_at?.slice(5, 10) || '—'} tone="orange" /></div><div className="toolbar"><div className="search"><span>⌕</span><input className="search-input" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && onSearch()} placeholder="搜索企业、岗位、地点或专业" /></div><button className="filter">筛选　⌄</button><button className="filter">排序：最近更新　⌄</button></div>{companies.length ? <div className="company-grid">{companies.map(company => <CompanyCard key={company.id} company={company} onClick={() => onOpen(company.id)} />)}</div> : <EmptyState />}</>
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone: string }) { return <div className="metric"><div className={`metric-icon ${tone}`}>{tone === 'blue' ? '◈' : tone === 'violet' ? '▣' : tone === 'green' ? '✓' : '◷'}</div><div><small>{label}</small><strong>{value}</strong></div></div> }
function CompanyCard({ company, onClick }: { company: Company; onClick: () => void }) { return <button className="company-card" onClick={onClick}><div className="company-top"><div className="company-avatar">{company.display_name.slice(0, 1)}</div><span className="more">···</span></div><h3>{company.display_name}</h3><div className="chips"><span>{company.primary_industry}</span><span>{company.job_count} 个岗位</span></div><p>{company.summary || '企业介绍将在联网检索或审核后补充。'}</p><div className="card-footer"><span>最近更新</span><time>{company.updated_at?.replace('T', ' ').slice(0, 16) || '—'}</time><span className="arrow">→</span></div></button> }
function EmptyState() { return <div className="empty-state"><div className="empty-icon">✦</div><h3>知识库还在等待第一条招聘信息</h3><p>从“导入信息”粘贴群消息或公开链接，系统会自动识别企业和岗位。</p></div> }

function CompanyView({ company, onBack, onState, onFollow }: { company: CompanyDetail; onBack: () => void; onState: (id: string, state: string, favorite?: boolean) => void; onFollow: (id: string, followed?: boolean) => void }) {
  const facts = [
    ['企业全称', company.legal_name], ['企业性质', company.company_nature], ['成立时间', company.founded_at],
    ['企业规模', company.company_size], ['总部及办公地点', company.headquarters], ['主要业务', company.businesses?.join('、')],
    ['企业亮点', company.highlights?.join('、')], ['官方网站', company.website], ['官方招聘渠道', company.official_channels?.join('、')],
  ].filter(([, value]) => value)
  return <><button className="back" onClick={onBack}>← 返回企业列表</button><PageHeader eyebrow="企业详情" title={company.display_name} description={`${company.primary_industry} · ${company.jobs.length} 个岗位`}><button className="secondary" onClick={() => onFollow(company.id)}>☆ 关注企业</button></PageHeader><div className="detail-layout"><section><div className="detail-card intro"><div className="large-avatar">{company.display_name.slice(0, 1)}</div><div><h2>企业概览</h2><p>{company.summary || '企业资料正在根据来源整理。'}</p><div className="chips">{company.aliases.map(alias => <span key={alias}>{alias}</span>)}</div></div></div>{facts.length > 0 && <div className="detail-card"><div className="section-title"><h2>企业资料</h2><span>由模型根据证据整理</span></div><dl className="company-facts">{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></div>}<div className="detail-card"><div className="section-title"><h2>招聘时间轴</h2><span>{company.events.length} 项</span></div>{company.events.length ? company.events.map(event => <TimelineEventCard event={event} key={event.id} />) : <div className="empty-inline">暂无明确时间事件</div>}</div><div className="detail-card"><div className="section-title"><h2>招聘岗位</h2><span>{company.jobs.length} 个岗位</span></div>{company.jobs.length ? company.jobs.map(job => <JobRow key={job.id} job={job} onState={onState} />) : <div className="empty-inline">暂无岗位</div>}</div></section><aside><div className="detail-card"><div className="section-title"><h2>来源与证据</h2><span>{company.evidences.length}</span></div>{company.evidences.map(evidence => <EvidenceCard key={evidence.id} evidence={evidence} />)}</div></aside></div></>
}

function ValueBlock({ label, value }: { label: string; value?: string | string[] | Record<string, any> }) {
  const empty = value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length) || (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length)
  const shown = empty ? '信息中未提及' : Array.isArray(value) ? value.join('、') : typeof value === 'object' ? JSON.stringify(value, null, 2) : value
  return <div className="job-field"><dt>{label}</dt><dd>{shown}</dd></div>
}

function JobRow({ job, onState }: { job: Job; onState: (id: string, state: string, favorite?: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const locations = job.locations || (() => { try { return JSON.parse(job.locations_json || '[]') } catch { return [] } })()
  return <div className={`job-card-row${open ? ' open' : ''}`}><div className="job-row" role="button" tabIndex={0} onClick={() => setOpen(value => !value)} onKeyDown={event => event.key === 'Enter' && setOpen(value => !value)}><div className="job-symbol">⌁</div><div className="job-main"><strong>{job.canonical_title}</strong><div><span>{job.recruitment_type}</span><span>{job.employment_type}</span><span>{locations.join('、') || '地点待确认'}</span></div></div><span className={`status ${job.status}`}>{job.status === 'active' ? '有效' : job.status === 'possibly_expired' ? '可能过期' : job.status}</span><button className="tiny-action" aria-label="收藏岗位" onClick={event => { event.stopPropagation(); onState(job.id, 'interested', true) }}>☆</button><span className="expand-mark">{open ? '⌃' : '⌄'}</span></div>{open && <dl className="job-details"><ValueBlock label="部门" value={job.department} /><ValueBlock label="岗位职责" value={job.responsibilities} /><ValueBlock label="任职要求" value={job.requirements} /><ValueBlock label="学历要求" value={job.education} /><ValueBlock label="专业要求" value={job.majors} /><ValueBlock label="经验要求" value={job.experience_requirement} /><ValueBlock label="薪资" value={job.salary} /><ValueBlock label="招聘人数" value={job.headcount} /><ValueBlock label="福利" value={job.benefits} /><ValueBlock label="截止日期" value={job.explicit_deadline} /><ValueBlock label="投递方式" value={job.application_methods} /><ValueBlock label="联系方式" value={job.contacts} /></dl>}</div>
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<Evidence | null>(null)
  const [error, setError] = useState('')
  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !detail) {
      try { setDetail(await api<Evidence>(`/evidences/${evidence.id}`)) }
      catch (reason) { setError((reason as Error).message) }
    }
  }
  const value = detail || evidence
  return <div className={`evidence${open ? ' open' : ''}`}><span className="evidence-dot" /><div><button className="evidence-toggle" onClick={toggle}><strong>{evidence.source_type}</strong><span>{open ? '收起' : '展开全文'}</span></button><p>{evidence.excerpt || '已保存来源证据'}</p>{open && <div className="evidence-full">{error && <div className="form-error">{error}</div>}<div className="evidence-meta">{value.source_group_name && <span>来源群：{value.source_group_name}</span>}{value.sender && <span>发送者：{value.sender}</span>}{value.sent_at && <span>消息时间：{formatDate(value.sent_at)}</span>}{value.observed_at && <span>处理时间：{formatDate(value.observed_at)}</span>}</div>{value.artifact_id && value.mime_type?.startsWith('image/') && <img src={`/api/v1/artifacts/${value.artifact_id}`} alt={value.filename || '来源图片'} />}{value.qr_values?.length ? <><h4>二维码链接</h4><div className="qr-list">{value.qr_values.map((qr, index) => /^https?:\/\//i.test(qr) ? <a href={qr} target="_blank" rel="noreferrer" key={`${qr}-${index}`}>{qr}</a> : <span key={`${qr}-${index}`}>{qr}</span>)}</div></> : null}{value.raw_text && <><h4>原始/提取正文</h4><pre>{value.raw_text}</pre></>}{value.ocr_text && value.ocr_text !== value.raw_text && <><h4>OCR 全文</h4><pre>{value.ocr_text}</pre></>}{value.excerpt && <><h4>结构化结果</h4><pre>{value.excerpt}</pre></>}</div>}{evidence.source_url && <a href={evidence.source_url} target="_blank" rel="noreferrer">打开原始来源 ↗</a>}</div></div>
}

function TimelineEventCard({ event, onOpenCompany }: { event: RecruitmentEvent; onOpenCompany?: (id: string) => void }) {
  return <details className="timeline-event"><summary><time>{event.start_at ? formatDate(event.start_at) : '时间待确认'}</time><div><strong>{event.title}</strong><span>{event.company_name}{event.city ? ` · ${event.city}` : ''}{event.location ? ` · ${event.location}` : ''}</span></div><span className={`status ${event.status}`}>{event.status === 'historical' ? '历史活动' : '即将开始'}</span></summary><div className="timeline-event-detail"><p>{event.notes || '暂无补充说明'}</p>{event.campus && <span>校区：{event.campus}</span>}{event.audience && <span>面向：{event.audience}</span>}{event.application_url && <a href={event.application_url} target="_blank" rel="noreferrer">打开网申/活动地址 ↗</a>}{onOpenCompany && <button className="secondary" onClick={() => onOpenCompany(event.company_id)}>查看企业</button>}</div></details>
}

function TimelinePage({ events, onOpenCompany }: { events: RecruitmentEvent[]; onOpenCompany: (id: string) => void }) {
  const [filter, setFilter] = useState('')
  const visible = events.filter(event => !filter || event.event_type === filter)
  const types = Array.from(new Set(events.map(event => event.event_type)))
  return <><PageHeader eyebrow="招聘日程" title="招聘时间轴" description="集中查看宣讲、网申截止、笔试、面试和其他招聘节点。" /><div className="toolbar"><select className="filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部事件</option>{types.map(value => <option value={value} key={value}>{value}</option>)}</select></div><div className="timeline-list">{visible.length ? visible.map(event => <TimelineEventCard event={event} onOpenCompany={onOpenCompany} key={event.id} />) : <div className="empty-state compact">暂无招聘时间事件</div>}</div></>
}

function ApplicationsPage({ applications, onState }: { applications: Application[]; onState: (id: string, state: string, favorite?: boolean) => void }) { const columns = [['interested', '感兴趣'], ['applied', '已投递'], ['interview', '面试中'], ['offer', 'Offer']] as const; return <><PageHeader eyebrow="我的行动" title="求职进度" description="把感兴趣的岗位，从看到变成投递和面试。" /><div className="kanban">{columns.map(([state, title]) => <ApplicationColumn key={state} title={title} state={state} jobs={applications.filter(job => job.state === state)} onState={onState} />)}</div>{!applications.length && <div className="empty-state compact"><div className="empty-icon">✓</div><h3>收藏岗位后，它们会出现在这里</h3><p>在企业详情中点击星标，即可开始记录求职进度。</p></div>}</> }
function ApplicationColumn({ title, state, jobs, onState }: { title: string; state: string; jobs: Job[]; onState: (id: string, state: string, favorite?: boolean) => void }) { return <div className="kanban-column"><div className="column-head"><strong>{title}</strong><span>{jobs.length}</span></div>{jobs.map(job => <button className="application-card" key={job.id} onClick={() => onState(job.id, state)}><strong>{job.canonical_title}</strong><small>{job.company_name}</small></button>)}<button className="add-card">＋ 添加岗位</button></div> }

function AdminPage({ onNavigate }: { onNavigate: (page: 'settings' | 'review') => void }) {
  const [invitations, setInvitations] = useState<Invitation[]>([])
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  const [initialPassword, setInitialPassword] = useState('')
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
      await api('/admin/invitations', { method: 'POST', body: JSON.stringify({ email: email.trim(), role, password: initialPassword }) })
      setEmail('')
      setRole('member')
      setInitialPassword('')
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

  return <><PageHeader eyebrow="管理员" title="管理台" description="管理访问权限，并从这里进入系统配置和审核队列。"><button className="secondary" onClick={() => onNavigate('settings')}>系统设置</button><button className="secondary" onClick={() => onNavigate('review')}>待审核</button></PageHeader><div className="metrics"><Metric label="邀请总数" value={invitations.length} tone="blue" /><Metric label="待登录" value={activeCount} tone="green" /><Metric label="已使用" value={usedCount} tone="violet" /><Metric label="已过期" value={expiredCount} tone="orange" /></div><div className="admin-grid"><section className="detail-card setting-section"><h2>邀请用户</h2><p className="setting-help">邀请有效期为 72 小时。请通过安全方式把初始密码告知受邀用户，受邀用户使用邮箱和密码登录。</p><form className="invite-form" onSubmit={invite}><label>邮箱<input type="email" required value={email} onChange={event => setEmail(event.target.value)} placeholder="name@example.com" /></label><label>角色<select value={role} onChange={event => setRole(event.target.value as 'member' | 'admin')}><option value="member">受邀用户</option><option value="admin">管理员</option></select></label><label>初始密码<input type="password" required minLength={8} maxLength={128} value={initialPassword} onChange={event => setInitialPassword(event.target.value)} placeholder="至少 8 位" /></label><button className="primary" disabled={busy || !email.trim() || !initialPassword} type="submit">{busy ? '创建中…' : '创建邀请'}</button></form>{message && <p className="setting-help admin-message">{message}</p>}</section><section className="detail-card setting-section"><h2>管理员工作流</h2><div className="admin-guide"><div><strong>1. 配置数据源</strong><p>在“系统设置”连接 TraceMemo，读取并选择招聘群。</p><button className="secondary" onClick={() => onNavigate('settings')}>打开系统设置 →</button></div><div><strong>2. 处理异常信息</strong><p>低置信度识别、字段冲突和失败任务会进入“待审核”。</p><button className="secondary" onClick={() => onNavigate('review')}>打开待审核 →</button></div><div><strong>3. 同步和导入</strong><p>回到“企业与岗位”点击“立即同步”，或使用“导入信息”手工补录。</p></div></div></section></div><section className="detail-card invitation-section"><div className="section-title"><h2>邀请记录</h2><div className="section-actions"><span>{invitations.length} 条</span><button className="secondary" onClick={load} disabled={loading}>↻ 刷新</button></div></div>{loading ? <div className="loading-inline">加载邀请记录…</div> : invitations.length ? <div className="invitation-list">{invitations.map(item => <InvitationRow key={item.id} invitation={item} />)}</div> : <div className="empty-inline">还没有邀请记录</div>}</section></>
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
  const pretty = (value: any) => typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)
  return <><PageHeader eyebrow="管理员" title="待审核" description="低置信度识别、字段冲突和处理失败会在这里保留完整错误、原始消息与阶段日志。"><button className="secondary" onClick={load}>↻ 刷新</button></PageHeader>{message && <div className="setting-help">{message}</div>}{items.length ? <div className="review-list">{items.map(item => { const payload = item.payload || {}; const original = payload.original_message || payload.original_messages; const task = payload.job || payload.processing_job; const decision = Object.fromEntries(Object.entries(payload).filter(([key]) => !['error', 'original_message', 'original_messages', 'job', 'processing_job', 'processing_logs'].includes(key))); return <div className="detail-card review-card" key={item.id}><div className="review-head"><div><strong>{item.kind}</strong><span>{item.entity_type || 'unknown'} / {item.entity_id || '—'}{item.created_at ? ` · ${formatDate(item.created_at)}` : ''}</span></div><div><button className="secondary" onClick={() => resolve(item.id, 'rejected')}>保留待查</button><button className="primary" onClick={() => resolve(item.id, 'resolved')}>标记已处理</button></div></div><div className="review-sections"><details className="review-section" open={Boolean(payload.error)}><summary>错误信息</summary><pre>{pretty(payload.error || payload.reason || '未提供错误信息')}</pre></details><details className="review-section" open={Boolean(original)}><summary>原始消息（完整内容）</summary><pre>{pretty(original || '未找到关联原始消息')}</pre></details><details className="review-section"><summary>处理任务</summary><pre>{pretty(task || '未找到关联处理任务')}</pre></details><details className="review-section"><summary>处理日志（{Array.isArray(payload.processing_logs) ? payload.processing_logs.length : 0} 条）</summary><pre>{pretty(payload.processing_logs || [])}</pre></details><details className="review-section"><summary>模型/审核结果</summary><pre>{pretty(decision)}</pre></details><details className="review-section"><summary>完整审核载荷</summary><pre>{pretty(payload)}</pre></details></div></div> })}</div> : <div className="empty-state compact"><div className="empty-icon">✓</div><h3>当前没有待审核项</h3><p>自动识别产生低置信度结果或字段冲突后，会在这里显示。</p></div>}</>
}

function ImportPage({ onImported }: { onImported: () => Promise<void> }) { const [text, setText] = useState(''); const [url, setUrl] = useState(''); const [busy, setBusy] = useState(false); const submitText = async () => { if (!text.trim()) return; setBusy(true); try { await api('/imports/text', { method: 'POST', body: JSON.stringify({ text }) }); setText(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitUrl = async () => { if (!url.trim()) return; setBusy(true); try { await api('/imports/url', { method: 'POST', body: JSON.stringify({ url }) }); setUrl(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitFile = async (file: File) => { setBusy(true); try { const form = new FormData(); form.append('file', file); await api('/imports/files', { method: 'POST', body: form }); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; return <><PageHeader eyebrow="数据入口" title="导入招聘信息" description="先把信息放进来，系统会自动解析、分类、去重并保留来源。" /><div className="import-grid"><div className="detail-card import-card"><div className="import-icon blue">✎</div><h2>粘贴群聊文字</h2><p>适合复制微信群中的招聘消息、合并转发和招聘说明。</p><textarea value={text} onChange={e => setText(e.target.value)} rows={10} placeholder="粘贴招聘群消息……" /><button className="primary" disabled={busy || !text.trim()} onClick={submitText}>{busy ? '提交中…' : '加入处理队列 →'}</button></div><div className="detail-card import-card"><div className="import-icon violet">↗</div><h2>公开链接与文件</h2><p>公众号文章、企业官网、招聘平台和其他公开页面均可。</p><input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/recruitment" /><div className="dropzone">将网页 URL 粘贴到上方<br /><span>验证码、登录页和小程序请手工补录</span></div><button className="secondary" disabled={busy || !url.trim()} onClick={submitUrl}>抓取并加入队列 →</button><label className="file-input">选择 PDF、DOCX、XLSX、CSV、TXT 或图片<input type="file" accept=".pdf,.docx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.webp" onChange={e => e.target.files?.[0] && submitFile(e.target.files[0])} /></label></div></div></> }

function QueuePage({ onSync, syncing }: { onSync: () => Promise<void>; syncing: boolean }) {
  const [queue, setQueue] = useState<ProcessingQueue | null>(null)
  const [filter, setFilter] = useState('')
  const [message, setMessage] = useState('')
  const [retrying, setRetrying] = useState('')
  const [logs, setLogs] = useState<Record<string, ProcessingLog[]>>({})
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  const load = async () => {
    try {
      const path = filter ? `/admin/processing-queue?status=${encodeURIComponent(filter)}` : '/admin/processing-queue'
      setQueue(await api<ProcessingQueue>(path))
      setMessage('')
    } catch (e) {
      setMessage((e as Error).message)
    }
  }

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => { void load() }, 3000)
    return () => window.clearInterval(timer)
  }, [filter])

  const retry = async (id: string) => {
    setRetrying(id)
    try {
      await api(`/admin/processing-queue/${id}/retry`, { method: 'POST' })
      await load()
    } catch (e) {
      setMessage((e as Error).message)
    } finally {
      setRetrying('')
    }
  }

  const control = async (action: 'run' | 'pause' | 'cancel_all') => {
    if (action === 'cancel_all' && !window.confirm('确认取消全部未完成任务？原始信息不会删除，可以重新入队。')) return
    try { await api('/admin/processing-queue/control', { method: 'POST', body: JSON.stringify({ action }) }); await load() }
    catch (e) { setMessage((e as Error).message) }
  }

  const cancel = async (id: string) => {
    try { await api(`/admin/processing-queue/${id}/cancel`, { method: 'POST' }); await load() }
    catch (e) { setMessage((e as Error).message) }
  }

  const cancelSelected = async () => {
    if (!selectedIds.length || !window.confirm(`确认取消选中的 ${selectedIds.length} 个任务？`)) return
    try {
      await api('/admin/processing-queue/cancel', { method: 'POST', body: JSON.stringify({ ids: selectedIds }) })
      setSelectedIds([])
      await load()
    } catch (e) { setMessage((e as Error).message) }
  }

  const toggleLogs = async (id: string) => {
    if (logs[id]) { setLogs(current => { const next = { ...current }; delete next[id]; return next }); return }
    try { setLogs(current => ({ ...current, [id]: [] })); const values = await api<ProcessingLog[]>(`/admin/processing-queue/${id}/logs`); setLogs(current => ({ ...current, [id]: values })) }
    catch (e) { setMessage((e as Error).message) }
  }

  const stats = queue?.stats || {}
  const attention = (stats.needs_review || 0) + (stats.paused_quota || 0) + (stats.failed || 0)
  const statusLabel: Record<string, string> = { pending: '等待处理', running: '处理中', succeeded: '已完成', needs_review: '需要处理', paused_quota: '额度暂停', failed: '失败', canceled: '已取消' }
  const kindLabel: Record<string, string> = { classify: '来源识别', consolidate_company: '企业内容整理' }
  const stageLabel: Record<string, string> = { queued: '等待领取', starting: '启动任务', extracting: '提取来源内容', codex_fallback: 'Codex 兜底提取 / OCR', classifying: '招聘识别与结构化', persisting: '写入企业、岗位与时间轴', waiting_for_sources: '等待来源汇总', consolidating: '合并企业资料', retry_wait: '等待自动重试', review: '等待人工审核', failed: '处理失败', completed: '已完成', canceled: '已取消' }

  const cancellableIds = queue?.items.filter(item => ['pending', 'running', 'needs_review', 'paused_quota', 'failed'].includes(item.status)).map(item => item.id) || []
  const allSelected = cancellableIds.length > 0 && cancellableIds.every(id => selectedIds.includes(id))
  return <><PageHeader eyebrow="管理员" title="处理队列" description={`当前队列${queue?.state === 'running' ? '正在运行' : '已暂停'}；聊天时间来自原始消息，入队时间仅表示系统开始处理的时间。`}><button className="secondary" onClick={load}>↻ 刷新</button>{queue?.state === 'running' ? <button className="secondary" onClick={() => control('pause')}>暂停队列</button> : <button className="primary" onClick={() => control('run')}>继续处理</button>}<button className="secondary danger" onClick={() => control('cancel_all')}>取消全部未完成</button><button className="primary" disabled={syncing} onClick={() => onSync()}>{syncing ? '同步中…' : '从已选微信群获取'}</button></PageHeader><div className="metrics"><Metric label="等待处理" value={stats.pending || 0} tone="blue" /><Metric label="正在处理" value={stats.running || 0} tone="violet" /><Metric label="需要处理" value={attention} tone="orange" /><Metric label="已完成" value={stats.succeeded || 0} tone="green" /></div><div className="toolbar queue-toolbar"><select className="filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部未取消任务</option><option value="pending">等待处理</option><option value="running">处理中</option><option value="needs_review">需要处理</option><option value="paused_quota">额度暂停</option><option value="canceled">已取消</option><option value="succeeded">已完成</option></select><label className="queue-select-all"><input type="checkbox" checked={allSelected} onChange={() => setSelectedIds(allSelected ? [] : cancellableIds)} />全选本页可取消任务</label><button className="secondary danger" disabled={!selectedIds.length} onClick={cancelSelected}>取消选中（{selectedIds.length}）</button><span className="queue-total">共 {queue?.total ?? '—'} 个任务，页面每 3 秒自动刷新</span></div>{message && <div className="setting-help">{message}</div>}{queue?.items.length ? <div className="queue-list">{queue.items.map(item => { const canRetry = ['needs_review', 'paused_quota', 'failed', 'canceled'].includes(item.status); const canCancel = ['pending', 'running', 'needs_review', 'paused_quota', 'failed'].includes(item.status); return <article className="detail-card queue-item" key={item.id}><div className="queue-item-head"><label className="queue-selector"><input type="checkbox" disabled={!canCancel} checked={selectedIds.includes(item.id)} onChange={() => setSelectedIds(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} /><span className="sr-only">选择任务</span></label><div><strong>{kindLabel[item.kind] || item.kind}</strong><span>{item.source_group_name || (item.connector_id === 'manual' ? '手动导入' : '系统任务')} · 入队 {formatDate(item.created_at)}</span></div><div className="queue-item-actions"><span className={`status queue-status ${item.status}`}>{statusLabel[item.status] || item.status}</span>{canCancel && <button className="secondary danger" onClick={() => cancel(item.id)}>取消</button>}{canRetry && <button className="secondary" disabled={retrying === item.id} onClick={() => retry(item.id)}>{retrying === item.id ? '重试中…' : '重试'}</button>}<button className="secondary" onClick={() => toggleLogs(item.id)}>{logs[item.id] ? '收起日志' : '查看日志'}</button></div></div><div className={`queue-current-step ${item.status}`}><span>当前步骤</span><strong>{stageLabel[item.stage] || item.stage || '等待领取'}</strong></div><p className="queue-preview">{item.text_preview || (item.kind === 'consolidate_company' ? '等待合并企业的全部来源信息' : '无文本内容')}</p>{item.error && <pre className="queue-error">{item.error}</pre>}<div className="queue-meta"><span>聊天时间：{item.sent_at ? formatDate(item.sent_at) : '未知'}</span><span>阶段：{item.stage || 'queued'}</span><span>尝试 {item.attempts} 次</span>{item.processor && <span>{item.processor}</span>}<span>{item.message_type || item.kind}{item.sender ? ` · ${item.sender}` : ''}</span><span className="queue-id">{item.raw_message_id || item.company_id || item.id}</span></div>{logs[item.id] && <div className="processing-log">{logs[item.id].length ? logs[item.id].map(log => <div className={`log-line ${log.level}`} key={log.id}><time>{formatDate(log.created_at)}</time><strong>{log.stage}</strong><span>{log.message}</span>{Object.keys(log.details).length > 0 && <pre>{JSON.stringify(log.details, null, 2)}</pre>}</div>) : <div className="empty-inline">暂无阶段日志</div>}</div>}</article> })}</div> : <div className="empty-state compact"><div className="empty-icon">✓</div><h3>{filter ? '没有匹配的任务' : '处理队列为空'}</h3><p>手动导入测试信息或从已选微信群获取后，任务会在这里显示。</p></div>}</>
}

function AccountSecurityPage() {
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [message, setMessage] = useState('')
  const savePassword = async (event: FormEvent) => {
    event.preventDefault()
    if (newPassword !== confirmPassword) { setMessage('两次输入的密码不一致'); return }
    try {
      await api('/auth/password', { method: 'POST', body: JSON.stringify({ password: newPassword }) })
      setNewPassword('')
      setConfirmPassword('')
      setMessage('密码已更新，下次请使用新密码登录')
    } catch (e) { setMessage((e as Error).message) }
  }
  return <><PageHeader eyebrow="账户安全" title="修改登录密码" description="为当前账户设置或更新账号密码；邮箱验证码登录仍保留，但当前默认关闭。" /><section className="detail-card setting-section security-card"><h2>账号密码</h2><form onSubmit={savePassword}><label>新密码<input type="password" required minLength={8} maxLength={128} value={newPassword} onChange={e => setNewPassword(e.target.value)} placeholder="至少 8 位" /></label><label>确认新密码<input type="password" required minLength={8} maxLength={128} value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} placeholder="再次输入新密码" /></label><button className="primary" type="submit">保存密码</button>{message && <p className="setting-help">{message}</p>}</form></section></>
}

function SettingsPage({ onSaved, onSync, syncing }: { onSaved: (message: string) => void; onSync: () => Promise<void>; syncing: boolean }) {
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
    const values = Object.fromEntries(Object.entries(settings).filter(([key]) => key !== 'initial_import_days'))
    await api('/admin/settings', { method: 'PUT', body: JSON.stringify({ values }) })
    await api('/admin/connectors/tracememo', { method: 'PUT', body: JSON.stringify({ ...trace, ...(traceToken ? { token: traceToken } : {}) }) })
    onSaved('设置已保存')
  }
  if (!loaded) return <div className="loading-inline">加载设置…</div>
  const provider = settings.llm_provider || {}
  const backup = settings.backup || {}
  const smtp = settings.smtp || {}
  return <><PageHeader eyebrow="管理员" title="系统设置" description="连接 TraceMemo、选择招聘处理器并配置备份与通知。"><button className="primary" onClick={save}>保存设置</button></PageHeader><div className="settings-grid"><section className="detail-card setting-section"><h2>同步与隐私</h2><label>同步间隔（分钟）<input type="number" min="1" value={settings.sync_interval_minutes || 10} onChange={e => set('sync_interval_minutes', Number(e.target.value))} /></label><label>导入天数（距今）<input type="number" min="1" value={settings.import_days ?? settings.initial_import_days ?? 30} onChange={e => set('import_days', Math.max(1, Number(e.target.value) || 1))} /></label><p className="setting-help">每次同步导入距今设定天数内的聊天记录。</p><label className="check"><input type="checkbox" checked={Boolean(settings.redaction_enabled)} onChange={e => set('redaction_enabled', e.target.checked)} />发送云模型前启用脱敏（默认关闭）</label></section><section className="detail-card setting-section"><h2>处理器与并发</h2><label>招聘识别与整理处理器<select value={settings.processing_engine || 'codex'} onChange={e => set('processing_engine', e.target.value)}><option value="codex">本地 Codex（默认）</option><option value="generic">通用模型 API</option></select></label><p className="setting-help">本地 Codex 固定使用 gpt-5.6-luna。切换只影响新任务和重新处理的任务。</p><label>模型并发（1–8）<input type="number" min="1" max="8" value={settings.model_concurrency || 2} onChange={e => set('model_concurrency', Math.max(1, Math.min(8, Number(e.target.value))))} /></label><label>Codex 并发（1–4）<input type="number" min="1" max="4" value={settings.codex_concurrency || 1} onChange={e => set('codex_concurrency', Math.max(1, Math.min(4, Number(e.target.value))))} /></label><label>本地提取并发<input type="number" min="1" max="16" value={settings.extract_concurrency || 4} onChange={e => set('extract_concurrency', Math.max(1, Math.min(16, Number(e.target.value))))} /></label><label>阶段日志保留天数<input type="number" min="1" value={settings.processing_log_retention_days || 30} onChange={e => set('processing_log_retention_days', Math.max(1, Number(e.target.value)))} /></label><button className="secondary" onClick={async () => { try { await api('/admin/models/test', { method: 'POST' }); onSaved('当前处理器连接成功') } catch (e) { alert((e as Error).message) } }}>测试当前处理器</button></section><section className="detail-card setting-section"><h2>TraceMemo</h2><label className="check"><input type="checkbox" checked={Boolean(trace.enabled)} onChange={e => setTrace({ ...trace, enabled: e.target.checked })} />启用自动同步</label><label>API 地址<input value={trace.base_url} onChange={e => setTrace({ ...trace, base_url: e.target.value })} /></label><label>Bearer Token<input type="password" value={traceToken} placeholder="已配置时留空保持不变" onChange={e => setTraceToken(e.target.value)} /></label><p className="setting-help">保存后可在后端接口读取群聊列表并手工选择招聘群；选择保存后，可点击下方按钮立即获取，自动同步仍按设定间隔运行。</p><TraceMemoGroups onSync={onSync} syncing={syncing} /></section><section className="detail-card setting-section"><h2>通用模型 API</h2><label className="check"><input type="checkbox" checked={Boolean(provider.enabled)} onChange={e => set('llm_provider', { ...provider, enabled: e.target.checked })} />启用云模型处理</label><label>API 风格<select value={provider.api_style || 'chat_completions'} onChange={e => set('llm_provider', { ...provider, api_style: e.target.value })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label><label>Base URL<input value={provider.base_url || ''} onChange={e => set('llm_provider', { ...provider, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label><label>模型名称<input value={provider.model || provider.text_model || ''} onChange={e => set('llm_provider', { ...provider, model: e.target.value, text_model: e.target.value })} /></label><label>API Key<input type="password" placeholder={provider.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={e => set('llm_provider', { ...provider, api_key: e.target.value })} /></label></section><section className="detail-card setting-section"><h2>AList WebDAV 备份</h2><label className="check"><input type="checkbox" checked={Boolean(backup.enabled)} onChange={e => set('backup', { ...backup, enabled: e.target.checked })} />启用每日备份</label><label>WebDAV URL<input value={backup.webdav_url || ''} onChange={e => set('backup', { ...backup, webdav_url: e.target.value })} placeholder="https://alist.example.com/dav/" /></label><label>用户名<input value={backup.username || ''} onChange={e => set('backup', { ...backup, username: e.target.value })} /></label><label>远端目录<input value={backup.remote_directory || '/JobPostings'} onChange={e => set('backup', { ...backup, remote_directory: e.target.value })} /></label><label>WebDAV 密码<input type="password" placeholder={backup.webdav_password_configured ? '已配置，留空保持不变' : '输入 WebDAV 密码'} onChange={e => set('backup', { ...backup, webdav_password: e.target.value })} /></label><label>备份密码<input type="password" placeholder={backup.backup_password_configured ? '已配置，留空保持不变' : '输入独立备份密码'} onChange={e => set('backup', { ...backup, backup_password: e.target.value })} /></label><div className="button-row"><button className="secondary" onClick={async () => { try { await api('/admin/backups/test', { method: 'POST' }); onSaved('WebDAV 连接成功') } catch (e) { alert((e as Error).message) } }}>测试 WebDAV</button><button className="secondary" onClick={async () => { try { await api('/admin/backups/run', { method: 'POST' }); onSaved('备份已完成') } catch (e) { alert((e as Error).message) } }}>立即备份</button></div></section><section className="detail-card setting-section"><h2>SMTP 邮件</h2><label className="check"><input type="checkbox" checked={Boolean(smtp.enabled)} onChange={e => set('smtp', { ...smtp, enabled: e.target.checked })} />启用邀请邮件（验证码登录当前关闭）</label><label>SMTP 主机<input value={smtp.host || ''} onChange={e => set('smtp', { ...smtp, host: e.target.value })} placeholder="smtp.example.com" /></label><label>端口<input type="number" value={smtp.port || 587} onChange={e => set('smtp', { ...smtp, port: Number(e.target.value) })} /></label><label>发件人邮箱<input type="email" value={smtp.from_email || ''} onChange={e => set('smtp', { ...smtp, from_email: e.target.value })} /></label><label>用户名<input value={smtp.username || ''} onChange={e => set('smtp', { ...smtp, username: e.target.value })} /></label><label>SMTP 密码<input type="password" placeholder={smtp.password_configured ? '已配置，留空保持不变' : '输入 SMTP 密码'} onChange={e => set('smtp', { ...smtp, password: e.target.value })} /></label><label className="check"><input type="checkbox" checked={smtp.starttls !== false} onChange={e => set('smtp', { ...smtp, starttls: e.target.checked })} />使用 STARTTLS</label></section><section className="detail-card setting-section"><h2>Agent API</h2><label className="check"><input type="checkbox" checked={Boolean(settings.agent_api_enabled)} onChange={e => set('agent_api_enabled', e.target.checked)} />允许创建受限 Agent Token</label><p className="setting-help">Token 默认只允许读取招聘目录和操作自己的求职进度，不能读取原始群消息或系统密钥。</p></section></div></>
}

function TraceMemoGroups({ onSync, syncing }: { onSync: (force?: boolean) => Promise<void>; syncing: boolean }) {
  const [groups, setGroups] = useState<TraceMemoGroup[]>([])
  const [query, setQuery] = useState('')
  const [message, setMessage] = useState('')
  const [forceRefetch, setForceRefetch] = useState(false)
  const refresh = async () => {
    try {
      const fetched = await api<TraceMemoGroup[]>('/admin/connectors/tracememo/groups')
      const seen = new Set<string>()
      const unique = fetched.filter(group => {
        const key = group.id || group.external_id
        if (!key || seen.has(key)) return false
        seen.add(key)
        return true
      })
      setGroups(unique)
      setMessage(unique.length ? `已读取 ${unique.length} 个群聊` : 'TraceMemo 暂未返回可用群聊')
    }
    catch (e) { setMessage((e as Error).message) }
  }
  useEffect(() => { void refresh() }, [])
  const save = async () => {
    try { await api('/admin/source-groups', { method: 'PUT', body: JSON.stringify({ groups }) }); setMessage('群组选择已保存'); return true }
    catch (e) { setMessage((e as Error).message); return false }
  }
  const selectedCount = groups.filter(group => group.selected).length
  const visibleGroups = groups.filter(group => `${group.name} ${group.external_id}`.toLowerCase().includes(query.trim().toLowerCase()))
  const toggle = (groupId: string, selected: boolean) => {
    if (selected && selectedCount >= 20) {
      setMessage('最多选择 20 个招聘群')
      return
    }
    setGroups(current => current.map(group => group.id === groupId ? { ...group, selected } : group))
    setMessage('')
  }
  const avatarSource = (avatar?: string | null) => avatar && /^(data:image\/|https?:\/\/|\/)/i.test(avatar) ? avatar : ''
  const initial = (name: string) => Array.from(name.trim())[0] || '群'
  const startSync = async () => {
    if (forceRefetch && !window.confirm('强制重新获取会先备份并删除当前招聘目录、原始消息、处理队列、审核记录及与旧岗位绑定的收藏/申请/备注/关注。账号、设置和微信群选择会保留。确认继续？')) return
    if (!await save()) return
    await onSync(forceRefetch)
  }
  return <div className="group-picker"><div className="group-picker-head"><strong>招聘群选择</strong><span>{selectedCount}/20</span></div><div className="group-picker-actions"><button className="secondary" onClick={refresh}>读取群列表</button><button className="secondary" onClick={save} disabled={!groups.length}>保存选择</button><button className="primary" onClick={startSync} disabled={syncing || selectedCount === 0}>{syncing ? '获取中…' : '立即从已选微信群获取'}</button></div><label className="check force-refetch"><input type="checkbox" checked={forceRefetch} onChange={event => setForceRefetch(event.target.checked)} />强制重新获取（删除旧招聘数据后重新拉取）</label>{forceRefetch && <p className="setting-warning">执行前会自动创建本地备份；旧目录、队列、审核、证据和与旧岗位绑定的用户操作记录会被清理。</p>}{groups.length > 0 && <div className="group-picker-toolbar"><input className="group-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索群名称或群标识" /><span>{visibleGroups.length} 个群聊</span></div>}<div className="group-options">{!groups.length ? <div className="group-empty">正在读取群聊；如果还没有群，请点击“读取群列表”</div> : !visibleGroups.length ? <div className="group-empty">没有匹配的群聊</div> : visibleGroups.map(group => { const source = avatarSource(group.avatar); return <label className={`group-option${group.selected ? ' selected' : ''}`} key={group.id}><input type="checkbox" checked={group.selected} onChange={event => toggle(group.id, event.target.checked)} /><span className="group-avatar-wrap">{source ? <img className="group-avatar-image" src={source} alt={`${group.name}头像`} loading="lazy" /> : <span className="group-avatar" aria-hidden="true">{initial(group.name)}</span>}</span><span className="group-option-copy"><strong>{group.name}</strong><small>{group.external_id}</small></span></label> })}</div><p className="setting-help">TraceMemo 当前群聊接口未提供头像时显示群名首字母；如接口返回头像资源则自动使用。{message && ` ${message}`}</p></div>
}

function PageHeader({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: React.ReactNode }) { return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div><div className="header-actions">{children}</div></header> }

export default App

createRoot(document.getElementById('root')!).render(<App />)
