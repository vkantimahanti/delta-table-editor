# Build the React frontend into static/ for Databricks App deployment.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location (Join-Path $Root "frontend")

Write-Host "=== Building frontend for deploy ==="
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Error "npm not found. Install Node.js 18+ and retry."
}
if (Test-Path "package-lock.json") {
    npm ci
} else {
    npm install
}
npm run build

$index = Join-Path $Root "static\index.html"
if (-not (Test-Path $index)) {
    Write-Error "Build did not produce static/index.html"
}

Write-Host "=== Ready for Databricks App deploy ==="
Write-Host "Output: $(Join-Path $Root 'static')"
Write-Host "Next: upload the data-editor/ folder as a Databricks App and bind a SQL warehouse."
