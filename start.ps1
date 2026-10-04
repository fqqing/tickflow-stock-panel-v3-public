# start.ps1 - one-shot launcher for THIS machine (backend FastAPI + frontend Vite)
#
# Why this exists: the official dev.ps1 requires a global `pnpm` on PATH. This
# machine has no global pnpm (deps live in frontend/node_modules), and no Docker.
# So this script calls the local binaries directly.
#
# Usage:
#   .\start.ps1
#   .\start.ps1 -BackendPort 3018 -FrontendPort 3011
#
# Ctrl-C closes both processes.
# If you see "running scripts is disabled":
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
# (GBK on zh-CN) unless a UTF-8 BOM is present; non-ASCII would break parsing.

[CmdletBinding()]
param(
    [int]$BackendPort  = 3018,
    [int]$FrontendPort = 3011
)

$ErrorActionPreference = 'Stop'

# Child processes (uvicorn, vite) emit UTF-8. A zh-CN console defaults to GBK,
# which garbles their Chinese log lines. Align the console to UTF-8.
# Harmless when stdout is redirected.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch { }

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Python   = Join-Path $Backend  '.venv\Scripts\python.exe'
$ViteJs   = Join-Path $Frontend 'node_modules\vite\bin\vite.js'

function Log-Info($m) { Write-Host "[start] $m" -ForegroundColor DarkGray }
function Log-Ok  ($m) { Write-Host "[start] $m" -ForegroundColor Green }
function Log-Err ($m) { Write-Host "[start] $m" -ForegroundColor Red }

# ===== 1. Preflight: say exactly what is missing and how to fix it =====
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
    Log-Err 'node not found. Install Node.js >= 20 (https://nodejs.org)'
    exit 1
}

if (-not (Test-Path $Python)) {
    Log-Err "backend venv not found: $Python"
    Write-Host '       install backend deps first:' -ForegroundColor Yellow
    Write-Host '       cd backend; uv sync --extra backtest --extra dev' -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $ViteJs)) {
    Log-Err "frontend deps not found: $ViteJs"
    Write-Host '       install frontend deps first (either works):' -ForegroundColor Yellow
    Write-Host '       cd frontend; pnpm install' -ForegroundColor Yellow
    Write-Host '       cd frontend; npm install' -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Log-Info 'no .env found; copying from .env.example (None mode, free daily K-line)'
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
}

# ===== 1b. Tesseract OCR (used by the watchlist screenshot importer) =====
# pytesseract locates the engine via PATH, and the traineddata via
# TESSDATA_PREFIX. Set both here so the backend works even when this shell was
# started before those user-level variables existed (a child process inherits
# its parent's environment block, not the registry). Skipped silently when
# Tesseract is not installed.
$tessRoots = @(
    'C:\Program Files\Tesseract-OCR',
    'C:\Program Files (x86)\Tesseract-OCR',
    (Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR')
)
foreach ($d in $tessRoots) {
    if (Test-Path (Join-Path $d 'tesseract.exe')) {
        if ($env:Path -notlike "*$d*") { $env:Path = "$env:Path;$d" }
        if (-not $env:TESSDATA_PREFIX) {
            $userTd = Join-Path $env:LOCALAPPDATA 'Tesseract-OCR\tessdata'
            if (Test-Path $userTd) { $env:TESSDATA_PREFIX = $userTd }
        }
        Log-Info "tesseract found: $d"
        break
    }
}

# ===== 2. Free ports =====
function Free-Port($name, $port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if (-not $conns) { return }
    $pids = @($conns.OwningProcess | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    $alive = @($pids | Where-Object {
        try { [System.Diagnostics.Process]::GetProcessById($_) | Out-Null; $true } catch { $false }
    })
    if ($alive.Count -eq 0) { return }
    Log-Info "port $port ($name) in use, killing PID: $($alive -join ', ')"
    foreach ($p in $alive) { $null = & cmd /c "taskkill /F /T /PID $p 2>nul" }
    Start-Sleep -Seconds 1
}

Free-Port 'backend'  $BackendPort
Free-Port 'frontend' $FrontendPort

# ===== 3. Banner =====
Write-Host ''
Write-Host '+----------------------------------------------+' -ForegroundColor Blue
Write-Host '|  tickflow-stock-panel  (local launcher)      |' -ForegroundColor Blue
Write-Host '|                                              |' -ForegroundColor Blue
Write-Host "|  panel   http://localhost:$FrontendPort               |" -ForegroundColor Blue
Write-Host "|  api     http://localhost:$BackendPort/docs          |" -ForegroundColor Blue
Write-Host '|                                              |' -ForegroundColor Blue
Write-Host '|  Ctrl-C closes both                          |' -ForegroundColor Blue
Write-Host '+----------------------------------------------+' -ForegroundColor Blue
Write-Host ''

# ===== 4. Launch =====
# Each job writes its $PID to a temp file so the main thread can taskkill /T the
# whole process tree on exit (Stop-Process only kills the parent, orphaning the
# uvicorn/vite children that actually hold the socket).
$backendPidFile  = Join-Path $env:TEMP 'tf-backend.pid'
$frontendPidFile = Join-Path $env:TEMP 'tf-frontend.pid'

$backendJob = Start-Job -Name 'tf-backend' -ScriptBlock {
    param($pidFile, $py, $dir, $port)
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
        $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
    } catch { }
    $PID | Out-File -FilePath $pidFile -Encoding ascii -Force
    $env:PYTHONUNBUFFERED = '1'
    Set-Location $dir
    & $py -m uvicorn app.main:app --host 0.0.0.0 --port $port 2>&1
} -ArgumentList $backendPidFile, $Python, $Backend, $BackendPort

$frontendJob = Start-Job -Name 'tf-frontend' -ScriptBlock {
    param($pidFile, $node, $vite, $dir, $backendPort, $port)
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
        $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
    } catch { }
    $PID | Out-File -FilePath $pidFile -Encoding ascii -Force
    Set-Location $dir
    $env:BACKEND_HOST = '127.0.0.1'
    $env:BACKEND_PORT = [string]$backendPort
    & $node $vite --host 0.0.0.0 --port $port 2>&1
} -ArgumentList $frontendPidFile, $nodeExe, $ViteJs, $Frontend, $BackendPort, $FrontendPort

$script:cleaning = $false
function Cleanup-All {
    if ($script:cleaning) { return }
    $script:cleaning = $true
    Write-Host ''
    Log-Info 'shutting down...'
    foreach ($pf in @($backendPidFile, $frontendPidFile)) {
        if (Test-Path $pf) {
            $childPid = (Get-Content $pf -ErrorAction SilentlyContinue) -as [string]
            if ($childPid -and $childPid.Trim()) {
                $null = & cmd /c "taskkill /F /T /PID $($childPid.Trim()) 2>nul"
            }
            Remove-Item $pf -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($j in @($backendJob, $frontendJob)) {
        if ($j) {
            Stop-Job   $j -ErrorAction SilentlyContinue
            Remove-Job $j -Force -ErrorAction SilentlyContinue
        }
    }
    Log-Ok 'bye'
}

# ===== 5. Main loop =====
# Treat Ctrl-C as input so try/finally is guaranteed to run. When stdin is
# redirected (background / piped run) [Console]::TreatControlCAsInput throws
# "The handle is invalid" -- fall back to a plain wait loop instead of dying.
$script:hasConsole = $true
try {
    $prevCtrlC = [Console]::TreatControlCAsInput
    [Console]::TreatControlCAsInput = $true
} catch {
    $script:hasConsole = $false
}

try {
    while ($true) {
        if ($script:hasConsole) {
            try {
                if ([Console]::KeyAvailable) {
                    $key = [Console]::ReadKey($true)
                    if (($key.Modifiers -band [ConsoleModifiers]::Control) -and $key.Key -eq 'C') {
                        break
                    }
                }
            } catch {
                $script:hasConsole = $false
            }
        }

        $b = Receive-Job $backendJob -ErrorAction SilentlyContinue
        if ($b) { foreach ($l in $b) { Write-Host '[backend ] ' -NoNewline -ForegroundColor Blue;  Write-Host $l } }

        $f = Receive-Job $frontendJob -ErrorAction SilentlyContinue
        if ($f) { foreach ($l in $f) { Write-Host '[frontend] ' -NoNewline -ForegroundColor Green; Write-Host $l } }

        if ($backendJob.State -ne 'Running' -or $frontendJob.State -ne 'Running') {
            Log-Info 'one process exited; closing the other...'
            break
        }

        Start-Sleep -Milliseconds 150
    }
}
finally {
    if ($script:hasConsole) { [Console]::TreatControlCAsInput = $prevCtrlC }
    Cleanup-All
}
