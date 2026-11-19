# pulse3 forward experiment logs (scheduled task Pulse3ForwardLog, weekdays 16:45 Asia/Shanghai; batch 3, fwdlog lane).
# The old recorder writes its done marker at about 16:20 (the extended recorder at 16:00), so the session is imported again
# here once it is complete (the 15:45 import may have seen a partial day); every import is incremental and idempotent.
# Then E077/E078, E089 and the paper replay (triggers v2.1) are computed for the session. Exit codes of fwdlog run:
# 0 ok, 2 a session failed, 3 no recordings imported. A failed step is logged and later steps still run.
. C:\pulse3\pulse3_env.ps1
if (-not $env:PULSE_LEGACY_DATA) { $env:PULSE_LEGACY_DATA = 'C:\pulse\data' }
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\fwdlog_$day.log"
function Step($name, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) $name"
    & $py @argv *>> $log
    if ($LASTEXITCODE -ne 0) { Add-Content $log "!! $name failed exit $LASTEXITCODE"; return $false }
    return $true
}
$ok = Step 'legacy-intraday' @('-m', 'pulse.workers.ingest', 'legacy-intraday', '--date', $day)
if ($ok) { $null = Step 'seal-facts' @('-m', 'pulse.workers.ingest', 'seal-facts', '--date', $day) }
$null = Step 'legacy-ext' @('-m', 'pulse.workers.ingest', 'legacy-ext', '--date', $day)
$null = Step 'fwdlog-run' @('-m', 'pulse.workers.fwdlog', 'run', '--date', $day)
# Batch 6 forward (E095 auction, E096 active flow; pre-registration 2026-11-18): exit 3 = no complete recording yet.
$null = Step 'batch6-forward' @('-m', 'pulse.workers.batch6', 'forward', '--date', $day)
Add-Content $log "== $(Get-Date -Format s) done"
