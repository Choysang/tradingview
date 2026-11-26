# pulse3 intraday tape (scheduled task Pulse3Tape, weekdays every 5 minutes 09:30-15:05 Asia/Shanghai).
# actionability: builds a quote snapshot from the native recorder's latest part (no request; exit 5 = no recording newer
# than 3 minutes while the market is trading, nothing published; outside the trading minutes (lunch 11:30-13:00, before the
# open, after the close, holidays) and in the first 3 minutes of a session the worker itself prints "market closed: ..." /
# "market opening: ..." and exits 0, so this script needs no clock of its own)
# for the release's top entries, holdings and watchlists (--scope focus: the whole board
# would add about 3,100 observations per run, 100-200 MB a day); tape: diffs consecutive snapshots into the market tape log.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\tape_$day.log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE"; return $false }
    return $true
}
$ok = Step 'actionability' @('-m', 'pulse.workers.analysis', 'actionability', '--scope', 'focus')
if ($ok) { $null = Step 'tape' @('-m', 'pulse.workers.analysis', 'tape') }
