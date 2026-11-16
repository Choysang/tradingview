"""Pure tests of the regime attribution (no IO): entry regime is the last COMPLETED session before the entry day, unmeasured dimensions are
unclassified, cells under the sample gate are flagged, empty buckets are missing (not 0), exit reasons are reconstructed with the replay's rules."""
from __future__ import annotations

from decimal import Decimal as D

from pulse.domain.playbooks import attribution as AT
from pulse.domain.playbooks.builder import RawSeries, build_views
from pulse.domain.playbooks.exits import ExitRules


def point(day, trend="bull", liquidity="expanding", sentiment="climax", style="small", **basis):
    b = {"trend": "ok", "liquidity": "ok", "sentiment": "ok", "style": "ok", **basis}
    return {"date": day, "trend": trend, "liquidity": liquidity, "sentiment": sentiment, "style": style, "basis": b}


PTS = [point("2026-03-02"), point("2026-03-03", trend="bear"), point("2026-03-04", trend="range")]


def test_regime_is_the_last_completed_session_before_entry_never_the_entry_day():
    assert AT.regime_at_entry(PTS, "2026-03-04")["date"] == "2026-03-03"       # the entry day's own label needs its close: not used
    assert AT.regime_at_entry(PTS, "2026-03-05")["date"] == "2026-03-04"
    assert AT.regime_at_entry(PTS, "2026-03-02") is None                        # nothing earlier
    assert AT.regime_at_entry(PTS, "2026-03-09")["date"] == "2026-03-04"        # weekend gap (3 days) is fine


def test_a_stale_regime_is_no_regime():
    assert AT.regime_at_entry(PTS, "2026-03-30") is None   # 26 days after the last point
    assert AT.regime_at_entry([], "2026-03-04") is None


def test_tag_trades_marks_missing_regime_as_none():
    tagged = AT.tag_trades([{"entry_day": "2026-03-05", "gross_return": D("0.1")}, {"entry_day": "2026-03-02", "gross_return": D("0.1")}], PTS)
    assert tagged[0]["regime_at_entry"]["trend"] == "range" and tagged[0]["regime_at_entry"]["session"] == "2026-03-04"
    assert tagged[1]["regime_at_entry"] is None


def trade(day, ret, pts=PTS):
    return AT.tag_trades([{"entry_day": day, "gross_return": None if ret is None else D(ret)}], pts)[0]


def cell(att, dim, bucket):
    d = next(x for x in att["dimensions"] if x["dimension"] == dim)
    return next(c for c in d["cells"] if c["bucket"] == bucket)


def test_average_return_per_bucket_and_win_rate():
    trades = [trade("2026-03-04", "0.10"), trade("2026-03-04", "-0.04"), trade("2026-03-05", "0.02")]   # bear, bear, range
    att = AT.attribute(trades)
    bear = cell(att, "trend", "bear")
    assert bear["trade_count"] == 2 and bear["avg_return"] == D("0.03") and bear["win_rate"] == D("0.5")
    assert cell(att, "trend", "range")["avg_return"] == D("0.02")
    assert att["closed_trades"] == 3 and att["open_trades"] == 0


def test_cells_under_ten_trades_are_flagged_and_ten_is_enough():
    nine = AT.attribute([trade("2026-03-04", "0.01") for _ in range(9)])
    ten = AT.attribute([trade("2026-03-04", "0.01") for _ in range(10)])
    assert cell(nine, "trend", "bear")["insufficient_sample"] is True
    assert cell(ten, "trend", "bear")["insufficient_sample"] is False
    assert cell(nine, "trend", "bear")["avg_return"] == D("0.01")               # still reported, not hidden


def test_empty_bucket_is_missing_not_zero_and_open_trades_carry_no_return():
    att = AT.attribute([trade("2026-03-04", "0.05"), trade("2026-03-04", None)])
    bull = cell(att, "trend", "bull")
    assert bull["trade_count"] == 0 and bull["avg_return"] is None and bull["win_rate"] is None
    assert att["closed_trades"] == 1 and att["open_trades"] == 1
    assert cell(att, "trend", "bear")["trade_count"] == 1                        # the open trade is not averaged in


def test_dimension_without_a_real_measurement_is_unclassified_for_that_dimension_only():
    pts = [{"date": "2026-03-02", "trend": "range", "liquidity": "steady", "sentiment": "ebb", "style": "unknown",
            "basis": {"trend": "insufficient_history", "liquidity": "ok", "sentiment": "ok", "style": "missing_index"}}]
    att = AT.attribute([trade("2026-03-03", "0.02", pts)])
    trend = next(d for d in att["dimensions"] if d["dimension"] == "trend")
    style = next(d for d in att["dimensions"] if d["dimension"] == "style")
    assert trend["unclassified"] == {"count": 1, "reasons": {"insufficient_history": 1}} and all(c["trade_count"] == 0 for c in trend["cells"])
    assert style["unclassified"] == {"count": 1, "reasons": {"missing_index": 1}} and "unknown" not in [c["bucket"] for c in style["cells"]]
    assert cell(att, "liquidity", "steady")["trade_count"] == 1                  # the measured dimension still counts it
    assert cell(att, "sentiment", "ebb")["trade_count"] == 1


def test_trade_without_any_regime_is_unclassified_everywhere():
    att = AT.attribute([trade("2026-03-02", "0.02")])
    assert all(d["unclassified"] == {"count": 1, "reasons": {"no_regime": 1}} for d in att["dimensions"])
    assert all(c["trade_count"] == 0 for d in att["dimensions"] for c in d["cells"])


def test_heatmap_columns_cover_every_bucket_and_rows_are_sorted():
    a = AT.attribute([trade("2026-03-04", "0.01")])
    hm = AT.heatmap({"b_play": a, "a_play": a})
    assert [r["playbook"] for r in hm["rows"]] == ["a_play", "b_play"]
    assert [c["dimension"] for c in hm["columns"]] == ["trend", "liquidity", "sentiment", "style"]
    assert [b["bucket"] for b in hm["columns"][3]["buckets"]] == ["small", "large", "balanced"]


# ---- exit reason ----------------------------------------------------------------------------------------------------
def view_of(closes):
    n = len(closes)
    days = [f"2026-03-{d:02d}" for d in range(2, 2 + n)]
    raw = RawSeries("z1", "main", tuple(days), tuple(closes), tuple(closes), tuple(closes), tuple(closes), tuple([1e7] * n))
    return build_views([raw], days)[0], days


def test_exit_reason_is_the_first_rule_that_fires_from_the_entry_bar():
    v, days = view_of([10.0, 10.0, 9.4, 9.0, 9.0, 9.0])
    rules = ExitRules(holding_days=10, stop_loss_pct=5.0)
    # entered at 10.0 on day 0; the close of day 2 (9.4) is -6%: the stop fires there, the sell happens at the next open
    assert AT.reconstruct_exit_reason(rules, v, days[0], days[3], 10.0) == "stop_loss"


def test_exit_reason_holding_days_and_unknown():
    v, days = view_of([10.0] * 8)
    assert AT.reconstruct_exit_reason(ExitRules(holding_days=3), v, days[0], days[3], 10.0) == "holding_days"
    assert AT.reconstruct_exit_reason(ExitRules(holding_days=30), v, days[0], days[3], 10.0) == "unknown"     # nothing fires: reported, not guessed
    assert AT.reconstruct_exit_reason(ExitRules(holding_days=3), v, "2020-01-01", days[3], 10.0) == "unknown"
