# Public HTTPS reverse proxy for the pulse3 site (scheduled task Pulse3Caddy, at startup): 443 -> 127.0.0.1:8200.
# The public address is read from C:\pulse3\caddy\public_host.txt (server only, never in the repository).
# Supervisor loop like run_api.ps1: a crashed caddy is restarted with a growing backoff.
$root = 'C:\pulse3'
$exe = "$root\tools\caddy\caddy.exe"
$hostFile = "$root\caddy\public_host.txt"
if (-not (Test-Path $exe) -or -not (Test-Path $hostFile)) { Add-Content "$root\logs\caddy_run.log" "$(Get-Date -Format s) missing $exe or $hostFile"; exit 2 }
$env:PULSE_PUBLIC_HOST = (Get-Content $hostFile -Raw).Trim()
$env:XDG_DATA_HOME = "$root\caddy\data"
$env:XDG_CONFIG_HOME = "$root\caddy\config"
$wait = 5
while ($true) {
    $t0 = Get-Date
    Add-Content "$root\logs\caddy_run.log" "== $(Get-Date -Format s) start caddy"
    & $exe run --config "$root\Caddyfile" --adapter caddyfile *>> "$root\logs\caddy_run.log"
    Add-Content "$root\logs\caddy_run.log" "== $(Get-Date -Format s) caddy exited $LASTEXITCODE; restart in $wait s"
    if (((Get-Date) - $t0).TotalSeconds -gt 300) { $wait = 5 } else { $wait = [Math]::Min(60, $wait * 2) }
    Start-Sleep -Seconds $wait
}
