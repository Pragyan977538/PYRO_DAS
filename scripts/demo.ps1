<#
.SYNOPSIS
    FireWatch: run the three-minute demo path (Stage 10).

.DESCRIPTION
    Starts the API and map on http://localhost:8000 if nothing is answering there,
    then walks scripts\demo.py's six stops: each opens in the default browser for
    -Pause seconds, with its narration printed here. -Check verifies every stop
    against the API and opens nothing. A server this script started is stopped
    again at the end; one that was already running is left alone.

    Needs the database up (.\make.ps1 db, or scripts\local_postgres.ps1 start) and
    the 2024 replay loaded (make inference, events, risk). ASCII-only, PowerShell
    5.1 compatible.

.EXAMPLE
    .\scripts\demo.ps1
    .\scripts\demo.ps1 -Pause 10
    .\scripts\demo.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [double]$Pause = 30
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Health = 'http://localhost:8000/api/health'

function Test-Api {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri $Health
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host '>> FAILED: .venv missing; run .\make.ps1 install' -ForegroundColor Red
    exit 1
}

$server = $null
if (-not (Test-Api)) {
    Write-Host '>> starting the API on http://localhost:8000'
    $server = Start-Process -FilePath $Python -WorkingDirectory $Repo -PassThru `
        -WindowStyle Hidden `
        -ArgumentList @('-m', 'uvicorn', 'firewatch.api.main:app', '--host', '127.0.0.1', '--port', '8000')
    for ($i = 0; $i -lt 60 -and -not (Test-Api); $i++) {
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Api)) {
        if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
        Write-Host '>> FAILED: the API did not come up in 60 s (is the database running?)' -ForegroundColor Red
        exit 1
    }
}

$code = 1
try {
    $pyArgs = @((Join-Path $Repo 'scripts\demo.py'), '--pause', "$Pause")
    if ($Check) { $pyArgs += '--check' }
    & $Python @pyArgs
    $code = $LASTEXITCODE
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
        Write-Host '>> API stopped'
    }
}
exit $code
