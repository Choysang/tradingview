# pulse3 NATIVE intraday recorder (main board: call auction, 5-level book, limit queues, hot subset; Tencent primary, optional Sina fallback).
# NOT REGISTERED anywhere: it is switched on only at the M6 cutover by the cutover script, together with the retirement of the legacy import
# (run_intraday_import.ps1) - the old recorder and this one must never run on the same server IP at the same time (two systems would double
# the requests to Sina / Tencent).
# Vendors: Tencent (qt.gtimg.cn, the old recorder's vendor) is primary because only it carries every legacy column (outer/inner lots,
# turnover, caps, volume ratio, vwap, vendor limit prices, after-hours fields). There is NO fallback by default: hq.sinajs.cn already answered
# 403 to the server IP on 2026-11-19 (Pulse3Tape), so a second host is not assumed to be available. A block of Tencent stops the run (exit 2)
# and keeps what was recorded. To enable the opt-in Sina fallback (columns it lacks are NULL, limit prices from the price-limit rules) add
# '--fallback sina' to the command below once Sina is known to be reachable from the server. Cost of the limit of 1 request/s per host:
# Tencent takes 80 symbols per request, so a full board (~3100 codes) takes about 39 s, the hot polls (every 20 s since the 2026-11-24 decision, the old recorder: 10 s) are cut by their 8 s budget (about
# 640 names) and slots lost are counted in the run summary (truncated_hot / missed_hot). The old recorder ran 10 requests/s; this is accepted.
# Measured 2026-11-23/24 with a 10 s hot grid: about half of the hot slots lost, 1.09M rows against the old 1.40M (76 requests/min needed, 60 available);
# decision 2026-11-24: hot every 20 s (58 requests/min), batch 80 and 1 request/s unchanged (see intraday_rows.HOT_EVERY_SECONDS). '--batch N' (symbols
# per request, default 80) stays an opt-in lever; it is NOT set below until Tencent's maximum is probed once by hand.
# Another process of this server that also calls qt.gtimg.cn (the extended-intraday recorder) shares its request budget when
# PULSE_HOST_PACING_DIR is set to one common directory for all of them (see adapters/sources/http.py); the cutover script should set it for every recorder.
# Intended start: weekdays 09:12 Asia/Shanghai (the process waits for 09:15). It ends at 15:33, after the after-hours fixed-price session.
# On a holiday it stops itself at the first full board after the auction (quotes not stamped today) and publishes nothing; at the weekend
# the script does not start it at all.
# Exit codes: 4 refused (before the cutover); of the recorder: 0 ok, 2 every quote source blocked or failing (it stopped at once and published what it had), 3 unreachable.
# After a normal end it derives the seal facts of the day (incremental and idempotent), like run_intraday_import.ps1 does for the legacy files.
. C:\pulse3\pulse3_env.ps1
# refuse before the cutover: the old recorder still runs and two recorders would double the requests from this IP (pulse3_env.ps1 sets $cutover)
if (-not $cutover) { 'refused: the old recorder still runs (cutover_state.json is not cutover)'; exit 4 }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
if ($cst.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { exit 0 }
$day = $cst.ToString('yyyy-MM-dd')
$log = "$root\logs\intraday_record_$day.log"
Add-Content $log "== $(Get-Date -Format s) intraday-record start"
& $py -m pulse.workers.ingest intraday-record --universe --until 15:33 --source tencent *>> $log
$code = $LASTEXITCODE
Add-Content $log "== $(Get-Date -Format s) intraday-record exit $code"
if ($code -eq 0 -or $code -eq 2) {
    # exit 2 still published the part before the block; the facts are derived from whatever the dataset holds
    & $py -m pulse.workers.ingest seal-facts --date $day *>> $log
    Add-Content $log "== $(Get-Date -Format s) seal-facts exit $LASTEXITCODE"
}
exit $code
