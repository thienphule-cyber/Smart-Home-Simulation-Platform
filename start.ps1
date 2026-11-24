# Start the Smart Building simulator in the background on the first free port.
# Usage: .\start.ps1 [-StartPort 8000] [-NoBrowser]
param(
    [int]$StartPort = 8000,
    [switch]$NoBrowser
)
Set-Location $PSScriptRoot

$pidFile  = ".server.pid"
$portFile = ".server.port"
$logDir   = "logs"

function Show-Links($port) {
    Write-Host "Platform : http://127.0.0.1:$port/frontend/"
    Write-Host "API docs : http://127.0.0.1:$port/docs"
}

function Test-PortFree($p) {
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
        $l.Start()
        $l.Stop()
        return $true
    } catch {
        return $false
    }
}

# Find the Python interpreter (project venv first, then system Python)
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "Python not found. Create the .venv first."
        exit 1
    }
    $py = $cmd.Source
}

# Already running? Just show the links again.
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile)
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
        $port = Get-Content $portFile
        Write-Host "Already running (PID $old)."
        Show-Links $port
        if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/frontend/" }
        exit 0
    }
    Remove-Item $pidFile, $portFile -ErrorAction SilentlyContinue
}

# Check dependencies
& $py -c "import fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Missing packages. Run: & '$py' -m pip install -r requirements.txt"
    exit 1
}

# Find the first free port, starting at StartPort
$port = $null
foreach ($p in $StartPort..($StartPort + 99)) {
    if (Test-PortFree $p) { $port = $p; break }
}
if (-not $port) {
    Write-Host "No free port found in $StartPort-$($StartPort + 99)."
    exit 1
}

# Launch hidden in the background; it keeps running after the terminal is closed
New-Item -ItemType Directory -Force $logDir | Out-Null
$proc = Start-Process -FilePath $py `
    -ArgumentList @("-m", "uvicorn", "main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "$port") `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput "$logDir\server.log" `
    -RedirectStandardError "$logDir\server.err.log" `
    -PassThru
Set-Content $pidFile $proc.Id
Set-Content $portFile $port

# Wait until the API answers (max ~20 s)
Write-Host -NoNewline "Starting"
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    if ($proc.HasExited) {
        Write-Host ""
        Write-Host "Server stopped unexpectedly. Last log lines:"
        Get-Content "$logDir\server.err.log" -Tail 20
        Remove-Item $pidFile, $portFile -ErrorAction SilentlyContinue
        exit 1
    }
    try {
        Invoke-WebRequest "http://127.0.0.1:$port/api/buildings" -UseBasicParsing -TimeoutSec 1 | Out-Null
        $ready = $true
        break
    } catch {
        Write-Host -NoNewline "."
        Start-Sleep -Milliseconds 500
    }
}
Write-Host ""

if (-not $ready) {
    Write-Host "Server did not answer in time. Check $logDir\server.err.log"
    exit 1
}

Write-Host "Server running (PID $($proc.Id), port $port)."
Show-Links $port
Write-Host "Log      : $logDir\server.err.log"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/frontend/" }