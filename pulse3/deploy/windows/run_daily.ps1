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
# 800 calendar days (about 540 sessions): the fusion scorer needs 400 sessions, and 600 days gave only 399 on 2026-11-19
# (holidays), so the daily run refused with history_too_short (BF_FUSION.md).
$since = (Get-Date).AddDays(-800).ToString('yyyy-MM-dd')
$ok = Step 'legacy-import' @('-m', 'pulse.workers.ingest', 'legacy-import', '--since', $since)
$imported = $ok   # the fusion scorer needs only the research snapshot, not the release below
# With the native reference data on (refdata_switch.ps1), the research inputs come from pulse3's own datasets: a failed legacy
# import is logged but no longer stops the build.
if ($env:PULSE_REFDATA_SOURCE -eq 'native' -and -not $ok) { Add-Content $log '-- legacy-import failed; native reference data is on, continuing'; $ok = $true; $imported = $true }
# Raw (execution-grade) daily bars for the whole universe: ~3,200 Sina requests at 1 request/s (about an hour).
# Without them the new research session has no raw closes and the build refuses (RawCoverageMissing).
if ($ok) { $ok = Step 'live-daily-universe' @('-m', 'pulse.workers.ingest', 'live-daily', '--universe', '--source', 'sina', '--count', '10') }
if ($ok -and -not $cutover) { $null = Step 'intel-news' @('-m', 'pulse.workers.intel', 'news', '--source', 'both', '--pages', '2') }
if ($ok) { $null = Step 'intel-claims' @('-m', 'pulse.workers.intel', 'claims') }
if ($ok) { $null = Step 'intel-seats' @('-m', 'pulse.workers.intel', 'seats', '--days', '2') }
if ($ok) { $ok = Step 'analysis-daily' @('-m', 'pulse.workers.analysis', 'daily') }
# A source timeout (exit 3) on 2026-11-20 skipped every later step: retry the build once after 10 minutes.
if ($imported -and -not $ok -and $LASTEXITCODE -eq 3) { Start-Sleep -Seconds 600; $ok = Step 'analysis-daily-retry' @('-m', 'pulse.workers.analysis', 'daily') }
# M5-E04 forward record: settle the previous session and decide the next one from the release just published.
if ($ok) { $null = Step 'forward-daily' @('-m', 'pulse.workers.forward', 'daily') }
# Backfill #169: rank IC / top-decile excess of every published ranking whose horizon is now observed (gross, append-only; pending horizons stay pending).
if ($ok) { $null = Step 'forward-monitor' @('-m', 'pulse.workers.forward', 'monitor') }
# Legacy #135: after-close summary of the release just published (once per release; nothing new if rerun).
if ($ok) { $null = Step 'daily-report' @('-m', 'pulse.workers.alerts', 'daily-report') }
# Ranking outcomes (#12/#94 track record): resolve published rankings whose horizon is now observed (append-only).
if ($imported) { $null = Step 'outcomes' @('-m', 'pulse.workers.analysis', 'outcomes') }
# Calendar (#114/#115): Eastmoney financial calendar, <= 1 request/s, a few requests; events derived from announcement
# titles; index-futures and option expiries for this month and the next.
$null = Step 'calendar-sync' @('-m', 'pulse.workers.alerts', 'calendar-sync', '--days', '60')
$null = Step 'calendar-derive' @('-m', 'pulse.workers.alerts', 'calendar-derive')
# The old system's calendar store as a declared pulse3 source (until the cutover; read by the fwdlog M3 catalyst rule).
$null = Step 'calendar-legacy' @('-m', 'pulse.workers.fillm', 'import-calendar')
$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
foreach ($m in @($cst, $cst.AddMonths(1))) { $null = Step "calendar-expiries-$($m.ToString('yyyy-MM'))" @('-m', 'pulse.workers.alerts', 'calendar-expiries', '--year', "$($m.Year)", '--month', "$($m.Month)") }
# Fund flow of the focus set (plan instruments + holdings, at most 60 codes; the old system does not crawl it).
# Guba samples (#136): the old news recorder sampled them; after the cutover pulse3 samples the focus set itself (same volume).
if ($cutover) { $null = Step 'community' @('-m', 'pulse.workers.intel', 'community', '--focus', '60') }
if ($imported) { $null = Step 'fundflow' @('-m', 'pulse.workers.intel', 'fundflow', '--focus', '60') }
# Fusion main book (batch 3, E012/E022 static40_safe + books E036/E040/E065/E068/E081/E085): last, about 13 CPU minutes.
if ($imported) { $null = Step 'fusion-daily' @('-m', 'pulse.workers.fusion', 'daily') }
# Factor series (#37/#80) for completed factor experiments that lack one; executed by Pulse3Jobs.
$null = Step 'fillr-factor-series' @('-m', 'pulse.workers.fillr', 'enqueue-factor-series')
# E092 live web-search LLM judgement (paid, pre-registered; caps and the 20-day stop are in the worker). Needs the fusion scores of today, so it runs after fusion-daily; asks nothing unless the switch file C:\pulse3\var\llm_live.enabled exists.
$day = $cst.ToString('yyyy-MM-dd')   # the China-time trading day of this run ($cst above), as run_fwdlog.ps1 passes it
if ($imported) { $null = Step 'llm-live-decide' @('-m', 'pulse.workers.llm_live', 'decide', '--date', $day) }
# E092 outcomes: score the answers whose 5-day window has closed (append-only; nothing to do on most days).
if ($imported) { $null = Step 'llm-live-track' @('-m', 'pulse.workers.llm_live', 'track', '--today', $day) }
Add-Content $log "== $(Get-Date -Format s) done ok=$ok"
