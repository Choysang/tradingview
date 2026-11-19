"""intraday-record command line: Tencent is the default primary, the fallback is opt-in and must be the other vendor."""
from __future__ import annotations

import pytest

from pulse.workers import ingest as I


def parse(*argv):
    return I._parser().parse_args(["intraday-record", "--universe", *argv])


def test_defaults_tencent_primary_and_no_fallback():
    a = parse()
    assert a.source == "tencent" and a.fallback is None and a.until.isoformat() == "15:33:00"


def test_fallback_is_opt_in():
    assert parse("--fallback", "sina").fallback == "sina"
    with pytest.raises(SystemExit):
        parse("--fallback", "eastmoney")


def test_fallback_equal_to_the_source_is_refused_before_anything_runs():
    a = parse("--source", "tencent", "--fallback", "tencent")
    with pytest.raises(SystemExit, match="other vendor"):
        I.run_live(a, None, None, None)


def test_the_run_script_refuses_before_the_cutover_and_has_no_fallback_by_default():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "deploy" / "windows" / "run_intraday_record.ps1"
    code = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    assert code[0].endswith("pulse3_env.ps1")
    assert code[1].startswith("if (-not $cutover)") and "exit 4" in code[1]          # right after the environment, before anything runs
    assert not any("--fallback" in ln for ln in code)                                  # opt-in: documented in the header, not enabled
