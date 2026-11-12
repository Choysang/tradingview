# pulse3 web/API on 127.0.0.1:8200 (scheduled task Pulse3Api, at startup).
. C:\pulse3\pulse3_env.ps1
Set-Location "$root\app"
& "$root\app\.venv\Scripts\python.exe" -m uvicorn pulse.api.app:app --host 127.0.0.1 --port 8200 *>> "$root\logs\api.log"
