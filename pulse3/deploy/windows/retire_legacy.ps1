# Retire the old system (user decision 2026-11-24: keep only pulse3). Dry run by default.
#   powershell -File C:\pulse3\retire_legacy.ps1                    # dry run: preflight + every change printed, nothing changed
#   powershell -File C:\pulse3\retire_legacy.ps1 -Apply -Detached   # does it, in a one-off SYSTEM task (RetireLegacyOnce); only after the user approved
#   powershell -File C:\pulse3\retire_legacy.ps1 -Rollback          # dry run of the rollback
#   powershell -File C:\pulse3\retire_legacy.ps1 -Rollback -Apply   # tasks back (before the rename only)
#   -Force accepts fewer than 10 consecutive SAME sessions of the LHB comparison (the only check it relaxes).
# -Apply takes minutes (tar, hashes) and must run DETACHED: a plain -Apply in an ssh session that drops is killed half-way (the failure
# handler cannot run then). -Detached checks everything in the foreground, registers the one-off task, starts it and exits; follow
# C:\pulse3\logs\retire_legacy_<date>.log and legacy_state.json. Remove the finished task with: Unregister-ScheduledTask RetireLegacyOnce -Confirm:$false
# What it does: the nine old scheduled tasks (PulseWeb, PulseLhbDaily, PulseLhbBackfill, PulsePaper, PulseForwardLab, PulseIntraday,
# PulseIntradayExt, PulseNews, PulseNewsAlerts) are exported (XML), stopped and disabled; C:\pulse is packed into ONE compressed archive
# C:\pulse3\backup\legacy_pulse_<date>.tgz (without .venv, node_modules, __pycache__ and C:\pulse\tools: all regenerable; SYSTEM and
# Administrators only, like the secrets folder), the archive is verified (sorted file list equal to the source list, sha256); then
# C:\pulse is renamed, the old tasks are unregistered and C:\pulse is deleted. State file: C:\pulse3\legacy_state.json, read by
# pulse3_env.ps1 (state 'deleting' or 'retired' switches every run script to the native data; see docs/m6/LEGACY_RETIREMENT.md).
# Invariants:
# * preflight first, also in a dry run; it refuses (exit 1) with a reason per failed check and changes nothing. It also refuses when
#   anything outside the old tasks still resolves into C:\pulse\: the venv's pyvenv.cfg, any other scheduled task (Execute, Arguments,
#   WorkingDirectory) or any service ImagePath (pulse3-pg included);
# * while it runs, Pulse3Daily, Pulse3ForwardLog, Pulse3IntradayImport and Pulse3NewsImport are disabled, and Pulse3IntradayRecord,
#   Pulse3ExtRecord, Pulse3NewsRecord too unless they are running at that moment (a running recorder is left alone). They are enabled
#   again in a finally block, on success and on failure. (Chosen instead of refusing near their NextRunTime.);
# * the old tasks are only DISABLED until C:\pulse has been renamed; they are unregistered after that, so a rollback before the rename
#   just enables them and never re-creates a task;
# * any failure BEFORE the rename restores the tasks, leaves C:\pulse as it was and writes state 'failed'. A partial archive is removed;
#   a VERIFIED archive is never deleted (state 'archive_kept');
# * C:\pulse is renamed first (a directory with open handles cannot be renamed, so a busy tree stops here, still intact), then removed;
# * nothing outside C:\pulse, the old tasks and these C:\pulse3 paths is touched: backup\legacy_pulse_<date>*, legacy_state.json,
#   var\legacy_calendar\events.jsonl, logs\retire_legacy_<date>.log. Other apps (OTN-AIOps :8000, :80, :8080) are never touched: processes
#   are selected by an image path or command line under C:\pulse\ only, and the one restart is pulse3's own API on :8200.
param([switch]$Apply, [switch]$Rollback, [switch]$Force, [switch]$Detached, [switch]$FromTask)
$ErrorActionPreference = 'Stop'
. C:\pulse3\pulse3_env.ps1
Set-Location $root          # never stay inside C:\pulse (a working directory there would block the rename)
$tar = "$env:SystemRoot\System32\tar.exe"
$py = "$root\app\.venv\Scripts\python.exe"
$old = 'C:\pulse'
$oldLeaf = Split-Path $old -Leaf                      # 'pulse'
$oldBase = (Split-Path $old -Parent).Replace('\', '/')   # 'C:/' (tar -C)
$oldRx = '(?i)' + ([regex]::Escape($old) -replace '\\\\', '[\\/]+') + '(?![0-9A-Za-z_])'   # the old install in any spelling, never C:\pulse3
$stateFile = "$root\legacy_state.json"
$day = Get-Date -Format yyyy-MM-dd
$folder = "$root\backup\legacy_pulse_$day"
$tgz = "$root\backup\legacy_pulse_$day.tgz"
$tgzN = 2
while (Test-Path $tgz) { $tgz = "$root\backup\legacy_pulse_${day}_$tgzN.tgz"; $tgzN++ }      # a kept archive of an earlier run is never overwritten
$logFile = "$root\logs\retire_legacy_$day.log"
$calSrc = "$old\app\backend\data\calendar\events.jsonl"
$calDir = "$root\var\legacy_calendar"
$calDst = "$calDir\events.jsonl"
$detachName = 'RetireLegacyOnce'
$oldTasks = 'PulseWeb', 'PulseLhbDaily', 'PulseLhbBackfill', 'PulsePaper', 'PulseForwardLab', 'PulseIntraday', 'PulseIntradayExt', 'PulseNews', 'PulseNewsAlerts'
$busyTasks = 'Pulse3Daily', 'Pulse3ForwardLog', 'Pulse3IntradayImport', 'Pulse3NewsImport'   # pulse3 runs that may still read C:\pulse: refused when running, disabled during the run
$idleTasks = 'Pulse3IntradayRecord', 'Pulse3ExtRecord', 'Pulse3NewsRecord'                    # native recorders: disabled during the run unless they are running now
$aclSids = 'S-1-5-18', 'S-1-5-32-544'                                                          # SYSTEM, Administrators: the same as the secrets folder
$excludeNames = '.venv', 'node_modules', '__pycache__'                                         # at any depth; plus C:\pulse\tools below
$tarExcludes = "--exclude=.venv --exclude=node_modules --exclude=__pycache__ --exclude=$oldLeaf/tools"
$minSameSessions = 10
$diskMargin = 512MB
$script:facts = [ordered]@{}
$script:paused = @()

function Log([string]$m) {
    $line = "$(Get-Date -Format s) $m"
    Write-Host $line
    if ($script:Apply) { Add-Content -Path $logFile -Value $line -Encoding UTF8 }
}
function Write-State([string]$s) {
    $doc = [ordered]@{ state = $s; at = (Get-Date).ToString('s'); by = $env:USERNAME }
    foreach ($k in $script:facts.Keys) { $doc[$k] = $script:facts[$k] }
    $tmp = "$stateFile.tmp"
    $doc | ConvertTo-Json -Depth 6 | Set-Content $tmp -Encoding UTF8
    Move-Item $tmp $stateFile -Force
}
function Restart-Api {   # the API reads the retired flags at start; same as cutover.ps1 (pulse3's own port only)
    Stop-ScheduledTask -TaskName Pulse3Api -ErrorAction SilentlyContinue
    $p = Get-NetTCPConnection -LocalPort 8200 -State Listen -ErrorAction SilentlyContinue | Select -First 1
    if ($p) { Stop-Process -Id $p.OwningProcess -Force }
    Start-ScheduledTask -TaskName Pulse3Api
}
function Run-Native([string]$file, [string]$argline, [string]$outFile, [string]$errFile) {
    # System.Diagnostics.Process, stdout copied byte for byte to $outFile, stderr to $errFile: no PS 5.1 stderr-to-ErrorRecord conversion
    # (native stderr would be fatal under $ErrorActionPreference Stop) and a reliable exit code (Start-Process -PassThru returned none).
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $file
    $psi.Arguments = $argline
    $psi.WorkingDirectory = "$root\app"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $so = [IO.File]::Create($outFile)
    try { $p.StandardOutput.BaseStream.CopyTo($so) } finally { $so.Close() }
    $p.WaitForExit()
    [IO.File]::WriteAllText($errFile, $errTask.Result)
    return $p.ExitCode
}
function Get-OldProcesses {
    # Every process running from the old install (image path or command line under C:\pulse\; C:\pulse3\ does not match), split into the
    # PulseWeb tree (the cmd wrapper run_web.cmd, uvicorn backend.app.main:app and all their descendants) and everything else.
    $all = @(Get-CimInstance Win32_Process)
    $hit = @($all | Where-Object {
        $_.ProcessId -ne $PID -and (("$($_.CommandLine)" -like '*C:\pulse\*') -or ("$($_.CommandLine)" -like '*C:/pulse/*') -or ("$($_.ExecutablePath)" -like 'C:\pulse\*'))
    })
    $web = New-Object 'System.Collections.Generic.HashSet[int]'
    $q = New-Object System.Collections.Queue
    foreach ($r in ($hit | Where-Object { "$($_.CommandLine)" -match 'run_web\.cmd|backend\.app\.main:app' })) { if ($web.Add([int]$r.ProcessId)) { $q.Enqueue([int]$r.ProcessId) } }
    while ($q.Count -gt 0) {
        $parent = $q.Dequeue()
        foreach ($c in ($all | Where-Object { $_.ParentProcessId -eq $parent })) { if ($web.Add([int]$c.ProcessId)) { $q.Enqueue([int]$c.ProcessId) } }
    }
    [pscustomobject]@{
        Web   = @($hit | Where-Object { $web.Contains([int]$_.ProcessId) })
        Other = @($hit | Where-Object { -not $web.Contains([int]$_.ProcessId) })
    }
}
function Test-OldReferences {
    # Things outside the old tasks that would break when C:\pulse goes: the venv's interpreter, any other task, any service.
    $out = @()
    $cfg = "$root\app\.venv\pyvenv.cfg"
    if (Test-Path $cfg) {
        foreach ($l in (Get-Content $cfg)) { if ($l -match $oldRx) { $out += "the virtual environment ($cfg) points into ${old}: $l" } }
    } else { $out += "$cfg not found: cannot tell where the interpreter of the virtual environment lives" }
    foreach ($t in @(Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        if ($oldTasks -contains $t.TaskName) { continue }
        foreach ($a in $t.Actions) {
            $text = "$($a.Execute) $($a.Arguments) $($a.WorkingDirectory)"
            if ($text -match $oldRx) { $out += "scheduled task $($t.TaskName) uses ${old}: $($text.Trim())" }
        }
    }
    foreach ($s in @(Get-CimInstance Win32_Service)) {
        if ("$($s.PathName)" -match $oldRx) { $out += "service $($s.Name) has an ImagePath in ${old}: $($s.PathName)" }
    }
    return $out
}
function Get-ArchiveSet {
    # Every file that goes into the archive: the walk skips the excluded directory names at any depth and C:\pulse\tools.
    $files = New-Object 'System.Collections.Generic.List[string]'
    $bytes = [long]0
    $stack = New-Object System.Collections.Stack
    $stack.Push([IO.DirectoryInfo]$old)
    while ($stack.Count -gt 0) {
        $d = $stack.Pop()
        foreach ($e in $d.GetFileSystemInfos()) {
            if ($e -is [IO.DirectoryInfo]) {
                if ($excludeNames -contains $e.Name) { continue }
                if ($e.FullName -eq "$old\tools") { continue }
            }
            if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "reparse point $($e.FullName): the file list check would not be reliable" }
            if ($e -is [IO.DirectoryInfo]) { $stack.Push($e) }
            else { $files.Add($e.FullName.Substring($old.Length + 1).Replace('\', '/')); $bytes += $e.Length }
        }
    }
    [pscustomobject]@{ Files = $files; Bytes = $bytes }
}
function Get-Sha([string]$path) { (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLower() }
function Sync-Calendar {
    # Copy the old calendar store into pulse3 and verify the hash; returns the sha256. An existing identical copy is kept.
    New-Item -ItemType Directory -Force $calDir | Out-Null
    $a = Get-Sha $calSrc
    if (-not (Test-Path $calDst) -or (Get-Sha $calDst) -ne $a) { Copy-Item $calSrc $calDst -Force }
    $b = Get-Sha $calDst
    if ($a -ne $b) { throw "calendar copy hash mismatch: source $a, copy $b (is the old site still writing it? stop PulseWeb and retry)" }
    return $a
}
function Set-PrivateAcl([string]$path) {
    # Explicit ACL like the secrets folder: no inheritance, only SYSTEM and Administrators (a folder passes it on to what it will hold).
    # icacls instead of Set-Acl: Set-Acl asked for SeSecurityPrivilege on a file that tar had just written.
    $perm = if ((Get-Item $path).PSIsContainer) { '(OI)(CI)F' } else { 'F' }
    $argl = "`"$path`" /inheritance:r"
    foreach ($sid in $aclSids) { $argl += " /grant:r *${sid}:$perm" }
    $tmp = [IO.Path]::GetTempFileName()
    $rc = Run-Native "$env:SystemRoot\System32\icacls.exe" $argl $tmp "$tmp.err"
    $msg = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue) + (Get-Content "$tmp.err" -Raw -ErrorAction SilentlyContinue)
    Remove-Item "$tmp*" -Force -ErrorAction SilentlyContinue
    if ($rc -ne 0) { throw "icacls failed on $path (exit ${rc}): $("$msg".Trim() -replace '\s+', ' ')" }
    $acl = Get-Acl $path
    $who = @($acl.Access | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value })
    $bad = @($who | Where-Object { $aclSids -notcontains $_ })
    if (-not $acl.AreAccessRulesProtected -or $bad.Count -gt 0) { throw "ACL of $path is not private: protected=$($acl.AreAccessRulesProtected), other principals: $($bad -join ', ')" }
    Log "ACL of ${path}: inheritance removed, only $(($acl.Access | ForEach-Object { $_.IdentityReference }) -join ', ')"
}
function Tar-CreateArgs([string]$archive, [string]$base) { "-czf `"$archive`" $tarExcludes -C $base $oldLeaf" }
function Test-TarExcludes([bool]$unicode) {
    # Runs the real exclusion arguments over a tiny synthetic tree (temp folder): tar must exist, work, and drop exactly the excluded paths.
    # With $unicode (the real set has non-ASCII names) one such name must also come back from the listing unchanged.
    $t = Join-Path ([IO.Path]::GetTempPath()) ('retire_tar_test_' + [Guid]::NewGuid().ToString('N'))
    try {
        $mk = @("$oldLeaf\app\.venv\x.txt", "$oldLeaf\app\node_modules\a\y.txt", "$oldLeaf\data\__pycache__\z.txt", "$oldLeaf\tools\t.txt", "$oldLeaf\data\keep.txt", "$oldLeaf\app\keep2.txt")
        $want = @("$oldLeaf/app/keep2.txt", "$oldLeaf/data/keep.txt")
        if ($unicode) { $u = [string][char]0x4E2D; $mk += "$oldLeaf\data\keep_$u.txt"; $want += "$oldLeaf/data/keep_$u.txt" }
        foreach ($p in $mk) {
            $f = Join-Path $t $p
            New-Item -ItemType Directory -Force (Split-Path $f) | Out-Null
            Set-Content $f 'x'
        }
        $out = "$t.tgz"
        $rc = Run-Native $tar (Tar-CreateArgs $out ($t.Replace('\', '/'))) "$t.o" "$t.e"
        if ($rc -ne 0) { return "tar create failed in the self-test (exit $rc)" }
        $rc = Run-Native $tar "-tzf `"$out`"" "$t.l" "$t.e2"
        if ($rc -ne 0) { return "tar list failed in the self-test (exit $rc)" }
        $names = @([IO.File]::ReadLines("$t.l") | Where-Object { $_ -and -not $_.EndsWith('/') } | Sort-Object)
        if (($names -join '|') -ne (($want | Sort-Object) -join '|')) { return "tar self-test failed, archive held: $($names -join ', ') (wanted $($want -join ', '))" }
        return $null
    } finally {
        Remove-Item "$t*" -Recurse -Force -ErrorAction SilentlyContinue
    }
}
function Restore-Tasks($tasks) {
    # Back to the recorded state: enabled / disabled / running as before. Before the rename the tasks are only disabled, so this is
    # Enable; a task that was unregistered anyway (older state files) is re-registered from the exported XML.
    foreach ($t in $tasks) {
        $cur = Get-ScheduledTask -TaskName $t.name -ErrorAction SilentlyContinue
        if (-not $cur) {
            Register-ScheduledTask -TaskName $t.name -Xml (Get-Content $t.xml -Raw) -User SYSTEM | Out-Null
            Log "restored: registered $($t.name) from $($t.xml)"
        }
        if ($t.enabled) { Enable-ScheduledTask -TaskName $t.name | Out-Null } else { Disable-ScheduledTask -TaskName $t.name | Out-Null }
        if ($t.running -and (Get-ScheduledTask -TaskName $t.name).State -ne 'Running') { Start-ScheduledTask -TaskName $t.name }
        Log "restored: $($t.name) enabled=$($t.enabled) running=$($t.running)"
    }
}
function Resume-Pulse3Tasks($names) {
    foreach ($n in $names) {
        if (-not $n) { continue }
        try { Enable-ScheduledTask -TaskName $n | Out-Null; Log "resumed $n" }
        catch { Log "could not enable ${n} again: $($_.Exception.Message) - enable it by hand" }
    }
}
function Pause-Pulse3Tasks {
    # Disable the pulse3 runs that could touch C:\pulse (and the native recorders, unless one is running now); remembered in state.
    foreach ($n in ($busyTasks + $idleTasks)) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t -or $t.State -eq 'Disabled') { continue }
        if ($idleTasks -contains $n -and $t.State -eq 'Running') { Log "left running: $n (a native recorder is not disabled while it runs)"; continue }
        if ($busyTasks -contains $n -and $t.State -eq 'Running') { throw "pulse3 task $n started during the run and may read $old" }
        Disable-ScheduledTask -TaskName $n | Out-Null
        $script:paused += $n
        $script:facts['pulse3_disabled'] = @($script:paused)
        Log "paused $n (enabled again at the end)"
    }
    Start-Sleep -Seconds 2
    foreach ($n in $busyTasks) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t -and $t.State -eq 'Running') { throw "pulse3 task $n is running and may read $old" }
    }
}
function Show-Manual-Restore($st) {
    "To bring the old system back by hand (only if the user wants it):"
    "  tar -xzf $($st.archive) -C C:/                                  # extracts C:\pulse (without .venv, node_modules, __pycache__, tools)"
    "  Copy-Item $root\tools C:\pulse\tools -Recurse                    # uv and the interpreter (regenerable; pulse3's own copy)"
    "  cd C:\pulse\app\backend; $root\tools\uv.exe sync                 # rebuild the virtual environment if the old site is wanted again"
    "  Register-ScheduledTask -TaskName <name> -Xml (Get-Content $($st.folder)\<name>.xml -Raw) -User SYSTEM    # one per task, XML files are in that folder"
    "  Remove-Item $stateFile                                           # so that pulse3_env.ps1 stops treating the old system as retired; then restart Pulse3Api"
}

if ($Detached -and (-not $Apply -or $Rollback)) { '-Detached only goes with -Apply (not with -Rollback): powershell -File C:\pulse3\retire_legacy.ps1 -Apply -Detached'; exit 1 }

# ---- rollback ----
if ($Rollback) {
    if (-not (Test-Path $stateFile)) { 'nothing to roll back: no legacy_state.json'; exit 0 }
    $st = Get-Content $stateFile -Raw | ConvertFrom-Json
    if ($st.state -in 'deleting', 'retired') {
        "rollback is not possible any more: state is '$($st.state)' (C:\pulse was renamed and deleted after the archive was verified)."
        Show-Manual-Restore $st
        exit 1
    }
    $partial = $st.archive -and (Test-Path "$($st.archive)") -and -not $st.archive_verified
    if (-not $Apply) {
        foreach ($t in @($st.tasks)) { if ($t) { "would: restore task $($t.name) (enabled=$($t.enabled) running=$($t.running))" } }
        foreach ($n in @($st.pulse3_disabled)) { if ($n) { "would: enable pulse3 task $n again" } }
        if ($partial) { "would: remove the partial archive $($st.archive)" }
        elseif ($st.archive -and (Test-Path "$($st.archive)")) { "kept: the verified archive $($st.archive) (never deleted by this script)" }
        'dry run: nothing changed (add -Apply)'; exit 0
    }
    foreach ($k in 'folder', 'archive', 'tasks', 'pulse3_disabled', 'archive_verified', 'sha256', 'counts') { if ($st.PSObject.Properties[$k]) { $script:facts[$k] = $st.$k } }
    if ($st.tasks) { Restore-Tasks @($st.tasks) }
    Resume-Pulse3Tasks @($st.pulse3_disabled)
    if ($partial) { Remove-Item "$($st.archive)" -Force; Log "removed the partial archive $($st.archive)" }
    Write-State 'rolled_back'
    Log 'state: rolled_back (C:\pulse was never renamed or deleted)'
    exit 0
}

# ---- preflight (always, also in a dry run; changes nothing except a temp folder and, with -Apply, the calendar copy) ----
$problems = @()
$info = @()
$prev = $null
if (Test-Path $stateFile) { $prev = Get-Content $stateFile -Raw | ConvertFrom-Json }
if ($prev -and $prev.state -in 'retired', 'deleting') { $problems += "already $($prev.state) (legacy_state.json); nothing to do" }
if ($prev -and $prev.state -eq 'switching') { $problems += "legacy_state.json says 'switching': an earlier run stopped half-way; run -Rollback -Apply first" }
$rsRaw = if (Test-Path "$root\refdata_source.txt") { Get-Content "$root\refdata_source.txt" -Raw } else { $null }
$rs = "$rsRaw".Trim()
if ($rs -ne 'native') {
    $problems += "$root\refdata_source.txt is $(if ($rs) { "'$rs'" } else { 'missing or empty' }), not 'native': pulse3 would still read the old warehouse (run refdata_switch.ps1 -Apply first)"
}
if (-not (Test-Path "$root\secrets\openai.key") -or (Get-Item "$root\secrets\openai.key").Length -eq 0) { $problems += "$root\secrets\openai.key missing or empty (run set_openai_key.ps1): the old copy goes with C:\pulse" }
if (-not (Test-Path $uv) -or -not (Test-Path $basepy)) { $problems += "uv or the interpreter missing under ${tools}: pulse3 would lose them with C:\pulse\tools" }
if (-not $cutover) { $problems += "cutover state is not 'cutover' (cutover_state.json): the old recorders may still be wanted" }
if (-not (Test-Path $tar)) { $problems += "$tar not found" }
if (-not (Test-Path $old)) { $problems += "$old not found: nothing to archive" }
if ((Test-Path $folder) -and -not ($prev -and $prev.state -in 'failed', 'rolled_back')) { $problems += "$folder exists: move or rename it" }   # a failed or rolled back run leaves it; the retry reuses it
if (Test-Path "$old.removing") { $problems += "$old.removing exists (leftover of an earlier delete): remove it first" }
if (-not (Test-Path $calSrc)) { $problems += "old calendar store $calSrc not found" }
foreach ($b in $busyTasks) {
    $t = Get-ScheduledTask -TaskName $b -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq 'Running') { $problems += "pulse3 task $b is running and may read C:\pulse: wait until it ends" }
}
foreach ($m in @(Test-OldReferences)) { $problems += $m }                                       # H1: pyvenv.cfg, other tasks, services
$pg = Get-CimInstance Win32_Service -Filter "Name='pulse3-pg'" -ErrorAction SilentlyContinue
$info += "service pulse3-pg: " + $(if ($pg) { "$($pg.PathName)" } else { 'not installed as a service' })
$present = @($oldTasks | Where-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue })
$info += "old tasks present: $($present -join ', ')"
$missing = @($oldTasks | Where-Object { $present -notcontains $_ })
if ($missing) { $info += "old tasks already absent (skipped): $($missing -join ', ')" }
$procs = Get-OldProcesses
if ($procs.Other.Count -gt 0) {
    $problems += "processes run from C:\pulse that are not PulseWeb: " + (($procs.Other | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join '; ')
}
$info += "PulseWeb processes to stop: " + $(if ($procs.Web.Count) { ($procs.Web | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join '; ' } else { 'none running' })
$set = $null
$nonAscii = 0
if (Test-Path $old) {
    try {
        $set = Get-ArchiveSet
        $nonAscii = @($set.Files | Where-Object { $_ -match '[^\x00-\x7F]' }).Count
        $free = (New-Object IO.DriveInfo 'C').AvailableFreeSpace
        $need = $set.Bytes + $diskMargin
        $info += ("archive set: {0} files ({1} with non-ASCII names), {2:N2} GB (compressed size is at most that); free on C: {3:N2} GB, needed {4:N2} GB" -f $set.Files.Count, $nonAscii, ($set.Bytes / 1GB), ($free / 1GB), ($need / 1GB))
        if ($free -lt $need) {
            $problems += ("free disk {0:N2} GB on C: is less than the {1:N2} GB needed for the archive (set size + 0.5 GB margin)" -f ($free / 1GB), ($need / 1GB))
            if ($prev -and $prev.archive_kept) { $problems += "a verified archive of the failed run is kept at $($prev.archive_kept): delete it by hand if the new run may replace it" }
        }
    } catch { $problems += "cannot list the archive set: $($_.Exception.Message)" }
}
if (Test-Path $tar) {
    $self = Test-TarExcludes ($nonAscii -gt 0)
    if ($self) { $problems += $self } else { $info += 'tar self-test: ok (exclusions work, gzip archive lists back' + $(if ($nonAscii -gt 0) { ', non-ASCII names too)' } else { ')' }) }
}
# LHB comparison: pulse3's seats against the old warehouse (read only); the old PulseLhbDaily goes only after 10 equal sessions
$tmp = [IO.Path]::GetTempFileName()
if (Test-Path $py) {
    $rc = Run-Native $py '-m pulse.workers.lhb_compare --json' $tmp "$tmp.err"
    $txt = Get-Content $tmp -Raw -Encoding UTF8
    $j = $null
    if ($txt) { try { $j = $txt | ConvertFrom-Json } catch { $j = $null } }
    if ($j -and $null -ne $j.same_tail) {
        $script:facts['lhb_same_tail'] = [int]$j.same_tail
        $info += "LHB comparison: $($j.same_tail) consecutive SAME sessions at the end (need $minSameSessions) over $($j.since) .. $($j.until)"
        if ($j.same_tail -lt $minSameSessions) {
            if ($Force) { $info += "LHB comparison below $minSameSessions accepted by -Force" }
            else { $problems += "LHB comparison shows only $($j.same_tail) consecutive SAME sessions (need $minSameSessions; -Force overrides)" }
        }
    } else {
        $e = (Get-Content "$tmp.err" -Raw -ErrorAction SilentlyContinue)
        $problems += "lhb_compare gave no result (exit $rc): $("$e".Trim() -replace '\s+', ' ')"
    }
    # the native calendar must hold events: PULSE_CATALYST_SOURCE=native replaces the old store after the retirement and never falls back
    $rc = Run-Native $py '-m pulse.workers.fwdlog catalyst-compare' $tmp "$tmp.err"
    $txt = Get-Content $tmp -Raw -Encoding UTF8
    $j = $null
    if ($txt) { try { $j = $txt | ConvertFrom-Json } catch { $j = $null } }
    if ($j -and $j.native_present -eq $true) { $info += "native calendar: $($j.native_events) events (old store: $($j.legacy_events), matched $($j.matched))" }
    else { $problems += "native calendar has no events (fwdlog catalyst-compare exit $rc): run alerts calendar-sync / calendar-derive before retiring" }
    Remove-Item "$tmp*" -Force -ErrorAction SilentlyContinue
} else { $problems += "$py not found" }
if (-not $problems -and (Test-Path $calSrc)) {
    if ($Apply -and -not $Detached) { $sha = Sync-Calendar; $info += "calendar store copied to $calDst and verified (sha256 $sha)" }
    else {
        $sha = Get-Sha $calSrc
        $same = (Test-Path $calDst) -and ((Get-Sha $calDst) -eq $sha)
        $info += "calendar store sha256 $sha; copy at $calDst " + $(if ($same) { 'is identical' } else { 'will be made (and verified) by -Apply' })
    }
}
if ($problems) {
    Log 'preflight failed:'
    foreach ($p in $problems) { Log "  - $p" }
    foreach ($i in $info) { Log "  info: $i" }
    exit 1
}
Log 'preflight ok'
foreach ($i in $info) { Log "  $i" }

if (-not $Apply) {
    "would: export the XML of $($present.Count) task(s) into $folder (folder and archive get an ACL for SYSTEM and Administrators only)"
    "would: disable $((@($busyTasks) + @($idleTasks)) -join ', ') for the duration (the recorders only if not running); enabled again at the end"
    $present | ForEach-Object { "would: stop and disable $_ (unregistered only after the rename)" }
    "would: stop the PulseWeb processes, copy the calendar store again (final) to $calDst"
    "would: tar $(Tar-CreateArgs $tgz $oldBase)   (excluded: .venv, node_modules, __pycache__, $old\tools)"
    "would: verify with tar -tzf (the sorted file list must equal the $($set.Files.Count) files of the source set, line by line) and sha256"
    "would: rename $old to $old.removing, write legacy_state.json 'deleting', unregister the old tasks, delete it, write 'retired', restart Pulse3Api"
    'dry run: nothing changed. To apply (only with the user''s approval) run it DETACHED: powershell -File C:\pulse3\retire_legacy.ps1 -Apply -Detached'
    exit 0
}

# ---- detached: register a one-off SYSTEM task that runs this script with -Apply, start it, leave ----
if ($Detached) {
    $ex = Get-ScheduledTask -TaskName $detachName -ErrorAction SilentlyContinue
    if ($ex -and $ex.State -eq 'Running') { Log "refused: $detachName is still running"; exit 1 }
    if ($ex) { Unregister-ScheduledTask -TaskName $detachName -Confirm:$false }
    $psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $argl = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Apply -FromTask" + $(if ($Force) { ' -Force' } else { '' })
    $act = New-ScheduledTaskAction -Execute $psExe -Argument $argl
    $set1 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $detachName -Action $act -Settings $set1 -User SYSTEM -RunLevel Highest | Out-Null
    Start-ScheduledTask -TaskName $detachName
    Log "started the one-off task $detachName ($argl)"
    Log "follow: Get-Content $logFile -Wait   and   Get-Content $stateFile; when it is over: Unregister-ScheduledTask $detachName -Confirm:`$false"
    exit 0
}
if (-not $FromTask) { Log "warning: running attached. If this session drops the run stops half-way; use -Apply -Detached (a one-off SYSTEM task) instead" }

# ---- apply ----
$tgzCreated = $false
$renamed = $false
$taskBefore = @()
$exitCode = 1
try {
    Log "retire_legacy start: archive $tgz"
    New-Item -ItemType Directory -Force $folder | Out-Null
    Set-PrivateAcl $folder
    foreach ($n in $present) {
        $t = Get-ScheduledTask -TaskName $n
        $xmlPath = "$folder\$n.xml"
        Export-ScheduledTask -TaskName $n | Set-Content $xmlPath -Encoding Unicode
        $null = [xml](Get-Content $xmlPath -Raw)          # must parse before anything is changed
        $taskBefore += [pscustomobject]@{ name = $n; enabled = ($t.State -ne 'Disabled'); running = ($t.State -eq 'Running'); xml = $xmlPath }
        Log "exported $n ($($t.State)) to $xmlPath"
    }
    $script:facts['folder'] = $folder
    $script:facts['archive'] = $tgz
    $script:facts['tasks'] = $taskBefore
    Write-State 'switching'
    Pause-Pulse3Tasks
    Write-State 'switching'                                  # now with pulse3_disabled, so a crash can still be rolled back
    foreach ($n in $present) {
        Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $n | Out-Null
        Log "stopped and disabled $n (unregistered only after the rename)"
    }
    Start-Sleep -Seconds 5
    # the PulseWeb tree as seen BEFORE its task was stopped (orphans lose their parent link), plus whatever the tree looks like now
    $webIds = @($procs.Web | ForEach-Object { [int]$_.ProcessId })
    $now = Get-OldProcesses
    $nowWeb = @($now.Web | ForEach-Object { [int]$_.ProcessId })
    foreach ($p in (@($now.Web) + @($now.Other))) {
        if (($nowWeb -contains [int]$p.ProcessId) -or ($webIds -contains [int]$p.ProcessId)) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Log "stopped PulseWeb process $($p.ProcessId) $($p.Name)"
        }
    }
    Start-Sleep -Seconds 3
    $left = Get-OldProcesses
    if ($left.Web.Count + $left.Other.Count -gt 0) { throw "processes still run from C:\pulse: $((@($left.Web) + @($left.Other) | ForEach-Object { $_.ProcessId }) -join ', ')" }
    if (Get-NetTCPConnection -LocalPort 8100 -State Listen -ErrorAction SilentlyContinue) { Log 'warning: something still listens on :8100 (not a C:\pulse process; left alone)' }
    $calSha = Sync-Calendar
    $script:facts['calendar_sha256'] = $calSha
    Log "calendar store frozen at $calDst (sha256 $calSha)"
    $set = Get-ArchiveSet          # again: the old system is stopped now, so this is the final set
    Log ("archive set: {0} files, {1:N2} GB" -f $set.Files.Count, ($set.Bytes / 1GB))
    $t0 = Get-Date
    $tgzCreated = $true
    New-Item -ItemType File $tgz | Out-Null                  # created empty and locked down BEFORE any data is written into it
    Set-PrivateAcl $tgz
    $rc = Run-Native $tar (Tar-CreateArgs $tgz $oldBase) "$folder\tar_create.out" "$folder\tar_create.err"
    if ($rc -ne 0) { throw "tar create exit $rc (see $folder\tar_create.err)" }
    Set-PrivateAcl $tgz                                      # again, explicitly, in case tar replaced the file
    Log ("archive written in {0:N0} s: {1:N2} GB" -f ((Get-Date) - $t0).TotalSeconds, ((Get-Item $tgz).Length / 1GB))
    $rc = Run-Native $tar "-tzf `"$tgz`"" "$folder\tar_list.txt" "$folder\tar_list.err"
    if ($rc -ne 0) { throw "tar -tzf exit ${rc}: the archive does not read back (see $folder\tar_list.err)" }
    # the sorted file list of the archive must equal the sorted source list, line by line (not just the count)
    $prefix = "$oldLeaf/"
    $arch = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in [IO.File]::ReadLines("$folder\tar_list.txt")) {
        if (-not $line -or $line.EndsWith('/')) { continue }
        if (-not $line.StartsWith($prefix)) { throw "unexpected archive entry '$line' (listing: $folder\tar_list.txt)" }
        $arch.Add($line.Substring($prefix.Length))
    }
    $want = New-Object 'System.Collections.Generic.List[string]' (, $set.Files)
    $arch.Sort([StringComparer]::Ordinal)
    $want.Sort([StringComparer]::Ordinal)
    $diffs = @()
    $m = [Math]::Max($arch.Count, $want.Count)
    for ($i = 0; $i -lt $m -and $diffs.Count -lt 10; $i++) {
        $a = if ($i -lt $arch.Count) { $arch[$i] } else { '<none>' }
        $b = if ($i -lt $want.Count) { $want[$i] } else { '<none>' }
        if ($a -cne $b) { $diffs += "line $($i + 1): archive '$a' / source '$b'" }
    }
    if ($arch.Count -ne $want.Count -or $diffs.Count -gt 0) {
        throw "archive file list differs from the source list (archive $($arch.Count), source $($want.Count)): $($diffs -join '; ')"
    }
    $sha = Get-Sha $tgz
    $size = (Get-Item $tgz).Length
    $script:facts['sha256'] = $sha
    $script:facts['counts'] = [ordered]@{ files_source = $want.Count; files_archived = $arch.Count; bytes_source = $set.Bytes; archive_bytes = $size
                                           excluded = '.venv, node_modules, __pycache__ (any depth), C:\pulse\tools' }
    $script:facts['archive_verified'] = $true
    Write-State 'switching'                                  # from here the archive counts as verified and is never deleted
    Log "archive verified: $($arch.Count) files, sorted list equal to the source list, sha256 $sha, $size bytes"
    # the point of no return starts at the rename; an open handle makes it fail here, with everything intact (old tasks only disabled)
    Rename-Item -Path $old -NewName "$oldLeaf.removing"
    $renamed = $true
    Write-State 'deleting'
    Log "renamed $old to $old.removing; state deleting"
    $notGone = @()
    foreach ($n in $present) {
        try { Unregister-ScheduledTask -TaskName $n -Confirm:$false; Log "unregistered $n" }
        catch { $notGone += $n; Log "warning: could not unregister ${n}: $($_.Exception.Message) (it stays disabled; remove it by hand)" }
    }
    if ($notGone) { $script:facts['tasks_not_unregistered'] = $notGone }
    & cmd.exe /c "rmdir /s /q `"$old.removing`"" | Out-Null
    if (Test-Path "$old.removing") {
        $script:facts['leftover'] = "$old.removing"
        Write-State 'retired'
        Log "delete incomplete: $old.removing remains; remove it by hand: cmd /c rmdir /s /q $old.removing"
        $exitCode = 1
    } else {
        Write-State 'retired'
        Log 'state: retired (C:\pulse deleted)'
        try { Restart-Api; Log 'Pulse3Api restarted (reads the retired flags)' } catch { Log "warning: Pulse3Api restart failed ($($_.Exception.Message)); restart the task by hand" }
        "retired. archive: $tgz ($size bytes, sha256 $sha)"
        Show-Manual-Restore ([pscustomobject]@{ archive = $tgz; folder = $folder })
        $exitCode = 0
    }
} catch {
    $msg = $_.Exception.Message
    Log "retirement failed: $msg"
    $exitCode = 1
    if ($renamed) {
        # renamed but not finished: C:\pulse no longer exists as a path; the archive is verified, so this is a finished retirement with a leftover
        $script:facts['error'] = $msg
        $script:facts['leftover'] = "$old.removing"
        try { Write-State 'retired' } catch { }
        Log "C:\pulse was renamed to $old.removing and the rest did not finish: remove it by hand (cmd /c rmdir /s /q $old.removing)"
    } else {
        try { Restore-Tasks $taskBefore } catch { Log "task restore error: $($_.Exception.Message) - enable them by hand (XML in $folder)" }
        if ($script:facts['archive_verified']) {
            $script:facts['archive_kept'] = $tgz                      # a verified archive is never deleted
            Log "kept the verified archive $tgz (state archive_kept)"
        } elseif ($tgzCreated -and (Test-Path $tgz)) {
            Remove-Item $tgz -Force -ErrorAction SilentlyContinue
            Log "removed the partial archive $tgz"
        }
        $script:facts['error'] = $msg
        try { Write-State 'failed' } catch { Log "could not write the state file: $($_.Exception.Message)" }
        Log 'state: failed (C:\pulse is intact, old tasks restored)'
    }
} finally {
    Resume-Pulse3Tasks $script:paused                                 # M3: the pulse3 tasks paused for the run, on success and on failure
}
exit $exitCode
