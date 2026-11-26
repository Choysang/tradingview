"""pulse3 stands alone after the old system (C:\\pulse) is retired: defaults, gating and a source scan (no database needed).

Why: ``deploy/windows/retire_legacy.ps1`` archives and deletes the old install. Anything in pulse3 that still pointed into it would
fail silently (an absent file reads as "no events" / "no recordings"), so the defaults must name pulse3 paths and the remaining
references must be gated or documentation. The PowerShell scripts cannot run here; their references are checked as text.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pulse import config

ROOT = Path(__file__).resolve().parents[2]
OLD_INSTALL = re.compile(r"C:[\\/]+pulse(?![0-9A-Za-z_])")        # the old install, never C:\pulse3
NEW_ROOT = "C:\\pulse3\\"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PULSE_LEGACY_DATA", "PULSE_FWDLOG_EVENTS", "PULSE_LEGACY_WAREHOUSE", "PULSE_LEGACY_RETIRED", "PULSE_CATALYST_SOURCE"):
        monkeypatch.delenv(k, raising=False)


# ---- python defaults -------------------------------------------------------------------------------------------------------------------
def test_recordings_root_defaults_into_pulse3_and_does_not_exist():
    from pulse.adapters.sources import legacy_recordings as LR

    assert LR.DEFAULT_ROOT.startswith(NEW_ROOT)
    assert not OLD_INSTALL.search(LR.DEFAULT_ROOT)
    assert str(LR.LegacyRecordings().root) == str(Path(LR.DEFAULT_ROOT))
    assert LR.LegacyRecordings("D:/x").root == Path("D:/x")                  # an explicit root still wins


def test_recordings_env_overrides_the_default(monkeypatch):
    from pulse.adapters.sources import legacy_recordings as LR

    monkeypatch.setenv("PULSE_LEGACY_DATA", "D:/old-data")
    assert LR.LegacyRecordings().root == Path("D:/old-data")


def test_missing_recordings_root_reads_as_no_files_not_as_an_error():
    from datetime import date

    from pulse.adapters.sources import legacy_recordings as LR

    rec = LR.LegacyRecordings("Z:/definitely/not/here")
    day = date(2026, 11, 20)
    for stream in ("quotes", "hot", "ext", "news", "guba", "alerts"):
        assert rec.parts(stream, day) == []
    assert rec.done_marker(day) is None


def test_calendar_store_defaults_are_the_frozen_pulse3_copy():
    from pulse.application import bf_fwdlog_inputs as IN
    from pulse.workers import fillm

    assert IN.DEFAULT_EVENTS == fillm.DEFAULT_EVENTS
    assert IN.DEFAULT_EVENTS == "C:\\pulse3\\var\\legacy_calendar\\events.jsonl"
    assert not OLD_INSTALL.search(IN.DEFAULT_EVENTS)
    assert IN.events_path() == Path(IN.DEFAULT_EVENTS)


def test_calendar_env_overrides_the_default(monkeypatch):
    from pulse.application import bf_fwdlog_inputs as IN

    monkeypatch.setenv("PULSE_FWDLOG_EVENTS", "D:/events.jsonl")
    assert IN.events_path() == Path("D:/events.jsonl")


def test_absent_frozen_calendar_reads_as_not_present(tmp_path):
    from pulse.application import bf_fwdlog_inputs as IN

    events, note = IN.read_events(tmp_path / "nope.jsonl")
    assert events == [] and note["path_present"] is False                     # "absent", never an empty calendar that looks complete


def test_catalyst_source_default_stays_legacy_until_the_env_says_native(monkeypatch):
    from pulse.application import bf_fwdlog_catalyst as CAT

    assert CAT.source_mode() == "legacy"
    monkeypatch.setenv("PULSE_CATALYST_SOURCE", "native")
    assert CAT.source_mode() == "native"


# ---- retired flag ----------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("no", False)])
def test_legacy_retired_flag(value, expected):
    assert config.legacy_retired({"PULSE_LEGACY_RETIRED": value}) is expected
    assert config.legacy_retired({}) is False


def test_fusion_reconcile_is_skipped_when_retired_and_never_touches_the_database(monkeypatch, capsys):
    from pulse.adapters.postgres import db
    from pulse.workers import fusion

    def no_db(*a, **k):
        raise AssertionError("reconcile must not connect when the old system is retired")

    monkeypatch.setattr(db, "connect", no_db)
    monkeypatch.setenv("PULSE_LEGACY_RETIRED", "1")
    assert fusion.run(["reconcile", "--pc", "D:/pc/.work/lab"]) == 3
    assert "skipped" in capsys.readouterr().out


def test_fusion_reconcile_runs_when_not_retired(monkeypatch):
    from pulse.adapters.postgres import db
    from pulse.workers import fusion

    class Reached(Exception):
        pass

    def stop(*a, **k):
        raise Reached

    monkeypatch.setattr(db, "connect", stop)
    with pytest.raises(Reached):
        fusion.run(["reconcile", "--pc", "D:/pc/.work/lab"])                 # gate open: it goes on to the database


def test_other_fusion_commands_are_not_gated_by_the_retired_flag(monkeypatch):
    from pulse.adapters.postgres import db
    from pulse.workers import fusion

    class Reached(Exception):
        pass

    def stop(*a, **k):
        raise Reached

    monkeypatch.setattr(db, "connect", stop)
    monkeypatch.setenv("PULSE_LEGACY_RETIRED", "1")
    with pytest.raises(Reached):
        fusion.run(["show"])


# ---- source scan: no unconditional reference to the old install ----------------------------------------------------------------
#: python files that still mention the old install, and why (documentation only, never a path that is opened)
PY_ALLOWED: set[str] = set()
#: files where the old install is the subject: the retirement script, and the cutover script that cannot run once the old tasks are gone
PS_SUBJECT = {"retire_legacy.ps1", "cutover.ps1"}


def _hits(path: Path) -> list[tuple[int, str]]:
    return [(n, line.strip()) for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if OLD_INSTALL.search(line)]


def test_python_sources_do_not_point_into_the_old_install():
    bad = {}
    for p in sorted((ROOT / "src" / "pulse").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel in PY_ALLOWED:
            continue
        if hits := _hits(p):
            bad[rel] = hits
    assert not bad, bad


def test_allowed_python_mentions_are_only_help_text():
    for rel in PY_ALLOWED:
        for n, line in _hits(ROOT / rel):
            assert "help=" in line, f"{rel}:{n}: {line}"


def test_powershell_scripts_reference_the_old_install_only_in_comments_or_the_gated_block():
    bad = {}
    for p in sorted((ROOT / "deploy" / "windows").glob("*.ps1")):
        if p.name in PS_SUBJECT:
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if p.name == "pulse3_env.ps1":
            begin = next(i for i, l in enumerate(lines) if "LEGACY-BEGIN" in l)
            end = next(i for i, l in enumerate(lines) if "LEGACY-END" in l)
            outside = [(i + 1, l.strip()) for i, l in enumerate(lines) if OLD_INSTALL.search(l) and not (begin < i < end) and not l.lstrip().startswith("#")]
            inside = "\n".join(lines[begin:end])
            assert "if (-not $retired) {" in inside, "the old paths must sit under the not-retired branch"
            assert inside.index("if (-not $retired) {") < inside.index("C:\\pulse\\app"), "old warehouse path must be inside the gate"
            assert "PULSE_CATALYST_SOURCE = 'native'" in inside and "PULSE_LEGACY_RETIRED = '1'" in inside
            if outside:
                bad[p.name] = outside
            continue
        code = [(i + 1, l.strip()) for i, l in enumerate(lines) if OLD_INSTALL.search(l) and not l.lstrip().startswith("#")]
        if code:
            bad[p.name] = code
    assert not bad, bad


def test_env_script_has_no_fallback_to_old_key_or_tools():
    text = (ROOT / "deploy" / "windows" / "pulse3_env.ps1").read_text(encoding="utf-8")
    assert "secrets\\openai.key" in text
    assert "tools\\uv.exe" in text
    for l in text.splitlines():
        if OLD_INSTALL.search(l):
            assert "tools" not in l and "openai.key" not in l, l


def test_run_scripts_skip_legacy_steps_when_retired():
    daily = (ROOT / "deploy" / "windows" / "run_daily.ps1").read_text(encoding="utf-8")
    assert daily.index("if ($retired) {") < daily.index("legacy-import skipped") < daily.index("Step 'legacy-import'")
    assert "PULSE_REFDATA_SOURCE -ne 'native'" in daily                      # retired without native data: stop, loudly
    assert re.search(r"if \(\$retired\) \{ Add-Content \$log '-- calendar-legacy skipped", daily)
    for name in ("run_intraday_import.ps1", "run_news_import.ps1", "run_fwdlog.ps1"):
        text = (ROOT / "deploy" / "windows" / name).read_text(encoding="utf-8")
        assert "PULSE_LEGACY_DATA" not in text, name                        # set (or not) by pulse3_env.ps1 only
    refdata = (ROOT / "deploy" / "windows" / "refdata_switch.ps1").read_text(encoding="utf-8")
    assert "$Rollback -and $retired" in refdata


def test_retire_script_is_deployed_and_dry_run_by_default():
    deploy = (ROOT / "deploy" / "windows" / "deploy_app.ps1").read_text(encoding="utf-8")
    assert "retire_legacy.ps1" in deploy
    script = (ROOT / "deploy" / "windows" / "retire_legacy.ps1").read_text(encoding="utf-8")
    assert re.search(r"param\(\[switch\]\$Apply, \[switch\]\$Rollback, \[switch\]\$Force, \[switch\]\$Detached, \[switch\]\$FromTask\)", script)
    assert not re.search(r"[^\x00-\x7f]", script), "PowerShell 5.1 reads a BOM-less file in the ANSI code page: keep the script ASCII"
    for name in ("PulseWeb", "PulseLhbDaily", "PulseLhbBackfill", "PulsePaper", "PulseForwardLab", "PulseIntraday", "PulseIntradayExt", "PulseNews", "PulseNewsAlerts"):
        assert f"'{name}'" in script, name


# ---- review fixes (2026-11-24): order of operations, guards, ACL ---------------------------------------------------------------------
def _ps(name: str) -> str:
    return (ROOT / "deploy" / "windows" / name).read_text(encoding="utf-8")


def test_cutover_rollback_refuses_after_retirement():
    cut = _ps("cutover.ps1")
    assert "if ($Rollback -and $retired)" in cut
    assert cut.index("if ($Rollback -and $retired)") < cut.index("# ---- preflight")


def test_old_tasks_are_unregistered_only_after_the_rename():
    s = _ps("retire_legacy.ps1")
    apply_part = s[s.index("# ---- apply ----"):]
    assert apply_part.index("Disable-ScheduledTask") < apply_part.index("Rename-Item") < apply_part.index("Unregister-ScheduledTask")
    assert "Unregister-ScheduledTask" not in apply_part[:apply_part.index("Rename-Item")]


def test_pulse3_tasks_are_paused_and_resumed_in_finally():
    s = _ps("retire_legacy.ps1")
    assert "finally {" in s and "Resume-Pulse3Tasks $script:paused" in s.split("finally {")[-1]
    for name in ("Pulse3Daily", "Pulse3ForwardLog", "Pulse3IntradayImport", "Pulse3NewsImport", "Pulse3IntradayRecord", "Pulse3ExtRecord", "Pulse3NewsRecord"):
        assert f"'{name}'" in s, name
    assert "a native recorder is not disabled while it runs" in s


def test_preflight_checks_venv_other_tasks_and_services_for_the_old_install():
    s = _ps("retire_legacy.ps1")
    body = s[s.index("function Test-OldReferences"):s.index("function Get-ArchiveSet")]
    for needle in ("pyvenv.cfg", "Get-ScheduledTask", "$a.Execute", "$a.Arguments", "$a.WorkingDirectory", "Win32_Service", "PathName"):
        assert needle in body, needle
    assert "foreach ($m in @(Test-OldReferences))" in s
    assert "pulse3-pg" in s


def test_archive_check_compares_the_sorted_lists_not_just_the_count():
    s = _ps("retire_legacy.ps1")
    assert "$arch.Sort([StringComparer]::Ordinal)" in s and "$want.Sort([StringComparer]::Ordinal)" in s
    assert "-cne" in s and "archive file list differs" in s


def test_verified_archive_is_kept_and_partial_one_is_removed():
    s = _ps("retire_legacy.ps1")
    assert "archive_kept" in s and "archive_verified" in s
    assert s.index("if ($script:facts['archive_verified'])") < s.index("Remove-Item $tgz")
    assert "while (Test-Path $tgz)" in s                      # a kept archive is never overwritten: the next run writes _2
    roll = s[s.index("# ---- rollback ----"):s.index("# ---- preflight")]
    assert "removed the partial archive" in roll and "-not $st.archive_verified" in roll


def test_archive_and_folder_get_the_secrets_acl():
    s = _ps("retire_legacy.ps1")
    assert "$aclSids = 'S-1-5-18', 'S-1-5-32-544'" in s
    assert "/inheritance:r" in s and "/grant:r" in s
    apply_part = s[s.index("# ---- apply ----"):]
    assert apply_part.index("New-Item -ItemType File $tgz") < apply_part.index("Set-PrivateAcl $tgz") < apply_part.index("Run-Native $tar (Tar-CreateArgs")
    assert apply_part.count("Set-PrivateAcl $tgz") == 2        # before tar writes, and again after it


def test_detached_mode_registers_a_one_off_system_task():
    s = _ps("retire_legacy.ps1")
    assert "-Detached only goes with -Apply" in s
    d = s[s.index("# ---- detached"):s.index("# ---- apply ----")]
    for needle in ("Register-ScheduledTask", "Start-ScheduledTask", "-User SYSTEM", "-Apply -FromTask", "exit 0"):
        assert needle in d, needle
    assert "run it DETACHED" in s


def test_empty_refdata_source_file_is_guarded():
    env = _ps("pulse3_env.ps1")
    assert "if ($rs) { $env:PULSE_REFDATA_SOURCE = $rs }" in env
    s = _ps("retire_legacy.ps1")
    assert "$rs = \"$rsRaw\".Trim()" in s and "missing or empty" in s
