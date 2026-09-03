# JobPostings 配置说明

## 本地启动

安装包启动后会自动监听 `127.0.0.1:17879` 并打开浏览器。开发环境可以使用：

```powershell
Set-Location D:\Projects\JobPostings
.\scripts\start-dev.ps1
```

运行数据默认位于 `%LOCALAPPDATA%\JobPostings`。不要把数据库、`vault.dat`、日志或附件复制到 Git 仓库。

本项目使用本地 SQLite 文件，不需要独立数据库服务。导出默认写入 `%USERPROFILE%\Downloads\JobPostings`；如需更换位置，设置 `JOBPOSTINGS_DOWNLOAD_DIR`。

## 首次使用

首次启动只允许从本机创建管理员。之后普通用户只能通过管理员邀请的邮箱登录。开发环境未配置 SMTP 时，可以通过 `JOBPOSTINGS_DEV_SHOW_OTP=true` 查看调试验证码；公网部署必须关闭该变量。

## TraceMemo

在“系统设置”填入 TraceMemo API 地址和 Token。默认地址：

```text
http://127.0.0.1:6131/api/v1
```

先执行连接测试，再从群聊列表手工选择招聘群。首次导入默认为 30 天；自动同步默认为 10 分钟。

## 模型

配置 OpenAI-compatible Chat Completions 或 Responses API：

- `base_url`
- 模型名
- API Key
- API 风格

API Key 会使用 Windows DPAPI 保护，不会以明文写入设置展示。脱敏开关默认关闭，可以在设置中启用。

## Cloudflare Tunnel

Tunnel 的 origin 必须指向：

```text
http://127.0.0.1:17879
```

应用仍然执行邮箱邀请和验证码登录，Cloudflare Access 只能作为额外保护层。

## AList WebDAV

在 AList 中配置百度网盘后，将 AList 的 WebDAV URL、用户名、密码和远端目录填入设置。备份密码由用户单独设置；恢复到新电脑时需要 WebDAV 凭据和备份密码。

可以先执行恢复校验（不会写入目标目录）：

```powershell
$env:PYTHONPATH = "D:\Projects\JobPostings\backend"
.\.venv\Scripts\python.exe scripts\restore_backup.py `
  --webdav-url "https://alist.example.com/dav/" `
  --username "user" `
  --webdav-password "webdav-password" `
  --remote-path "JobPostings/20260903T020000Z-12345678.jpe" `
  --backup-password "backup-password" `
  --output "D:\Restore\JobPostings"
```

确认输出无误后再增加 `--confirm`。恢复输出目录应是专用目录，不要直接指向当前运行目录。

## Windows 打包

在已安装 Node.js 和 Python 3.12/3.13 的环境运行：

```powershell
.\scripts\build.ps1
```

脚本会生成 `build\jobpostings-server` 和 `build\JobPostings.exe`。`installer\jobpostings.iss` 是可选的 Inno Setup 安装器配置；本地没有 Inno Setup 时仍可直接分发上述目录和启动器。
