import { FormEvent, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import './queue.css'

type User = { id: string; email: string; role: string; password_configured?: boolean }
type CompanyTag = { category: 'company_type' | 'industry' | string; code: string; label: string }
type PublicFinding = { id: string; finding_type?: string; title: string; summary: string; source_title?: string | null; source_url: string; resolved_url?: string | null; published_at?: string | null; severity: string; retrieved_at: string }
type Company = { id: string; display_name: string; legal_name?: string; website?: string; summary?: string; primary_industry: string; secondary_industries?: string[]; tags?: CompanyTag[]; job_count: number; updated_at: string; company_nature?: string; founded_at?: string; company_size?: string; headquarters?: string; businesses?: string[]; highlights?: string[]; official_channels?: string[]; major_requirements?: string[]; public_researched_at?: string | null; verification_status?: string }
type Job = { id: string; canonical_title: string; recruitment_type: string; employment_type: string; status: string; locations_json?: string; locations?: string[]; company_name?: string; updated_at?: string; department?: string; headcount?: string; education?: string[]; majors?: string[]; experience_requirement?: string; salary?: Record<string, any>; responsibilities?: string; requirements?: string; benefits?: string[]; application_methods?: string[]; contacts?: string[]; explicit_deadline?: string }
type Application = Job & { state: string; favorite: number; updated_at: string }
type Evidence = { id: string; source_url?: string; source_type: string; excerpt?: string; artifact_id?: string; artifact_ids?: string[]; qr_values?: string[]; observed_at?: string; raw_text?: string; ocr_text?: string; sender?: string; sent_at?: string; source_group_name?: string; filename?: string; mime_type?: string; metadata?: Record<string, any> }
type RecruitmentEvent = { id: string; company_id: string; company_name: string; batch_name?: string; title: string; event_type: string; start_at?: string; end_at?: string; timezone: string; format: string; city?: string; campus?: string; location?: string; application_url?: string; audience?: string; notes?: string; job_ids: string[]; evidence_ids: string[]; status: string }
type CompanyDetail = Company & { aliases: string[]; jobs: Job[]; evidences: Evidence[]; events: RecruitmentEvent[]; public_findings?: PublicFinding[] }
type Notification = { id: string; title: string; body: string; read_at?: string | null }
type Invitation = { id: string; email: string; role: string; expires_at: string; used_at?: string | null; created_at: string }
type ReviewItem = { id: string; kind: string; entity_type?: string; entity_id?: string; created_at?: string; payload: Record<string, any> }
type TraceMemoGroup = { id: string; external_id: string; name: string; avatar?: string | null; selected: boolean; enabled?: boolean }
type TraceMemoMessage = { id: string; external_message_id?: string | null; source_group_id: string; group_name: string; sent_at?: string | null; sender?: string; message_type: string; text_preview: string; imported: boolean }
type TraceMemoMessagesResponse = { days: number; groups: number; total: number; items: TraceMemoMessage[] }
type ProcessingLog = { id: string; stage: string; level: string; message: string; details: Record<string, any>; created_at: string }
type ProcessingQueueItem = { id: string; kind: string; raw_message_id?: string | null; company_id?: string | null; status: string; stage: string; attempts: number; lease_until?: string | null; next_attempt_at?: string | null; processor?: string | null; error?: string | null; created_at: string; updated_at: string; connector_id?: string | null; source_group_id?: string | null; source_group_name?: string | null; message_type?: string | null; sender?: string | null; sent_at?: string | null; recognition_status?: string | null; recognized_at?: string | null; recognition_error?: string | null; text_preview?: string | null }
type ProcessingQueue = { state: 'running' | 'paused'; stats: Record<string, number>; items: ProcessingQueueItem[]; total: number }
type LocalBackup = { name: string; size: number; created_at: string }
type LocalStorageSnapshot = { database: { path: string; size: number }; backups: LocalBackup[]; tracememo_cache: { groups: number; messages: number; bytes: number }; chat_records: { messages: number; artifacts: number; artifact_bytes: number } }

const INDUSTRY_OPTIONS = ['internet_software', 'ai_data', 'electronics_semiconductor', 'telecommunications', 'manufacturing_automation', 'automotive_transport_equipment', 'energy_chemical_materials', 'construction_real_estate', 'finance', 'consumer_retail_ecommerce', 'healthcare_biopharma', 'education_research', 'media_culture_entertainment', 'logistics_transportation', 'professional_services', 'government_public_nonprofit', 'agriculture', 'military_defense', 'other']
const TAG_LABELS: Record<string, string> = { private: '民营企业', state_owned: '国有企业', foreign_owned: '外资/外企', joint_venture: '合资企业', public_company: '上市公司', government: '政府/事业单位', unknown: '企业类型待确认', internet_software: '互联网/软件', ai_data: '人工智能/数据', electronics_semiconductor: '电子/半导体', telecommunications: '通信', manufacturing_automation: '制造/自动化', automotive_transport_equipment: '汽车/交通装备', energy_chemical_materials: '能源/化工/材料', construction_real_estate: '建筑/房地产', finance: '金融', consumer_retail_ecommerce: '消费/零售/电商', healthcare_biopharma: '医疗/生物医药', education_research: '教育/科研', media_culture_entertainment: '媒体/文化/娱乐', logistics_transportation: '物流/交通运输', professional_services: '专业服务', government_public_nonprofit: '政府/事业单位/公益组织', agriculture: '农业', military_defense: '军工/国防', other: '其他行业' }
const SOURCE_TYPE_LABELS: Record<string, string> = { wechat_group: '微信群聊', wechat_official_account: '微信公众号', public_web: '公开网页', manual_import: '手动导入', public_negative_news: '公开负面信息' }
const ADMIN_ONLY_PAGES = new Set(['import', 'admin', 'queue', 'settings', 'review'])

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
  const [researching, setResearching] = useState(false)

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
    source.addEventListener('company.updated', refresh)
    source.addEventListener('sync.completed', refresh)
    source.addEventListener('processing.updated', refresh)
    return () => source.close()
  }, [user, query])

  useEffect(() => {
    if (user && user.role !== 'admin' && ADMIN_ONLY_PAGES.has(page)) {
      setSelected(null)
      setPage('companies')
    }
  }, [user, page])

  useEffect(() => {
    if (!window.history.state?.jobPostingsRoot) {
      window.history.replaceState({ ...(window.history.state || {}), jobPostingsRoot: true }, '', window.location.href)
    }
    const handlePopState = () => {
      const companyId = window.history.state?.jobPostingsCompanyId
      if (!companyId) {
        setSelected(null)
        setPage('companies')
        return
      }
      api<CompanyDetail>(`/companies/${companyId}`).then(setSelected).catch(reason => setError((reason as Error).message))
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  const flash = (message: string) => {
    setNotice(message)
    window.setTimeout(() => setNotice(''), 3000)
  }

  if (loading) return <div className="loading-screen"><div className="spinner" />正在加载 JobPostings…</div>
  if (!user) return initialized ? <Login onLoggedIn={setUser} /> : <Bootstrap onLoggedIn={setUser} />

  const isAdmin = user.role === 'admin'
  const canViewPage = isAdmin || !ADMIN_ONLY_PAGES.has(page)

  const sync = async (force = false) => {
    if (syncing) return
    setSyncing(true)
    try {
      const result = await api<{ fetched: number; added: number; created: number; updated: number; duplicates: number; recognized_skipped?: number; filtered_system?: number; media_attached?: number; media_failed?: number; cache_mode?: string; remote_fetches?: number; cached_messages?: number; manual_import_pending?: boolean; reset?: { backup_path?: string; deleted?: Record<string, number> } }>('/admin/sync', { method: 'POST', ...(force ? { body: JSON.stringify({ force: true }) } : {}) })
      const cleared = Object.values(result.reset?.deleted || {}).reduce((total, value) => total + value, 0)
      const resetText = force ? `；已备份并清理 ${cleared} 条旧记录` : ''
      const pendingText = result.manual_import_pending ? '；新消息已放入待导入列表，请手动勾选导入' : ''
      const mediaText = result.media_attached || result.media_failed ? `；图片处理成功 ${result.media_attached || 0}，失败 ${result.media_failed || 0}` : ''
      const cacheText = result.cache_mode === 'cache' ? `；使用本地缓存 ${result.cached_messages || 0} 条` : result.cache_mode === 'mixed' ? `；远程获取 ${result.remote_fetches || 0} 个群，本地缓存 ${result.cached_messages || 0} 条` : result.cache_mode === 'tracememo' ? `；远程获取 ${result.remote_fetches || 0} 个群` : ''
      flash(`同步完成：读取 ${result.fetched} 条，新增 ${result.created} 条，更新 ${result.updated} 条，重复 ${result.duplicates} 条，已识别跳过 ${result.recognized_skipped || 0} 条，过滤系统消息 ${result.filtered_system || 0} 条${cacheText}${mediaText}${resetText}${pendingText}`)
      await loadData()
    }
    catch (e) { setError((e as Error).message) }
    finally { setSyncing(false) }
  }
  const researchCompanies = async (force = false) => {
    if (researching) return
    setResearching(true)
    try {
      const result = await api<{ queued: number; companies: number; skipped_existing: number; skipped_active: number }>('/admin/company-research', { method: 'POST', body: JSON.stringify({ force }) })
      flash(`企业概览任务已排队：新增 ${result.queued} 家，已有记录 ${result.skipped_existing} 家，正在处理 ${result.skipped_active} 家`)
      await loadData()
    } catch (e) { setError((e as Error).message) }
    finally { setResearching(false) }
  }
  const openCompany = async (id: string) => {
    try {
      setSelected(await api<CompanyDetail>(`/companies/${id}`))
      setPage('companies')
      window.history.pushState({ ...(window.history.state || {}), jobPostingsCompanyId: id }, '', window.location.href)
    }
    catch (e) { setError((e as Error).message) }
  }
  const backFromCompany = () => {
    if (window.history.state?.jobPostingsCompanyId) window.history.back()
    else { setSelected(null); setPage('companies') }
  }
  const updateCompany = async (company: CompanyDetail) => {
    setSelected(company)
    await loadData()
    flash('企业资料已保存')
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
        {isAdmin && <NavButton active={page === 'import'} onClick={() => { setPage('import'); setSelected(null) }} icon="＋">导入信息</NavButton>}
        {isAdmin && <NavButton active={page === 'admin'} onClick={() => { setPage('admin'); setSelected(null) }} icon="▦">管理台</NavButton>}
        {isAdmin && <NavButton active={page === 'queue'} onClick={() => { setPage('queue'); setSelected(null) }} icon="≋">处理队列</NavButton>}
        {isAdmin && <NavButton active={page === 'settings'} onClick={() => { setPage('settings'); setSelected(null) }} icon="⚙">系统设置</NavButton>}
        <NavButton active={page === 'security'} onClick={() => { setPage('security'); setSelected(null) }} icon="⌑">账户安全</NavButton>
        {isAdmin && <NavButton active={page === 'review'} onClick={() => { setPage('review'); setSelected(null) }} icon="!">待审核</NavButton>}
      </nav>
      <div className="sidebar-bottom"><div className="user-avatar">{user.email.slice(0, 1).toUpperCase()}</div><div><strong>{user.email}</strong><small>{user.role === 'admin' ? '管理员' : '受邀用户'}</small></div><button className="logout" onClick={async () => { await api('/auth/logout', { method: 'POST' }); location.reload() }}>↗</button></div>
    </aside>
    <main className="content">
      {error && <div className="error-banner">{error}<button onClick={() => setError('')}>×</button></div>}
      {notice && <div className="notice">{notice}</div>}
      {!selected && notifications.filter(item => !item.read_at).slice(0, 3).map(item => <div className="notification-strip" key={item.id}><div><strong>{item.title}</strong><span>{item.body}</span></div><button onClick={() => markNotificationRead(item.id)}>知道了</button></div>)}
      {selected ? <CompanyDetailShell company={selected} onBack={backFromCompany} onState={updateState} onFollow={followCompany} editable={isAdmin} onUpdated={updateCompany} /> : !canViewPage ? <CompaniesPage companies={companies} jobs={jobs} query={query} setQuery={setQuery} onSearch={() => loadData()} onSync={isAdmin ? sync : undefined} syncing={syncing} onResearch={isAdmin ? researchCompanies : undefined} researching={researching} onOpen={openCompany} onExport={exportJobs} onImport={isAdmin ? () => setPage('import') : undefined} /> : page === 'companies' ? <CompaniesPage companies={companies} jobs={jobs} query={query} setQuery={setQuery} onSearch={() => loadData()} onSync={isAdmin ? sync : undefined} syncing={syncing} onResearch={isAdmin ? researchCompanies : undefined} researching={researching} onOpen={openCompany} onExport={exportJobs} onImport={isAdmin ? () => setPage('import') : undefined} /> : page === 'timeline' ? <TimelinePage events={timeline} onOpenCompany={openCompany} /> : page === 'applications' ? <ApplicationsPage applications={applications} onState={updateState} /> : page === 'import' ? <ImportPage onImported={async () => { flash('已加入处理队列'); await loadData(); setPage('queue') }} /> : page === 'admin' ? <AdminPage onNavigate={target => { setPage(target); setSelected(null) }} /> : page === 'queue' ? <QueuePage onSync={sync} syncing={syncing} /> : page === 'security' ? <AccountSecurityPage /> : page === 'review' ? <ReviewPage onResolved={async () => { await loadData() }} /> : <SettingsPage onSaved={flash} onSync={sync} syncing={syncing} />}
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

function CompaniesPage({ companies, jobs, query, setQuery, onSearch, onSync, syncing, onResearch, researching, onOpen, onExport, onImport }: { companies: Company[]; jobs: Job[]; query: string; setQuery: (value: string) => void; onSearch: () => void; onSync?: (force?: boolean) => void; syncing?: boolean; onResearch?: (force?: boolean) => void; researching?: boolean; onOpen: (id: string) => void; onExport: (format: 'xlsx' | 'csv' | 'json') => void; onImport?: () => void }) {
  return <><PageHeader eyebrow="招聘知识库" title="企业与岗位" description="把分散在群聊、公众号和文件里的招聘信息，整理成可以行动的机会。">{onSync && <button className="secondary" disabled={syncing} onClick={() => onSync()}>{syncing ? '同步中…' : '↻ 从微信群同步'}</button>}{onResearch && <button className="secondary" disabled={researching} onClick={() => onResearch()}>{researching ? '概览排队中…' : '⌕ 自动获取企业概览'}</button>}<button className="secondary" onClick={() => onExport('csv')}>导出 CSV</button><button className="secondary" onClick={() => onExport('xlsx')}>导出 Excel</button>{onImport && <button className="primary" onClick={onImport}>＋ 快速导入</button>}</PageHeader><div className="metrics"><Metric label="企业" value={companies.length} tone="blue" /><Metric label="岗位" value={jobs.length} tone="violet" /><Metric label="有效岗位" value={jobs.filter(j => j.status === 'active').length} tone="green" /><Metric label="最近更新" value={jobs[0]?.updated_at?.slice(5, 10) || '—'} tone="orange" /></div><div className="toolbar"><div className="search"><span>⌕</span><input className="search-input" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && onSearch()} placeholder="搜索企业、岗位、地点或专业" /></div><button className="filter">筛选　⌄</button><button className="filter">排序：最近更新　⌄</button></div>{companies.length ? <div className="company-grid">{companies.map(company => <CompanyCard key={company.id} company={company} onClick={() => onOpen(company.id)} />)}</div> : <EmptyState />}</>
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone: string }) { return <div className="metric"><div className={`metric-icon ${tone}`}>{tone === 'blue' ? '◈' : tone === 'violet' ? '▣' : tone === 'green' ? '✓' : '◷'}</div><div><small>{label}</small><strong>{value}</strong></div></div> }
function CompanyTags({ tags, primaryIndustry, limit = 5 }: { tags?: CompanyTag[]; primaryIndustry?: string; limit?: number }) {
  const values = tags?.length ? tags : primaryIndustry ? [{ category: 'industry', code: primaryIndustry, label: TAG_LABELS[primaryIndustry] || primaryIndustry }] : []
  return <div className="chips company-tags">{values.slice(0, limit).map(tag => <span className={`company-tag ${tag.category}`} key={`${tag.category}-${tag.code}`}>{tag.label || TAG_LABELS[tag.code] || tag.code}</span>)}</div>
}
function CompanyCard({ company, onClick }: { company: Company; onClick: () => void }) { return <button className="company-card" onClick={onClick}><div className="company-top"><div className="company-avatar">{company.display_name.slice(0, 1)}</div><span className="more">···</span></div><h3>{company.display_name}</h3><CompanyTags tags={company.tags} primaryIndustry={company.primary_industry} /><div className="chips company-card-meta"><span>{company.job_count} 个岗位</span></div><p>{company.summary || '企业介绍将在联网检索或审核后补充。'}</p><div className="card-footer"><span>最近更新</span><time>{company.updated_at?.replace('T', ' ').slice(0, 16) || '—'}</time><span className="arrow">→</span></div></button> }
function EmptyState() { return <div className="empty-state"><div className="empty-icon">✦</div><h3>知识库还在等待第一条招聘信息</h3><p>从“导入信息”粘贴群消息或公开链接，系统会自动识别企业和岗位。</p></div> }

type CompanyEditForm = {
  display_name: string
  legal_name: string
  aliases: string
  summary: string
  primary_industry: string
  secondary_industries: string
  website: string
  company_nature: string
  founded_at: string
  company_size: string
  headquarters: string
  businesses: string
  highlights: string
  official_channels: string
}

function splitList(value: string) { return Array.from(new Set(value.split(/\r?\n/).map(item => item.trim()).filter(Boolean))) }

function CompanyDetailShell({ company, onBack, onState, onFollow, editable, onUpdated }: { company: CompanyDetail; onBack: () => void; onState: (id: string, state: string, favorite?: boolean) => void; onFollow: (id: string, followed?: boolean) => void; editable: boolean; onUpdated: (company: CompanyDetail) => Promise<void> }) {
  const [editing, setEditing] = useState(false)
  if (editing) return <><button className="back" onClick={onBack}>← 返回企业列表</button><PageHeader eyebrow="企业详情" title={company.display_name} description="管理员正在编辑企业资料"><button className="secondary" onClick={() => setEditing(false)}>取消编辑</button></PageHeader><CompanyEditor company={company} onCancel={() => setEditing(false)} onSaved={updated => { setEditing(false); void onUpdated(updated) }} /></>
  return <><div className="company-edit-bar">{editable ? <><span>管理员可以手动修正企业全称、别名、简介和企业资料。</span><button className="secondary" onClick={() => setEditing(true)}>编辑企业资料</button></> : <span>企业资料由来源证据和模型整理。</span>}</div><CompanyView company={company} onBack={onBack} onState={onState} onFollow={onFollow} /></>
}

function CompanyEditor({ company, onCancel, onSaved }: { company: CompanyDetail; onCancel: () => void; onSaved: (company: CompanyDetail) => void }) {
  const [form, setForm] = useState<CompanyEditForm>({
    display_name: company.display_name || '',
    legal_name: company.legal_name || '',
    aliases: company.aliases?.join('\n') || '',
    summary: company.summary || '',
    primary_industry: company.primary_industry || 'other',
    secondary_industries: company.secondary_industries?.join('\n') || '',
    website: company.website || '',
    company_nature: company.company_nature || '',
    founded_at: company.founded_at || '',
    company_size: company.company_size || '',
    headquarters: company.headquarters || '',
    businesses: company.businesses?.join('\n') || '',
    highlights: company.highlights?.join('\n') || '',
    official_channels: company.official_channels?.join('\n') || '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const update = (field: keyof CompanyEditForm, value: string) => setForm(current => ({ ...current, [field]: value }))
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!form.display_name.trim()) { setError('企业显示名称不能为空'); return }
    setBusy(true)
    setError('')
    try {
      const updated = await api<CompanyDetail>(`/companies/${company.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          display_name: form.display_name.trim(),
          legal_name: form.legal_name.trim() || null,
          aliases: splitList(form.aliases),
          summary: form.summary.trim() || null,
          primary_industry: form.primary_industry || 'other',
          secondary_industries: splitList(form.secondary_industries),
          website: form.website.trim() || null,
          company_nature: form.company_nature.trim() || null,
          founded_at: form.founded_at.trim() || null,
          company_size: form.company_size.trim() || null,
          headquarters: form.headquarters.trim() || null,
          businesses: splitList(form.businesses),
          highlights: splitList(form.highlights),
          official_channels: splitList(form.official_channels),
        }),
      })
      onSaved(updated)
    } catch (reason) { setError((reason as Error).message) }
    finally { setBusy(false) }
  }
  const industries = form.primary_industry && !INDUSTRY_OPTIONS.includes(form.primary_industry) ? [form.primary_industry, ...INDUSTRY_OPTIONS] : INDUSTRY_OPTIONS
  return <form className="detail-card company-editor" onSubmit={submit}><div className="editor-grid"><label>显示名称<input required value={form.display_name} onChange={event => update('display_name', event.target.value)} /></label><label>企业全称<input value={form.legal_name} onChange={event => update('legal_name', event.target.value)} /></label><label>企业性质<input value={form.company_nature} onChange={event => update('company_nature', event.target.value)} /></label><label>成立时间<input value={form.founded_at} onChange={event => update('founded_at', event.target.value)} placeholder="如：2005 年" /></label><label>企业规模<input value={form.company_size} onChange={event => update('company_size', event.target.value)} /></label><label>总部及办公地点<input value={form.headquarters} onChange={event => update('headquarters', event.target.value)} /></label><label>主营行业<select value={form.primary_industry} onChange={event => update('primary_industry', event.target.value)}>{industries.map(industry => <option value={industry} key={industry}>{industry}</option>)}</select></label><label>官方网站<input type="url" value={form.website} onChange={event => update('website', event.target.value)} placeholder="https://" /></label></div><label className="editor-wide">企业简介<textarea rows={6} value={form.summary} onChange={event => update('summary', event.target.value)} /></label><div className="editor-grid editor-lists"><label>企业别名<textarea rows={3} value={form.aliases} onChange={event => update('aliases', event.target.value)} placeholder="一行一个别名" /></label><label>其他行业<textarea rows={3} value={form.secondary_industries} onChange={event => update('secondary_industries', event.target.value)} placeholder="一行一个行业" /></label><label>主要业务<textarea rows={3} value={form.businesses} onChange={event => update('businesses', event.target.value)} placeholder="一行一项" /></label><label>企业亮点<textarea rows={3} value={form.highlights} onChange={event => update('highlights', event.target.value)} placeholder="一行一项" /></label><label>官方招聘渠道<textarea rows={3} value={form.official_channels} onChange={event => update('official_channels', event.target.value)} placeholder="一行一个渠道或链接" /></label></div><div className="button-row"><button className="secondary" type="button" onClick={onCancel}>取消</button><button className="primary" type="submit" disabled={busy}>{busy ? '保存中…' : '保存企业资料'}</button></div>{error && <div className="form-error">{error}</div>}</form>
}

function CompanyView({ company, onBack, onState, onFollow }: { company: CompanyDetail; onBack: () => void; onState: (id: string, state: string, favorite?: boolean) => void; onFollow: (id: string, followed?: boolean) => void }) {
  const facts = [
    ['企业全称', company.legal_name], ['企业性质', company.company_nature], ['成立时间', company.founded_at],
    ['企业规模', company.company_size], ['总部及办公地点', company.headquarters], ['主要业务', company.businesses?.join('、')],
    ['企业亮点', company.highlights?.join('、')], ['官方网站', company.website], ['官方招聘渠道', company.official_channels?.join('、')],
  ].filter(([, value]) => value)
  const findings = company.public_findings || []
  return <><button className="back" onClick={onBack}>← 返回企业列表</button><PageHeader eyebrow="企业详情" title={company.display_name} description={`${TAG_LABELS[company.primary_industry] || company.primary_industry} · ${company.jobs.length} 个岗位`}><button className="secondary" onClick={() => onFollow(company.id)}>☆ 关注企业</button></PageHeader><div className="detail-layout"><section><div className="detail-card intro"><div className="large-avatar">{company.display_name.slice(0, 1)}</div><div><h2>企业概览</h2><p>{company.summary || '企业资料正在根据来源整理。'}</p><CompanyTags tags={company.tags} primaryIndustry={company.primary_industry} /><div className="chips alias-tags">{company.aliases.map(alias => <span key={alias}>{alias}</span>)}</div>{company.public_researched_at && <small className="research-time">公开信息检索于 {formatDate(company.public_researched_at)}</small>}</div></div>{findings.length > 0 ? <div className="detail-card public-findings"><div className="section-title"><h2>公开信息核查</h2><span>{findings.length} 条需留意</span></div><p className="research-disclaimer">以下内容来自公开报道或监管/司法公开来源，需结合原文核实，不等同于对企业作出司法结论。</p>{findings.map(finding => <article className={`public-finding ${finding.severity}`} key={finding.id}><div className="public-finding-head"><strong>{finding.title}</strong><span className={`severity ${finding.severity}`}>{finding.severity === 'high' ? '高关注' : finding.severity === 'medium' ? '中关注' : finding.severity === 'low' ? '低关注' : '待确认'}</span></div><p>{finding.summary}</p><div className="public-finding-meta">{finding.source_title && <span>{finding.source_title}</span>}{finding.published_at && <span>报道时间：{finding.published_at}</span>}<a href={finding.source_url} target="_blank" rel="noreferrer">查看原始来源 ↗</a></div>{finding.resolved_url && finding.resolved_url !== finding.source_url && <details className="resolved-source"><summary>查看跳转诊断地址</summary><code>{finding.resolved_url}</code></details>}</article>)}</div> : company.verification_status?.startsWith('public_web') && <div className="detail-card public-findings empty-public-findings"><div className="section-title"><h2>公开信息核查</h2><span>本次已检索</span></div><p>本次检索未发现带有可靠直接来源的重大负面公开信息；这不等于不存在相关信息，建议结合原始来源持续复核。</p></div>}{facts.length > 0 && <div className="detail-card"><div className="section-title"><h2>企业资料</h2><span>由模型根据证据整理</span></div><dl className="company-facts">{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></div>}{company.major_requirements?.length ? <div className="detail-card"><div className="section-title"><h2>企业需求专业</h2><span>{company.major_requirements.length} 项</span></div><div className="chips alias-tags">{company.major_requirements.map(major => <span key={major}>{major}</span>)}</div></div> : null}<div className="detail-card"><div className="section-title"><h2>招聘时间轴</h2><span>{company.events.length} 项</span></div>{company.events.length ? company.events.map(event => <TimelineEventCard event={event} key={event.id} />) : <div className="empty-inline">暂无明确时间事件</div>}</div><div className="detail-card"><div className="section-title"><h2>招聘岗位</h2><span>{company.jobs.length} 个岗位</span></div>{company.jobs.length ? company.jobs.map(job => <JobRow key={job.id} job={job} onState={onState} />) : <div className="empty-inline">暂无岗位</div>}</div></section><aside><div className="detail-card"><div className="section-title"><h2>来源与证据</h2><span>{company.evidences.length}</span></div>{company.evidences.map(evidence => <EvidenceCard key={evidence.id} evidence={evidence} />)}</div></aside></div></>
}

function ValueBlock({ label, value }: { label: string; value?: string | string[] | Record<string, any> }) {
  const empty = value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length) || (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length)
  const shown = empty ? '信息中未提及' : Array.isArray(value) ? value.join('、') : typeof value === 'object' ? JSON.stringify(value, null, 2) : value
  return <div className="job-field"><dt>{label}</dt><dd>{shown}</dd></div>
}

function JobRow({ job, onState }: { job: Job; onState: (id: string, state: string, favorite?: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const locations = job.locations || (() => { try { return JSON.parse(job.locations_json || '[]') } catch { return [] } })()
  return <div className={`job-card-row${open ? ' open' : ''}`}><div className="job-row" role="button" tabIndex={0} onClick={() => setOpen(value => !value)} onKeyDown={event => event.key === 'Enter' && setOpen(value => !value)}><div className="job-symbol">⌁</div><div className="job-main"><strong>{job.canonical_title}</strong><div><span>{job.recruitment_type}</span><span>{job.employment_type}</span><span>{locations.join('、') || '地点待确认'}</span></div></div><span className={`status ${job.status}`}>{job.status === 'active' ? '有效' : job.status === 'possibly_expired' ? '可能过期' : job.status}</span><button className="tiny-action" aria-label="收藏岗位" onClick={event => { event.stopPropagation(); onState(job.id, 'interested', true) }}>☆</button><span className="expand-mark">{open ? '⌃' : '⌄'}</span></div>{open && <dl className="job-details"><ValueBlock label="部门" value={job.department} /><ValueBlock label="岗位职责" value={job.responsibilities} /><ValueBlock label="任职要求" value={job.requirements} /><ValueBlock label="学历要求" value={job.education} /><ValueBlock label="经验要求" value={job.experience_requirement} /><ValueBlock label="薪资" value={job.salary} /><ValueBlock label="招聘人数" value={job.headcount} /><ValueBlock label="福利" value={job.benefits} /><ValueBlock label="截止日期" value={job.explicit_deadline} /><ValueBlock label="投递方式" value={job.application_methods} /><ValueBlock label="联系方式" value={job.contacts} /></dl>}</div>
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
  return <div className={`evidence${open ? ' open' : ''}`}><span className="evidence-dot" /><div><button className="evidence-toggle" onClick={toggle}><strong>{SOURCE_TYPE_LABELS[evidence.source_type] || evidence.source_type}</strong><span>{open ? '收起' : '展开全文'}</span></button><p>{evidence.excerpt || '已保存来源证据'}</p>{open && <div className="evidence-full">{error && <div className="form-error">{error}</div>}<div className="evidence-meta">{value.source_group_name && <span>来源群：{value.source_group_name}</span>}{value.sender && <span>发送者：{value.sender}</span>}{value.sent_at && <span>消息时间：{formatDate(value.sent_at)}</span>}{value.observed_at && <span>处理时间：{formatDate(value.observed_at)}</span>}</div>{value.artifact_id && value.mime_type?.startsWith('image/') && <img src={`/api/v1/artifacts/${value.artifact_id}`} alt={value.filename || '来源图片'} />}{value.qr_values?.length ? <><h4>二维码链接</h4><div className="qr-list">{value.qr_values.map((qr, index) => /^https?:\/\//i.test(qr) ? <a href={qr} target="_blank" rel="noreferrer" key={`${qr}-${index}`}>{qr}</a> : <span key={`${qr}-${index}`}>{qr}</span>)}</div></> : null}{value.raw_text && <><h4>原始/提取正文</h4><pre>{value.raw_text}</pre></>}{value.ocr_text && value.ocr_text !== value.raw_text && <><h4>OCR 全文</h4><pre>{value.ocr_text}</pre></>}{value.excerpt && <><h4>结构化结果</h4><pre>{value.excerpt}</pre></>}</div>}{evidence.source_url && <a href={evidence.source_url} target="_blank" rel="noreferrer">打开原始来源 ↗</a>}</div></div>
}

const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

function formatEventDate(value?: string | null, timezone?: string) {
  if (!value) return '时间待确认'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return formatDate(value)
  try {
    return new Intl.DateTimeFormat('zh-CN', { timeZone: timezone || 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date)
  } catch {
    return formatDate(value)
  }
}

function eventDateKey(value?: string | null, timezone?: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  try {
    const parts = new Intl.DateTimeFormat('en', { timeZone: timezone || 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date)
    const part = (type: string) => parts.find(item => item.type === type)?.value || ''
    return `${part('year')}-${part('month')}-${part('day')}`
  } catch {
    return localDateKey(date)
  }
}

function localDateKey(date: Date) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function startOfWeek(value: Date) {
  const result = new Date(value.getFullYear(), value.getMonth(), value.getDate())
  const day = result.getDay()
  result.setDate(result.getDate() - (day === 0 ? 6 : day - 1))
  return result
}

function addDays(value: Date, days: number) {
  const result = new Date(value)
  result.setDate(result.getDate() + days)
  return result
}

function formatShortDate(value: Date) {
  return `${value.getMonth() + 1}月${value.getDate()}日`
}

function formatWeekRange(start: Date, end: Date) {
  return `${start.getFullYear()}年${formatShortDate(start)}—${formatShortDate(end)}`
}

function TimelineEventCard({ event, onOpenCompany }: { event: RecruitmentEvent; onOpenCompany?: (id: string) => void }) {
  return <details className="timeline-event"><summary><time>{event.start_at ? formatEventDate(event.start_at, event.timezone) : '时间待确认'}</time><div><strong>{event.title}</strong><span>{event.company_name}{event.city ? ` · ${event.city}` : ''}{event.location ? ` · ${event.location}` : ''}</span></div><span className={`status ${event.status}`}>{event.status === 'historical' ? '历史活动' : '即将开始'}</span></summary><div className="timeline-event-detail"><p>{event.notes || '暂无补充说明'}</p>{event.start_at && <span>活动时间：{formatEventDate(event.start_at, event.timezone)}{event.end_at ? ` 至 ${formatEventDate(event.end_at, event.timezone)}` : ''}</span>}{event.campus && <span>校区：{event.campus}</span>}{event.audience && <span>面向：{event.audience}</span>}{event.application_url && <a href={event.application_url} target="_blank" rel="noreferrer">打开网申/活动地址 ↗</a>}{onOpenCompany && <button className="secondary" onClick={() => onOpenCompany(event.company_id)}>查看企业</button>}</div></details>
}

function TimelinePage({ events, onOpenCompany }: { events: RecruitmentEvent[]; onOpenCompany: (id: string) => void }) {
  const [filter, setFilter] = useState('')
  const [viewMode, setViewMode] = useState<'list' | 'week'>('list')
  const [weekStart, setWeekStart] = useState(() => startOfWeek(new Date()))
  const visible = events.filter(event => !filter || event.event_type === filter)
  const types = Array.from(new Set(events.map(event => event.event_type)))
  const weekDays = Array.from({ length: 7 }, (_, index) => addDays(weekStart, index))
  const weekStartKey = localDateKey(weekStart)
  const weekEndKey = localDateKey(weekDays[6])
  const weekEvents = visible.filter(event => {
    if (!event.start_at) return false
    const key = eventDateKey(event.start_at, event.timezone)
    return key >= weekStartKey && key <= weekEndKey
  })
  const uncertainEvents = visible.filter(event => !event.start_at)
  const sorted = (items: RecruitmentEvent[]) => [...items].sort((left, right) => {
    if (!left.start_at) return 1
    if (!right.start_at) return -1
    return new Date(left.start_at).getTime() - new Date(right.start_at).getTime()
  })
  return <><PageHeader eyebrow="招聘日程" title="招聘时间轴" description="集中查看宣讲、网申截止、笔试、面试和其他招聘节点。"><div className="timeline-view-switch"><button className={`filter${viewMode === 'list' ? ' active-filter' : ''}`} onClick={() => setViewMode('list')}>列表</button><button className={`filter${viewMode === 'week' ? ' active-filter' : ''}`} onClick={() => setViewMode('week')}>周视图</button></div></PageHeader><div className="toolbar"><select className="filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部事件</option>{types.map(value => <option value={value} key={value}>{value}</option>)}</select>{viewMode === 'week' && <div className="timeline-week-controls"><button className="secondary" onClick={() => setWeekStart(addDays(weekStart, -7))}>上一周</button><button className="secondary" onClick={() => setWeekStart(startOfWeek(new Date()))}>本周</button><button className="secondary" onClick={() => setWeekStart(addDays(weekStart, 7))}>下一周</button><span>{formatWeekRange(weekStart, weekDays[6])}</span></div>}</div>{viewMode === 'list' ? <div className="timeline-list">{visible.length ? sorted(visible).map(event => <TimelineEventCard event={event} onOpenCompany={onOpenCompany} key={event.id} />) : <div className="empty-state compact">暂无招聘时间事件</div>}</div> : <>{weekEvents.length ? <div className="timeline-week">{weekDays.map((day, index) => { const key = localDateKey(day); const dayEvents = sorted(weekEvents.filter(event => eventDateKey(event.start_at, event.timezone) === key)); return <section className="timeline-day" key={key}><header><strong>{WEEKDAY_LABELS[index]}</strong><time>{formatShortDate(day)}</time></header>{dayEvents.length ? dayEvents.map(event => <TimelineEventCard event={event} onOpenCompany={onOpenCompany} key={event.id} />) : <div className="timeline-day-empty">暂无活动</div>}</section>})}</div> : <div className="empty-state compact">本周暂无明确时间事件</div>}{uncertainEvents.length > 0 && <section className="timeline-uncertain"><div className="section-title"><h2>时间待确认</h2><span>{uncertainEvents.length} 项</span></div><div className="timeline-list">{uncertainEvents.map(event => <TimelineEventCard event={event} onOpenCompany={onOpenCompany} key={event.id} />)}</div></section>}</>}</>
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

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`
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

function ImportPage({ onImported }: { onImported: () => Promise<void> }) {
  const [text, setText] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [messages, setMessages] = useState<TraceMemoMessage[]>([])
  const [selectedMessageIds, setSelectedMessageIds] = useState<string[]>([])
  const [messageQuery, setMessageQuery] = useState('')
  const [messageNotice, setMessageNotice] = useState('')

  const loadMessages = async () => {
    setLoadingMessages(true)
    try {
      const result = await api<TraceMemoMessagesResponse>(`/admin/tracememo/messages?limit=200${messageQuery.trim() ? `&query=${encodeURIComponent(messageQuery.trim())}` : ''}`)
      setMessages(result.items)
      setSelectedMessageIds(current => current.filter(id => result.items.some(item => item.id === id && !item.imported)))
      setMessageNotice(`已读取 ${result.groups} 个已勾选群聊中的 ${result.total} 条记录（最近 ${result.days} 天）`)
    } catch (e) {
      setMessages([])
      setSelectedMessageIds([])
      setMessageNotice((e as Error).message)
    } finally {
      setLoadingMessages(false)
    }
  }

  useEffect(() => { void loadMessages() }, [])

  const submitText = async () => {
    if (!text.trim()) return
    setBusy(true)
    try { await api('/imports/text', { method: 'POST', body: JSON.stringify({ text }) }); setText(''); await onImported() }
    catch (e) { alert((e as Error).message) }
    finally { setBusy(false) }
  }
  const submitUrl = async () => {
    if (!url.trim()) return
    setBusy(true)
    try { await api('/imports/url', { method: 'POST', body: JSON.stringify({ url }) }); setUrl(''); await onImported() }
    catch (e) { alert((e as Error).message) }
    finally { setBusy(false) }
  }
  const submitFile = async (file: File) => {
    setBusy(true)
    try { const form = new FormData(); form.append('file', file); await api('/imports/files', { method: 'POST', body: form }); await onImported() }
    catch (e) { alert((e as Error).message) }
    finally { setBusy(false) }
  }
  const selectableMessages = messages.filter(item => !item.imported)
  const allSelected = selectableMessages.length > 0 && selectableMessages.every(item => selectedMessageIds.includes(item.id))
  const toggleMessage = (id: string, selected: boolean) => setSelectedMessageIds(current => selected ? [...current, id].filter((value, index, values) => values.indexOf(value) === index) : current.filter(value => value !== id))
  const submitSelectedMessages = async () => {
    if (!selectedMessageIds.length) return
    setBusy(true)
    try {
      const result = await api<{ requested: number; created: number; updated: number; duplicates: number; recognized_skipped: number }>('/admin/tracememo/messages/import', { method: 'POST', body: JSON.stringify({ message_ids: selectedMessageIds }) })
      setMessageNotice(`已提交 ${result.requested} 条记录：新建 ${result.created} 条，更新 ${result.updated} 条，重复/已识别 ${result.duplicates + result.recognized_skipped} 条`)
      setSelectedMessageIds([])
      await onImported()
    } catch (e) { setMessageNotice((e as Error).message) }
    finally { setBusy(false) }
  }

  return <><PageHeader eyebrow="数据入口" title="导入招聘信息" description="先把信息放进来，系统会自动解析、分类、去重并保留来源。" /><div className="import-grid"><div className="detail-card import-card"><div className="import-icon blue">✎</div><h2>粘贴群聊文字</h2><p>适合复制微信群中的招聘消息、合并转发和招聘说明。</p><textarea value={text} onChange={e => setText(e.target.value)} rows={10} placeholder="粘贴招聘群消息……" /><button className="primary" disabled={busy || !text.trim()} onClick={submitText}>{busy ? '提交中…' : '加入处理队列 →'}</button></div><div className="detail-card import-card"><div className="import-icon violet">↗</div><h2>公开链接与文件</h2><p>公众号文章、企业官网、招聘平台和其他公开页面均可。</p><input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/recruitment" /><div className="dropzone">将网页 URL 粘贴到上方<br /><span>验证码、登录页和小程序请手工补录</span></div><button className="secondary" disabled={busy || !url.trim()} onClick={submitUrl}>抓取并加入队列 →</button><label className="file-input">选择 PDF、DOCX、XLSX、CSV、TXT 或图片<input type="file" accept=".pdf,.docx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.webp" onChange={e => e.target.files?.[0] && submitFile(e.target.files[0])} /></label></div><section className="detail-card import-card import-messages-card"><div className="import-icon green">☷</div><h2>选择群聊记录导入</h2><p>先勾选要查看的微信群，再从这些群聊的具体消息中手动选择需要导入的记录。</p><TraceMemoGroups compact syncing={busy} onSaved={() => { void loadMessages() }} /><div className="import-message-toolbar"><input className="message-search" value={messageQuery} onChange={e => setMessageQuery(e.target.value)} onKeyDown={e => e.key === 'Enter' && void loadMessages()} placeholder="搜索消息内容、发送人或群名" /><button className="secondary" disabled={loadingMessages || busy} onClick={() => void loadMessages()}>{loadingMessages ? '读取中…' : '读取记录'}</button><label className="check"><input type="checkbox" checked={allSelected} onChange={() => setSelectedMessageIds(allSelected ? [] : selectableMessages.map(item => item.id))} />全选未导入</label></div>{messageNotice && <p className="setting-help admin-message">{messageNotice}</p>}{messages.length ? <div className="import-message-list">{messages.map(item => <label className={`import-message${item.imported ? ' imported' : ''}`} key={item.id}><input type="checkbox" disabled={busy || item.imported} checked={selectedMessageIds.includes(item.id)} onChange={event => toggleMessage(item.id, event.target.checked)} /><span className="import-message-copy"><strong>{item.group_name} · {item.sent_at ? formatDate(item.sent_at) : '时间未知'}</strong><span>{item.sender || '未知发送人'} · {item.message_type}</span><p>{item.text_preview || '（图片、文件或无文本消息）'}</p></span><span className={`status ${item.imported ? 'expired' : 'active'}`}>{item.imported ? '已导入' : '待导入'}</span></label>)}</div> : <div className="empty-inline">暂无可选记录。请先在上方勾选招聘群，并点击“读取记录”。</div>}<button className="primary" disabled={busy || !selectedMessageIds.length} onClick={submitSelectedMessages}>{busy ? '导入中…' : `导入选中的 ${selectedMessageIds.length} 条记录 →`}</button></section></div></>
}

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
  const recognitionLabel: Record<string, string> = { pending: '待识别', running: '识别中', succeeded: '已识别', needs_review: '识别需审核', canceled: '已取消', filtered: '系统消息已过滤' }
  const kindLabel: Record<string, string> = { classify: '来源识别', consolidate_company: '企业内容整理', research_company: '企业公开概览' }
  const stageLabel: Record<string, string> = { queued: '等待领取', starting: '启动任务', extracting: '提取来源内容', codex_fallback: 'Codex 兜底提取 / OCR', classifying: '招聘识别与结构化', persisting: '写入企业、岗位与时间轴', waiting_for_sources: '等待来源汇总', consolidating: '合并企业资料', research_queued: '等待公开检索', researching: '联网检索企业概览与风险', saving_research: '保存概览、标签与来源', retry_wait: '等待自动重试', review: '等待人工审核', failed: '处理失败', completed: '已完成', canceled: '已取消' }

  const cancellableIds = queue?.items.filter(item => ['pending', 'running', 'needs_review', 'paused_quota', 'failed'].includes(item.status)).map(item => item.id) || []
  const allSelected = cancellableIds.length > 0 && cancellableIds.every(id => selectedIds.includes(id))
  return <><PageHeader eyebrow="管理员" title="处理队列" description={`当前队列${queue?.state === 'running' ? '正在运行' : '已暂停'}；聊天时间来自原始消息，入队时间仅表示系统开始处理的时间。`}><button className="secondary" onClick={load}>↻ 刷新</button>{queue?.state === 'running' ? <button className="secondary" onClick={() => control('pause')}>暂停队列</button> : <button className="primary" onClick={() => control('run')}>继续处理</button>}<button className="secondary danger" onClick={() => control('cancel_all')}>取消全部未完成</button><button className="primary" disabled={syncing} onClick={() => onSync()}>{syncing ? '同步中…' : '从已选微信群获取'}</button></PageHeader><div className="metrics"><Metric label="等待处理" value={stats.pending || 0} tone="blue" /><Metric label="正在处理" value={stats.running || 0} tone="violet" /><Metric label="需要处理" value={attention} tone="orange" /><Metric label="已完成" value={stats.succeeded || 0} tone="green" /></div><div className="toolbar queue-toolbar"><select className="filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部未取消任务</option><option value="pending">等待处理</option><option value="running">处理中</option><option value="needs_review">需要处理</option><option value="paused_quota">额度暂停</option><option value="canceled">已取消</option><option value="succeeded">已完成</option></select><label className="queue-select-all"><input type="checkbox" checked={allSelected} onChange={() => setSelectedIds(allSelected ? [] : cancellableIds)} />全选本页可取消任务</label><button className="secondary danger" disabled={!selectedIds.length} onClick={cancelSelected}>取消选中（{selectedIds.length}）</button><span className="queue-total">共 {queue?.total ?? '—'} 个任务，页面每 3 秒自动刷新</span></div>{message && <div className="setting-help">{message}</div>}{queue?.items.length ? <div className="queue-list">{queue.items.map(item => { const canRetry = ['needs_review', 'paused_quota', 'failed', 'canceled'].includes(item.status); const canCancel = ['pending', 'running', 'needs_review', 'paused_quota', 'failed'].includes(item.status); return <article className="detail-card queue-item" key={item.id}><div className="queue-item-head"><label className="queue-selector"><input type="checkbox" disabled={!canCancel} checked={selectedIds.includes(item.id)} onChange={() => setSelectedIds(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} /><span className="sr-only">选择任务</span></label><div><strong>{kindLabel[item.kind] || item.kind}</strong><span>{item.source_group_name || (item.connector_id === 'manual' ? '手动导入' : '系统任务')} · 入队 {formatDate(item.created_at)}</span></div><div className="queue-item-actions"><span className={`status queue-status ${item.status}`}>{statusLabel[item.status] || item.status}</span>{canCancel && <button className="secondary danger" onClick={() => cancel(item.id)}>取消</button>}{canRetry && <button className="secondary" disabled={retrying === item.id} onClick={() => retry(item.id)}>{retrying === item.id ? '重试中…' : '重试'}</button>}<button className="secondary" onClick={() => toggleLogs(item.id)}>{logs[item.id] ? '收起日志' : '查看日志'}</button></div></div><div className={`queue-current-step ${item.status}`}><span>当前步骤</span><strong>{stageLabel[item.stage] || item.stage || '等待领取'}</strong></div><p className="queue-preview">{item.text_preview || (item.kind === 'consolidate_company' ? '等待合并企业的全部来源信息' : item.kind === 'research_company' ? '正在检索企业官网、行业属性和负面公开报道' : '无文本内容')}</p>{item.error && <pre className="queue-error">{item.error}</pre>}<div className="queue-meta"><span>聊天时间：{item.sent_at ? formatDate(item.sent_at) : '未知'}</span><span>阶段：{item.stage || 'queued'}</span><span>识别：{recognitionLabel[item.recognition_status || ''] || item.recognition_status || '未关联原始消息'}</span><span>尝试 {item.attempts} 次</span>{item.processor && <span>{item.processor}</span>}<span>{item.message_type || item.kind}{item.sender ? ` · ${item.sender}` : ''}</span><span className="queue-id">{item.raw_message_id || item.company_id || item.id}</span></div>{logs[item.id] && <div className="processing-log">{logs[item.id].length ? logs[item.id].map(log => <div className={`log-line ${log.level}`} key={log.id}><time>{formatDate(log.created_at)}</time><strong>{log.stage}</strong><span>{log.message}</span>{Object.keys(log.details).length > 0 && <pre>{JSON.stringify(log.details, null, 2)}</pre>}</div>) : <div className="empty-inline">暂无阶段日志</div>}</div>}</article> })}</div> : <div className="empty-state compact"><div className="empty-icon">✓</div><h3>{filter ? '没有匹配的任务' : '处理队列为空'}</h3><p>手动导入测试信息或从已选微信群获取后，任务会在这里显示。</p></div>}</>
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

function LocalStoragePanel() {
  const [storage, setStorage] = useState<LocalStorageSnapshot | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const load = async () => {
    setBusy('load')
    try { setStorage(await api<LocalStorageSnapshot>('/admin/local-storage')); setMessage('') }
    catch (reason) { setMessage((reason as Error).message) }
    finally { setBusy('') }
  }
  useEffect(() => { void load() }, [])
  const clear = async (kind: 'cache' | 'chat-records') => {
    const title = kind === 'cache' ? 'TraceMemo 聊天缓存' : '本地聊天记录、附件和对应处理任务'
    if (!window.confirm(`确认清除${title}吗？此操作不可自动恢复。`)) return
    setBusy(kind)
    try {
      const result = await api<{ storage: LocalStorageSnapshot }>(`/admin/local-storage/${kind}`, { method: 'DELETE' })
      setStorage(result.storage)
      setMessage(kind === 'cache' ? 'TraceMemo 缓存已清除，下次普通同步会重新访问 TraceMemo。' : '聊天记录及其附件、处理任务已清除；企业和岗位目录已保留。')
    } catch (reason) { setMessage((reason as Error).message) }
    finally { setBusy('') }
  }
  const removeBackup = async (name: string) => {
    if (!window.confirm(`确认删除本地备份“${name}”吗？`)) return
    setBusy(`backup:${name}`)
    try {
      const result = await api<{ storage: LocalStorageSnapshot }>(`/admin/local-storage/backups/${encodeURIComponent(name)}`, { method: 'DELETE' })
      setStorage(result.storage)
      setMessage(`本地备份“${name}”已删除。`)
    } catch (reason) { setMessage((reason as Error).message) }
    finally { setBusy('') }
  }
  return <section className="detail-card setting-section local-storage-section"><div className="section-title"><h2>本地数据管理</h2><button className="secondary" onClick={load} disabled={Boolean(busy)}>{busy === 'load' ? '刷新中…' : '刷新'}</button></div>{storage ? <><div className="storage-summary"><div className="storage-stat"><small>当前数据库</small><strong>{formatBytes(storage.database.size)}</strong><code>{storage.database.path}</code></div><div className="storage-stat"><small>聊天记录</small><strong>{storage.chat_records.messages} 条</strong><span>附件 {storage.chat_records.artifacts} 个 · {formatBytes(storage.chat_records.artifact_bytes)}</span></div><div className="storage-stat"><small>TraceMemo 缓存</small><strong>{storage.tracememo_cache.messages} 条</strong><span>{storage.tracememo_cache.groups} 个群 · {formatBytes(storage.tracememo_cache.bytes)}</span></div><div className="storage-stat"><small>本地 DB 备份</small><strong>{storage.backups.length} 个</strong><span>强制重取前会自动创建</span></div></div><div className="storage-actions"><button className="secondary danger" onClick={() => clear('cache')} disabled={Boolean(busy)}>清除 TraceMemo 缓存</button><button className="secondary danger" onClick={() => clear('chat-records')} disabled={Boolean(busy)}>清除本地聊天记录</button></div><p className="setting-help">清除缓存只影响后续同步是否访问 TraceMemo；清除聊天记录会删除原始消息、附件、相关处理队列和阶段日志，但保留已整理的企业与岗位目录。清除前请确认已有备份。</p><div className="storage-backups"><div className="section-title"><strong>本地数据库备份</strong><span>{storage.backups.length} 个</span></div>{storage.backups.length ? storage.backups.map(backup => <div className="storage-backup-row" key={backup.name}><div><strong>{backup.name}</strong><span>{formatBytes(backup.size)} · {formatDate(backup.created_at)}</span></div><button className="secondary danger" onClick={() => removeBackup(backup.name)} disabled={Boolean(busy)}>删除</button></div>) : <div className="empty-inline">暂无本地数据库备份；勾选强制重新获取时会自动创建。</div>}</div></> : <div className="loading-inline">加载本地存储信息…</div>}{message && <p className="setting-help">{message}</p>}</section>
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
  return <><PageHeader eyebrow="管理员" title="系统设置" description="连接 TraceMemo、选择招聘处理器并配置备份与通知。"><button className="primary" onClick={save}>保存设置</button></PageHeader><div className="settings-grid"><section className="detail-card setting-section"><h2>同步与隐私</h2><label>同步间隔（分钟）<input type="number" min="1" value={settings.sync_interval_minutes || 10} onChange={e => set('sync_interval_minutes', Number(e.target.value))} /></label><label>导入天数（距今）<input type="number" min="1" value={settings.import_days ?? settings.initial_import_days ?? 30} onChange={e => set('import_days', Math.max(1, Number(e.target.value) || 1))} /></label><p className="setting-help">每次同步导入距今设定天数内的聊天记录。</p><label className="check"><input type="checkbox" checked={Boolean(settings.redaction_enabled)} onChange={e => set('redaction_enabled', e.target.checked)} />发送云模型前启用脱敏（默认关闭）</label></section><section className="detail-card setting-section"><h2>处理器与并发</h2><label>招聘识别与整理处理器<select value={settings.processing_engine || 'codex'} onChange={e => set('processing_engine', e.target.value)}><option value="codex">本地 Codex（默认）</option><option value="generic">通用模型 API</option></select></label><p className="setting-help">本地 Codex 固定使用 gpt-5.6-luna。切换只影响新任务和重新处理的任务。</p><label>模型并发（1–8）<input type="number" min="1" max="8" value={settings.model_concurrency || 2} onChange={e => set('model_concurrency', Math.max(1, Math.min(8, Number(e.target.value))))} /></label><label>Codex 并发（1–4）<input type="number" min="1" max="4" value={settings.codex_concurrency || 1} onChange={e => set('codex_concurrency', Math.max(1, Math.min(4, Number(e.target.value))))} /></label><label>本地提取并发<input type="number" min="1" max="16" value={settings.extract_concurrency || 4} onChange={e => set('extract_concurrency', Math.max(1, Math.min(16, Number(e.target.value))))} /></label><label>阶段日志保留天数<input type="number" min="1" value={settings.processing_log_retention_days || 30} onChange={e => set('processing_log_retention_days', Math.max(1, Number(e.target.value)))} /></label><button className="secondary" onClick={async () => { try { await api('/admin/models/test', { method: 'POST' }); onSaved('当前处理器连接成功') } catch (e) { alert((e as Error).message) } }}>测试当前处理器</button></section><section className="detail-card setting-section"><h2>TraceMemo</h2><label className="check"><input type="checkbox" checked={Boolean(trace.enabled)} onChange={e => setTrace({ ...trace, enabled: e.target.checked })} />启用自动同步</label><label>API 地址<input value={trace.base_url} onChange={e => setTrace({ ...trace, base_url: e.target.value })} /></label><label>Bearer Token<input type="password" value={traceToken} placeholder="已配置时留空保持不变" onChange={e => setTraceToken(e.target.value)} /></label><p className="setting-help">保存后可在后端接口读取群聊列表并手工选择招聘群；普通同步优先使用已保存的聊天记录，只有强制重新获取才会清理缓存并重新访问 TraceMemo。</p><TraceMemoGroups onSync={onSync} syncing={syncing} /></section><section className="detail-card setting-section"><h2>通用模型 API</h2><label className="check"><input type="checkbox" checked={Boolean(provider.enabled)} onChange={e => set('llm_provider', { ...provider, enabled: e.target.checked })} />启用云模型处理</label><label>API 风格<select value={provider.api_style || 'chat_completions'} onChange={e => set('llm_provider', { ...provider, api_style: e.target.value })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label><label>Base URL<input value={provider.base_url || ''} onChange={e => set('llm_provider', { ...provider, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label><label>模型名称<input value={provider.model || provider.text_model || ''} onChange={e => set('llm_provider', { ...provider, model: e.target.value, text_model: e.target.value })} /></label><label>API Key<input type="password" placeholder={provider.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={e => set('llm_provider', { ...provider, api_key: e.target.value })} /></label></section><section className="detail-card setting-section"><h2>AList WebDAV 备份</h2><label className="check"><input type="checkbox" checked={Boolean(backup.enabled)} onChange={e => set('backup', { ...backup, enabled: e.target.checked })} />启用每日备份</label><label>WebDAV URL<input value={backup.webdav_url || ''} onChange={e => set('backup', { ...backup, webdav_url: e.target.value })} placeholder="https://alist.example.com/dav/" /></label><label>用户名<input value={backup.username || ''} onChange={e => set('backup', { ...backup, username: e.target.value })} /></label><label>远端目录<input value={backup.remote_directory || '/JobPostings'} onChange={e => set('backup', { ...backup, remote_directory: e.target.value })} /></label><label>WebDAV 密码<input type="password" placeholder={backup.webdav_password_configured ? '已配置，留空保持不变' : '输入 WebDAV 密码'} onChange={e => set('backup', { ...backup, webdav_password: e.target.value })} /></label><label>备份密码<input type="password" placeholder={backup.backup_password_configured ? '已配置，留空保持不变' : '输入独立备份密码'} onChange={e => set('backup', { ...backup, backup_password: e.target.value })} /></label><div className="button-row"><button className="secondary" onClick={async () => { try { await api('/admin/backups/test', { method: 'POST' }); onSaved('WebDAV 连接成功') } catch (e) { alert((e as Error).message) } }}>测试 WebDAV</button><button className="secondary" onClick={async () => { try { await api('/admin/backups/run', { method: 'POST' }); onSaved('备份已完成') } catch (e) { alert((e as Error).message) } }}>立即备份</button></div></section><section className="detail-card setting-section"><h2>SMTP 邮件</h2><label className="check"><input type="checkbox" checked={Boolean(smtp.enabled)} onChange={e => set('smtp', { ...smtp, enabled: e.target.checked })} />启用邀请邮件（验证码登录当前关闭）</label><label>SMTP 主机<input value={smtp.host || ''} onChange={e => set('smtp', { ...smtp, host: e.target.value })} placeholder="smtp.example.com" /></label><label>端口<input type="number" value={smtp.port || 587} onChange={e => set('smtp', { ...smtp, port: Number(e.target.value) })} /></label><label>发件人邮箱<input type="email" value={smtp.from_email || ''} onChange={e => set('smtp', { ...smtp, from_email: e.target.value })} /></label><label>用户名<input value={smtp.username || ''} onChange={e => set('smtp', { ...smtp, username: e.target.value })} /></label><label>SMTP 密码<input type="password" placeholder={smtp.password_configured ? '已配置，留空保持不变' : '输入 SMTP 密码'} onChange={e => set('smtp', { ...smtp, password: e.target.value })} /></label><label className="check"><input type="checkbox" checked={smtp.starttls !== false} onChange={e => set('smtp', { ...smtp, starttls: e.target.checked })} />使用 STARTTLS</label></section><section className="detail-card setting-section"><h2>Agent API</h2><label className="check"><input type="checkbox" checked={Boolean(settings.agent_api_enabled)} onChange={e => set('agent_api_enabled', e.target.checked)} />允许创建受限 Agent Token</label><p className="setting-help">Token 默认只允许读取招聘目录和操作自己的求职进度，不能读取原始群消息或系统密钥。</p></section><LocalStoragePanel /></div></>
}

function TraceMemoGroups({ onSync, syncing, compact = false, onSaved }: { onSync?: (force?: boolean) => Promise<void>; syncing?: boolean; compact?: boolean; onSaved?: () => void }) {
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
    try { await api('/admin/source-groups', { method: 'PUT', body: JSON.stringify({ groups }) }); setMessage('群组选择已保存'); onSaved?.(); return true }
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
    if (onSync) await onSync(forceRefetch)
  }
  return <div className={`group-picker${compact ? ' compact' : ''}`}><div className="group-picker-head"><strong>招聘群选择</strong><span>{selectedCount}/20</span></div><div className="group-picker-actions"><button className="secondary" onClick={refresh}>读取群列表</button><button className="secondary" onClick={save} disabled={!groups.length}>保存选择</button>{onSync && <button className="primary" onClick={startSync} disabled={syncing || selectedCount === 0}>{syncing ? '获取中…' : '立即从已选微信群获取'}</button>}</div>{onSync && <><label className="check force-refetch"><input type="checkbox" checked={forceRefetch} onChange={event => setForceRefetch(event.target.checked)} />强制重新获取（清理旧数据并放入待导入列表）</label>{forceRefetch && <p className="setting-warning">执行前会自动创建本地备份；旧目录、队列、审核、证据、聊天记录和 TraceMemo 缓存会被清理，重新获取的消息不会自动导入，请到“选择群聊记录导入”中手动勾选。</p>}</>}{groups.length > 0 && <div className="group-picker-toolbar"><input className="group-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索群名称或群标识" /><span>{visibleGroups.length} 个群聊</span></div>}<div className="group-options">{!groups.length ? <div className="group-empty">正在读取群聊；如果还没有群，请点击“读取群列表”</div> : !visibleGroups.length ? <div className="group-empty">没有匹配的群聊</div> : visibleGroups.map(group => { const source = avatarSource(group.avatar); return <label className={`group-option${group.selected ? ' selected' : ''}`} key={group.id}><input type="checkbox" checked={group.selected} onChange={event => toggle(group.id, event.target.checked)} /><span className="group-avatar-wrap">{source ? <img className="group-avatar-image" src={source} alt={`${group.name}头像`} loading="lazy" /> : <span className="group-avatar" aria-hidden="true">{initial(group.name)}</span>}</span><span className="group-option-copy"><strong>{group.name}</strong><small>{group.external_id}</small></span></label> })}</div><p className="setting-help">TraceMemo 当前群聊接口未提供头像时显示群名首字母；在此保存勾选后，下面的消息列表会按当前群聊重新读取。{message && ` ${message}`}</p></div>
}

function PageHeader({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: React.ReactNode }) { return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div><div className="header-actions">{children}</div></header> }

export default App

createRoot(document.getElementById('root')!).render(<App />)
