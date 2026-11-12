"""CLI surface of workers/intel.py (parser, weekday walk, provider gating). Live behaviour is shown in docs/intel/I3_evidence.md."""
from __future__ import annotations

from datetime import date

import pytest

from pulse.workers import intel


def test_parser_has_the_five_commands_and_a_polite_default_rate():
    ap = intel._parser()
    assert ap.parse_args(["news"]).min_interval == 1.0
    assert ap.parse_args(["community", "--codes", "000001", "600000"]).codes == ["000001", "600000"]
    assert ap.parse_args(["seats", "--days", "2"]).days == 2
    assert ap.parse_args(["fundflow", "--codes", "000001"]).limit == 10
    assert ap.parse_args(["claims"]).provider == "rules"
    with pytest.raises(SystemExit):
        ap.parse_args(["community"])  # codes are mandatory: there is no full-market crawl


def test_weekday_walk_skips_weekends():
    assert intel._weekdays(3, date(2026, 11, 9)) == [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
    assert intel._weekdays(1, date(2026, 11, 8)) == [date(2026, 11, 6)]


def test_openai_provider_is_refused_without_a_key_file(monkeypatch, capsys):
    monkeypatch.delenv("PULSE_OPENAI_KEY_FILE", raising=False)
    monkeypatch.setattr(intel.db, "connect", lambda *a, **k: type("C", (), {"close": lambda s: None})())
    assert intel.run(["claims", "--provider", "openai"]) == 1
    assert "PULSE_OPENAI_KEY_FILE" in capsys.readouterr().err
