# TraceMemo 连接器约定

JobPostings 将 TraceMemo 作为外部消息源，不包含 TraceMemo 代码。连接器使用本地 HTTP API，保存群聊选择和每个群的同步游标。

同步行为：

1. 每次导入按选定群聊和“导入天数”限制为距今范围，默认最近 30 天。
2. 增量同步默认每 10 分钟执行。
3. 同步游标仅用于记录同步状态，实际导入范围始终以“导入天数”和消息自身的 `datetime` 为准。
4. 外部消息 ID 优先去重，没有稳定 ID 时使用内容指纹。
5. 消息进入 SQLite 后才进入解析和模型队列。
6. TraceMemo 不可用时，复制粘贴、URL 和文件导入仍然可用。

文件消息兼容：

- 微信文件可能以 `type=share` 返回，文件名通常位于 `contentData.title`；系统会结合文件名、MIME 和文件头识别 PDF、DOC、DOCX、XLS、XLSX 等格式。
- 媒体读取优先使用消息内嵌二进制/本地路径，其次使用显式媒体 ID、同源媒体 URL；文件消息还会尝试 `serverId`、`messageId`、`id` 等候选标识，图片仍优先使用官方消息 `id`。
- PDF/DOCX/XLSX 可直接解析；旧版 DOC/XLS 需要目标机安装 `antiword`、`catdoc` 或 LibreOffice 之一，否则会保留原文件和明确解析错误。
- 启动时会自动调用可重入历史附件修复；也可调用 `/api/v1/admin/maintenance/tracememo-files` 手动重试。修复前会创建数据库备份，未成功下载的记录不会被标记为已修复。
- 当前已安装 TraceMemo Reader 的公开端点只明确保证图片媒体读取；如果聊天文件消息没有内嵌资源、媒体 URL 或上游文件下载端点，JobPostings 无法凭文件名还原原始字节，只会保留文件消息和失败原因。

当前微信版本与 TraceMemo 文档测试版本不完全一致，首次配置必须执行真实健康、群聊、消息和媒体冒烟测试。
