param(
  [string]$OutputDir = "$PSScriptRoot\..\..\data\backups"
)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $OutputDir "comparator-$stamp.sql"
docker exec comparator-postgres pg_dump -U comparator comparator | Set-Content -Path $out -Encoding utf8
Write-Host "Wrote $out"
Write-Host "Restore: Get-Content $out | docker exec -i comparator-postgres psql -U comparator comparator"
