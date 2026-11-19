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
# 600 calendar days (about 400 sessions): the fusion scorer's longest expression window needs it (BF_FUSION.md).
$since = (Get-Date).AddDays(-600).ToString('yyyy-MM-dd')
$ok = Step 'legacy-import' @('-m', 'pulse.workers.ingest', 'legacy-import', '--since', $since)
# Raw (execution-grade) daily bars for the whole universe: ~3,200 Sina requests at 1 request/s (about an hour).
# Without them the new research session has no raw closes and the build refuses (RawCoverageMissing).
if ($ok) { $ok = Step 'live-daily-universe' @('-m', 'pulse.workers.ingest', 'live-daily', '--universe', '--source', 'sina', '--count', '10') }
if ($ok -and -not $cutover) { $null = Step 'intel-news' @('-m', 'pulse.workers.intel', 'news', '--source', 'both', '--pages', '2') }
if ($ok) { $null = Step 'intel-claims' @('-m', 'pulse.workers.intel', 'claims') }
if ($ok) { $null = Step 'intel-seats' @('-m', 'pulse.workers.intel', 'seats', '--days', '2') }
if ($ok) { $ok = Step 'analysis-daily' @('-m', 'pulse.workers.analysis', 'daily') }
# M5-E04 forward record: settle the previous session and decide the next one from the release just published.
if ($ok) { $null = Step 'forward-daily' @('-m', 'pulse.workers.forward', 'daily') }
# Backfill #169: rank IC / top-decile excess of every published ranking whose horizon is now observed (gross, append-only; pending horizons stay pending).
if ($ok) { $null = Step 'forward-monitor' @('-m', 'pulse.workers.forward', 'monitor') }
# Legacy #135: after-close summary of the release just published (once per release; nothing new if rerun).
if ($ok) { $null = Step 'daily-report' @('-m', 'pulse.workers.alerts', 'daily-report') }
# Ranking outcomes (#12/#94 track record): resolve published rankings whose horizon is now observed (append-only).
if ($ok) { $null = Step 'outcomes' @('-m', 'pulse.workers.analysis', 'outcomes') }
# Calendar (#114/#115): Eastmoney financial calendar, <= 1 request/s, a few requests; events derived from announcement
# titles; index-futures and option expiries for this month and the next.
$null = Step 'calendar-sync' @('-m', 'pulse.workers.alerts', 'calendar-sync', '--days', '60')
$null = Step 'calendar-derive' @('-m', 'pulse.workers.alerts', 'calendar-derive')
# The old system's calendar store as a declared pulse3 source (until the cutover; read by the fwdlog M3 catalyst rule).
$null = Step 'calendar-legacy' @('-m', 'pulse.workers.fillm', 'import-calendar')
$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
foreach ($m in @($cst, $cst.AddMonths(1))) { $null = Step "calendar-expiries-$($m.ToString('yyyy-MM'))" @('-m', 'pulse.workers.alerts', 'calendar-expiries', '--year', "$($m.Year)", '--month', "$($m.Month)") }
# Fund flow of the focus set (plan instruments + holdings, at most 60 codes; the old system does not crawl it).
if ($ok) { $null = Step 'fundflow' @('-m', 'pulse.workers.intel', 'fundflow', '--focus', '60') }
# Fusion main book (batch 3, E012/E022 static40_safe + books E036/E040/E065/E068/E081/E085): last, about 13 CPU minutes.
if ($ok) { $null = Step 'fusion-daily' @('-m', 'pulse.workers.fusion', 'daily') }
# Factor series (#37/#80) for completed factor experiments that lack one; executed by Pulse3Jobs.
$null = Step 'fillr-factor-series' @('-m', 'pulse.workers.fillr', 'enqueue-factor-series')
Add-Content $log "== $(Get-Date -Format s) done ok=$ok"
