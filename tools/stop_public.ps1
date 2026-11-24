# Остановка публичного стенда InTrAvia по logs\public_pids.txt.
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File tools\stop_public.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root 'logs\public_pids.txt'

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host '[stop] PID-файл не найден — запущенных процессов нет.' -ForegroundColor Yellow
    exit 0
}

$serverPid = $null
$cloudflaredPid = $null
$rawNumbers = @()

foreach ($line in (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue)) {
    $text = ([string]$line).Trim()
    if ($text -match '^server\s*=\s*(\d+)$') {
        $serverPid = [int]$Matches[1]
    } elseif ($text -match '^cloudflared\s*=\s*(\d+)$') {
        $cloudflaredPid = [int]$Matches[1]
    } elseif ($text -match '^(\d+)$') {
        $rawNumbers += [int]$Matches[1]
    }
}
if ($null -eq $serverPid -and $rawNumbers.Count -ge 1) { $serverPid = $rawNumbers[0] }
if ($null -eq $cloudflaredPid -and $rawNumbers.Count -ge 2) { $cloudflaredPid = $rawNumbers[1] }

$failed = $false

function Stop-One {
    param([int]$ProcessId, [string]$ExpectedName)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Host ("[stop] PID $ProcessId уже не запущен.") -ForegroundColor DarkGray
        return
    }

    $actualName = $process.ProcessName
    if ($ExpectedName -and $actualName -notmatch $ExpectedName) {
        Write-Host ("[!] PID $ProcessId — это '$actualName', а не '$ExpectedName'; пропускаю (PID переиспользован).") -ForegroundColor Yellow
        return
    }

    Write-Host ("[stop] Останавливаю $actualName (PID $ProcessId)...") -ForegroundColor Cyan
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        [void]$process.WaitForExit(5000)
    } catch { }

    try {
        $process.Refresh()
        if (-not $process.HasExited) {
            Write-Host ("[stop] Процесс PID $ProcessId не завершился, применяю taskkill /T.") -ForegroundColor Yellow
            & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
            [void]$process.WaitForExit(5000)
        }
    } catch { }

    try {
        $process.Refresh()
        if (-not $process.HasExited) {
            Write-Host ("[ОШИБКА] Не удалось остановить PID $ProcessId.") -ForegroundColor Red
            $script:failed = $true
        }
    } catch {
        $script:failed = $true
    }
}

if ($null -ne $serverPid) { Stop-One -ProcessId $serverPid -ExpectedName 'python' }
if ($null -ne $cloudflaredPid) { Stop-One -ProcessId $cloudflaredPid -ExpectedName 'cloudflared' }

if ($failed) {
    Write-Host '[stop] Часть процессов не остановлена; PID-файл сохранён для повторной попытки.' -ForegroundColor Red
    exit 1
}

Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Write-Host '[stop] Готово: сервер и туннель остановлены.' -ForegroundColor Green
exit 0
