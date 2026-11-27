# pulse3 Nexus 2026 intelligence cross-verification, real-time RAPTOR adjustment & Swiss-Glicko2 Arena runner.
# Runs safely with per-host isolated RateLimitedClient (<= 1 req/s/host, stop immediately on 403/429/456/501).
# Modes:
#   -Mode intel   (every 15 min 08:30-21:00 weekdays after cutover; cross-verifies Xinhua/CNINFO/CLS/EM/THS + computes RAPTOR tilts)
#   -Mode arena   (after close / weekly; runs real GIPS-AShare-2026-v1 backtests & Swiss-Glicko2 promotion + AlphaPROBE discovery)
#   -Mode audit   (integrity & 7-entrypoint accounting parity check)
#   -Offline      (cross-verifies existing DB documents only without issuing outbound HTTP requests)
param(
    [ValidateSet('intel', 'arena', 'discover', 'audit', 'all')]
    [string]$Mode = 'intel',
    [switch]$Offline,
    [int]$Rounds = 5
)

$envFile = 'C:\pulse3\pulse3_env.ps1'
if (Test-Path $envFile) {
    . $envFile
    Set-Location "$root\app"
    $py = "$root\app\.venv\Scripts\python.exe"
    $logDir = "$root\logs"
} else {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    Set-Location $repoRoot
    $py = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $logDir = Join-Path $repoRoot 'var\logs'
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $cutover = $true
}

if (-not $Offline -and $Mode -in @('intel', 'all') -and -not $cutover) {
    Write-Output 'refused: live poll-intel waits for cutover_state.json = cutover (use -Offline to cross-verify stored docs)'
    exit 4
}

$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
$day = $cst.ToString('yyyy-MM-dd')
$log = Join-Path $logDir "nexus_${Mode}_${day}.log"

function Run-Step($label, [string[]]$argv) {
    Add-Content $log "== $(Get-Date -Format s) nexus $label"
    & $py @argv *>> $log
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Add-Content $log "!! nexus $label exited with $code"
    }
    return $code
}

$exitCode = 0
if ($Mode -in @('intel', 'all')) {
    $args = @('-m', 'pulse.workers.nexus', 'poll-intel')
    if ($Offline) { $args += '--offline' }
    $c = Run-Step 'poll-intel' $args
    if ($c -ne 0) { $exitCode = $c }
    $c2 = Run-Step 'realtime-adjust' @('-m', 'pulse.workers.nexus', 'realtime-adjust')
    if ($c2 -ne 0 -and $exitCode -eq 0) { $exitCode = $c2 }
}

if ($Mode -in @('arena', 'all')) {
    $c = Run-Step 'run-arena' @('-m', 'pulse.workers.nexus', 'run-arena', '--rounds', "$Rounds")
    if ($c -ne 0 -and $exitCode -eq 0) { $exitCode = $c }
}

if ($Mode -in @('discover', 'all')) {
    $c = Run-Step 'discover-strategies' @('-m', 'pulse.workers.nexus', 'discover-strategies')
    if ($c -ne 0 -and $exitCode -eq 0) { $exitCode = $c }
}

if ($Mode -in @('audit', 'all')) {
    $c = Run-Step 'audit-integrity' @('-m', 'pulse.workers.nexus', 'audit-integrity')
    if ($c -ne 0 -and $exitCode -eq 0) { $exitCode = $c }
}

Add-Content $log "== $(Get-Date -Format s) nexus mode=$Mode done exit=$exitCode"
exit $exitCode
