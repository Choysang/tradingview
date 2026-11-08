"""Board rules and factor definitions, pinned so they cannot drift silently."""
from __future__ import annotations

from datetime import date, datetime, time

import polars as pl
import pytest

from backend.app.factors.engine import WEIGHTS, compute
from backend.app.market.calendar import Calendar, has_price_limit
from backend.app.market.simulation import price_limit


def test_calendar_uses_weekdays_and_configured_holidays() -> None:
    calendar = Calendar(holidays=frozenset({"2026-10-01"}))
    assert calendar.is_trading_day(date(2026, 10, 20)) is True
    assert calendar.is_trading_day(date(2026, 10, 24)) is False  # Saturday
    assert calendar.is_trading_day(date(2026, 10, 1)) is False  # configured holiday


def test_session_phases_cover_the_a_share_day() -> None:
    calendar = Calendar()
    day = date(2026, 10, 20)
    assert calendar.session(datetime.combine(day, time(9, 0))) == "pre_open"
    assert calendar.session(datetime.combine(day, time(10, 0))) == "morning"
    assert calendar.session(datetime.combine(day, time(12, 0))) == "lunch_break"
    assert calendar.session(datetime.combine(day, time(14, 0))) == "afternoon"
    assert calendar.session(datetime.combine(day, time(16, 0))) == "closed"
    assert calendar.session(datetime.combine(date(2026, 10, 24), time(10, 0))) == "closed"


def test_first_five_sessions_have_no_price_limit() -> None:
    assert has_price_limit(None) is True
    assert has_price_limit(0) is False
    assert has_price_limit(4) is False
    assert has_price_limit(5) is True


def test_limit_price_per_board() -> None:
    assert price_limit(10.0, "10cm") == 11.0
    assert price_limit(10.0, "20cm") == 12.0
    assert price_limit(10.0, "30cm") == 13.0


def test_st_only_narrows_the_main_board() -> None:
    assert price_limit(10.0, "10cm", is_st=True) == 10.5
    # A 20cm name keeps its ratio even when flagged ST.
    assert price_limit(10.0, "20cm", is_st=True) == 12.0


def test_limit_price_rounds_half_up_to_cents() -> None:
    assert price_limit(3.33, "10cm") == 3.66
    assert price_limit(0.05, "10cm") == 0.06


def _row(**overrides) -> dict:
    row = {
        "code": "600000",
        "turnover_today": 5.0,
        "turnover_yesterday": 5.0,
        "price": 11.0,
        "limit_price": 11.0,
        "prev_limit": 1,
        "prev_growth": 3,
        "prev_cum": 7.0,
        "pct_chg": 10.0,
        "seal_order_amount": 1.0,
        "amount_today": 10.0,
        "free_float_mkt_cap": 50.0,
        "high_60d": 12.0,
        "seal_success_rate_1y": 80.0,
        "open_board_count": 0,
        "volume_ratio": 1.0,
    }
    row.update(overrides)
    return row


def test_ladder_streak_counts_from_yesterday() -> None:
    frame = pl.DataFrame([_row(), _row(code="600001", prev_limit=0, price=9.5, pct_chg=2.0)])
    rows = {row["code"]: row for row in compute(frame).to_dicts()}
    assert rows["600000"]["is_limit_up"] is True
    assert rows["600000"]["limit_streak"] == 2
    assert rows["600001"]["is_limit_up"] is False
    assert rows["600001"]["limit_streak"] == 0


def test_doji_tolerance_is_separate_from_strict_streak() -> None:
    doji = _row(
        code="600002",
        price=9.98,
        limit_price=10.99,
        pct_chg=-0.2,
        turnover_today=3.6,
        turnover_yesterday=5.0,
        prev_growth=4,
        prev_limit=0,
        seal_order_amount=0.0,
    )
    frame = pl.DataFrame([_row(), doji])
    rows = {row["code"]: row for row in compute(frame).to_dicts()}
    assert rows["600000"]["growth_streak"] == 4
    assert rows["600002"]["growth_streak"] == 0
    assert rows["600002"]["tolerant_growth"] == 5
    assert rows["600002"]["turnover_ratio_yd"] == pytest.approx(0.72)
    assert rows["600002"]["seal_to_amount_ratio"] is None


def test_composite_renormalises_over_the_dimensions_that_have_data() -> None:
    frame = pl.DataFrame([_row(high_60d=None)])
    row = compute(frame).to_dicts()[0]
    assert row["dist_to_60d_high"] is None
    assert row["score_trend"] is None
    assert row["score_momentum"] is not None
    available = WEIGHTS["momentum"] + WEIGHTS["liquidity"] + WEIGHTS["quality"]
    expected = (
        row["score_momentum"] * WEIGHTS["momentum"]
        + row["score_liquidity"] * WEIGHTS["liquidity"]
        + row["score_quality"] * WEIGHTS["quality"]
    ) / available
    assert row["composite_score"] == pytest.approx(round(expected, 2), abs=0.02)


def test_seal_span_plugin_is_discovered() -> None:
    from backend.app.factors.registry import FACTORS, discover_plugins

    assert "seal_span" in discover_plugins()
    assert "seal_span_minutes" in FACTORS["seal_span"][1]

    sealed = _row(first_seal_time="09:31:00", last_seal_time="09:58:00", price=11.0, limit_price=11.0)
    late = _row(code="600002", first_seal_time="10:20:00", last_seal_time="10:22:00", price=9.5, pct_chg=2.0, limit_price=10.99, seal_order_amount=0.0)
    row_by_code = {row["code"]: row for row in compute(pl.DataFrame([sealed, late])).to_dicts()}
    assert row_by_code["600000"]["seal_span_minutes"] == pytest.approx(27.0)
    assert row_by_code["600000"]["early_seal"] is True
    assert row_by_code["600002"]["early_seal"] is False
    assert row_by_code["600002"]["seal_span_minutes"] == pytest.approx(2.0)


def test_plugin_discovery_survives_a_broken_file(tmp_path, monkeypatch, caplog) -> None:
    from backend.app.factors import registry

    (tmp_path / "good.py").write_text(
        "from backend.app.factors.registry import register_factor\n"
        "@register_factor('good_plugin', [], ['good'])\n"
        "def good(frame):\n"
        "    return frame.with_columns(__import__('polars').lit(True).alias('good'))\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.setattr(registry, "PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(registry, "_DISCOVERED", False)

    names = registry.discover_plugins()
    assert "good_plugin" in names
    assert "seal_span" in names  # the shipped plugin still loads
    assert any("bad.py" in record.message for record in caplog.records)
    registry.FACTORS.pop("good_plugin", None)


def test_tencent_kline_parses_rows_in_contract_order() -> None:
    from backend.app.research import tencent_history

    payload = {
        "code": 0,
        "data": {
            "sh600000": {
                "qfqday": [
                    ["2026-09-29", "9.10", "9.30", "9.40", "9.00", "1000.000"],
                    ["2026-09-30", "9.22", "9.48", "9.49", "9.16", "2000.000"],
                ]
            }
        },
    }
    bars = tencent_history.parse(payload, "sh600000")
    assert [bar.date for bar in bars] == ["2026-09-29", "2026-09-30"]
    first = bars[0]
    assert (first.open, first.close, first.high, first.low) == (9.10, 9.30, 9.40, 9.00)
    # The quote reports 手; the contract stores 股.
    assert first.volume == 100_000
    assert tencent_history.parse(payload, "sz000001") == []


def test_tencent_fetch_daily_handles_http_failure(monkeypatch) -> None:
    import httpx

    from backend.app.research import tencent_history

    def broken(*args, **kwargs):
        raise httpx.HTTPError("blocked")

    monkeypatch.setattr(httpx.Client, "get", broken)
    assert tencent_history.fetch_daily("600000") is None


def test_bar_derived_metrics() -> None:
    from backend.app.research.metrics import derive
    from contracts.schema import OHLCBar

    def bar(day: str, high: float, close: float) -> OHLCBar:
        return OHLCBar(date=day, open=close, high=high, low=close - 0.5, close=close, volume=1_000)

    rising = [bar(f"2026-09-{day:02d}", 10 + index * 0.5, 10 + index * 0.5) for index, day in enumerate(range(1, 20))]
    rising.append(bar("2026-09-20", 29.1, 19.1))  # 上影线创新高，收盘仅 +0.5%
    derived = derive("600000", rising)
    assert derived["high_60d"] == 29.1
    assert derived["zt_count_1y_bar"] == 0  # daily gains below 9.5%

    limit_days = [bar(f"2026-09-{day:02d}", 11, 11) for day in range(1, 4)]
    limit_days.append(bar("2026-09-04", 12.1, 12.1))  # +10%
    assert derive("600000", limit_days)["zt_count_1y_bar"] == 1
    assert derive("600000", limit_days, price=11.0)["dist_to_60d_high"] == pytest.approx(-9.1)
