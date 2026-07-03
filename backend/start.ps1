$backendPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $backendPath

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env из .env.example"
}

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -q
python run.py
