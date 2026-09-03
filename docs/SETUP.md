# JobPostings 配置说明

## 本地启动

安装包启动后会自动监听 `127.0.0.1:17879` 并打开浏览器。开发环境可以使用：

```powershell
Set-Location D:\Projects\JobPostings
.\scripts\start-dev.ps1
```

运行数据默认位于 `%LOCALAPPDATA%\JobPostings`。不要把数据库、`vault.dat`、日志或附件复制到 Git 仓库。

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

