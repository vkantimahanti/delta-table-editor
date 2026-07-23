# Start FastAPI backend (kills stale listeners on the target port first).
param(
    [int]$Port = 8001
)

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "Stopping processes listening on port $Port ..."
Get-NetTCPConnection -LocalPort $Port -State Listen | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force
}

# Also try default port 8000 if using 8001 (stale zombie often blocks 8000)
if ($Port -eq 8001) {
    Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object {
        Write-Host "Warning: port 8000 still held by PID $($_.OwningProcess) — use VITE_BACKEND_PORT=8001 in frontend/.env.development"
    }
}

Start-Sleep -Seconds 2
Write-Host "Starting uvicorn on http://127.0.0.1:$Port ..."
uvicorn backend.main:app --host 127.0.0.1 --port $Port --reload
