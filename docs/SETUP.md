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

首次启动只允许从本机创建管理员，初始化时需要设置账号密码。之后普通用户使用管理员邀请的邮箱和初始密码登录。邮箱验证码接口仍保留，但默认关闭，当前登录页不会启用该方式。开发环境未配置 SMTP 时，可以通过 `JOBPOSTINGS_DEV_SHOW_OTP=true` 查看调试验证码；公网部署必须关闭该变量。

## 登录与管理台

使用 `scripts\start-dev.ps1` 启动后，浏览器访问 `http://127.0.0.1:17879`。如果数据库已经初始化，显示登录页是正常现象：输入管理员邮箱和密码即可进入工作台。已有旧账号如果尚未设置密码，仍保持登录时可打开“账户安全”设置；如果会话已经失效，请在运行服务的本机打开该地址，登录页会显示一次性的管理员初始密码设置表单。首次初始化时没有创建过管理员的环境，才会显示“创建本机管理员”。

当前版本没有独立的 `/admin` 地址。管理员登录后，左侧导航会显示“管理台”“系统设置”和“待审核”：

- “管理台”用于邀请受邀用户或其他管理员，并查看邀请记录及有效期。
- “系统设置”用于配置 TraceMemo、模型、脱敏、SMTP 和 AList WebDAV；邮箱验证码登录默认关闭。
- “账户安全”用于为当前账号设置或修改登录密码。
- “待审核”用于处理低置信度识别、字段冲突和失败任务。

在“管理台”创建邀请时设置初始密码，受邀用户使用被邀请的邮箱和初始密码登录。邀请有效期为 72 小时；邀请通知仍可通过 SMTP 发送，但密码应通过安全方式单独告知。个人使用时可以只保留本机管理员，不创建其他用户。

开发启动脚本在未设置 `JOBPOSTINGS_DATA_DIR` 时使用项目内的 `runtime` 目录；安装版默认使用 `%LOCALAPPDATA%\JobPostings`。因此两种启动方式的账号和数据默认不是同一份。

## TraceMemo

在“系统设置”填入 TraceMemo API 地址和 Token。默认地址：

```text
http://127.0.0.1:6131/api/v1
```

先执行连接测试，再从群聊列表手工选择招聘群。导入天数默认为 30 天，每次同步都获取距今范围内的聊天记录；自动同步默认为 10 分钟。

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

应用使用账号密码登录；邮箱邀请通知和验证码接口仍保留，但验证码登录默认关闭。Cloudflare Access 只能作为额外保护层。

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
