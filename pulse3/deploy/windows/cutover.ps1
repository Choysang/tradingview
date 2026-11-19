# M6 cutover (step 2 of the 2026-11-18 audit): the old system's recorders stop, pulse3's own recorders start.
#   powershell -File C:\pulse3\cutover.ps1                    # dry run: preflight + every change printed, nothing changed
#   powershell -File C:\pulse3\cutover.ps1 -Apply             # does it; run only after the user approved the cutover
#   powershell -File C:\pulse3\cutover.ps1 -Rollback -Apply   # undo: old recorders back on, native recorders off
# Why: two recorders on one IP double the requests to Tencent/Sina/CLS/Eastmoney and risk a block for both, so the switch is
# one step. The old recording tasks are disabled (never deleted; C:\pulse and its data are not touched), their python
# processes are stopped, then the native tasks are registered. The import tasks keep running: with the state file saying
# 'cutover' (C:\pulse3\cutover_state.json, read by pulse3_env.ps1) they skip the old files and only derive seal facts, scan
# and deliver alerts, and run the forward logs.
# Invariants:
# * preflight first: all four old task names exist, the native scripts are deployed, pulse3's phone push address exists
#   (C:\pulse3\secrets\push_webhook.url; -NoPush accepts losing phone pushes), and the time is outside weekday 07:30-22:10
#   Asia/Shanghai (the native news task starts at 07:55, the old recorders run until 22:00), so no session day is half old, half native;
# * the state file says 'switching' while tasks change; any error rolls every change back and writes 'failed';
# * the end state is verified: old tasks Disabled with no python process left under C:\pulse running a recorder.
# Not covered: retiring the old web app or its warehouse (still read by legacy-import until refdata replaces it).
param([switch]$Apply, [switch]$Rollback, [switch]$AnyTime, [switch]$NoPush)
$ErrorActionPreference = 'Stop'
. C:\pulse3\pulse3_env.ps1
$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$stateFile = "$root\cutover_state.json"
$oldRecorders = 'PulseIntraday', 'PulseIntradayExt', 'PulseNews', 'PulseNewsAlerts'
$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'
$native = @(
    @{ Name = 'Pulse3IntradayRecord'; Script = 'run_intraday_record.ps1'; At = '09:10'; Hours = 7; Every = 0 },
    @{ Name = 'Pulse3ExtRecord';      Script = 'run_ext_record.ps1';      At = '08:55'; Hours = 8; Every = 0 },
    @{ Name = 'Pulse3NewsRecord';     Script = 'run_news_record.ps1';     At = '07:55'; Hours = 15; Every = 0 },
    @{ Name = 'Pulse3SyncJobs';       Script = 'run_sync_jobs.ps1';       At = '00:00'; Hours = 5; Every = 10 }
)

function Write-State([string]$s) {
    @{ state = $s; at = (Get-Date).ToString('s'); by = $env:USERNAME } | ConvertTo-Json | Set-Content $stateFile -Encoding UTF8
}
function Get-OldRecorderProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'uv.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'backend\.app\.warehouse\.(intraday|intraday_ext|news|news_alerts) run' }
    # the old recorders run as `-m backend.app.warehouse.<name> run`, each as a venv launcher plus a child base interpreter whose
    # path is outside C:\pulse (checked on the server 2026-11-19), so the module name, not the path, identifies them
}
function Register-Native($n) {
    if (Get-ScheduledTask -TaskName $n.Name -ErrorAction SilentlyContinue) { Enable-ScheduledTask -TaskName $n.Name | Out-Null; return }
    $a = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File $root\$($n.Script)"
    if ($n.Every -gt 0) {
        $tr = New-ScheduledTaskTrigger -Once -At $n.At -RepetitionInterval (New-TimeSpan -Minutes $n.Every) -RepetitionDuration (New-TimeSpan -Days 3650)
    } else {
        $tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $n.At
    }
    $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours $n.Hours) -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $n.Name -Action $a -Trigger $tr -Settings $s -User SYSTEM -RunLevel Highest | Out-Null
}
function Restart-Api {   # the API reads the cutover flags (sync executor) at start
    Stop-ScheduledTask -TaskName Pulse3Api -ErrorAction SilentlyContinue
    $p = Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue | Select -First 1
    if ($p) { Stop-Process -Id $p.OwningProcess -Force }
    Start-ScheduledTask -TaskName Pulse3Api
}
function To-Legacy {
    foreach ($n in $native) {
        if (Get-ScheduledTask -TaskName $n.Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $n.Name; Disable-ScheduledTask -TaskName $n.Name | Out-Null
        }
    }
    if (Get-ScheduledTask -TaskName Pulse3Tape -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName Pulse3Tape | Out-Null }
    foreach ($t in $oldRecorders) { if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Enable-ScheduledTask -TaskName $t | Out-Null } }
}

# ---- preflight (always, also in a dry run) ----
$problems = @()
foreach ($t in $oldRecorders) { if (-not (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue)) { $problems += "old task $t not found" } }
foreach ($n in $native) { if (-not (Test-Path "$root\$($n.Script)")) { $problems += "$root\$($n.Script) missing: deploy the round that ships it" } }
if (-not $Rollback -and -not $NoPush -and -not (Test-Path "$root\secrets\push_webhook.url")) {
    $problems += "no $root\secrets\push_webhook.url: phone pushes would stop with the old PulseNewsAlerts (create it, or pass -NoPush)"
}
$cst = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'China Standard Time')
if (-not $AnyTime -and $cst.DayOfWeek -notin 'Saturday', 'Sunday' -and $cst.TimeOfDay -ge [TimeSpan]'07:30' -and $cst.TimeOfDay -lt [TimeSpan]'22:10') {
    $problems += "it is $($cst.ToString('HH:mm')) on a weekday in Asia/Shanghai: run after 22:10, before 07:30 or at the weekend (or -AnyTime)"
}
if ($problems) { 'preflight failed:'; $problems | ForEach-Object { "  - $_" }; exit 1 }
'preflight ok'

if (-not $Apply) {
    if ($Rollback) {
        $native | ForEach-Object { "would: stop and disable $($_.Name)" }; 'would: disable Pulse3Tape'
        $oldRecorders | ForEach-Object { "would: enable $_" }
    } else {
        $oldRecorders | ForEach-Object { "would: stop and disable old task $_" }
        'would: stop python processes under C:\pulse running a recorder'
        $native | ForEach-Object { "would: register $($_.Name) ($($_.Script))" }; 'would: enable Pulse3Tape'
    }
    'dry run: nothing changed (add -Apply)'
    exit 0
}

if ($Rollback) {
    To-Legacy
    Write-State 'legacy'
    Restart-Api
    'state: legacy (the old recorder tasks start again at their next trigger)'
    exit 0
}

Write-State 'switching'
try {
    foreach ($t in $oldRecorders) { Stop-ScheduledTask -TaskName $t; Disable-ScheduledTask -TaskName $t | Out-Null; "done: disabled $t" }
    Start-Sleep -Seconds 5
    Get-OldRecorderProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "done: stopped old process $($_.ProcessId)" }
    foreach ($n in $native) { Register-Native $n; "done: registered $($n.Name)" }
    if (Get-ScheduledTask -TaskName Pulse3Tape -ErrorAction SilentlyContinue) { Enable-ScheduledTask -TaskName Pulse3Tape | Out-Null; 'done: enabled Pulse3Tape' }
    # verify the end state before declaring it
    foreach ($t in $oldRecorders) { if ((Get-ScheduledTask -TaskName $t).State -ne 'Disabled') { throw "old task $t is not Disabled" } }
    $left = @(Get-OldRecorderProcesses)
    if ($left.Count -gt 0) { throw "old recorder processes still running: $($left.ProcessId -join ', ')" }
    foreach ($n in $native) { if ((Get-ScheduledTask -TaskName $n.Name).State -eq 'Disabled') { throw "$($n.Name) is Disabled" } }
    Write-State 'cutover'
    Restart-Api
    'state: cutover (API restarted)'
} catch {
    "cutover failed: $($_.Exception.Message); rolling back"
    try { To-Legacy } catch { "rollback error: $($_.Exception.Message) - check the tasks by hand" }
    Write-State 'failed'
    exit 1
}
