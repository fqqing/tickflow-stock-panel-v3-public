# start.ps1 - one-shot launcher for THIS machine.
#
# Why this exists: the official dev.ps1 requires a global `pnpm` on PATH. This
# machine has no global pnpm (deps live in frontend/node_modules) and no Docker,
# so this script calls the local binaries directly.
#
# Default mode = backend only. The backend serves the prebuilt frontend from
# frontend/dist (see backend/app/config.py: static_dir), so ONE process is
# enough: fast to start, one port, one thing to stop. Use -Dev only when you
# are editing the frontend and want the Vite dev server (HMR) on its own port.
#
# Usage:
#   .\start.ps1                       # backend on 3018, serves frontend/dist
#   .\start.ps1 -Dev                  # also start Vite dev server on 3011
#   .\start.ps1 -BackendPort 8080     # override backend port
#   .\start.ps1 -BindHost 0.0.0.0     # expose on the LAN (default 127.0.0.1)
#   .\start.ps1 -NoOpen               # do not auto-open the browser
#
# Ctrl-C closes everything.
#
# If you see "running scripts is disabled" (ExecutionPolicy blocks .ps1):
#   easiest  -> double-click start.bat   (bypasses the policy for that one run)
#   or once  -> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as ANSI
# (GBK on zh-CN) unless a UTF-8 BOM is present; non-ASCII would break parsing.

[CmdletBinding()]
param(
    [int]$BackendPort  = 3018,
    [int]$FrontendPort = 3011,
    [string]$BindHost  = '127.0.0.1',
    [switch]$Dev,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'

$StartFrontend = [bool]$Dev.IsPresent

# Child processes (uvicorn, vite) emit UTF-8. A zh-CN console defaults to GBK,
# which garbles their Chinese log lines. Align the console to UTF-8.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch { }

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Python   = Join-Path $Backend  '.venv\Scripts\python.exe'
$ViteJs   = Join-Path $Frontend 'node_modules\vite\bin\vite.js'
$DistIdx  = Join-Path $Frontend 'dist\index.html'

function Log-Info($m) { Write-Host "[start] $m" -ForegroundColor DarkGray }
function Log-Ok  ($m) { Write-Host "[start] $m" -ForegroundColor Green }
function Log-Warn($m) { Write-Host "[start] $m" -ForegroundColor Yellow }
function Log-Err ($m) { Write-Host "[start] $m" -ForegroundColor Red }

# TCP readiness probe - used to decide when to open the browser.
function Test-TcpReady($h, $p) {
    $c = $null
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($h, $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        if ($c) { try { $c.Close() } catch { } }
    }
}

# ===== 1. Preflight: say exactly what is missing and how to fix it =====
if (-not (Test-Path $Python)) {
    Log-Err "backend venv not found: $Python"
    Write-Host '       install backend deps first:' -ForegroundColor Yellow
    Write-Host '       cd backend; uv sync --extra backtest --extra dev' -ForegroundColor Yellow
    exit 1
}

if ($StartFrontend -and -not (Get-Command node -ErrorAction SilentlyContinue)) {
    Log-Err 'node not found (needed for -Dev). Install Node.js >= 20 or drop -Dev.'
    exit 1
}

if ($StartFrontend -and -not (Test-Path $ViteJs)) {
    Log-Err "frontend deps not found: $ViteJs"
    Write-Host '       install frontend deps first (either works):' -ForegroundColor Yellow
    Write-Host '       cd frontend; pnpm install' -ForegroundColor Yellow
    Write-Host '       cd frontend; npm install' -ForegroundColor Yellow
    exit 1
}

if (-not $StartFrontend -and -not (Test-Path $DistIdx)) {
    Log-Warn "frontend/dist/index.html not found - the panel would be blank."
    Log-Warn 'build it once:  cd frontend; npx vite build --emptyOutDir=false'
    Log-Warn 'or run with -Dev for the Vite dev server instead.'
}

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Log-Info 'no .env found; copying from .env.example (None mode, free daily K-line)'
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
}

# ===== 1b. Tesseract OCR (used by the watchlist screenshot importer) =====
# pytesseract locates the engine via PATH and the traineddata via
# TESSDATA_PREFIX. Set both here so the backend works even when this shell was
# started before those user-level variables existed.
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

Free-Port 'backend' $BackendPort
if ($StartFrontend) { Free-Port 'frontend' $FrontendPort }

# ===== 3. Banner =====
$probeHost = if ($BindHost -in @('0.0.0.0', '::')) { '127.0.0.1' } else { $BindHost }
$openHost  = if ($BindHost -in @('0.0.0.0', '::')) { 'localhost' } else { $BindHost }
$panelPort = if ($StartFrontend) { $FrontendPort } else { $BackendPort }
$panelUrl  = "http://${openHost}:$panelPort"
$modeText  = if ($StartFrontend) { 'backend + vite dev (HMR)' } else { 'backend only, serves frontend/dist' }

Write-Host ''
Write-Host '+----------------------------------------------------+' -ForegroundColor Blue
Write-Host '|   tickflow-stock-panel   (one-click launcher)      |' -ForegroundColor Blue
Write-Host '+----------------------------------------------------+' -ForegroundColor Blue
Write-Host "    panel    $panelUrl"                              -ForegroundColor Blue
Write-Host "    api      http://${openHost}:$BackendPort/docs"    -ForegroundColor Blue
Write-Host "    mode     $modeText"                              -ForegroundColor DarkGray
Write-Host '    Ctrl-C   stops everything'                       -ForegroundColor DarkGray
Write-Host '+----------------------------------------------------+' -ForegroundColor Blue
Write-Host ''

# ===== 4. Launch =====
# Each job writes its $PID to a temp file so the main thread can taskkill /T the
# whole process tree on exit (Stop-Process only kills the parent, orphaning the
# uvicorn/vite children that actually hold the socket).
$backendPidFile = Join-Path $env:TEMP 'tf-backend.pid'
if ($StartFrontend) { $frontendPidFile = Join-Path $env:TEMP 'tf-frontend.pid' }

$backendJob = Start-Job -Name 'tf-backend' -ScriptBlock {
    param($pidFile, $py, $dir, $host_, $port)
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
        $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
    } catch { }
    $PID | Out-File -FilePath $pidFile -Encoding ascii -Force
    $env:PYTHONUNBUFFERED = '1'
    Set-Location $dir
    & $py -m uvicorn app.main:app --host $host_ --port $port 2>&1
} -ArgumentList $backendPidFile, $Python, $Backend, $BindHost, $BackendPort

$frontendJob = $null
if ($StartFrontend) {
    $nodeExe = (Get-Command node).Source
    $frontendJob = Start-Job -Name 'tf-frontend' -ScriptBlock {
        param($pidFile, $node, $vite, $dir, $backendPort, $host_, $port)
        try {
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
            $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
        } catch { }
        $PID | Out-File -FilePath $pidFile -Encoding ascii -Force
        Set-Location $dir
        $env:BACKEND_HOST = $host_
        $env:BACKEND_PORT = [string]$backendPort
        & $node $vite --host $host_ --port $port 2>&1
    } -ArgumentList $frontendPidFile, $nodeExe, $ViteJs, $Frontend, $BackendPort, $BindHost, $FrontendPort
}

$script:cleaning = $false
function Cleanup-All {
    if ($script:cleaning) { return }
    $script:cleaning = $true
    Write-Host ''
    Log-Info 'shutting down...'
    foreach ($pf in @($backendPidFile, $frontendPidFile)) {
        if ($pf -and (Test-Path $pf)) {
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

$script:browserOpened = $false

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

        if ($frontendJob) {
            $f = Receive-Job $frontendJob -ErrorAction SilentlyContinue
            if ($f) { foreach ($l in $f) { Write-Host '[frontend] ' -NoNewline -ForegroundColor Green; Write-Host $l } }
        }

        # Auto-open the browser once the panel port accepts connections.
        if ((-not $NoOpen) -and (-not $script:browserOpened) -and (Test-TcpReady $probeHost $panelPort)) {
            $script:browserOpened = $true
            try { Start-Process $panelUrl } catch { }
            Log-Ok "opened $panelUrl"
        }

        $backendDead  = ($backendJob.State -ne 'Running')
        $frontendDead = ($frontendJob -and $frontendJob.State -ne 'Running')
        if ($backendDead -or $frontendDead) {
            Log-Info 'a process exited; closing the rest...'
            break
        }

        Start-Sleep -Milliseconds 150
    }
}
finally {
    if ($script:hasConsole) { [Console]::TreatControlCAsInput = $prevCtrlC }
    Cleanup-All
}
