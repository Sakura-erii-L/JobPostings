# JobPostings 架构说明

本文说明项目的目录、模块边界、数据流和接口入口。当前源码仓库为 `D:\Projects\JobPostings`。

## 1. 总体架构

```text
TraceMemo / 手工文本 / 公开 URL / 文件
                 │
                 ▼
        FastAPI 后端（backend/app/main.py）
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
  SQLite 数据库  文件 Blob  本地处理队列
       │                   │
       │          解析/OCR/浏览器渲染
       │                   │
       └──────────┬────────┘
                  ▼
       Codex 或 OpenAI-compatible 模型
                  │
                  ▼
   企业 / 岗位 / 事件 / 证据 / 审核 / 求职状态
                  │
                  ▼
       React + TypeScript 浏览器工作台
```

技术栈：

- 后端：Python 3.12+、FastAPI、Uvicorn、Pydantic。
- 前端：React 19、TypeScript、Vite。
- 存储：本地 SQLite；附件保存到本地 Blob 目录。
- 解析：BeautifulSoup、Playwright、`openpyxl`，PDF/DOCX 解析，以及通过可选 `antiword`/`catdoc`/LibreOffice 转换的 DOC/XLS 兼容路径、RapidOCR。
- 模型：本地 Codex CLI，或 Chat Completions / Responses API。
- 桌面发布：PyInstaller 服务目录、托盘启动器和可选 Inno Setup 安装器。

FastAPI 同时提供 API 和生产前端静态文件：存在 `frontend/dist` 时使用编译前端，否则回退到 `backend/app/static/index.html`。开发时 Vite 默认监听 `5173`，并把 `/api` 代理到后端 `17879`。

## 2. 目录结构

```text
JobPostings/
├─ AGENTS.md                         # Codex 规则和快速入口
├─ README.md                         # 用户向概览、安装和运行
├─ pyproject.toml                    # Python 包、依赖和 pytest 配置
├─ .env.example                      # 环境变量示例
├─ backend/
│  ├─ run_server.py                  # 后端/PyInstaller 启动入口
│  └─ app/
│     ├─ main.py                     # API、生命周期、同步、后台循环
│     ├─ db.py                       # SQLite schema、索引和迁移
│     ├─ config.py                   # 环境变量和存储路径
│     ├─ processing.py               # 入库、队列、解析/OCR、审核
│     ├─ parsers.py                  # 网页/文件/图片/时间/消息解析
│     ├─ browser.py                  # Playwright 渲染和懒加载图片
│     ├─ catalog.py                  # 目录写入、归并、去重
│     ├─ model_provider.py           # 通用模型、额度、脱敏、schema
│     ├─ codex_agent.py              # 本地 Codex 隔离调用
│     ├─ company_research.py         # 企业公开资料和风险检索
│     ├─ tracememo.py                # TraceMemo HTTP 客户端
│     ├─ tracememo_cache.py          # TraceMemo 消息缓存
│     ├─ auth.py / security.py       # 登录、scope、DPAPI、密码和 Token
│     ├─ backups.py                  # 加密快照、AList WebDAV、恢复校验
│     ├─ local_storage.py            # 本地存储统计和清理
│     ├─ maintenance.py              # 修复、迁移、强制重置
│     ├─ exports.py / events.py      # 导出和 SSE 事件
│     ├─ prompt_templates.py         # Markdown prompt 注册/渲染
│     ├─ prompts/                    # 各处理任务 prompt
│     ├─ data/major_names.csv        # 专业名称目录
│     └─ static/index.html           # 未构建前端的回退页
├─ frontend/
│  ├─ src/main.tsx                   # 单文件 React 应用
│  ├─ src/styles.css                 # 主样式
│  ├─ src/queue.css                  # 队列样式
│  ├─ package.json                   # 前端脚本和依赖
│  └─ vite.config.ts                 # Vite 和后端代理
├─ desktop/launcher.py               # 后端、健康检查、浏览器和托盘
├─ scripts/                          # 开发、构建、队列修复、恢复
├─ docs/                             # 配置、隐私、TraceMemo、架构、状态
├─ tests/                            # 后端单元/API/集成测试
└─ installer/jobpostings.iss         # 可选 Inno Setup 配置
```

`runtime/`、`build/`、前端 `dist/`、`.venv/` 和 `.pytest*` 是运行/构建产物，不是源码；数据库、密钥、日志和附件不应进入 Git。

## 3. 核心数据流

### 3.1 来源到目录

1. 管理员配置 TraceMemo 并选择最多 20 个招聘群，或通过文本、公开 URL、PDF/DOC/DOCX/XLS/XLSX/CSV/TXT/图片手工导入。
2. `processing.ingest_message()` 以外部消息 ID 或内容指纹去重，把原始消息写入 `raw_messages`，附件写入 `artifacts`，并创建 `processing_jobs`。
3. TraceMemo 文件消息即使外层类型为 `share`，也会根据 `contentData` 中的文件名、MIME 或文件头识别；媒体按内嵌资源、显式媒体 ID/URL、文件 `serverId` 等候选顺序读取，成功后按文件名和内容解析。
4. 队列先由本地解析器提取正文、链接、图片、文件、时间和元数据。公众号页面遇到环境验证或正文过短时由 Playwright 渲染，并触发图片懒加载。
5. 图片默认由本地 Codex OCR；Codex 无法使用或无可读文本时回退到可选 RapidOCR，同时解析二维码。
6. `model_provider.py` 或 `codex_agent.py` 调用模型，以 Markdown prompt 和严格 JSON schema 输出招聘分类与结构化字段。
7. `catalog.apply_model_item()` 写入企业、招聘批次、共享详情、岗位、事件和证据，并执行归并、去重和版本记录。
8. 低置信度、字段冲突、模型失败或企业归并异常生成 `review_items`，管理员可查看原始消息、处理任务和阶段日志。

### 3.2 后台循环

`main.py` 的 FastAPI `lifespan` 会初始化数据库并启动：

- `worker_loop`：领取队列任务，按处理器并发设置执行分类/归并/研究。
- `retention_loop`：清理过期且已不再需要的非招聘原始消息及旧日志。
- `auto_sync_loop`：按设置的时间间隔增量同步 TraceMemo。
- `notification_loop`：为收藏岗位生成截止日期通知。
- `auto_backup_loop`：按配置时间创建 AList WebDAV 备份。

前端通过 `/api/v1/events` 使用 SSE 监听 `job.created`、`job.updated`、`company.created`、`company.updated`、`sync.completed` 和 `processing.updated`，然后刷新目录数据。

## 4. 关键模块

| 模块 | 职责 | 关键边界 |
| --- | --- | --- |
| `main.py` | API、应用生命周期、TraceMemo 同步 | 权限依赖、状态码、后台任务清理 |
| `processing.py` | 原始消息入库、队列、解析/OCR、重试、审核 | 幂等、取消、阶段状态和日志必须一致 |
| `parsers.py` | URL 安全校验、内容提取、时间和消息类型 | SSRF 防护、原始时间/URL、文件限制 |
| `catalog.py` | 结构化结果持久化、企业/岗位/事件归并去重 | 证据关联、岗位身份、专业与岗位类别边界 |
| `model_provider.py` | OpenAI-compatible API、额度、脱敏、JSON | provider 启用状态、Token 统计、输出结构 |
| `codex_agent.py` | 本地 Codex CLI 隔离调用 | `CODEX_CLI_PATH`、只读沙箱、临时文件清理 |
| `tracememo.py` / cache | TraceMemo API、群聊归一化、缓存和媒体候选 | 真实字段映射、Bearer Token、文件资源只访问配置的同源服务 |
| `company_research.py` | 企业公开资料、标签和风险来源 | 公开来源必须可追溯，不能覆盖招聘来源 |
| `db.py` | schema、索引、旧库迁移 | 新字段必须兼容旧数据库 |
| `auth.py` / `security.py` | 本机 bootstrap、登录、邀请、scope、秘密 | HttpOnly Cookie、DPAPI、禁止泄密 |
| `backups.py` / `maintenance.py` | 备份、恢复、修复、强制重置和历史附件修复 | 破坏性操作先备份，恢复先校验；附件失败不标记为已完成 |
| `frontend/src/main.tsx` | 页面、导航、API、SSE、异步交互 | 管理员可见性、持久反馈、避免重复提交 |

## 5. SQLite 数据域

所有表由 `backend/app/db.py` 初始化，并通过 `schema_meta` 和兼容性迁移维护。

| 领域 | 主要表 |
| --- | --- |
| 账号与权限 | `users`、`invitations`、`otp_challenges`、`sessions`、`api_tokens` |
| 连接器与来源 | `connectors`、`source_groups`、`sync_cursors`、`ingest_runs`、`raw_messages`、`artifacts` |
| TraceMemo 缓存 | `tracememo_message_cache`、`tracememo_cache_state` |
| 处理与审核 | `processing_jobs`、`queue_control`、`processing_logs`、`review_items`、`llm_calls` |
| 企业与招聘 | `companies`、`company_versions`、`company_relations`、`company_claims`、`company_public_findings`、`company_merge_rules`、`recruitment_batches`、`recruitment_shared_details`、`jobs`、`job_versions`、`evidences`、`recruitment_events`、`recruitment_event_versions`、`recruitment_event_evidences` |
| 个人求职 | `user_job_states`、`application_events`、`user_notes`、`user_tags`、`job_tag_links`、`user_follows`、`notifications` |
| 系统运维 | `system_settings`、`backups` |

默认数据根目录为 `%LOCALAPPDATA%\JobPostings`，包含 `data/jobpostings.db`、`data/blobs/`、`secrets/vault.dat`、`logs/` 和 `exports/`。开发脚本未设置 `JOBPOSTINGS_DATA_DIR` 时使用项目内 `runtime/`，因此开发运行和安装版默认不是同一份数据。

## 6. API 和前端入口

`main.py` 目前集中定义路由：

| 路径 | 用途 |
| --- | --- |
| `/health` | 健康检查；不在 `/api/v1` 前缀下 |
| `/api/v1/bootstrap`、`/api/v1/auth/*` | 初始化、密码登录、OTP、密码设置、会话 |
| `/api/v1/admin/invitations`、`/api/v1/admin/settings` | 邀请、系统设置、模型测试 |
| `/api/v1/admin/connectors/tracememo/*`、`/admin/source-groups`、`/admin/sync` | TraceMemo 配置、选群、同步、消息导入 |
| `/api/v1/admin/maintenance/tracememo-files` | 重试缺失/不可读的 TraceMemo 文件附件并重新排队 |
| `/api/v1/admin/processing-queue/*` | 队列运行/暂停、取消、重试、文本和日志 |
| `/api/v1/imports/*` | 文本、URL、文件导入 |
| `/api/v1/companies/*`、`/jobs`、`/recruitment-events`、`/evidences/*`、`/artifacts/*` | 招聘目录和证据 |
| `/api/v1/me/*`、`/notifications/*` | 求职状态、收藏、备注、时间线、关注、通知 |
| `/api/v1/admin/review-items/*` | 审核 |
| `/api/v1/exports/*` | XLSX/CSV/JSON 导出和下载 |
| `/api/v1/admin/backups/*`、`/admin/local-storage/*` | 远端备份、恢复校验、本地清理 |
| `/api/v1/api-tokens`、`/api/v1/events` | Agent Token 和 SSE |

前端 `page` 状态对应：企业与岗位、招聘时间轴、求职进度、导入信息、管理台、处理队列、系统设置、账户安全和待审核。管理员页面没有独立 `/admin` 地址；前端可见性不是安全边界，后端依赖才是最终权限控制。

## 7. 外部依赖边界

- TraceMemo 默认 `http://127.0.0.1:6131/api/v1`，健康检查、群聊、消息和媒体接口必须在目标环境做真实冒烟测试。
- 本地 Codex 需要原生 `codex.exe` 和有效登录状态；可用 `CODEX_CLI_PATH` 指定路径。
- 通用模型需要在管理台启用 provider，并配置 Base URL、模型名和 API Key；支持 Chat Completions 或 Responses API。
- SMTP 只用于邀请/验证码能力，OTP 登录当前默认关闭。
- AList 通过 WebDAV 作为可选备份目标；备份密码独立于 WebDAV 密码。
- Cloudflare Tunnel 只需把 origin 指向 `http://127.0.0.1:17879`，凭据轮换在项目外完成。
