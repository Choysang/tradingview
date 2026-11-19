"""The refdata CLI: argument contract and the 1 request/second floor (no database needed)."""
from __future__ import annotations

from datetime import date

import pytest

from pulse.workers import refdata as W


def test_a_faster_rate_than_one_request_per_second_is_refused_before_anything_connects(capsys):
    assert W.run(["--min-interval", "0.5", "listings"]) == 1
    assert "1 request per second" in capsys.readouterr().err


def test_subcommands_and_code_sources_are_exclusive():
    ap = W._parser()
    assert ap.parse_args(["adjusted-bars", "--codes", "000001", "600000"]).mode == "full"
    assert ap.parse_args(["adjusted-bars", "--mode", "refresh", "--universe-listings"]).universe_listings
    with pytest.raises(SystemExit):
        ap.parse_args(["adjusted-bars"])                                       # a code source is required: there is no implicit full-market run
    with pytest.raises(SystemExit):
        ap.parse_args(["adjusted-bars", "--codes", "000001", "--universe"])
    assert ap.parse_args(["boards", "--limit", "2"]).limit == 2
    assert ap.parse_args(["reconcile", "--only", "boards", "listings"]).only == ["boards", "listings"]


def test_years_are_bounded_by_what_one_kline_request_returns():
    assert W._start(3, date(2026, 11, 19)) == date(2023, 11, 20)
    with pytest.raises(SystemExit):
        W._start(5, date(2026, 11, 19))
    with pytest.raises(SystemExit):
        W._start(0, date(2026, 11, 19))
