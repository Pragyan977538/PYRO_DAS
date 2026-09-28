<#
.SYNOPSIS
    A portable PostgreSQL 16 + PostGIS for machines without Docker.

.DESCRIPTION
    Everything lives under %LOCALAPPDATA%\firewatch: no installer, no admin rights,
    no Windows service, nothing registered with the system. Delete that folder and
    it is gone. The server runs as your user on the port in .env, with the
    credentials in .env, so DATABASE_URL works unchanged.

    TimescaleDB is not included; the schema detects that and keeps detections as
    a plain table. Docker Desktop remains the setup for the demo.

    Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).

.EXAMPLE
    .\scripts\local_postgres.ps1 install   # download, unpack, initdb, create the db
    .\scripts\local_postgres.ps1 start
    .\scripts\local_postgres.ps1 stop
    .\scripts\local_postgres.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'start', 'stop', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Root = Join-Path $env:LOCALAPPDATA 'firewatch'
$Pg = Join-Path $Root 'pgsql'
$Bin = Join-Path $Pg 'bin'
$Data = Join-Path $Root 'pgdata'
$Log = Join-Path $Root 'postgres.log'
$Downloads = Join-Path $Root 'downloads'

$PgZipUrl = 'https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip'
$PostgisZipUrl = 'https://download.osgeo.org/postgis/windows/pg16/postgis-bundle-pg16-3.6.2x64.zip'

function Write-Step([string]$Message) { Write-Host ">> $Message" }

function Get-DotEnv([string]$Key, [string]$Default) {
    $path = Join-Path $Repo '.env'
    if (Test-Path -LiteralPath $path) {
        foreach ($line in Get-Content -LiteralPath $path) {
            if ($line -match "^\s*$Key\s*=\s*(.*?)\s*$" -and $Matches[1]) { return $Matches[1] }
        }
    }
    return $Default
}

$User = Get-DotEnv -Key 'POSTGRES_USER' -Default 'firewatch'
$Password = Get-DotEnv -Key 'POSTGRES_PASSWORD' -Default 'firewatch'
$Db = Get-DotEnv -Key 'POSTGRES_DB' -Default 'firewatch'
$Port = Get-DotEnv -Key 'POSTGRES_PORT' -Default '5432'

function Invoke-Pg {
    param([string]$Exe, [string[]]$ArgumentList)
    & (Join-Path $Bin $Exe) @ArgumentList
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

function Test-Running {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & (Join-Path $Bin 'pg_ctl.exe') status -D $Data 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false } finally { $ErrorActionPreference = $saved }
}

function Get-Zip([string]$Url, [string]$Name) {
    $path = Join-Path $Downloads $Name
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
        Write-Step "downloading $Url"
        & curl.exe -fL --retry 8 --retry-delay 5 -o "$path.part" $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed: $Url" }
        Move-Item -LiteralPath "$path.part" -Destination $path
    }
    return $path
}

function Start-Pg {
    if (Test-Running) { Write-Step "postgres already running on port $Port"; return }
    Write-Step "starting postgres on port $Port (log: $Log)"
    # Launched through the shell in a hidden window, with no redirection and no
    # -Wait. Started any other way, the long-running server inherits this script's
    # stdout, and if that is a pipe (CI, a wrapper, | Tee-Object) it holds the pipe
    # open and whoever reads it waits forever; -Wait would also wait on the server,
    # a descendant. So: fire, then poll until it accepts connections.
    Start-Process -FilePath (Join-Path $Bin 'pg_ctl.exe') -WindowStyle Hidden `
        -ArgumentList @('start', '-D', "`"$Data`"", '-l', "`"$Log`"", '-o', "`"-p $Port`"")
    for ($i = 0; $i -lt 60; $i++) {
        & (Join-Path $Bin 'pg_isready.exe') -h localhost -p $Port -q
        if ($LASTEXITCODE -eq 0) { Write-Step 'postgres accepting connections'; return }
        Start-Sleep -Seconds 1
    }
    throw "postgres did not start within 60 s; see $Log"
}

switch ($Action) {
    'install' {
        if (-not (Test-Path -LiteralPath (Join-Path $Bin 'pg_ctl.exe'))) {
            $zip = Get-Zip $PgZipUrl 'pg16.zip'
            Write-Step 'unpacking PostgreSQL'
            # tar (bsdtar, built into Windows) is far faster than Expand-Archive.
            & "$env:SystemRoot\System32\tar.exe" -xf $zip -C $Root
            if ($LASTEXITCODE -ne 0) { throw 'could not unpack PostgreSQL' }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $Pg 'share\extension\postgis.control'))) {
            $zip = Get-Zip $PostgisZipUrl 'postgis.zip'
            $tmp = Join-Path $Root 'postgis-unpack'
            New-Item -ItemType Directory -Force -Path $tmp | Out-Null
            Write-Step 'unpacking PostGIS into the PostgreSQL tree'
            & "$env:SystemRoot\System32\tar.exe" -xf $zip -C $tmp
            if ($LASTEXITCODE -ne 0) { throw 'could not unpack PostGIS' }
            $bundle = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
            Copy-Item -Path (Join-Path $bundle.FullName '*') -Destination $Pg -Recurse -Force
            Remove-Item -LiteralPath $tmp -Recurse -Force
        }
        if (-not (Test-Path -LiteralPath (Join-Path $Data 'PG_VERSION'))) {
            Write-Step "initialising a cluster in $Data"
            $pw = Join-Path $Root 'pwfile.tmp'
            Set-Content -LiteralPath $pw -Value $Password -NoNewline -Encoding Ascii
            try {
                Invoke-Pg 'initdb.exe' @('-D', $Data, '-U', $User, "--pwfile=$pw",
                                        '-A', 'scram-sha-256', '-E', 'UTF8', '--no-locale')
            } finally { Remove-Item -LiteralPath $pw -Force }
        }
        Start-Pg
        $env:PGPASSWORD = $Password
        $exists = & (Join-Path $Bin 'psql.exe') -h localhost -p $Port -U $User -d postgres `
            -tAc "SELECT 1 FROM pg_database WHERE datname = '$Db'"
        if ("$exists".Trim() -ne '1') {
            Write-Step "creating database $Db"
            Invoke-Pg 'createdb.exe' @('-h', 'localhost', '-p', $Port, '-U', $User, $Db)
        }
        Write-Step 'ready - now run: .\make.ps1 migrate'
    }
    'start' { Start-Pg }
    'stop' {
        if (Test-Running) { Invoke-Pg 'pg_ctl.exe' @('stop', '-D', $Data, '-m', 'fast') }
        else { Write-Step 'postgres is not running' }
    }
    'status' {
        if (-not (Test-Path -LiteralPath (Join-Path $Bin 'pg_ctl.exe'))) {
            Write-Step 'not installed - run: .\scripts\local_postgres.ps1 install'
        } elseif (Test-Running) { Write-Step "running on port $Port, data in $Data" }
        else { Write-Step 'installed but stopped - run: .\scripts\local_postgres.ps1 start' }
    }
}
