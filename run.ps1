# Starts the API without Docker.
#
# Creates the virtualenv if it is missing, installs requirements, applies the
# Alembic migrations to the SQLite development database, and serves the app with
# reload enabled. Everything runs from backend/, because .env, alembic.ini and
# dev.db are all anchored there.
#
#   .\run.ps1              # http://127.0.0.1:8000
#   .\run.ps1 -Port 9000
#   .\run.ps1 -NoMigrate   # skip alembic upgrade head

param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$NoMigrate,
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

$backend = Join-Path $PSScriptRoot "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"

Push-Location $backend
try {
    if (-not (Test-Path $python)) {
        Write-Host "venv not found, creating backend\.venv" -ForegroundColor Yellow
        py -3 -m venv .venv
        if (-not (Test-Path $python)) { throw "failed to create backend\.venv" }
        & $python -m pip install --upgrade pip
        & $python -m pip install -r requirements.txt
    }
    elseif (-not $NoInstall) {
        & $python -m pip install -q -r requirements.txt
    }

    if (-not (Test-Path ".env")) {
        Write-Host "backend\.env missing, copying from .env.example" -ForegroundColor Yellow
        Copy-Item ".env.example" ".env"
    }

    if (-not $NoMigrate) {
        Write-Host "alembic upgrade head" -ForegroundColor Cyan
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }
    }

    Write-Host ""
    Write-Host "  Swagger UI  http://${BindHost}:${Port}/docs" -ForegroundColor Green
    Write-Host "  ReDoc       http://${BindHost}:${Port}/redoc" -ForegroundColor Green
    Write-Host "  OpenAPI     http://${BindHost}:${Port}/openapi.json" -ForegroundColor Green
    Write-Host "  Health      http://${BindHost}:${Port}/api/v1/health" -ForegroundColor Green
    Write-Host ""

    & $python -m uvicorn app.main:app --host $BindHost --port $Port --reload
}
finally {
    Pop-Location
}
