# One-time setup of pulse3 on the Windows server, beside the old Pulse.
# Idempotent: re-running skips finished steps. Never touches C:\pulse (old system).
#   C:\pulse3\pg18        PostgreSQL 18 binaries
#   C:\pulse3\pgdata      cluster (127.0.0.1:55432 only, scram auth)
#   C:\pulse3\app         application snapshot
#   C:\pulse3\var\lake    immutable lake objects
#   C:\pulse3\logs        logs
$ErrorActionPreference = 'Stop'
$root = 'C:\pulse3'
New-Item -ItemType Directory -Force "$root\logs", "$root\var\lake", "$root\secrets" | Out-Null

if (-not (Test-Path "$root\pg18\pgsql\bin\postgres.exe")) {
    $zip = "$root\pg18.zip"
    if (-not (Test-Path $zip)) {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://get.enterprisedb.com/postgresql/postgresql-18.3-1-windows-x64-binaries.zip' -OutFile $zip
    }
    Expand-Archive -Path $zip -DestinationPath "$root\pg18" -Force
}
$bin = "$root\pg18\pgsql\bin"

if (-not (Test-Path "$root\pgdata\PG_VERSION")) {
    # Local-only cluster; superuser password file lives in secrets (ACL-restricted).
    $pwfile = "$root\secrets\pg_superuser.txt"
    if (-not (Test-Path $pwfile)) {
        $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        [IO.File]::WriteAllText($pwfile, [Convert]::ToBase64String($bytes))
        icacls "$root\secrets" /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null
    }
    # initdb runs under a restricted token without Administrators, so it cannot read secrets\;
    # hand it a short-lived copy and delete it right after.
    $tmp = "$root\initdb_pw.tmp"
    Copy-Item $pwfile $tmp
    try { & "$bin\initdb.exe" -D "$root\pgdata" -U postgres --pwfile=$tmp -A scram-sha-256 -E UTF8 --locale=C }
    finally { Remove-Item $tmp -Force }
    if (-not (Test-Path "$root\pgdata\PG_VERSION")) { throw "initdb failed" }
    Add-Content "$root\pgdata\postgresql.conf" "`nlisten_addresses = '127.0.0.1'`nport = 55432`nshared_buffers = 512MB`nwork_mem = 16MB`nmax_connections = 40`nlogging_collector = on`n"
}
Write-Output "setup ok: $bin"

# Windows service for the cluster (NetworkService, auto start). Idempotent.
if (-not (Get-Service pulse3-pg -ErrorAction SilentlyContinue)) {
    icacls "$root\pgdata" /grant 'NT AUTHORITY\NetworkService:(OI)(CI)F' | Out-Null
    & "$bin\pg_ctl.exe" register -N pulse3-pg -U 'NT AUTHORITY\NetworkService' -D "$root\pgdata" -S auto -w
}
if ((Get-Service pulse3-pg).Status -ne 'Running') { Start-Service pulse3-pg }
Write-Output "pg service: $((Get-Service pulse3-pg).Status)"
