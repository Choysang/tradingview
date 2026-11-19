# pulse3 weekly research refresh (scheduled task Pulse3Weekly, Saturday 09:00 Asia/Shanghai).
# Re-runs the playbook and factor-lab catalog (52 playbooks + 6 factors) on the data of the week, so the playbook
# board, the factor tests and the exit studies are never older than a week. No network request.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$py = "$root\app\.venv\Scripts\python.exe"
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time').ToString('yyyy-MM-dd')
$log = "$root\logs\weekly_$day.log"
Add-Content $log "== $(Get-Date -Format s) ext-b-run"
& $py -m pulse.workers.research ext-b-run --key "weekly-$day" *>> $log
if ($LASTEXITCODE -ne 0) { Add-Content $log "!! ext-b-run failed exit $LASTEXITCODE" }
Add-Content $log "== $(Get-Date -Format s) done"
