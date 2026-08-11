$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Tao virtualenv..."
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Da tao .env — dien OPENROUTER_KEY vao roi chay lai." -ForegroundColor Yellow
    exit 1
}

$env:PYTHONUTF8 = "1"
Start-Process "http://localhost:7799"
& ".venv\Scripts\python.exe" server.py
