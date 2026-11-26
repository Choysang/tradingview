# pulse3 intraday import (scheduled task Pulse3IntradayImport, weekdays 15:45 Asia/Shanghai).
# Until the M6 cutover the OLD system records every session; pulse3 only READS those files (no second recorder, no extra requests to
# Sina/Tencent). Order: main-board snapshots -> estimated seal facts -> extended intraday (ETF, index futures, A50, CNH).
# Each command is incremental and idempotent: re-running a day with nothing new publishes nothing. A failed step is logged and the
# later steps still run (the seal facts need the snapshots, so they are skipped when that step failed).
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
# the previous calendar day too: files written after the last run of that day (22:00 cut, night sessions) are picked up;
# every import is incremental and idempotent, so re-reading a finished day adds nothing
$prev = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow.AddDays(-1), 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\intraday_import_$day.log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE"; return $false }
    return $true
}
# After the cutover the native recorder publishes the snapshots itself: only the seal facts are derived here.
$ok = if ($cutover) { $true } else { Step 'legacy-intraday' @('-m', 'pulse.workers.ingest', 'legacy-intraday', '--date', $day) }
if ($ok) { $null = Step 'seal-facts' @('-m', 'pulse.workers.ingest', 'seal-facts', '--date', $day) }
if (-not $cutover) {
$null = Step 'legacy-ext-prev' @('-m', 'pulse.workers.ingest', 'legacy-ext', '--date', $prev)
$null = Step 'legacy-ext' @('-m', 'pulse.workers.ingest', 'legacy-ext', '--date', $day)
}
Add-Content $log "== $(Get-Date -Format s) done"
