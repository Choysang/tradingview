# pulse3 news import + alert scan (scheduled task Pulse3NewsImport, every 10 minutes 08:00-22:00 Asia/Shanghai on weekdays).
# The OLD system records the 7x24 feed continuously; pulse3 READS those files as the declared source legacy_news_recorder.
# Order: messages -> document revisions -> claims (inside legacy-news) -> pulse3's own alert scan (P0-P2 against pulse3 holdings
# and watch lists). The old system's alerts.jsonl is imported only as a comparison dataset, never into pulse3 alerts.
# Incremental and idempotent; if an earlier run is still going the task scheduler ignores the new start (IgnoreNew).
. C:\pulse3\pulse3_env.ps1
if (-not $env:PULSE_LEGACY_DATA) { $env:PULSE_LEGACY_DATA = 'C:\pulse\data' }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
# the previous calendar day too: files written after the last run of that day (22:00 cut, night sessions) are picked up;
# every import is incremental and idempotent, so re-reading a finished day adds nothing
$prev = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow.AddDays(-1), 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\news_import_$day.log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE"; return $false }
    return $true
}
# After the cutover the native news recorder stores documents and claims itself; only the scan and delivery run here.
if (-not $cutover) {
$null = Step 'legacy-news-prev' @('-m', 'pulse.workers.ingest', 'legacy-news', '--date', $prev)
$ok = Step 'legacy-news' @('-m', 'pulse.workers.ingest', 'legacy-news', '--date', $day)
}
# the scan reads claims only; running it after a failed import still picks up claims that earlier runs produced
$null = Step 'alerts-scan' @('-m', 'pulse.workers.alerts', 'scan')
# delivery: in-app always; webhook only when pulse3 has its own push address configured (during the parallel run the old
# system keeps pushing to the phone, so pulse3 leaves PULSE_PUSH_WEBHOOK unset to avoid duplicate messages)
$null = Step 'alerts-deliver' @('-m', 'pulse.workers.alerts', 'deliver')
if (-not $cutover) {
$null = Step 'legacy-guba-prev' @('-m', 'pulse.workers.ingest', 'legacy-guba', '--date', $prev)
$null = Step 'legacy-guba' @('-m', 'pulse.workers.ingest', 'legacy-guba', '--date', $day)
$null = Step 'legacy-alerts-prev-comparison' @('-m', 'pulse.workers.ingest', 'legacy-alerts', '--date', $prev)
$null = Step 'legacy-alerts-comparison' @('-m', 'pulse.workers.ingest', 'legacy-alerts', '--date', $day)
}
