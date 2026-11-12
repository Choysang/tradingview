# pulse3 web/API on 127.0.0.1:8200 (scheduled task Pulse3Api, at startup).
# Supervisor loop: the task only restarts on its own failure, so a killed or crashed uvicorn is restarted here
# (M5-E02 drill: a killed API stayed down). Backoff grows to 60 s while it keeps failing fast.
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
$wait = 5
while ($true) {
    $t0 = Get-Date
    Add-Content "$root\logs\api.log" "== $(Get-Date -Format s) start uvicorn"
    & "$root\app\.venv\Scripts\python.exe" -m uvicorn pulse.api.app:app --host 127.0.0.1 --port 8200 *>> "$root\logs\api.log"
    Add-Content "$root\logs\api.log" "== $(Get-Date -Format s) uvicorn exited $LASTEXITCODE; restart in $wait s"
    if (((Get-Date) - $t0).TotalSeconds -gt 300) { $wait = 5 } else { $wait = [Math]::Min(60, $wait * 2) }
    Start-Sleep -Seconds $wait
}
