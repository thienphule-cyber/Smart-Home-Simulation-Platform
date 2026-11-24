# Stop the background Smart Building simulator started by start.ps1.
Set-Location $PSScriptRoot

$pidFile  = ".server.pid"
$portFile = ".server.port"

if (-not (Test-Path $pidFile)) {
    Write-Host "Server is not running (no PID file)."
    exit 0
}

$id = [int](Get-Content $pidFile)
if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
    # /T also kills child processes (the venv launcher starts the real Python as a child)
    taskkill /F /T /PID $id | Out-Null
    Write-Host "Server stopped (PID $id)."
} else {
    Write-Host "Process $id was not running (stale PID file)."
}

Remove-Item $pidFile, $portFile -ErrorAction SilentlyContinue