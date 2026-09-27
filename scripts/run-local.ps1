# 单机版（Windows，不使用 Docker）：Python ≥ 3.10、Node ≥ 20
#   powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path "backend\.venv")) {
  python -m venv backend\.venv
  backend\.venv\Scripts\pip install -q -e backend
}
if (-not (Test-Path "frontend\dist")) {
  Push-Location frontend; npm ci --no-audit --no-fund; npm run build; Pop-Location
}
$env:REVIEWDESK_PROFILE = "local"
if (-not $env:REVIEWDESK_DATA_DIR) { $env:REVIEWDESK_DATA_DIR = Join-Path (Get-Location) "data" }
$port = if ($env:PORT) { $env:PORT } else { "8000" }
backend\.venv\Scripts\reviewdesk serve --host 127.0.0.1 --port $port
