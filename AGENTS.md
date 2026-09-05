# JobPostings Codex 指南

## 项目是什么

JobPostings 是 Windows 本地优先的招聘信息聚合和求职进度管理系统。它通过 TraceMemo 或手工导入获取微信群消息、公开 URL 和文件，使用本地 Codex 或 OpenAI-compatible 模型识别招聘内容，并将企业、招聘批次、岗位、招聘事件和来源证据保存到本地 SQLite。

源码根目录是 `D:\Projects\JobPostings`。先阅读：

- [`README.md`](README.md)：项目概览、安装和运行。
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：目录、模块、数据流和 API。
- [`docs/STATUS.md`](docs/STATUS.md)：当前状态、验证结果和已知问题。
- [`docs/SETUP.md`](docs/SETUP.md)：部署、账号、TraceMemo、备份和打包细节。
- [`docs/MODEL_AND_PRIVACY.md`](docs/MODEL_AND_PRIVACY.md)：模型输入、脱敏和隐私边界。

## 关键约束

- 保留原始消息、原始 URL、发送/观察时间、附件和证据关联；模型推测不能覆盖来源事实。
- 数据库是本地 SQLite。修改 schema 时必须在 `backend/app/db.py` 中提供旧库迁移，并保持事务和 JSON 字段约定。
- TraceMemo 群聊要使用真实接口字段；通常 `m_nsUsrName` 是 `external_id`、`m_nsNickName` 是显示名。ID 必须稳定、非空、互不重复，前端 React key 也必须保持唯一。
- 处理队列的状态、阶段日志、重试、取消和审核载荷必须保持一致。异步 UI 操作需要持久 loading、成功、失败和错误详情，并在请求期间阻止重复提交。
- `local_codex` 与 `generic_llm` 是两条不同处理路径；`LLM provider is disabled` 表示配置状态，不等同于网络故障。
- API Key、TraceMemo Token、SMTP/WebDAV 密码和备份密码不得写入 Git、文档、URL、截图或普通日志。不要把 `runtime/`、`build/`、`dist/`、`.venv/` 或 `.pytest*` 当作源码。
- Cloudflare Tunnel 不由项目管理；项目只约定 origin 为 `http://127.0.0.1:17879`，不要在仓库中处理或记录 Tunnel token。

## 重要文件

| 文件 | 作用 |
| --- | --- |
| `backend/app/main.py` | FastAPI 生命周期、全部 API 路由、同步和后台循环 |
| `backend/app/processing.py` | 入库、解析/OCR、处理队列、重试和审核 |
| `backend/app/parsers.py` | URL/文件/图片解析、时间归一化和安全校验 |
| `backend/app/catalog.py` | 企业/岗位/事件/证据写入、归并和去重 |
| `backend/app/model_provider.py` | 通用模型 API、schema、额度和脱敏 |
| `backend/app/codex_agent.py` | 隔离只读的本地 Codex CLI 调用 |
| `backend/app/tracememo.py` | TraceMemo API 和群聊字段归一化 |
| `backend/app/db.py` | SQLite schema、索引和兼容性迁移 |
| `frontend/src/main.tsx` | React 页面、API 调用、SSE 刷新和异步 UI |

## 常用命令

```powershell
# 开发后端
.\.venv\Scripts\python -m pip install -e ".[dev,documents]"
.\scripts\start-dev.ps1

# 前端
Set-Location frontend
npm ci
npm run dev
npm run typecheck
npm run build

# 后端测试
Set-Location D:\Projects\JobPostings
.\.venv\Scripts\python.exe -m pytest -q

# Windows 打包
.\scripts\build.ps1
```

默认后端地址是 `http://127.0.0.1:17879`，默认 Python 范围是 `>=3.12,<3.14`。修改后运行相关最小测试和前端构建；若被 Windows 权限、外部服务或桌面环境阻塞，要区分已通过项与环境限制。项目文件修改完成后提交并推送到现有 `origin`。
