param(
    [string]$Python = ".\backend\.venv\Scripts\python.exe",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repoRoot $Python

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python executable not found: $pythonPath"
}

Set-Location -LiteralPath $repoRoot
while ($true) {
    & $pythonPath -m uvicorn arbitrage.backend.main:app --host $HostAddress --port $Port
    $exitCode = $LASTEXITCODE
    Write-Warning "Arbitrage scanner stopped with exit code $exitCode. Restarting in $RestartDelaySeconds seconds. Press Ctrl+C to stop."
    Start-Sleep -Seconds $RestartDelaySeconds
}
