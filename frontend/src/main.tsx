import { FormEvent, useEffect, useState } from 'react'
import './styles.css'

type User = { id: string; email: string; role: string }
type Company = { id: string; display_name: string; summary?: string; primary_industry: string; job_count: number; updated_at: string }
type Job = { id: string; canonical_title: string; recruitment_type: string; employment_type: string; status: string; locations_json?: string; locations?: string[]; company_name?: string; updated_at?: string }
type Application = Job & { state: string; favorite: number; updated_at: string }
type CompanyDetail = Company & { aliases: string[]; jobs: Job[]; evidences: { source_url?: string; source_type: string; excerpt?: string }[] }

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
  const [page, setPage] = useState<'companies' | 'applications' | 'import' | 'settings'>('companies')
  const [companies, setCompanies] = useState<Company[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [applications, setApplications] = useState<Application[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<CompanyDetail | null>(null)
  const [notice, setNotice] = useState('')

  const loadData = async () => {
    const [companyData, jobData, applicationData] = await Promise.all([
      api<Company[]>(`/companies?q=${encodeURIComponent(query)}`),
      api<Job[]>('/jobs'),
      api<Application[]>('/me/applications'),
    ])
    setCompanies(companyData)
    setJobs(jobData)
    setApplications(applicationData)
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
  const updateState = async (jobId: string, state: string, favorite = false) => {
    try { await api(`/me/jobs/${jobId}/state`, { method: 'PUT', body: JSON.stringify({ state, favorite }) }); flash('求职进度已更新'); await loadData() }
    catch (e) { setError((e as Error).message) }
  }
  const followCompany = async (companyId: string, followed = true) => {
    try { await api(`/me/companies/${companyId}/follow`, { method: 'PUT', body: JSON.stringify({ followed }) }); flash(followed ? '已关注企业' : '已取消关注') }
    catch (e) { setError((e as Error).message) }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="logo"><span className="logo-mark">J</span><span>JobPostings</span></div>
      <div className="side-caption">招聘信息工作台</div>
      <nav>
        <NavButton active={page === 'companies'} onClick={() => { setPage('companies'); setSelected(null) }} icon="⌂">企业与岗位</NavButton>
        <NavButton active={page === 'applications'} onClick={() => { setPage('applications'); setSelected(null) }} icon="✓">求职进度</NavButton>
        <NavButton active={page === 'import'} onClick={() => { setPage('import'); setSelected(null) }} icon="＋">导入信息</NavButton>
        {user.role === 'admin' && <NavButton active={page === 'settings'} onClick={() => { setPage('settings'); setSelected(null) }} icon="⚙">系统设置</NavButton>}
      </nav>
      <div className="sidebar-bottom"><div className="user-avatar">{user.email.slice(0, 1).toUpperCase()}</div><div><strong>{user.email}</strong><small>{user.role === 'admin' ? '管理员' : '受邀用户'}</small></div><button className="logout" onClick={async () => { await api('/auth/logout', { method: 'POST' }); location.reload() }}>↗</button></div>
    </aside>
    <main className="content">
      {error && <div className="error-banner">{error}<button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="notice">{notice}</div>}
      {selected ? <CompanyView company={selected} onBack={() => setSelected(null)} onState={updateState} onFollow={followCompany} /> : page === 'companies' ? <CompaniesPage companies={companies} jobs={jobs} query={query} setQuery={setQuery} onSearch={() => loadData()} onSync={sync} onOpen={openCompany} /> : page === 'applications' ? <ApplicationsPage applications={applications} onState={updateState} /> : page === 'import' ? <ImportPage onImported={async () => { flash('已加入处理队列'); await loadData() }} /> : <SettingsPage onSaved={flash} />}
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

function CompaniesPage({ companies, jobs, query, setQuery, onSearch, onSync, onOpen }: { companies: Company[]; jobs: Job[]; query: string; setQuery: (value: string) => void; onSearch: () => void; onSync: () => void; onOpen: (id: string) => void }) {
  return <><PageHeader eyebrow="招聘知识库" title="企业与岗位" description="把分散在群聊、公众号和文件里的招聘信息，整理成可以行动的机会。"><button className="secondary" onClick={onSync}>↻ 立即同步</button><button className="primary" onClick={() => document.querySelector<HTMLInputElement>('.search-input')?.focus()}>＋ 快速导入</button></PageHeader><div className="metrics"><Metric label="企业" value={companies.length} tone="blue" /><Metric label="岗位" value={jobs.length} tone="violet" /><Metric label="有效岗位" value={jobs.filter(j => j.status === 'active').length} tone="green" /><Metric label="最近更新" value={jobs[0]?.updated_at?.slice(5, 10) || '—'} tone="orange" /></div><div className="toolbar"><div className="search"><span>⌕</span><input className="search-input" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && onSearch()} placeholder="搜索企业、岗位、地点或专业" /></div><button className="filter">筛选　⌄</button><button className="filter">排序：最近更新　⌄</button></div>{companies.length ? <div className="company-grid">{companies.map(company => <CompanyCard key={company.id} company={company} onClick={() => onOpen(company.id)} />)}</div> : <EmptyState />}</>
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone: string }) { return <div className="metric"><div className={`metric-icon ${tone}`}>{tone === 'blue' ? '◈' : tone === 'violet' ? '▣' : tone === 'green' ? '✓' : '◷'}</div><div><small>{label}</small><strong>{value}</strong></div></div> }
function CompanyCard({ company, onClick }: { company: Company; onClick: () => void }) { return <button className="company-card" onClick={onClick}><div className="company-top"><div className="company-avatar">{company.display_name.slice(0, 1)}</div><span className="more">···</span></div><h3>{company.display_name}</h3><div className="chips"><span>{company.primary_industry}</span><span>{company.job_count} 个岗位</span></div><p>{company.summary || '企业介绍将在联网检索或审核后补充。'}</p><div className="card-footer"><span>最近更新</span><time>{company.updated_at?.replace('T', ' ').slice(0, 16) || '—'}</time><span className="arrow">→</span></div></button> }
function EmptyState() { return <div className="empty-state"><div className="empty-icon">✦</div><h3>知识库还在等待第一条招聘信息</h3><p>从“导入信息”粘贴群消息或公开链接，系统会自动识别企业和岗位。</p></div> }

function CompanyView({ company, onBack, onState, onFollow }: { company: CompanyDetail; onBack: () => void; onState: (id: string, state: string, favorite?: boolean) => void; onFollow: (id: string, followed?: boolean) => void }) { return <><button className="back" onClick={onBack}>← 返回企业列表</button><PageHeader eyebrow="企业详情" title={company.display_name} description={`${company.primary_industry} · ${company.jobs.length} 个岗位`}><button className="secondary" onClick={() => onFollow(company.id)}>☆ 关注企业</button></PageHeader><div className="detail-layout"><section><div className="detail-card intro"><div className="large-avatar">{company.display_name.slice(0, 1)}</div><div><h2>企业概览</h2><p>{company.summary || '暂无企业介绍。完成公开网页检索后将在这里显示带来源的企业介绍。'}</p><div className="chips">{company.aliases.map(alias => <span key={alias}>{alias}</span>)}</div></div></div><div className="detail-card"><div className="section-title"><h2>招聘岗位</h2><span>{company.jobs.length} 个岗位</span></div>{company.jobs.length ? company.jobs.map(job => <JobRow key={job.id} job={job} onState={onState} />) : <div className="empty-inline">暂无岗位</div>}</div></section><aside><div className="detail-card"><div className="section-title"><h2>来源与证据</h2><span>{company.evidences.length}</span></div>{company.evidences.slice(0, 8).map((e, index) => <div className="evidence" key={`${e.source_url}-${index}`}><span className="evidence-dot" /><div><strong>{e.source_type}</strong><p>{e.excerpt || '已保存来源证据'}</p>{e.source_url && <a href={e.source_url} target="_blank">打开来源 ↗</a>}</div></div>)}</div></aside></div></> }
function JobRow({ job, onState }: { job: Job; onState: (id: string, state: string, favorite?: boolean) => void }) { const locations = job.locations || (() => { try { return JSON.parse(job.locations_json || '[]') } catch { return [] } })(); return <div className="job-row"><div className="job-symbol">⌁</div><div className="job-main"><strong>{job.canonical_title}</strong><div><span>{job.recruitment_type}</span><span>{job.employment_type}</span><span>{locations.join('、') || '地点待确认'}</span></div></div><span className={`status ${job.status}`}>{job.status === 'active' ? '有效' : job.status === 'possibly_expired' ? '可能过期' : job.status}</span><button className="tiny-action" onClick={() => onState(job.id, 'interested', true)}>☆</button></div> }

function ApplicationsPage({ applications, onState }: { applications: Application[]; onState: (id: string, state: string, favorite?: boolean) => void }) { const columns = [['interested', '感兴趣'], ['applied', '已投递'], ['interview', '面试中'], ['offer', 'Offer']] as const; return <><PageHeader eyebrow="我的行动" title="求职进度" description="把感兴趣的岗位，从看到变成投递和面试。" /><div className="kanban">{columns.map(([state, title]) => <ApplicationColumn key={state} title={title} state={state} jobs={applications.filter(job => job.state === state)} onState={onState} />)}</div>{!applications.length && <div className="empty-state compact"><div className="empty-icon">✓</div><h3>收藏岗位后，它们会出现在这里</h3><p>在企业详情中点击星标，即可开始记录求职进度。</p></div>}</> }
function ApplicationColumn({ title, state, jobs, onState }: { title: string; state: string; jobs: Job[]; onState: (id: string, state: string, favorite?: boolean) => void }) { return <div className="kanban-column"><div className="column-head"><strong>{title}</strong><span>{jobs.length}</span></div>{jobs.map(job => <button className="application-card" key={job.id} onClick={() => onState(job.id, state)}><strong>{job.canonical_title}</strong><small>{job.company_name}</small></button>)}<button className="add-card">＋ 添加岗位</button></div> }

function ImportPage({ onImported }: { onImported: () => Promise<void> }) { const [text, setText] = useState(''); const [url, setUrl] = useState(''); const [busy, setBusy] = useState(false); const submitText = async () => { if (!text.trim()) return; setBusy(true); try { await api('/imports/text', { method: 'POST', body: JSON.stringify({ text }) }); setText(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitUrl = async () => { if (!url.trim()) return; setBusy(true); try { await api('/imports/url', { method: 'POST', body: JSON.stringify({ url }) }); setUrl(''); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; const submitFile = async (file: File) => { setBusy(true); try { const form = new FormData(); form.append('file', file); await api('/imports/files', { method: 'POST', body: form }); await onImported() } catch (e) { alert((e as Error).message) } finally { setBusy(false) } }; return <><PageHeader eyebrow="数据入口" title="导入招聘信息" description="先把信息放进来，系统会自动解析、分类、去重并保留来源。" /><div className="import-grid"><div className="detail-card import-card"><div className="import-icon blue">✎</div><h2>粘贴群聊文字</h2><p>适合复制微信群中的招聘消息、合并转发和招聘说明。</p><textarea value={text} onChange={e => setText(e.target.value)} rows={10} placeholder="粘贴招聘群消息……" /><button className="primary" disabled={busy || !text.trim()} onClick={submitText}>{busy ? '提交中…' : '加入处理队列 →'}</button></div><div className="detail-card import-card"><div className="import-icon violet">↗</div><h2>公开链接与文件</h2><p>公众号文章、企业官网、招聘平台和其他公开页面均可。</p><input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/recruitment" /><div className="dropzone">将网页 URL 粘贴到上方<br /><span>验证码、登录页和小程序请手工补录</span></div><button className="secondary" disabled={busy || !url.trim()} onClick={submitUrl}>抓取并加入队列 →</button><label className="file-input">选择 PDF、DOCX、XLSX、CSV、TXT 或图片<input type="file" accept=".pdf,.docx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.webp" onChange={e => e.target.files?.[0] && submitFile(e.target.files[0])} /></label></div></div></> }

function SettingsPage({ onSaved }: { onSaved: (message: string) => void }) { const [settings, setSettings] = useState<Record<string, any>>({}); const [loaded, setLoaded] = useState(false); useEffect(() => { api<Record<string, any>>('/admin/settings').then(value => { setSettings(value); setLoaded(true) }).catch(() => undefined) }, []); const set = (key: string, value: any) => setSettings(current => ({ ...current, [key]: value })); const save = async () => { await api('/admin/settings', { method: 'PUT', body: JSON.stringify({ values: settings }) }); onSaved('设置已保存') }; if (!loaded) return <div className="loading-inline">加载设置…</div>; const provider = settings.llm_provider || {}; return <><PageHeader eyebrow="管理员" title="系统设置" description="连接 TraceMemo、通用模型、SMTP 和 AList WebDAV。"><button className="primary" onClick={save}>保存设置</button></PageHeader><div className="settings-grid"><section className="detail-card setting-section"><h2>同步与隐私</h2><label>同步间隔（分钟）<input type="number" min="1" value={settings.sync_interval_minutes || 10} onChange={e => set('sync_interval_minutes', Number(e.target.value))} /></label><label>首次导入天数<input type="number" min="1" value={settings.initial_import_days || 30} onChange={e => set('initial_import_days', Number(e.target.value))} /></label><label className="check"><input type="checkbox" checked={Boolean(settings.redaction_enabled)} onChange={e => set('redaction_enabled', e.target.checked)} />发送云模型前启用脱敏（默认关闭）</label></section><section className="detail-card setting-section"><h2>通用模型 API</h2><label className="check"><input type="checkbox" checked={Boolean(provider.enabled)} onChange={e => set('llm_provider', { ...provider, enabled: e.target.checked })} />启用云模型处理</label><label>API 风格<select value={provider.api_style || 'chat_completions'} onChange={e => set('llm_provider', { ...provider, api_style: e.target.value })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label><label>Base URL<input value={provider.base_url || ''} onChange={e => set('llm_provider', { ...provider, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label><label>模型名称<input value={provider.model || provider.text_model || ''} onChange={e => set('llm_provider', { ...provider, model: e.target.value, text_model: e.target.value })} /></label><label>API Key<input type="password" placeholder={provider.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={e => set('llm_provider', { ...provider, api_key: e.target.value })} /></label></section><section className="detail-card setting-section"><h2>Agent API</h2><label className="check"><input type="checkbox" checked={Boolean(settings.agent_api_enabled)} onChange={e => set('agent_api_enabled', e.target.checked)} />允许创建受限 Agent Token</label><p className="setting-help">Token 默认只允许读取招聘目录和操作自己的求职进度，不能读取原始群消息或系统密钥。</p></section></div></> }

function PageHeader({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: React.ReactNode }) { return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div><div className="header-actions">{children}</div></header> }

export default App
