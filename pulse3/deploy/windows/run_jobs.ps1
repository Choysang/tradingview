# pulse3 queued-job runner (scheduled task Pulse3Jobs, every 5 minutes, all day).
# Commands from the web (recompute, calendar sync, plan proposals, exports, experiments, screens, tactic replays, batch
# comparisons) only enqueue a job; this runner executes them. Each worker claims with a lease, so a run that overlaps
# the API's own background execution never runs a job twice. An empty queue costs a few database queries.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\jobs_$day.log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE" }
}
Step 'alerts-drain' @('-m', 'pulse.workers.alerts', 'drain', '--max', '20')
Step 'research-run' @('-m', 'pulse.workers.research', 'run', '--max', '10')
Step 'screen-run' @('-m', 'pulse.workers.research', 'screen-run', '--max', '5')
Step 'tactic-run' @('-m', 'pulse.workers.research', 'tactic-run', '--max', '5')
Step 'lab-batch' @('-m', 'pulse.workers.lab', 'run-batch', '--max', '5')
Step 'fillr-run' @('-m', 'pulse.workers.fillr', 'run', '--max', '5')   # custom runs, factor series, analog studies
