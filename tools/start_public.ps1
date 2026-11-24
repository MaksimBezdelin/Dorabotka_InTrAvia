# Публичный доступ к InTrAvia: waitress + Cloudflare Quick Tunnel.
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File tools\start_public.ps1
#   powershell -ExecutionPolicy Bypass -File tools\start_public.ps1 -Port 5000
#
# Остановка: Ctrl+C в этом окне либо tools\stop_public.ps1 из другого окна.
# PID-файл: logs\public_pids.txt (в git не коммитится).

[CmdletBinding()]
param(
    [int]$Port = 5000
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$LogsDir = Join-Path $Root 'logs'
$PidFile = Join-Path $LogsDir 'public_pids.txt'
$ServerLog = Join-Path $LogsDir 'server.log'
$ServerErr = Join-Path $LogsDir 'server.err.log'
$CloudflaredLog = Join-Path $LogsDir 'cloudflared.log'
$CloudflaredOut = Join-Path $LogsDir 'cloudflared.out.log'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$ServePy = Join-Path $Root 'serve.py'

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Test-TcpPort {
    param([int]$PortNumber)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $PortNumber, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

function Get-LogTail {
    param([string]$Path, [int]$Count = 12)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        return ((Get-Content -LiteralPath $Path -Tail $Count -ErrorAction Stop) -join [Environment]::NewLine)
    } catch {
        return ''
    }
}

function Get-TunnelUrl {
    param([string[]]$Paths)
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $text = $null
        try {
            $text = [System.IO.File]::ReadAllText($path)
        } catch {
            try { $text = Get-Content -LiteralPath $path -Raw -ErrorAction Stop } catch { $text = $null }
        }
        if ($text) {
            $match = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($match.Success) { return $match.Value }
        }
    }
    return $null
}

function Write-PidFile {
    param([int]$ServerPid, $TunnelPid)
    $lines = @('server=' + $ServerPid)
    if ($null -ne $TunnelPid) { $lines += ('cloudflared=' + $TunnelPid) }
    Set-Content -LiteralPath $PidFile -Value $lines -Encoding ASCII
}

function Stop-ChildProcess {
    param($Process, [string]$Label)
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            Write-Host ("[stop] Останавливаю $Label (PID " + $Process.Id + ')...') -ForegroundColor DarkGray
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            [void]$Process.WaitForExit(5000)
        }
    } catch { }
    try {
        if (-not $Process.HasExited) {
            & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
        }
    } catch { }
}

# --- 1. Проверки окружения -------------------------------------------------
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host ("[ОШИБКА] Не найден Python: " + $Python) -ForegroundColor Red
    Write-Host 'Создайте окружение: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt' -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path -LiteralPath $ServePy)) {
    Write-Host ("[ОШИБКА] Не найден serve.py: " + $ServePy) -ForegroundColor Red
    exit 1
}
if (Test-TcpPort -PortNumber $Port) {
    Write-Host ("[ОШИБКА] Порт 127.0.0.1:$Port уже занят.") -ForegroundColor Red
    Write-Host 'Остановите старый запуск: powershell -ExecutionPolicy Bypass -File tools\stop_public.ps1' -ForegroundColor Yellow
    Write-Host 'Либо укажите другой порт: -Port 5001' -ForegroundColor Yellow
    exit 1
}

$Cloudflared = $null
$cloudflaredCandidates = @(
    'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    (Join-Path $env:ProgramFiles 'cloudflared\cloudflared.exe')
)
foreach ($candidate in $cloudflaredCandidates) {
    if (Test-Path -LiteralPath $candidate) { $Cloudflared = $candidate; break }
}
if (-not $Cloudflared) {
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) { $Cloudflared = $command.Source }
}

$server = $null
$tunnel = $null
$exitCode = 0

try {
    # --- 2. waitress на loopback ------------------------------------------
    Write-Host ("[app] Запускаю serve.py на 127.0.0.1:$Port ...") -ForegroundColor Cyan
    $previousPort = $env:INTRAVIA_PORT
    $env:INTRAVIA_PORT = [string]$Port
    try {
        $server = Start-Process -FilePath $Python -ArgumentList @($ServePy) `
            -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $ServerLog -RedirectStandardError $ServerErr
    } finally {
        if ($null -eq $previousPort) {
            Remove-Item Env:INTRAVIA_PORT -ErrorAction SilentlyContinue
        } else {
            $env:INTRAVIA_PORT = $previousPort
        }
    }

    $localUrl = "http://127.0.0.1:$Port/login.html"
    $deadline = (Get-Date).AddSeconds(30)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($server.HasExited) { break }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $localUrl -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                $ready = $true
                break
            }
        } catch { }
        Start-Sleep -Milliseconds 700
    }

    if (-not $ready) {
        $tail = Get-LogTail -Path $ServerErr -Count 15
        if (-not $tail) { $tail = Get-LogTail -Path $ServerLog -Count 15 }
        throw ("Сервер не ответил на $localUrl за 30 с." + [Environment]::NewLine + $tail)
    }
    Write-Host ("[app] Сервер готов: http://127.0.0.1:$Port") -ForegroundColor Green

    # --- 3. cloudflared или работа только локально -------------------------
    if (-not $Cloudflared) {
        Write-Host '[!] cloudflared не найден — публичный туннель не запускаю.' -ForegroundColor Yellow
        Write-Host '    Установите cloudflared или запустите только локальный стенд.' -ForegroundColor Yellow
        Write-PidFile -ServerPid $server.Id -TunnelPid $null
        Write-Host ''
        Write-Host ("  Локальный URL: http://127.0.0.1:$Port") -ForegroundColor Green
        Write-Host '  Ctrl+C — остановить сервер.'
        Write-Host ''
        while (-not $server.HasExited) { Start-Sleep -Seconds 1 }
        if ($server.HasExited) { Write-Host '[app] Сервер завершился.' }
    } else {
        Write-Host '[tunnel] Запускаю Cloudflare Quick Tunnel...' -ForegroundColor Cyan
        $previousGodebug = $env:GODEBUG
        $env:GODEBUG = 'http2client=0'
        try {
            $tunnelArgs = @(
                'tunnel',
                '--url', "http://127.0.0.1:$Port",
                '--no-autoupdate'
            )
            $tunnel = Start-Process -FilePath $Cloudflared -ArgumentList $tunnelArgs `
                -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
                -RedirectStandardOutput $CloudflaredOut -RedirectStandardError $CloudflaredLog
        } finally {
            if ($null -eq $previousGodebug) {
                Remove-Item Env:GODEBUG -ErrorAction SilentlyContinue
            } else {
                $env:GODEBUG = $previousGodebug
            }
        }
        Write-PidFile -ServerPid $server.Id -TunnelPid $tunnel.Id

        $publicUrl = $null
        $urlDeadline = (Get-Date).AddSeconds(60)
        while (-not $publicUrl -and (Get-Date) -lt $urlDeadline) {
            if ($tunnel.HasExited) { break }
            $publicUrl = Get-TunnelUrl -Paths @($CloudflaredLog, $CloudflaredOut)
            if (-not $publicUrl) { Start-Sleep -Milliseconds 750 }
        }

        if (-not $publicUrl) {
            $tail = Get-LogTail -Path $CloudflaredLog -Count 20
            if ($tunnel.HasExited) {
                throw ("cloudflared завершился до получения URL." + [Environment]::NewLine + $tail)
            }
            throw ("Не удалось получить https://*.trycloudflare.com за 60 с." + [Environment]::NewLine + $tail)
        }

        Write-Host ''
        Write-Host ("  Публичный URL: " + $publicUrl) -ForegroundColor Green
        Write-Host ("  Локальный URL: http://127.0.0.1:$Port")
        Write-Host ("  Лог туннеля:  " + $CloudflaredLog)
        Write-Host ("  PID-файл:     " + $PidFile)
        Write-Host '  Ctrl+C — остановить сервер и туннель.'
        Write-Host ''

        while ($true) {
            Start-Sleep -Seconds 1
            if ($server.HasExited) {
                Write-Host '[app] Сервер завершился — останавливаю туннель.' -ForegroundColor Yellow
                break
            }
            if ($tunnel.HasExited) {
                Write-Host '[tunnel] cloudflared завершился — останавливаю сервер.' -ForegroundColor Yellow
                break
            }
        }
    }
} catch {
    Write-Host ("[ОШИБКА] " + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
} finally {
    Stop-ChildProcess -Process $tunnel -Label 'cloudflared'
    Stop-ChildProcess -Process $server -Label 'serve.py'
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

exit $exitCode
