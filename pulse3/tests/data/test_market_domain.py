"""M1-E05 + spec FINAL §4.1: interval volume, suspension vs missing vs zero, period aggregation, adjustment, capability."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse.contracts import check
from pulse.domain.market import adjustment as adj
from pulse.domain.market import capability as cap
from pulse.domain.market.bars import (
    Bar, CumulativePoint, aggregate, classify_sessions, cumulative_to_interval, minute_grid, sampled_approx_bars, window_report)
from pulse.domain.market.builders import wire_bar_payload
from pulse.domain.market.calendar import TradingCalendar, session_close_utc, session_open_utc

IID = "260eb48a-a8aa-5638-a898-ea93c1e9d5b5"
D = Decimal


def sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def mk(d: date, o, h, l, c, v, amt=None, final=True, mode="raw") -> Bar:
    close = session_close_utc(d)
    return Bar(IID, "1d", "regular_continuous_v1", d, session_open_utc(d), close, D(o), D(h), D(l), D(c), v,
               None if amt is None else D(amt), close + timedelta(hours=2), final, "valid", adjustment_mode=mode)


def test_minute_grid_has_240_regular_minutes_and_no_lunch():
    g = minute_grid(date(2026, 11, 10))
    assert len(g) == 240 and g[0] == datetime(2026, 11, 10, 1, 31, tzinfo=timezone.utc) and g[-1] == datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
    assert datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc) not in g  # 12:00 CST


def test_cumulative_snapshots_become_interval_volume_and_gaps_are_not_filled():
    g = minute_grid(date(2026, 11, 10))
    pts = [CumulativePoint(g[0], 1000, D("10000")), CumulativePoint(g[1], 1800, D("18000")), CumulativePoint(g[2], 2100, D("21000")),
           # minutes 3 and 4 never observed
           CumulativePoint(g[5], 3000, D("30000")), CumulativePoint(g[6], 2900, D("29000"))]
    out = cumulative_to_interval(pts, g[:8])
    assert [o["volume_shares"] for o in out[:3]] == [1000, 800, 300]
    assert out[3]["status"] == out[4]["status"] == "missing" and out[3]["volume_shares"] is None
    assert out[5]["status"] == "gap_span" and out[5]["volume_shares"] is None  # 900 shares cannot be assigned to one minute
    assert out[6]["status"] == "cumulative_decreased" and out[6]["volume_shares"] is None
    assert out[7]["status"] == "missing"


def test_suspension_missing_and_true_zero_are_three_different_facts():
    days = sessions(date(2026, 11, 2), date(2026, 11, 6))
    bars = [mk(days[0], 10, 10, 10, 10, 0, "0"), mk(days[1], 10, 11, 10, 11, 500, "5000")]
    status = classify_sessions(days, bars, suspended=[days[2]])
    assert status[days[0]] == "true_zero" and status[days[1]] == "observed"
    assert status[days[2]] == "suspended" and status[days[3]] == "missing" and status[days[4]] == "missing"
    rep = window_report(days, bars, [days[2]])
    assert rep["missing_dates"] == [days[3].isoformat(), days[4].isoformat()] and rep["window_complete"] is False
    assert window_report(days, [], [])["empty"] is True


def test_weekly_monthly_aggregation_over_trading_calendar_with_provisional_flags():
    all_days = sessions(date(2026, 10, 1), date(2026, 11, 10))
    all_days.remove(date(2026, 10, 1)); all_days.remove(date(2026, 10, 2))  # holiday (calendar says not sessions)
    cal = TradingCalendar(tuple(all_days), known_through=date(2026, 11, 10))
    bars = [mk(d, str(10 + i / 10), str(10.5 + i / 10), str(9.5 + i / 10), str(10.2 + i / 10), 1000 + i, str(10000 + i)) for i, d in enumerate(all_days)]
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    weekly = aggregate(bars, cal, "1w", computed_at=now)
    by_end = {w.trade_date: w for w in weekly}
    last_full = by_end[date(2026, 10, 30)]
    assert last_full.is_final and last_full.volume_shares == sum(b.volume_shares for b in bars if date(2026, 10, 26) <= b.trade_date <= date(2026, 10, 30))
    assert last_full.open == bars[all_days.index(date(2026, 10, 26))].open and last_full.close == bars[all_days.index(date(2026, 10, 30))].close
    current = by_end[date(2026, 11, 10)]
    assert not current.is_final and "period_not_complete" in current.issues  # week of 11-09 is not over
    monthly = {m.trade_date.month: m for m in aggregate(bars, cal, "1mo", computed_at=now)}
    assert monthly[10].is_final and not monthly[11].is_final
    assert monthly[10].bar_open_time == session_open_utc(date(2026, 10, 5))  # first TRADING day of October
    # a gap inside a finished period: partial and not final, sums are not invented
    gapped = [b for b in bars if b.trade_date != date(2026, 10, 14)]
    m10 = {m.trade_date.month: m for m in aggregate(gapped, cal, "1mo", computed_at=now)}[10]
    assert not m10.is_final and m10.quality_status == "partial" and any(i.startswith("missing_sessions") for i in m10.issues)
    # a missing component volume makes the aggregate NULL, never a partial sum
    holes = [b if b.trade_date != date(2026, 10, 14) else Bar(**{**b.__dict__, "volume_shares": None}) for b in bars]
    m10 = {m.trade_date.month: m for m in aggregate(holes, cal, "1mo", computed_at=now)}[10]
    assert m10.volume_shares is None
    qy = aggregate(bars, cal, "1q", computed_at=now) + aggregate(bars, cal, "1y", computed_at=now)
    assert not any(b.is_final for b in qy)


def test_aggregation_rejects_mixed_adjustment_and_multiple_instruments():
    d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
    cal = TradingCalendar((d1, d2), date(2026, 11, 3))
    with pytest.raises(ValueError, match="mixed adjustment"):
        aggregate([mk(d1, 1, 1, 1, 1, 1, "1"), mk(d2, 1, 1, 1, 1, 1, "1", mode="forward_adjusted")], cal, "1w", computed_at=datetime.now(timezone.utc))


def test_sampled_snapshots_never_become_true_ohlc():
    g = minute_grid(date(2026, 11, 10))
    pts = [CumulativePoint(g[i], None, None, D("10") + D(i) / 10) for i in range(0, 7)]
    bars = sampled_approx_bars(IID, pts, {"seconds": 300}, g[10])
    assert bars and all(b.estimated and "sampled_approximation" in b.issues and b.quality_status == "partial" and not b.is_final for b in bars)
    assert all(b.volume_shares is None for b in bars)


def test_adjustment_events_forward_backward_and_inverse_mapping():
    d = [date(2026, 6, 1) + timedelta(days=i) for i in range(6)]
    raw = [mk(d[i], p, p, p, p, 100, "1000") for i, p in enumerate(["10", "10", "9.5", "9.5", "9.5", "9.6"])]
    # ex-date d[2]: reference price 9.5 against previous raw close 10 -> ratio 0.95
    raw[2] = Bar(**{**raw[2].__dict__, "exchange_reference_price": D("9.5")})
    events = adj.events_from_reference_prices(raw)
    assert len(events) == 1 and events[0].ex_date == d[2] and events[0].ratio == D("0.95")
    fwd = adj.adjust(raw, events, "forward_adjusted")
    assert [b.close for b in fwd[:2]] == [D("9.5"), D("9.5")] and fwd[3].close == D("9.5") and fwd[5].close == D("9.6")
    assert fwd[0].adjustment_mode == "forward_adjusted" and fwd[0].adjustment_version.startswith("adj-")
    bwd = adj.adjust(raw, events, "backward_adjusted")
    assert bwd[0].close == D("10") and bwd[3].close.quantize(D("0.0001")) == D("10.0000")
    back = adj.to_raw(fwd[0], events)
    assert back.close == D("10.00") and back.adjustment_mode == "raw"
    # the raw series is untouched and adjusted bars are never raw
    assert raw[0].close == D("10") and raw[0].adjustment_mode == "raw"
    with pytest.raises(ValueError):
        adj.adjust(fwd, events, "forward_adjusted")  # adjusting adjusted data


def test_events_inferred_from_provider_adjusted_pair_on_real_recorded_series():
    """Tencent qfq is additive (adj = raw - offset); ex-dates and cash amounts are recovered from offset steps."""
    import json
    from .helpers import recorded

    raw_rows = json.loads(recorded("tencent_day_raw_sz000001_long.json").decode().split("=", 1)[1])["data"]["sz000001"]["day"]
    qfq_rows = json.loads(recorded("tencent_day_qfq_sz000001_long.json").decode().split("=", 1)[1])["data"]["sz000001"]["qfqday"]
    raw = [mk(date.fromisoformat(r[0]), r[1], r[3], r[4], r[2], int(float(r[5]) * 100)) for r in raw_rows]
    qfq = {date.fromisoformat(r[0]): D(r[2]) for r in qfq_rows}
    events = adj.derive_events_from_pair(raw, qfq)
    assert 1 <= len(events) <= 6 and all(e.origin == "derived_provider_pair_cash_equivalent" and D("0.9") < e.ratio < 1 for e in events)
    offset0 = D(raw_rows[0][2]) - qfq[date.fromisoformat(raw_rows[0][0])]
    prev_close = {b.trade_date: p.close for p, b in zip(raw, raw[1:])}
    total_cash = sum((prev_close[e.ex_date] * (1 - e.ratio) for e in events), D(0))
    assert abs(total_cash - offset0) < D("0.003") * len(events)
    # our multiplicative replay differs from the provider's additive series by design, but keeps the anchor and direction
    fwd = adj.adjust(raw, events, "forward_adjusted")
    assert fwd[-1].close == raw[-1].close and fwd[0].close < raw[0].close


def test_capability_routing_rejects_what_the_data_cannot_support():
    reg = cap.REGISTRY
    need_true_minutes = cap.Requirement("intraday_break_strategy", frozenset({cap.MINUTE_BARS_TRUE_OHLC, cap.INTERVAL_VOLUME}))
    sampled = reg["minute_close_sampled"]
    d = cap.evaluate(need_true_minutes, sampled)
    assert d.outcome == "rejected" and not d.data_ready
    assert "missing_required:interval_volume,minute_bars_true_ohlc" in d.reasons and "estimated_data_not_accepted" in d.reasons
    assert cap.evaluate(need_true_minutes, reg["bars_raw_1m"]).data_ready
    # a strategy that explicitly accepts estimated inputs is DEGRADED, never silently accepted
    lenient = cap.Requirement("coarse_intraday_shape", frozenset({cap.MINUTE_CLOSE_SAMPLED}), forbid_estimated=False)
    assert cap.evaluate(lenient, sampled).outcome == "degraded"
    # raw execution price needs raw: the legacy adjusted dataset is rejected
    exec_req = cap.Requirement("paper_fill", frozenset({cap.DAILY_BARS, cap.RAW_PRICES}))
    assert cap.evaluate(exec_req, reg["bars_legacy_fwd_adjusted_1d"]).outcome == "rejected"
    assert cap.route(exec_req, [reg["bars_legacy_fwd_adjusted_1d"], reg["bars_raw_1d"]]).dataset_id == "bars_raw_1d"
    turnover_req = cap.Requirement("amount_factor", frozenset({cap.DAILY_BARS, cap.TURNOVER_AMOUNT}))
    assert cap.route(turnover_req, list(reg.values())).outcome == "rejected"  # no registered daily source carries amount
    with pytest.raises(ValueError):
        cap.Capability("x", "s", "e", frozenset({cap.MINUTE_BARS_TRUE_OHLC}), estimated=True)
    assert cap.Capability.from_doc(sampled.to_doc()) == sampled


def test_wire_payload_passes_the_v3_contract():
    d = date(2026, 11, 10)
    bar = mk(d, "10.00", "10.50", "9.80", "10.20", 10025, "100250.00")
    snap = "6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d1111"
    fq = {"*": {"status": "observed", "reason": None, "raw_object_id": None, "value_origin": "primary_source"}}
    check("CanonicalBar", wire_bar_payload(bar, snapshot_id=snap, field_quality=fq))
    adj_bar = adj.adjust([bar], [], "forward_adjusted", "legacy-qfq-unversioned")[0]
    check("AdjustedBar", wire_bar_payload(adj_bar, snapshot_id=snap, field_quality=fq))
