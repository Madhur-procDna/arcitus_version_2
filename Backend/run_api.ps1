# Start HTTP API for the Next.js frontend (from repo root: .\Backend\run_api.ps1)
# Port must match Frontendd/.env.local SDA_BACKEND_URL (see .env.local.example).
Set-Location $PSScriptRoot\src
Write-Host "API: http://127.0.0.1:8001  (health: /health, query: POST /query)"
uvicorn api_server:app --reload --host 127.0.0.1 --port 8001
