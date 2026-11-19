# Health-page data sync runs (#49/#89), task Pulse3SyncJobs every 10 minutes, registered only by cutover.ps1 (the runs make
# live requests; before the cutover the old system fetches the same hosts from this IP). One run per start (--max 1); the
# task time limit (5 h) covers the longest sync kind; IgnoreNew keeps a second start from overlapping a long run.
. C:\pulse3\pulse3_env.ps1
if (-not $cutover) { 'refused: the old system still runs (cutover_state.json is not cutover)'; exit 4 }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\sync_jobs_$day.log"
Add-Content $log "== $(Get-Date -Format s) sync-jobs"
& $py -m pulse.workers.sync_jobs run --max 1 *>> $log
if ($LASTEXITCODE -ne 0) { Add-Content $log "!! sync-jobs exit $LASTEXITCODE" }
