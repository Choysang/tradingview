"""E092 data seams without PostgreSQL: the day plan from synthetic fusion inputs, the 5-day labels from a synthetic panel, and the worker's exit codes / switch path."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from pulse.application import bf_fusion_service as S
from pulse.application import bf_llm_live_inputs as IN
from pulse.application.bf_llm_live_service import ContextMissing
from pulse.domain.fusion import panel as PN
from pulse.domain.llm_live import rules as R
from pulse.workers import llm_live as W

DAY = date(2026, 11, 23)
CUT = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


def sessions(n: int, last: date = DAY) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def long_frame(codes: list[str], days: list[date]) -> pl.DataFrame:
    rows = [{"code": c, "date": d, "open": 10.0 + i * 0.01 + k, "high": 11.0, "low": 9.0, "close": 10.0 + i * 0.01, "volume": 1e6, "pct_chg": 0.1, "amount": 2.0 + k % 3, "float_cap": 40.0 + k,
             "is_limit_up": i == len(days) - 1 and k == 0, "is_limit_down": False, "industry": "ind1" if k % 2 else None, "tradable": True}
            for k, c in enumerate(codes) for i, d in enumerate(days)]
    return pl.DataFrame(rows)


@pytest.fixture
def seams(monkeypatch):
    codes = [f"60{i:04d}" for i in range(60)]
    days = sessions(70)
    loaded = S.Loaded(long_frame(codes, days), DAY, CUT, {"research_snapshot": "snap"}, {c: f"N{c}" for c in codes}, ["note"])
    monkeypatch.setattr(IN, "scores_of_day", lambda conn, lake, day: {c: float(i) for i, c in enumerate(codes)})
    monkeypatch.setattr(S, "load_inputs", lambda conn, lake, cutoff=None: loaded)
    monkeypatch.setattr(IN, "index_closes", lambda conn, lake, cutoff, day: [3000.0 + i for i in range(80)])
    monkeypatch.setattr(IN, "industry_names", lambda conn, ids: ({"ind1": "Banks"}, []))
    return codes, loaded


def test_the_day_plan_holds_30_top_and_10_control_names_with_their_prompts(seams):
    codes, _ = seams
    plan = IN.load_day_plan(None, None, DAY)
    assert len(plan.jobs) == 40 and [j["group"] for j in plan.jobs] == ["top"] * 30 + ["control"] * 10
    assert [j["code"] for j in plan.jobs[:30]] == codes[::-1][:30]                                  # the best scores first
    first = plan.jobs[0]
    assert first["id"] == "2026-11-23_" + first["code"] and first["prompt"].startswith("市场：上证指数 2026-11-23 收盘") and "近20个交易日：" in first["prompt"]
    assert len(first["prompt"].split("\n")) == 3 + 20 and plan.refs["research_snapshot"] == "snap" and "note" in plan.notes
    assert any("行业 Banks" in j["prompt"] for j in plan.jobs) and any("行业" not in j["prompt"] for j in plan.jobs)       # names without an industry get no line


def test_a_day_the_bars_do_not_reach_is_refused(seams, monkeypatch):
    _, loaded = seams
    monkeypatch.setattr(S, "load_inputs", lambda conn, lake, cutoff=None: S.Loaded(loaded.long, date(2026, 11, 20), CUT, {}, loaded.names, []))
    with pytest.raises(ContextMissing) as e:
        IN.load_day_plan(None, None, DAY)
    assert e.value.code == "bars_not_ready"


def test_missing_scores_or_index_refuse_the_day(seams, monkeypatch):
    monkeypatch.setattr(IN, "index_closes", lambda conn, lake, cutoff, day: [])
    with pytest.raises(ContextMissing) as e:
        IN.load_day_plan(None, None, DAY)
    assert e.value.code == "market_line_missing"
    monkeypatch.setattr(IN, "scores_of_day", lambda conn, lake, day: {})
    with pytest.raises(ContextMissing) as e:
        IN.load_day_plan(None, None, DAY)
    assert e.value.code == "scores_not_ready"


def test_labels_are_open_to_open_five_sessions_minus_the_day_mean_and_only_for_closed_windows(monkeypatch):
    codes = ["600000", "600001", "600002", "600003"]
    days = sessions(40, date(2026, 12, 31))
    d0, late = days[25], days[36]                                   # D+6 is row 31 (inside) for d0 and row 42 (beyond the data) for ``late``
    rows = []
    for k, c in enumerate(codes):
        for i, d in enumerate(days):
            o = 10.0 * (1 + 0.01 * (i - 26) * (k + 1)) if i >= 26 else 10.0       # from D+1 (row 26) the stocks drift at 1, 2, 3, 4 percent a session
            rows.append({"code": c, "date": d, "open": o, "high": o, "low": o, "close": o, "volume": 1e8, "float_cap_100m": 50.0, "has_total_cap": True, "list_date": date(2010, 1, 1)})
    loaded = S.Loaded(PN.build_long(pl.DataFrame(rows)), days[-1], CUT, {}, {}, [])
    monkeypatch.setattr(S, "load_inputs", lambda conn, lake, cutoff=None: loaded)
    out = IN.load_labels(None, None, [d0, late])
    assert set(out) == {d0} and set(out[d0]) == set(codes)
    assert [out[d0][c][0] for c in codes] == pytest.approx([5.0, 10.0, 15.0, 20.0], rel=1e-3)             # open[D+6] / open[D+1] - 1
    assert [out[d0][c][1] for c in codes] == pytest.approx([-7.5, -2.5, 2.5, 7.5], abs=1e-3)              # minus the day's mean (12.5)


def test_exit_codes_separate_outcomes_refusals_and_failures():
    assert W._exit_code("decide", {"status": "done"}) == 0 and W._exit_code("decide", {"status": "stopped"}) == 0 and W._exit_code("decide", {"status": "check"}) == 0
    assert W._exit_code("decide", {"status": "failed"}) == 2
    for refused in ("late", "limit", "budget", "cost_cap", "no_key", "bad_key", "busy", "not_ready", "switch_off", "stopped_today"):
        assert W._exit_code("decide", {"status": refused}) == 3
    assert W._exit_code("track", {"status": "nothing_due"}) == 3 and W._exit_code("track", {"status": "tracked"}) == 0 and W._exit_code("judge", {"status": "progress"}) == 0


def test_the_switch_file_defaults_next_to_the_lake_and_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv(W.ENV_SWITCH, raising=False)
    assert W.switch_path(Path("C:/pulse3/var/lake")) == Path("C:/pulse3/var/llm_live.enabled")
    monkeypatch.setenv(W.ENV_SWITCH, str(tmp_path / "on"))
    assert W.switch_path(Path("x/lake")) == tmp_path / "on"


def test_the_parser_knows_the_five_commands():
    p = W._parser()
    assert p.parse_args(["decide", "--date", "2026-11-23", "--check"]).check is True
    for cmd in ("track", "judge", "status", "register"):
        assert p.parse_args([cmd]).cmd == cmd
    assert R.SPEC_SHA256
