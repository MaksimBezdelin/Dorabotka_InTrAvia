# Публичный доступ к InTrAvia через Cloudflare Quick Tunnel.
# Запуск:  powershell -ExecutionPolicy Bypass -File .\host_public.ps1
# Остановка: Ctrl+C в этом окне (сервер Flask останется в свёрнутом окне).

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 1. Сервер на 127.0.0.1:8080 — поднять, если ещё не слушает
$listening = Test-NetConnection 127.0.0.1 -Port 8080 -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $listening) {
    Write-Host '[app] Запускаю serve.py (waitress)...'
    Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'serve.py' `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
    Start-Sleep -Seconds 5
} else {
    Write-Host '[app] Сервер уже слушает 127.0.0.1:8080'
}

# 2. Регистрируем quick tunnel через API вручную.
#    Сам cloudflared иногда не может: пустой POST к api.trycloudflare.com
#    уходит в таймаут. Invoke-RestMethod с JSON-телом проходит нормально.
Write-Host '[tunnel] Запрашиваю hostname у Cloudflare...'
$resp = Invoke-RestMethod -Method Post -Uri 'https://api.trycloudflare.com/tunnel' `
    -ContentType 'application/json' -Body '{}' -TimeoutSec 60

$cred = @{
    AccountTag   = $resp.result.account_tag
    TunnelSecret = $resp.result.secret
    TunnelID     = $resp.result.id
} | ConvertTo-Json -Compress

$credPath = Join-Path $env:TEMP 'intravia-cf-creds.json'
Set-Content -Path $credPath -Value $cred -Encoding ASCII

Write-Host ''
Write-Host "  Публичный URL: https://$($resp.result.hostname)" -ForegroundColor Green
Write-Host ''

# 3. Подключаемся к edge с готовыми credentials
$env:GODEBUG = 'http2client=0'
# --region us: через этот VPN маршрут до Frankfurt рвётся каждые ~35 с,
# US-edge (iad/mia/ord) держит соединение стабильно.
& "$env:USERPROFILE\.local\bin\cloudflared.exe" tunnel --no-autoupdate `
    --edge-ip-version 4 --protocol http2 --region us run `
    --credentials-file $credPath --url http://127.0.0.1:8080 $resp.result.id
