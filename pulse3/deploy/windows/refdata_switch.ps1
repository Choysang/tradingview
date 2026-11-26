# Switch pulse3's reference data (adjusted and index bars, industry membership) from the old warehouse to its own native
# datasets (LEGACY_TRIAGE step 2, rows 108/112/113). Dry run by default.
#   powershell -File C:\pulse3\refdata_switch.ps1                    # preflight + what would change
#   powershell -File C:\pulse3\refdata_switch.ps1 -Apply             # only with the user's approval
#   powershell -File C:\pulse3\refdata_switch.ps1 -Rollback -Apply   # back to the legacy warehouse
# Why: the old site's daily warehouse sync and pulse3's own fetches request the same Sina bars; after this switch pulse3
# keeps its bars current itself (Pulse3RefdataDaily weekdays 16:30, Pulse3RefdataWeekly Saturday 06:00) and the old sync
# can be retired. The choice is the file C:\pulse3\refdata_source.txt ('native' or 'legacy'), read by pulse3_env.ps1.
# Invariants: preflight refuses when the native adjusted or index dataset is missing (refdata reconcile says so); every
# change is reversible (tasks disabled, not deleted); the API restarts so it reads the new choice.
param([switch]$Apply, [switch]$Rollback)
$ErrorActionPreference = 'Stop'
. C:\pulse3\pulse3_env.ps1
$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$choice = "$root\refdata_source.txt"
$tasks = @(
    @{ Name = 'Pulse3RefdataDaily';  Mode = 'daily';  Days = @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'); At = '16:30'; Hours = 2 },
    @{ Name = 'Pulse3RefdataWeekly'; Mode = 'weekly'; Days = @('Saturday'); At = '06:00'; Hours = 3 }
)
function Restart-Api {
    Stop-ScheduledTask -TaskName Pulse3Api -ErrorAction SilentlyContinue
    $p = Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue | Select -First 1
    if ($p) { Stop-Process -Id $p.OwningProcess -Force }
    Start-ScheduledTask -TaskName Pulse3Api
}

if ($Rollback -and $retired) { 'refused: the old system is retired (legacy_state.json), its warehouse no longer exists; the legacy refdata source cannot come back'; exit 1 }
if (-not $Rollback) {
    Set-Location "$root\app"
    $rep = & "$root\app\.venv\Scripts\python.exe" -m pulse.workers.refdata reconcile 2>&1 | Out-String
    $missing = [regex]::Matches($rep, 'native dataset missing').Count
    if ($rep -notmatch 'reconcile adjusted bars' -or $rep -match '== reconcile adjusted bars ==\s+note: native dataset missing' -or
        $rep -match '== reconcile index bars ==\s+note: native dataset missing') {
        "preflight failed: the native adjusted or index dataset is missing (run the backfill first)"; exit 1
    }
    "preflight ok ($missing dataset(s) still missing in the reconcile report; adjusted and index present)"
}
if (-not $Apply) {
    if ($Rollback) { "would: write legacy to $choice, disable $($tasks.Name -join ', '), restart the API" }
    else { "would: write native to $choice, register $($tasks.Name -join ', '), restart the API" }
    'dry run: nothing changed (add -Apply)'; exit 0
}
if ($Rollback) {
    'legacy' | Set-Content $choice -Encoding ASCII
    foreach ($t in $tasks) { if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName $t.Name | Out-Null; "done: disabled $($t.Name)" } }
} else {
    foreach ($t in $tasks) {
        if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) { Enable-ScheduledTask -TaskName $t.Name | Out-Null }
        else {
            $a = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File $root\run_refdata.ps1 -Mode $($t.Mode)"
            $tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $t.Days -At $t.At
            $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours $t.Hours) -StartWhenAvailable -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $t.Name -Action $a -Trigger $tr -Settings $s -User SYSTEM -RunLevel Highest | Out-Null
        }
        "done: $($t.Name) ($($t.Mode) $($t.At))"
    }
    'native' | Set-Content $choice -Encoding ASCII
}
Restart-Api
"refdata source: $((Get-Content $choice).Trim()) (API restarted)"
