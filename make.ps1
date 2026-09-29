<#
.SYNOPSIS
    FireWatch task runner for Windows: the same targets as the Makefile.

.DESCRIPTION
    Windows PowerShell 5.1 has no && operator and does not stop when a native
    command fails, so every step here checks its own exit code and the run stops
    at the first failure. Works under Windows PowerShell 5.1 and PowerShell 7.

    Keep this file ASCII-only: PowerShell 5.1 reads a BOM-less script in the ANSI
    code page, and a stray Unicode dash or quote can break parsing.

.EXAMPLE
    .\make.ps1 install
    .\make.ps1 db
    .\make.ps1 migrate
    .\make.ps1 test
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$VenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$Container = 'firewatch-db'

function Write-Step([string]$Message) {
    Write-Host ">> $Message"
}

function Invoke-Native {
    # Run a native command and throw if it exits non-zero.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "'$FilePath $($ArgumentList -join ' ')' exited with code $LASTEXITCODE"
    }
}

function Get-NativeOutput {
    # Run a native command for its output, ignoring stderr and failures. Under
    # PowerShell 5.1 a redirected stderr line becomes an error record, which
    # would be fatal with ErrorActionPreference = Stop.
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @ArgumentList 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $out
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Get-DotEnv([string]$Key, [string]$Default) {
    $path = Join-Path $PSScriptRoot '.env'
    if (Test-Path -LiteralPath $path) {
        foreach ($line in Get-Content -LiteralPath $path) {
            if ($line -match "^\s*$Key\s*=\s*(.*?)\s*$" -and $Matches[1]) {
                return $Matches[1]
            }
        }
    }
    return $Default
}

function Assert-Venv {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "No virtualenv at .venv - run: .\make.ps1 install"
    }
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is not installed. Install Docker Desktop (needs WSL2 and admin rights), then retry.'
    }
}

function Get-BootstrapPython {
    # Every pinned wheel exists for Python 3.13 on Windows; several do not exist
    # for 3.14, which may be the default python. The py launcher can pick 3.13.
    if (Get-NativeOutput -FilePath 'py' -ArgumentList @('-3.13', '-c', 'print(1)')) {
        return @('py', '-3.13')
    }
    $version = Get-NativeOutput -FilePath 'python' -ArgumentList @(
        '-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
    if ($version -eq '3.13') {
        return @('python')
    }
    throw 'Python 3.13 not found. Install it from python.org; the py launcher will find it.'
}

function Show-Help {
    Write-Host 'FireWatch - build tasks (.\make.ps1 <target>)'
    Write-Host '  install   create .venv (Python 3.13) and install dependencies'
    Write-Host '  db        start postgis(+timescaledb) and wait for healthy'
    Write-Host '  migrate   apply sql\*.sql in order'
    Write-Host '  psql      open a psql shell in the db container'
    Write-Host '  test      run pytest'
    Write-Host '  lint      ruff check'
    Write-Host '  fixture   stage 1: write the synthetic test fixture to data\mock'
    Write-Host '  backfill  stage 2: load everything into the database, then check it'
    Write-Host '  registry  stage 3: build the source registry, then check it'
    Write-Host '  labels    stage 4: weak labels for every registry source'
    Write-Host '  train     stage 4: train Model 1 and write reports\model1_metrics.json'
    Write-Host '  inference stage 5: route the latest year through Roads A, B and C'
    Write-Host '  events    stage 6: assemble the latest run into incidents'
    Write-Host '  risk      stage 7: score every event (hazard x exposure x vulnerability)'
    Write-Host '  spike     stage 1.5: cluster one real year of FIRMS (no database)'
    Write-Host '  down      stop containers (data kept)'
    Write-Host '  clean     stop containers AND delete the volume'
}

try {
    switch ($Target) {
        'help' {
            Show-Help
        }
        'install' {
            if (-not (Test-Path -LiteralPath $VenvPython)) {
                Write-Step 'creating .venv with Python 3.13'
                $boot = @(Get-BootstrapPython)
                # @() matters: a one-element slice is a bare string, and
                # string + array concatenates text instead of arrays.
                $bootArgs = @()
                if ($boot.Count -gt 1) { $bootArgs = @($boot[1..($boot.Count - 1)]) }
                Invoke-Native -FilePath $boot[0] -ArgumentList ($bootArgs + @('-m', 'venv', '.venv'))
            }
            Write-Step 'installing python dependencies'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'install', '-r', 'requirements.txt')
            if (-not (Test-Path -LiteralPath '.env')) {
                Copy-Item -LiteralPath '.env.example' -Destination '.env'
                Write-Step 'created .env from .env.example'
            }
        }
        'db' {
            if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
                # No Docker: fall back to the portable PostgreSQL + PostGIS, if installed.
                $local = Join-Path $env:LOCALAPPDATA 'firewatch\pgsql\bin\pg_ctl.exe'
                if (-not (Test-Path -LiteralPath $local)) {
                    throw ('Docker is not installed. Either install Docker Desktop, or run ' +
                           '.\scripts\local_postgres.ps1 install for a portable database.')
                }
                Write-Step 'no Docker: using the portable PostgreSQL'
                & (Join-Path $PSScriptRoot 'scripts\local_postgres.ps1') start
                exit 0
            }
            Write-Step 'starting database'
            Invoke-Native -FilePath 'docker' -ArgumentList @('compose', 'up', '-d', 'db')
            Write-Step 'waiting for healthy (timescale/postgis image is a large first pull)'
            for ($i = 0; $i -lt 60; $i++) {
                $status = Get-NativeOutput -FilePath 'docker' -ArgumentList @(
                    'inspect', '--format', '{{.State.Health.Status}}', $Container)
                if ("$status".Trim() -eq 'healthy') {
                    Write-Step 'database healthy'
                    exit 0
                }
                Start-Sleep -Seconds 3
            }
            & docker compose logs --tail=30 db
            throw 'timed out waiting for the database'
        }
        'migrate' {
            Assert-Venv
            Write-Step 'applying sql\*.sql'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\migrate.py')
        }
        'psql' {
            Assert-Docker
            $user = Get-DotEnv -Key 'POSTGRES_USER' -Default 'firewatch'
            $db = Get-DotEnv -Key 'POSTGRES_DB' -Default 'firewatch'
            Invoke-Native -FilePath 'docker' -ArgumentList @('compose', 'exec', 'db', 'psql', '-U', $user, '-d', $db)
        }
        'test' {
            Assert-Venv
            Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'pytest', 'tests/', '-v')
        }
        'lint' {
            Assert-Venv
            Invoke-Native -FilePath $VenvPython -ArgumentList @('-m', 'ruff', 'check', 'firewatch/', 'tests/', 'scripts/')
        }
        'fixture' {
            Assert-Venv
            Write-Step 'stage 1: synthetic test fixture -> data\mock'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\generate_fixture.py')
        }
        'backfill' {
            Assert-Venv
            Write-Step 'stage 2: backfill (MOCK_MODE decides real vs fixture)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\backfill.py')
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\check_ingest.py')
        }
        'registry' {
            Assert-Venv
            Write-Step 'stage 3: registry (gate, cluster, fingerprints, baselines)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\build_registry.py')
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\check_registry.py')
        }
        'labels' {
            Assert-Venv
            Write-Step 'stage 4: weak labels (OSM, power plants, WorldCover)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\build_labels.py')
        }
        'train' {
            Assert-Venv
            Write-Step 'stage 4: Model 1 (leakage guard, block CV, final fit)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\train.py')
        }
        'inference' {
            Assert-Venv
            Write-Step 'stage 5: router, Road A, Road C, promotion (replays the latest year)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\run_inference.py')
        }
        'events' {
            Assert-Venv
            Write-Step 'stage 6: event assembly (latest inference run)'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\build_events.py')
        }
        'risk' {
            Assert-Venv
            Write-Step 'stage 7: risk scoring, then its checks'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\score_risk.py')
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\check_risk.py')
        }
        'spike' {
            Assert-Venv
            Write-Step 'stage 1.5: one real year of FIRMS, clustered three ways'
            Invoke-Native -FilePath $VenvPython -ArgumentList @('scripts\spike_cluster.py')
        }
        'logs' {
            Assert-Docker
            & docker compose logs -f db
        }
        'down' {
            Assert-Docker
            Invoke-Native -FilePath 'docker' -ArgumentList @('compose', 'down')
        }
        'clean' {
            Assert-Docker
            Invoke-Native -FilePath 'docker' -ArgumentList @('compose', 'down', '-v')
            Write-Step 'volume removed'
        }
        default {
            Show-Help
            throw "unknown target '$Target'"
        }
    }
} catch {
    Write-Host ">> FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
