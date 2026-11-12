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
if (Test-Path 'C:\pulse\secrets\openai.key') { $env:PULSE_OPENAI_KEY_FILE = 'C:\pulse\secrets\openai.key' }
$uv = 'C:\pulse\tools\uv.exe'
