# JobPostings

面向个人和受邀用户的招聘信息聚合系统。系统运行在 Windows 本机，以浏览器为主要界面，通过 TraceMemo 读取已选择的微信群消息，使用可配置的 OpenAI-compatible 模型进行招聘识别和结构化抽取，并保留来源、岗位版本、企业介绍和个人求职进度。

## 运行

正式基线使用 Python 3.12。开发环境可执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,documents]"
$env:PYTHONPATH = "backend"
\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 17879
```

浏览器打开 `http://127.0.0.1:17879`。

系统使用本地 SQLite，不需要单独安装 MySQL、PostgreSQL 等数据库服务。导出文件默认写入 `%USERPROFILE%\Downloads\JobPostings`，可用 `JOBPOSTINGS_DOWNLOAD_DIR` 覆盖；数据库、密钥和附件仍保存在 `%LOCALAPPDATA%\JobPostings`。

## 外部服务

- TraceMemo 默认地址：`http://127.0.0.1:6131/api/v1`。
- 模型供应商使用 OpenAI-compatible Chat Completions 或 Responses API。
- SMTP 用于邀请和邮箱验证码。
- AList 通过 WebDAV 提供备份目标。

首次启动只能从本机创建管理员。开发环境未配置 SMTP 时，可使用 `JOBPOSTINGS_DEV_SHOW_OTP=true` 在 API 响应中查看验证码；生产环境必须关闭该选项并配置 SMTP。

生成 Windows 可执行文件：

```powershell
.\scripts\build.ps1
```

该脚本生成 `build\jobpostings-server` 和 `build\JobPostings.exe`。若需要 `Setup.exe`，需另行安装 Inno Setup 后编译 `installer\jobpostings.iss`。
