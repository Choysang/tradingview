# Deploy a pulse3 bundle uploaded to C:\pulse3\incoming\<round>\ (app.zip, optional pulse.dump, lake.zip).
#   powershell -File C:\pulse3\deploy_app.ps1 -Round r1 [-RestoreDb] [-RestoreLake]
# Backs up the previous app to C:\pulse3\backup\<round>\ first and refuses if that backup exists.
# Never touches C:\pulse (the old system).
param([Parameter(Mandatory)][string]$Round, [switch]$RestoreDb, [switch]$RestoreLake)
$ErrorActionPreference = 'Stop'
# Native tools (uv, pg_restore) write progress to stderr; PS 5.1 would turn that into a terminating error.
function Native([scriptblock]$b) { $ErrorActionPreference = 'Continue'; & $b 2>&1 | ForEach-Object { "$_" }; return $LASTEXITCODE }
. C:\pulse3\pulse3_env.ps1
$in = "$root\incoming\$Round"
$bk = "$root\backup\$Round"
if (Test-Path $bk) { throw "backup $bk exists; choose a new round name" }
New-Item -ItemType Directory -Force $bk | Out-Null
if (Test-Path "$root\app") { Copy-Item "$root\app" "$bk\app" -Recurse -Exclude '.venv' }

Get-ScheduledTask -TaskName Pulse3Api -ErrorAction SilentlyContinue | Stop-ScheduledTask
$p = Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue | Select -First 1
if ($p) { Stop-Process -Id $p.OwningProcess -Force }

$venv = "$root\app\.venv"
if (Test-Path "$root\app") { Get-ChildItem "$root\app" -Exclude '.venv' | Remove-Item -Recurse -Force }
Expand-Archive "$in\app.zip" -DestinationPath "$root\app" -Force
Set-Location "$root\app"
$rc = Native { & $uv sync --frozen --no-dev --python 'C:\pulse\tools\python\cpython-3.13.15-windows-x86_64-none\python.exe' }
if ($rc[-1] -ne 0) { throw "uv sync failed" }

$bin = "$root\pg18\pgsql\bin"
$env:PGPASSWORD = $pw
if ($RestoreDb) {
    & "$bin\psql.exe" -h 127.0.0.1 -p 55432 -U postgres -tAc "select 1 from pg_database where datname='pulse'" | Out-String | ForEach-Object {
        if ($_.Trim() -eq '1') { throw "database pulse already exists; refusing to overwrite" } }
    $rc = Native { & "$bin\createdb.exe" -h 127.0.0.1 -p 55432 -U postgres pulse }
    if ($rc[-1] -ne 0) { throw "createdb failed" }
    $rc = Native { & "$bin\pg_restore.exe" -h 127.0.0.1 -p 55432 -U postgres -d pulse --no-owner -j 2 "$in\pulse.dump" }
    if ($rc[-1] -ne 0) { throw "pg_restore failed" }
}
if ($RestoreLake) {
    if ((Test-Path "$root\var\lake\catalog.sqlite3")) { throw "lake already present; refusing to overwrite" }
    Expand-Archive "$in\lake.zip" -DestinationPath "$root\var\lake" -Force
}
$rc = Native { & "$root\app\.venv\Scripts\python.exe" -m pulse.adapters.postgres.migrate }
if ($rc[-1] -ne 0) { throw "migrate failed" }
Remove-Item Env:PGPASSWORD

Copy-Item "$root\app\deploy\windows\run_api.ps1", "$root\app\deploy\windows\run_daily.ps1", "$root\app\deploy\windows\pulse3_env.ps1" $root -Force
$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Get-ScheduledTask -TaskName Pulse3Api -ErrorAction SilentlyContinue)) {
    $a = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File $root\run_api.ps1"
    $t = New-ScheduledTaskTrigger -AtStartup
    $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName Pulse3Api -Action $a -Trigger $t -Settings $s -User SYSTEM -RunLevel Highest | Out-Null
}
if (-not (Get-ScheduledTask -TaskName Pulse3Daily -ErrorAction SilentlyContinue)) {
    $a = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File $root\run_daily.ps1"
    $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 18:10
    $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -StartWhenAvailable
    Register-ScheduledTask -TaskName Pulse3Daily -Action $a -Trigger $t -Settings $s -User SYSTEM -RunLevel Highest | Out-Null
}
Start-ScheduledTask -TaskName Pulse3Api
Write-Output "deployed $Round"
