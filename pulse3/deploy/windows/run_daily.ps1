# pulse3 after-close pipeline (scheduled task Pulse3Daily, weekdays 18:10 Asia/Shanghai).
# Order: legacy warehouse increment (old system keeps writing it) -> live raw daily + announcements ->
# intelligence (news, claims, seats) -> analysis release. Each step logs and a failure stops the release build,
# so no new READY release is built on incomplete inputs.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$log = "$root\logs\daily_$(Get-Date -Format yyyy-MM-dd).log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE"; return $false }
    return $true
}
# The research snapshot is exactly the imported window (not merged with earlier ones): keep it long enough for
# listing age, 250-session features and the chart window.
$since = (Get-Date).AddDays(-400).ToString('yyyy-MM-dd')
$ok = Step 'legacy-import' @('-m', 'pulse.workers.ingest', 'legacy-import', '--since', $since)
# Raw (execution-grade) daily bars for the whole universe: ~3,200 Sina requests at 1 request/s (about an hour).
# Without them the new research session has no raw closes and the build refuses (RawCoverageMissing).
if ($ok) { $ok = Step 'live-daily-universe' @('-m', 'pulse.workers.ingest', 'live-daily', '--universe', '--source', 'sina', '--count', '10') }
if ($ok) { $null = Step 'intel-news' @('-m', 'pulse.workers.intel', 'news', '--source', 'both', '--pages', '2') }
if ($ok) { $null = Step 'intel-claims' @('-m', 'pulse.workers.intel', 'claims') }
if ($ok) { $null = Step 'intel-seats' @('-m', 'pulse.workers.intel', 'seats', '--days', '2') }
if ($ok) { $ok = Step 'analysis-daily' @('-m', 'pulse.workers.analysis', 'daily') }
Add-Content $log "== $(Get-Date -Format s) done ok=$ok"
