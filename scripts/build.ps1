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

$frontendDist = Join-Path $projectRoot "frontend\dist"
$staticDir = Join-Path $projectRoot "backend\app\static"
& .venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath build --workpath build\work --specpath build --name jobpostings-server --onedir --paths backend --add-data "$frontendDist;frontend\dist" --add-data "$staticDir;app\static" backend\run_server.py
if ($LASTEXITCODE -ne 0) { throw "Backend PyInstaller build failed with exit code $LASTEXITCODE" }
& .venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath build --workpath build\launcher-work --specpath build --name JobPostings --onefile --paths backend desktop\launcher.py
if ($LASTEXITCODE -ne 0) { throw "Launcher PyInstaller build failed with exit code $LASTEXITCODE" }

Write-Host "Backend: $projectRoot\build\jobpostings-server"
Write-Host "Launcher: $projectRoot\build\JobPostings.exe"
