# Shared environment for pulse3 processes on the server. Dot-source it: . C:\pulse3\pulse3_env.ps1
# The DB password is read from the ACL-restricted secrets folder at run time and never written elsewhere.
$root = 'C:\pulse3'
$pw = (Get-Content "$root\secrets\pg_superuser.txt" -Raw).Trim()
$env:PULSE_DSN = "host=127.0.0.1 port=55432 dbname=pulse user=postgres password=$pw"
$env:PULSE_LAKE = "$root\var\lake"
$env:PULSE_API_PORT = '8200'
# Single-user site reachable only through the SSH tunnel to 127.0.0.1 (loopback-only bind); one fixed principal owns
# layouts/notes. The API refuses PULSE_DEV_PRINCIPAL unless PULSE_ENV=dev AND PULSE_API_HOST is a loopback address.
$env:PULSE_ENV = 'dev'
$env:PULSE_API_HOST = '127.0.0.1'
$env:PULSE_DEV_PRINCIPAL = '00000000-0000-0000-0000-000000000001'
# Public access goes through Caddy (HTTPS on 443, task Pulse3Caddy) to 127.0.0.1:8200. Every proxied request must pass the site
# login (pulse/api/site_auth.py); without a password file the gate answers 503. Set the password with set_site_password.ps1.
$env:PULSE_SITE_PASSWORD_FILE = "$root\secrets\site_password.txt"
$env:PULSE_SITE_SECRET_FILE = "$root\secrets\site_secret.txt"
$env:PYTHONUTF8 = '1'
# M6 cutover state (cutover.ps1 writes it): after the cutover the run scripts skip the imports of the old recorder's files,
# because pulse3's own recorders write the same datasets and one day must come from one recorder only.
$cutover = $false
if (Test-Path "$root\cutover_state.json") { $cutover = ((Get-Content "$root\cutover_state.json" -Raw | ConvertFrom-Json).state -eq 'cutover') }
# Retirement of the old system (retire_legacy.ps1 writes legacy_state.json): 'deleting' and 'retired' mean C:\pulse is archived and
# gone, so nothing below may point into it.
$retired = $false
if (Test-Path "$root\legacy_state.json") { $retired = ((Get-Content "$root\legacy_state.json" -Raw | ConvertFrom-Json).state -in 'deleting', 'retired') }
# LEGACY-BEGIN: the ONLY references to the old install in the pulse3 scripts. They exist until the old system is retired.
if (-not $retired) {
    $env:PULSE_LEGACY_WAREHOUSE = 'C:\pulse\app\backend\data\warehouse\live'                       # legacy-import, lhb_compare, cap snapshot (legacy refdata)
    if (-not $env:PULSE_LEGACY_DATA) { $env:PULSE_LEGACY_DATA = 'C:\pulse\data' }                  # old recorder files (skipped after the cutover)
    if (-not $env:PULSE_FWDLOG_EVENTS) { $env:PULSE_FWDLOG_EVENTS = 'C:\pulse\app\backend\data\calendar\events.jsonl' }   # old calendar store (live)
} else {
    foreach ($v in 'PULSE_LEGACY_WAREHOUSE', 'PULSE_LEGACY_DATA') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    $env:PULSE_LEGACY_RETIRED = '1'                                                                 # python: skip comparisons with the old system's files
    $env:PULSE_FWDLOG_EVENTS = "$root\var\legacy_calendar\events.jsonl"                             # frozen copy made by retire_legacy.ps1
    $env:PULSE_CATALYST_SOURCE = 'native'                                                           # the old store stopped growing: M3 reads pulse3's calendar
}
# LEGACY-END
# Reference-data source (refdata_switch.ps1 writes it): 'native' reads pulse3's own bars/industry, else the legacy warehouse.
if (Test-Path "$root\refdata_source.txt") { $rs = "$(Get-Content "$root\refdata_source.txt" -Raw)".Trim(); if ($rs) { $env:PULSE_REFDATA_SOURCE = $rs } }   # an empty file leaves it unset (python: legacy; run_daily stops when retired)
# Cross-process pacing per host (<= 1 request/s even when two recorders share a host): every pulse3 process uses the same dir.
$env:PULSE_HOST_PACING_DIR = "$root\var\pacing"
if (-not (Test-Path $env:PULSE_HOST_PACING_DIR)) { New-Item -ItemType Directory -Force $env:PULSE_HOST_PACING_DIR | Out-Null }
if ($cutover) {
    $env:PULSE_SYNC_EXECUTOR = '1'                                              # health-page sync runs execute only after the cutover
    if (Test-Path "$root\secrets\push_webhook.url") { $env:PULSE_PUSH_WEBHOOK_FILE = "$root\secrets\push_webhook.url" }   # phone push moves from the old PulseNewsAlerts
}
# Key and tools live under C:\pulse3 only (key: set_openai_key.ps1; tools: the copies made once from the old install, 2026-11).
# The fallbacks to the C:\pulse copies are gone: the retirement deletes them.
if (Test-Path "$root\secrets\openai.key") { $env:PULSE_OPENAI_KEY_FILE = "$root\secrets\openai.key" }
$tools = "$root\tools"
$pyrel = 'python\cpython-3.13.15-windows-x86_64-none\python.exe'
$uv = "$tools\uv.exe"
$basepy = "$tools\$pyrel"
