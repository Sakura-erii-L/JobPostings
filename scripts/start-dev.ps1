$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$env:PYTHONPATH = Join-Path $projectRoot "backend"
if (-not $env:JOBPOSTINGS_DATA_DIR) { $env:JOBPOSTINGS_DATA_DIR = Join-Path $projectRoot "runtime" }
& (Join-Path $projectRoot ".venv\Scripts\python.exe") -m uvicorn app.main:app --host 127.0.0.1 --port 17879

