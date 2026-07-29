# Quick Cloudflare tunnel to local Chunking Service (:5000)
# Uses HTTP/2 — more stable than QUIC/UDP on many home networks.
$ErrorActionPreference = "Stop"

$cloudflared = Join-Path $PSScriptRoot "..\.tools\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    Write-Error "cloudflared not found at $cloudflared"
}

Write-Host "Starting tunnel (http2) -> http://localhost:5000"
Write-Host "После появления URL обновите backend/.env:"
Write-Host '  PUBLIC_BASE_URL=https://xxxx.trycloudflare.com'
Write-Host "и перезапустите backend (нужно для WebSocket)."
Write-Host ""

& $cloudflared tunnel --url http://localhost:5000 --protocol http2
