"""E089 recorder health (#173): the native extended recorder's coverage is read when present, the legacy files otherwise, and a missing record is an explicit
note, never a silent all-None row. No database."""
from __future__ import annotations

import json
from datetime import date

import polars as pl

from pulse.application import bf_fwdlog_inputs as IN
from pulse.domain.fwdlog import state as ST

DAY = date(2026, 11, 12)


def ext_with(coverage: dict) -> IN.DayFrames:
    return IN.DayFrames(DAY, {"coverage_document": coverage}, pl.DataFrame(), True)


NATIVE = {"recorder": "native", "recorder_summary": {"missed": 3, "failed_requests": 2, "error_count": 1}, "blocked_hosts": {"push2.eastmoney.com": 4},
          "end_reason": "done", "source_complete": True, "steps_done": 12}


def test_native_coverage_is_read_and_never_returned_is_unknown_not_zero(tmp_path):
    summary, merged, prov = IN.recorder_health(ext_with(NATIVE), tmp_path)
    assert (summary["end"], summary["missed"], summary["failed_requests"], summary["error_count"]) == ("done", 3, 2, 1)
    assert summary["blocked"] == {"push2.eastmoney.com": 4} and summary["source"] == "native_coverage" and merged is True
    assert prov["health_source"] == "native_coverage" and len(prov["ext_summary_sha256"]) == 64
    health = ST.health_state(summary, None, DAY, merged=merged)
    assert health["never_returned"] is None, "the native recorder keeps no never_returned list: unknown, not 0"
    assert health["blocked"] == ["push2.eastmoney.com"] and health["source"] == "native_coverage" and health["note"] is None


def test_a_native_version_that_is_not_final_says_so(tmp_path):
    cov = {**NATIVE, "end_reason": None, "source_complete": False}
    summary, merged, _ = IN.recorder_health(ext_with(cov), tmp_path)
    assert summary["end"] is None and merged is False and "not final" in summary["note"]
    assert ST.health_state(summary, None, DAY, merged=merged)["note"]


def test_native_coverage_wins_over_a_stale_legacy_summary_in_the_folder(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"end": "legacy", "missed": 99}), encoding="utf-8")
    summary, _, prov = IN.recorder_health(ext_with(NATIVE), tmp_path)
    assert summary["missed"] == 3 and prov["health_source"] == "native_coverage"


def test_legacy_summary_and_merged_marker_are_used_when_there_is_no_native_coverage(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"end": "done", "missed": 1, "failed_requests": 0, "never_returned": ["a", "b"], "blocked": {"h": 1}}), encoding="utf-8")
    (tmp_path / "quotes_ext.parquet").write_bytes(b"x")
    for ext in (None, ext_with({"dataset": "x"})):
        summary, merged, prov = IN.recorder_health(ext, tmp_path)
        assert summary["source"] == "legacy_summary" and merged is True and prov["health_source"] == "legacy_summary"
        health = ST.health_state(summary, None, DAY, merged=merged)
        assert health["never_returned"] == 2 and health["missed"] == 1 and health["blocked"] == ["h"]


def test_no_health_record_is_an_explicit_note(tmp_path):
    summary, merged, prov = IN.recorder_health(None, tmp_path)
    assert summary["source"] == "none" and "no recorder health record" in summary["note"] and merged is False
    assert prov == {"health_source": "none", "ext_summary_sha256": None}
    health = ST.health_state(summary, None, DAY, merged=merged)
    assert health["source"] == "none" and health["note"] and health["end"] is None and health["missed"] is None and health["never_returned"] is None


def test_an_unreadable_legacy_summary_is_reported_as_unreadable(tmp_path):
    (tmp_path / "summary.json").write_text("{not json", encoding="utf-8")
    summary, _, prov = IN.recorder_health(None, tmp_path)
    assert summary["unreadable"] is True and prov["health_source"] == "unreadable"
    health = ST.health_state(summary, None, DAY, merged=False)
    assert health["source"] == "unreadable" and health["note"]


def test_the_state_domain_still_accepts_the_old_summary_shape():
    assert ST.health_state({}, None, DAY, merged=False)["note"] == "no recorder health record for this session"
    h = ST.health_state({"end": "done", "never_returned": []}, None, DAY, merged=True)
    assert h["never_returned"] == 0 and h["source"] == "legacy_summary" and h["note"] is None
