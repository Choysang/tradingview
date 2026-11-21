# pulse3 NATIVE reference-data producers: ratio-adjusted daily bars, index bars, board membership, listing dates.
# NOT REGISTERED anywhere, not even by cutover.ps1: its datasets are NEW ids (*_native_*), so consumers switch in a separate,
# later step (LEGACY_TRIAGE step 2) together with the retirement of the legacy warehouse import (the old system fetches the same data from the same server IP; two systems would double the requests to Sina/Tencent).
# Every step honours <= 1 request per second and stops at the first block (exit code 2: later steps are skipped, what was fetched is published).
# Never run it from the development PC: the full universe is thousands of requests.
#
# -Mode daily    (intended weekdays 16:10 Asia/Shanghai, after the 16:00 finality time of a daily bar)
#     index-bars (6 requests) then adjusted-bars --mode refresh (about universe/50 quote requests + 2 per ex-right / gap / new code).
# -Mode weekly   (intended Saturday morning)
#     listings (2 requests) then boards (about 1000 requests, resumable within the day, publishes only a complete snapshot).
#     The weekly run must have happened once before the first daily run: the daily refresh takes its universe from the listing dataset.
# -Mode backfill (manual, repeat until it reports nothing left): adjusted-bars --universe-listings --max-codes N, 2 requests per code,
#     resumable cursor; start it once after `weekly`, one batch of codes per run, and only on the server.
# Exit codes: 0 ok, 1 failure, 2 blocked, 3 unreachable (the first non-zero code of a step is the script's exit code).
param([ValidateSet('daily', 'weekly', 'backfill')][string]$Mode = 'daily', [int]$MaxCodes = 1200)
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
$day = $cst.ToString('yyyy-MM-dd')
if ($Mode -eq 'daily' -and $cst.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { exit 0 }
$log = "$root\logs\refdata_${Mode}_$day.log"
$final = 0
function Step($name, [string[]]$argv) {
    if ($script:final -eq 2) { Add-Content $log "-- $name skipped: an earlier step was blocked"; return }
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py -m pulse.workers.refdata @argv *>> $log
    $code = $LASTEXITCODE
    Add-Content $log "== $(Get-Date -Format s) $name exit $code"
    if ($code -ne 0 -and $script:final -eq 0) { $script:final = $code }
}
switch ($Mode) {
    'daily' {
        Step 'index-bars' @('index-bars', '--years', '3')
        Step 'adjusted-refresh' @('adjusted-bars', '--mode', 'refresh', '--universe-listings')
        # no HTTP: new listings, short-name / ST versions and the cap snapshot from the datasets above and the day's close recording
        if ($env:PULSE_REFDATA_SOURCE -eq 'native') { Step 'register-native' @('register-native') }
    }
    'weekly' {
        Step 'listings' @('listings')
        Step 'boards' @('boards')
    }
    'backfill' {
        Step 'adjusted-backfill' @('adjusted-bars', '--universe-listings', '--max-codes', "$MaxCodes")
    }
}
exit $final
