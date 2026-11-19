# pulse3 intraday tape (scheduled task Pulse3Tape, weekdays every 5 minutes 09:30-15:05 Asia/Shanghai).
# actionability: one Sina quote snapshot of the current release's top entries (<= 1 request/s, stops on a block, a
# few requests per run); tape: diffs consecutive quote snapshots into the market tape log (no request, idempotent).
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
$ok = Step 'actionability' @('-m', 'pulse.workers.analysis', 'actionability')
if ($ok) { $null = Step 'tape' @('-m', 'pulse.workers.analysis', 'tape') }
