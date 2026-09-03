$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  python -m venv .venv
}
& .venv\Scripts\python.exe -m pip install -e ".[dev,documents,desktop]"
Set-Location frontend
npm ci
npm run build
Set-Location $projectRoot

& .venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath build --workpath build\work --specpath build --name jobpostings-server --onedir --paths backend --add-data "frontend\dist;frontend\dist" --add-data "backend\app\static;app\static" backend\app\main.py
& .venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath build --workpath build\launcher-work --specpath build --name JobPostings --onefile --paths backend desktop\launcher.py

Write-Host "Backend: $projectRoot\dist\jobpostings-server"
Write-Host "Launcher: $projectRoot\dist\JobPostings.exe"
