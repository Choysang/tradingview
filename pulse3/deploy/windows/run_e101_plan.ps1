# E101 plan reports (pre-registration 2026-11-25 section 5): recorded only, never a trading instruction.
# Scheduled tasks Pulse3E101PlanR (weekdays 09:00, -Kind r) and Pulse3E101PlanD0 (weekdays 14:30, -Kind d0).
# Worker exit codes: 0 written; 3 no US release has this report day (most days, not a failure); 4 the previous session's bars are not ready
# (retried every 10 minutes, 3 more times, then logged as a failure); 2 a step failed.
param([ValidateSet('d0', 'r')][string]$Kind = 'd0')
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\e101_plan_$day.log"
$code = 2
for ($try = 1; $try -le 4; $try++) {
    Add-Content $log "== $(Get-Date -Format s) e101 plan $Kind (try $try)"
    & $py -m pulse.workers.e101 plan $Kind --date $day --out-dir "$root\var\reports\e101" *>> $log
    $code = $LASTEXITCODE
    if ($code -ne 4) { break }
    if ($try -lt 4) { Add-Content $log "-- bars not ready (exit 4), retry in 10 minutes"; Start-Sleep -Seconds 600 }
}
if ($code -eq 3) { Add-Content $log "-- no US release has this report day (exit 3)"; exit 0 }
if ($code -ne 0) { Add-Content $log "!! e101 plan $Kind failed exit $code" }
exit $code
