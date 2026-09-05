# JobPostings 当前状态

审阅日期：2026-09-05
代码审阅基线：`b614670`；本次文件解析实现见近期修改记录
文档提交：`51d686d`（本地已提交，待推送到 `origin/main`）
版本：`0.1.0`

## 1. 已完成能力

- 浏览器工作台、管理员/成员权限、密码登录、本机管理员初始化和邀请流程。
- TraceMemo 群聊选择、真实字段归一化、消息缓存、滚动窗口同步、强制重新获取和手工消息导入。
- 文本、公开 URL、公众号页面、PDF/DOCX/XLSX/CSV/TXT/图片导入；浏览器渲染、OCR、二维码、附件和来源证据保留。
- 聊天文件兼容解析：根据文件名、MIME 和文件头识别 PDF/DOC/DOCX/XLS/XLSX；TraceMemo 文件消息支持内嵌资源、同源媒体 URL、多标识回退和历史附件重试。
- 本地 Codex 与通用 OpenAI-compatible 模型两条处理路径，任务 prompt、严格 JSON schema、Token 统计、预算暂停和脱敏。
- 企业、招聘批次、共享招聘信息、岗位、招聘事件、企业关系、公开研究、版本、证据、审核和去重。
- 求职状态、收藏、备注、时间线、企业关注、截止日期通知和 XLSX/CSV/JSON 导出。
- 本地 SQLite 备份、加密快照、AList WebDAV、恢复校验、本地存储管理、队列修复和数据维护脚本。
- Windows 桌面启动器、PyInstaller 打包和可选 Inno Setup 安装器。

## 2. 验证结果

### 已通过

- `frontend\npm run build`：TypeScript 检查通过，Vite 成功生成 `frontend/dist`。
- Python 测试中已有基线测试通过；本次定向回归共 65 个测试通过。
- 本次定向回归：`tests/test_parsers.py tests/test_tracememo.py` 29 个通过；`tests/test_processing.py` 21 个通过（含 1 个新增历史附件修复集成用例）；`tests/test_api.py` 15 个通过。

### 当前验证限制

完整 `pytest -q` 共收集到 100 个测试，其中 53 个在 `tmp_path` 初始化或 pytest 会话清理阶段因当前受管 Windows 环境无法访问临时目录而报 `PermissionError: [WinError 5]`；没有证据表明这些是业务断言失败。使用仓库内 `--basetemp` 后仍在清理阶段遇到同类权限错误。

本次没有验证真实 TraceMemo、外部模型、SMTP、AList、Cloudflare Tunnel 或桌面安装流程；构建通过不等于部署完成。

### 本次历史文件修复实测

开发数据目录 `runtime/data/jobpostings.db` 中检测到 30 条缓存文件消息，其中 2 条已经导入 JobPostings 原始消息，28 条尚未导入，因此不会被本次“附件修复”擅自加入招聘目录。对 2 条已导入且缺少附件的记录已执行可重入修复：两条均从 TraceMemo 本地账号资源目录唯一匹配并恢复，成功写入 1 个 artifact、提取文本并重新排队；第二次重跑未产生重复 artifact 或重复排队。

当前安装版 TraceMemo Reader 的公开媒体接口仍明确偏向图片；对于没有本地原始文件、内嵌二进制或媒体 URL 的历史文件，仍无法仅凭文件名和消息标识还原字节。代码会保留原文件消息和失败原因；待 TraceMemo 提供文件字节或上游恢复后，可通过启动自动重试或 `POST /api/v1/admin/maintenance/tracememo-files` 继续修复。

## 3. 已知问题和风险

1. **模型连接测试的前端反馈不完整。** `frontend/src/main.tsx` 的设置页调用 `/api/v1/admin/models/test` 时，成功只显示短暂提示，失败使用浏览器 `alert`，按钮缺少独立的持久 loading、成功/失败详情和请求期间明确禁用状态。后端路由存在，但用户可能感觉“没有提示”。
2. **TraceMemo 文件媒体受上游能力限制。** 当前连接器已覆盖 `contentData.title`、文件 MIME/文件头、内嵌资源、同源 URL、`serverId`/`id` 回退和本地账号资源目录的唯一文件名匹配；但当前安装版 Reader 的公开媒体端点只保证图片，没有可定位的原始文件时仍无法凭文件名还原字节。首次配置仍应验证 `/health`、`/chatroom`、`/chatlog` 和媒体读取。头像缺失时前端应继续显示群名首字母。
3. **旧版 DOC/XLS 需要可选转换器。** PDF、DOCX、XLSX 可直接解析；旧版 DOC/XLS 需目标机安装 `antiword`、`catdoc` 或 LibreOffice。本次环境未检测到这些转换器，因此只能保存原文件并记录解析错误。
4. **Cloudflare Tunnel 不由本项目管理。** 仓库只约定 origin，不保存 Tunnel token，也不能靠修改项目文件完成凭据轮换。实际轮换前需检查 Windows 服务安装模式，并避免在任何项目产物中记录 token。
5. **开发版和安装版默认数据目录不同。** 开发脚本默认 `runtime/`，安装版默认 `%LOCALAPPDATA%\JobPostings`；切换启动方式后看不到原账号/数据通常是路径不同。
6. **本地 Codex 是默认处理依赖。** 目标机器需要原生 Codex CLI 和有效登录状态；切换通用模型时必须启用 provider 并填写 Base URL、模型名和 API Key。
7. **自动化测试受 Windows 权限影响。** 当前 pytest 无法稳定创建和清理系统临时目录，修复权限或指定可清理的测试目录前，不能宣称“全套 pytest 通过”。
8. **OTP/SMTP 是保留能力。** 邮箱验证码接口仍在后端，但当前登录页不启用；生产使用前需配置 SMTP 并重新确认策略，`JOBPOSTINGS_DEV_SHOW_OTP=true` 不能用于公网。

## 4. 近期修改记录

以下是当前分支最近的实现方向，详细行为以代码和测试为准：

| 提交 | 内容 |
| --- | --- |
| `b614670` | 修复岗位类别恢复和共享招聘详情 |
| `def0614` | 使用专业名称目录辅助岗位分类 |
| `b2ab372` | 将专业与招聘岗位分离 |
| `2ce6343` | 为 Luna 配置最大 reasoning |
| `8647113` | 将 Codex 临时文件放入 runtime |
| `3b3e4e9` | 优先使用 Codex 进行图片 OCR |
| `534f97e` | 增加任务专用 Codex prompt |
| `89cad21` | 重新导入时重置已取消队列消息 |
| 当前变更（待提交） | 增加聊天 PDF/DOC/DOCX/XLS/XLSX 解析、TraceMemo 媒体候选回退和历史附件修复 |

## 5. 下一步建议

按优先级建议：

1. 修复设置页模型连接测试的持久化状态反馈，并补充前端交互验证。
2. 在目标机器完成 TraceMemo 全链路冒烟测试，记录真实字段和媒体行为。
3. 解决测试环境临时目录的 Windows 权限问题，再重新运行完整 Python 测试。
4. 在不泄露凭据的前提下验证 Cloudflare Tunnel、SMTP、AList 和安装版数据目录。
