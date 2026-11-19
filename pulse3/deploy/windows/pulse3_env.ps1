# Shared environment for pulse3 processes on the server. Dot-source it: . C:\pulse3\pulse3_env.ps1
# The DB password is read from the ACL-restricted secrets folder at run time and never written elsewhere.
$root = 'C:\pulse3'
$pw = (Get-Content "$root\secrets\pg_superuser.txt" -Raw).Trim()
$env:PULSE_DSN = "host=127.0.0.1 port=55432 dbname=pulse user=postgres password=$pw"
$env:PULSE_LAKE = "$root\var\lake"
$env:PULSE_LEGACY_WAREHOUSE = 'C:\pulse\app\backend\data\warehouse\live'
$env:PULSE_API_PORT = '8200'
# Single-user site reachable only through the SSH tunnel to 127.0.0.1 (loopback-only bind); one fixed principal owns
# layouts/notes. The API refuses PULSE_DEV_PRINCIPAL unless PULSE_ENV=dev AND PULSE_API_HOST is a loopback address.
$env:PULSE_ENV = 'dev'
$env:PULSE_API_HOST = '127.0.0.1'
$env:PULSE_DEV_PRINCIPAL = '00000000-0000-0000-0000-000000000001'
$env:PYTHONUTF8 = '1'
# M6 cutover state (cutover.ps1 writes it): after the cutover the run scripts skip the imports of the old recorder's files,
# because pulse3's own recorders write the same datasets and one day must come from one recorder only.
$cutover = $false
if (Test-Path "$root\cutover_state.json") { $cutover = ((Get-Content "$root\cutover_state.json" -Raw | ConvertFrom-Json).state -eq 'cutover') }
# Cross-process pacing per host (<= 1 request/s even when two recorders share a host): every pulse3 process uses the same dir.
$env:PULSE_HOST_PACING_DIR = "$root\var\pacing"
if (-not (Test-Path $env:PULSE_HOST_PACING_DIR)) { New-Item -ItemType Directory -Force $env:PULSE_HOST_PACING_DIR | Out-Null }
if ($cutover) {
    $env:PULSE_SYNC_EXECUTOR = '1'                                              # health-page sync runs execute only after the cutover
    if (Test-Path "$root\secrets\push_webhook.url") { $env:PULSE_PUSH_WEBHOOK_FILE = "$root\secrets\push_webhook.url" }   # phone push moves from the old PulseNewsAlerts
}
# Keys and tools live under C:\pulse3 (key: set_openai_key.ps1; tools: deploy_app.ps1 copies them once). The C:\pulse copies
# are read only as a fallback until the cutover retires the old system (#199/#203).
foreach ($k in "$root\secrets\openai.key", 'C:\pulse\secrets\openai.key') { if (Test-Path $k) { $env:PULSE_OPENAI_KEY_FILE = $k; break } }
$tools = "$root\tools"
$pyrel = 'python\cpython-3.13.15-windows-x86_64-none\python.exe'
$uv = if (Test-Path "$tools\uv.exe") { "$tools\uv.exe" } else { 'C:\pulse\tools\uv.exe' }
$basepy = if (Test-Path "$tools\$pyrel") { "$tools\$pyrel" } else { "C:\pulse\tools\$pyrel" }
