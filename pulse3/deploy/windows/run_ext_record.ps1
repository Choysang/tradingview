# pulse3 native extended-intraday recorder (NOT registered: the main thread registers it in the cutover script).
# Until the M6 cutover the OLD system records ETF / index / CFFEX index futures / A50 / HSI futures / CNH / gold / crude and pulse3 imports
# those files (run_intraday_import.ps1, step legacy-ext). Running this too would double the requests from the server IP: never both for one day.
# At the cutover: register this as a weekday task starting 08:55 Asia/Shanghai and remove the legacy-ext steps of run_intraday_import.ps1.
# One process per session: polls every minute 09:00-16:00 (the old schedule), the close snapshot at 15:01:30 and the minute bars at 16:00:30,
# ends by itself (hard stop 16:10). Exit code 2 = a host refused us (that host was closed for the run, the others were recorded); the failure is
# logged, never retried in this script (a block is not retried).
# CFFEX cadence: default 60 s like the old recorder; pass -CffexSeconds 300 only after the E089 health check asks for it.
param([int]$CffexSeconds = 60, [string]$Until = '16:10')
. C:\pulse3\pulse3_env.ps1
if (-not $cutover) { 'refused: the old recorder still runs (cutover_state.json is not cutover)'; exit 4 }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\ext_record_$day.log"
Add-Content $log "== $(Get-Date -Format s) rec_ext record --until $Until --cffex-seconds $CffexSeconds"
& $py -m pulse.workers.rec_ext record --date $day --until $Until --cffex-seconds $CffexSeconds *>> $log
if ($LASTEXITCODE -ne 0) { Add-Content $log "!! rec_ext failed exit $LASTEXITCODE" }
Add-Content $log "== $(Get-Date -Format s) done"
exit $LASTEXITCODE
