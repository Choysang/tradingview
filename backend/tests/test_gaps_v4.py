"""Valuation fields (SIM + LIVE parsing + screener filters) and single-stock exit rules.

No network: Tencent responses are faked, bars are built by hand.
"""
from __future__ import annotations

from datetime import date, timedelta
import sys
import types

import polars as pl
import pytest

from backend.app.core.engine import MarketEngine, QUALITY_FIELDS
from backend.app.market import live
from backend.app.market.live import AkShareFeed
from backend.app.market.simulation import Simulator, valuation
from backend.app.research import backtest
from contracts.schema import BacktestRequest, DataProvenance, OHLCBar, ScreenerFilterRequest


# --- valuation: simulator ------------------------------------------------------------------


def test_sim_valuation_is_deterministic_and_never_below_float_cap() -> None:
    frame = Simulator().tick()
    assert (frame["total_mkt_cap"] >= frame["float_mkt_cap"]).all()
    assert frame["total_mkt_cap"].null_count() == 0 and frame["pb"].min() > 0
    again = Simulator().tick()
    assert frame["pe_ttm"].to_list() == again["pe_ttm"].to_list()
    assert valuation("600000", 100.0) == valuation("600000", 100.0)


def test_sim_has_a_realistic_share_of_loss_makers() -> None:
    frame = Simulator().tick()
    share = (frame["pe_ttm"] < 0).sum() / frame.height
    assert 0.02 < share < 0.2


def test_simulator_random_stream_is_unchanged_by_valuation_fields() -> None:
    """The new fields use their own generator: the pinned scenario must not move."""
    first = Simulator().rows[0]
    assert first["price"] > 0
    assert Simulator().rows[5]["amount_today"] == Simulator().rows[5]["amount_today"]


# --- valuation: live quote parsing ---------------------------------------------------------


def _quote_line(code: str, **fields: str) -> str:
    raw = [""] * 56
    raw[2] = code
    raw[38], raw[49], raw[44], raw[47] = "2.5", "1.1", "300.5", "11.0"
    for index, value in fields.items():
        raw[int(index[1:])] = value
    return 'v_sh{}="{}";'.format(code, "~".join(raw))


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


def test_tencent_quote_parses_valuation_and_treats_blank_or_zero_as_unknown(monkeypatch) -> None:
    text = "\n".join(
        [
            _quote_line("600000", f39="12.5", f45="520.5", f46="1.3"),
            _quote_line("600001", f39="", f45="0", f46="0"),  # unknown, not zero
            _quote_line("600002", f39="-35.2", f45="88", f46="2.1"),  # loss-maker keeps its sign
        ]
    )
    monkeypatch.setattr(live.httpx, "get", lambda url, timeout: _Response(text))
    quotes = live._tencent_quotes(["600000", "600001", "600002"])
    assert quotes["600000"]["pe"] == 12.5 and quotes["600000"]["total_cap"] == 520.5 and quotes["600000"]["pb"] == 1.3
    assert quotes["600001"]["pe"] is None and quotes["600001"]["total_cap"] is None and quotes["600001"]["pb"] is None
    assert quotes["600002"]["pe"] == -35.2
    # A row with a broken valuation field is still kept for the core fields.
    assert quotes["600001"]["float_cap"] == 300.5


def test_enrichment_fills_valuation_columns_and_leaves_gaps_null(monkeypatch) -> None:
    spot = pl.DataFrame(
        {
            "代码": ["600000", "300750"],
            "名称": ["浦发银行", "宁德时代"],
            "最新价": [10.0, 200.0],
            "涨跌幅": [1.0, 2.0],
            "成交额": [1e9, 2e9],
            "换手率": [1.5, 2.5],
            "量比": [1.2, 2.0],
            "流通市值": [1e11, 5e11],
        }
    )
    quotes = {
        "600000": {"turnover": 1.5, "volume_ratio": 1.2, "float_cap": 1000.0, "limit_up": 11.0,
                   "total_cap": 1500.0, "pe": 6.0, "pb": 0.5},
        "300750": {"turnover": 2.5, "volume_ratio": 2.0, "float_cap": 5000.0, "limit_up": 240.0,
                   "total_cap": None, "pe": None, "pb": None},
    }
    monkeypatch.setattr("backend.app.market.live._tencent_quotes", lambda codes: quotes)
    module = types.ModuleType("akshare")
    module.stock_zh_a_spot_em = lambda: spot
    module.stock_zt_pool_em = lambda date: pl.DataFrame()
    monkeypatch.setitem(sys.modules, "akshare", module)
    frame = AkShareFeed().refresh_now()
    assert frame is not None
    rows = {row["code"]: row for row in frame.to_dicts()}
    assert rows["600000"]["total_mkt_cap"] == 1500.0 and rows["600000"]["pe_ttm"] == 6.0 and rows["600000"]["pb"] == 0.5
    assert rows["300750"]["total_mkt_cap"] is None and rows["300750"]["pe_ttm"] is None and rows["300750"]["pb"] is None


# --- valuation: screener -------------------------------------------------------------------


@pytest.fixture
def valued() -> MarketEngine:
    """An engine whose first rows carry known valuation values, incl. nulls and loss."""
    market = MarketEngine()
    frame = market._frame.sort("code")
    n = frame.height
    pe = [15.0, -8.0, None, 20.0, 20.01] + [50.0] * (n - 5)
    pb = [1.0, 1.0, None, 2.0, 2.01] + [5.0] * (n - 5)
    cap = [120.0, 500.0, None, 50.0, 300.0] + [1000.0] * (n - 5)
    market._frame = frame.with_columns(
        pl.Series("pe_ttm", pe, dtype=pl.Float64),
        pl.Series("pb", pb, dtype=pl.Float64),
        pl.Series("total_mkt_cap", cap, dtype=pl.Float64),
    ).with_columns(pl.lit(False).alias("is_suspended"), pl.lit(False).alias("is_st"), pl.lit(False).alias("is_new"))
    market.first_codes = frame["code"].to_list()[:5]
    return market


def _matched(market: MarketEngine, **filters) -> set[str]:
    request = ScreenerFilterRequest(**filters)
    first = set(market.first_codes)
    return set(market._apply(request)["code"].to_list()) & first


def test_pe_max_keeps_only_positive_known_pe_up_to_the_cap(valued: MarketEngine) -> None:
    codes = valued.first_codes
    # 15.0 and 20.0 pass; loss-maker (-8), unknown (None) and 20.01 do not.
    assert _matched(valued, pe_max=20) == {codes[0], codes[3]}


def test_pb_max_requires_positive_known_pb(valued: MarketEngine) -> None:
    codes = valued.first_codes
    assert _matched(valued, pb_max=2) == {codes[0], codes[1], codes[3]}


def test_total_cap_range_lets_unknown_pass_like_other_ranges(valued: MarketEngine) -> None:
    codes = valued.first_codes
    assert _matched(valued, total_cap_min=100, total_cap_max=400) == {codes[0], codes[2], codes[4]}


def test_no_valuation_filter_means_no_effect(valued: MarketEngine) -> None:
    assert _matched(valued) == set(valued.first_codes)


def test_valuation_fields_are_in_quality_report(valued: MarketEngine) -> None:
    assert {"total_mkt_cap", "pe_ttm", "pb"} <= {name for name, _ in QUALITY_FIELDS}
    report = {item.field: item for item in valued.quality().fields}
    assert report["pe_ttm"].missing == 1 and report["pb"].missing == 1


def test_valuation_reaches_the_stock_record(valued: MarketEngine) -> None:
    code = valued.first_codes[0]
    record = valued.stock(code)
    assert record is not None and record.pe_ttm == 15.0 and record.pb == 1.0 and record.total_mkt_cap == 120.0


# --- single-stock exit rules ---------------------------------------------------------------

CODE = "600000"
START = date(2026, 3, 2)
ENTRY = 6  # the signal fires on bar 5, so the order fills at bar 6's open


def _bar(index: int, o: float, h: float, low: float, c: float) -> OHLCBar:
    return OHLCBar(date=(START + timedelta(days=index)).isoformat(), open=o, high=h, low=low, close=c, volume=1e6)


def _series(overrides: dict[int, tuple[float, float, float, float]], count: int = 20) -> list[OHLCBar]:
    """Flat at 10 with per-bar (open, high, low, close) overrides."""
    bars = []
    for index in range(count):
        o, h, low, c = overrides.get(index, (10.0, 10.05, 9.95, 10.0))
        bars.append(_bar(index, o, h, low, c))
    return bars


def _provenance() -> DataProvenance:
    return DataProvenance(mode="SIM", source="test", as_of="2026-03-30T10:00:00+08:00",
                          trade_date="2026-03-30", detail="fixture", coverage=1)


def _run(bars: list[OHLCBar], monkeypatch, signal_bars: tuple[int, ...] = (5,), **rules):
    monkeypatch.setattr(backtest, "_signal", lambda kind, closes, highs, index, lookback: index in signal_bars)
    request = BacktestRequest(
        code=CODE, lookback=5, holding_days=10,
        commission_bps=0, slippage_bps=0, stamp_duty_bps=0, **rules,
    )
    return backtest.run(request, bars, _provenance())


def test_defaults_keep_the_original_exit_and_assumptions(monkeypatch) -> None:
    result = _run(_series({}), monkeypatch)
    assert result.trade_count == 1
    assert result.trades[0].exit_date == (START + timedelta(days=ENTRY + 10)).isoformat()
    assert not any("离场规则" in note for note in result.assumptions)


def test_stop_loss_fills_at_the_stop_and_ignores_the_entry_bar(monkeypatch) -> None:
    bars = _series({
        ENTRY: (10.0, 10.05, 9.0, 9.9),  # crashes through the stop on the entry bar: T+1 means no exit
        ENTRY + 1: (9.8, 9.85, 9.4, 9.5),  # stop 9.5 touched
    })
    result = _run(bars, monkeypatch, stop_loss_pct=5)
    trade = result.trades[0]
    assert trade.exit_date == (START + timedelta(days=ENTRY + 1)).isoformat()
    assert trade.exit_price == pytest.approx(9.5)
    assert any("止损 1" in note for note in result.assumptions)


def test_stop_gap_down_fills_at_the_open(monkeypatch) -> None:
    bars = _series({ENTRY + 1: (9.2, 9.3, 9.1, 9.2)})  # opens below the 9.5 stop (-8%: not locked)
    trade = _run(bars, monkeypatch, stop_loss_pct=5).trades[0]
    assert trade.exit_price == pytest.approx(9.2)


def test_stop_is_checked_before_target_on_the_same_bar(monkeypatch) -> None:
    bars = _series({ENTRY + 1: (10.0, 10.8, 9.4, 10.0)})  # spans both 9.5 and 10.5
    trade = _run(bars, monkeypatch, stop_loss_pct=5, take_profit_pct=5).trades[0]
    assert trade.exit_price == pytest.approx(9.5)


def test_take_profit_fills_at_target_or_better_open(monkeypatch) -> None:
    at_target = _run(_series({ENTRY + 1: (10.1, 10.6, 10.0, 10.4)}), monkeypatch, take_profit_pct=5)
    assert at_target.trades[0].exit_price == pytest.approx(10.5)
    gap_up = _run(_series({ENTRY + 1: (10.8, 10.9, 10.7, 10.8)}), monkeypatch, take_profit_pct=5)
    assert gap_up.trades[0].exit_price == pytest.approx(10.8)


def test_no_reentry_on_the_bar_that_stopped_out(monkeypatch) -> None:
    bars = _series({ENTRY + 1: (9.8, 9.85, 9.4, 9.5)})
    result = _run(bars, monkeypatch, signal_bars=(5, 6), stop_loss_pct=5)
    assert result.trade_count == 1


def test_trailing_stop_exits_next_open_after_close_breaks_the_peak(monkeypatch) -> None:
    bars = _series({
        ENTRY: (10.0, 10.05, 9.95, 10.0),
        ENTRY + 1: (10.0, 11.05, 9.98, 11.0),  # peak close 11 -> trigger level 10.45
        ENTRY + 2: (11.0, 11.02, 10.35, 10.4),  # close 10.4 < 10.45: exit queued
        ENTRY + 3: (10.3, 10.4, 10.2, 10.3),  # sells at this open
    })
    result = _run(bars, monkeypatch, trailing_stop_pct=5)
    trade = result.trades[0]
    assert trade.exit_date == (START + timedelta(days=ENTRY + 3)).isoformat()
    assert trade.exit_price == pytest.approx(10.3)
    assert any("移动止损 1" in note for note in result.assumptions)


def test_locked_limit_down_open_defers_the_stop_exit(monkeypatch) -> None:
    bars = _series({
        ENTRY + 1: (8.91, 8.91, 8.91, 8.91),  # -10% from 9.9? previous close is 10.0 -> -10.9%: locked
        ENTRY + 2: (9.0, 9.1, 8.95, 9.05),  # first sellable open
    })
    result = _run(bars, monkeypatch, stop_loss_pct=5)
    trade = result.trades[0]
    assert trade.exit_date == (START + timedelta(days=ENTRY + 2)).isoformat()
    assert trade.exit_price == pytest.approx(9.0)
    assert any("顺延卖出 1 次" in note for note in result.assumptions)


def test_rules_that_never_trigger_match_the_default_run(monkeypatch) -> None:
    plain = _run(_series({}), monkeypatch)
    ruled = _run(_series({}), monkeypatch, stop_loss_pct=50, take_profit_pct=200, trailing_stop_pct=50)
    assert [t.model_dump() for t in plain.trades] == [t.model_dump() for t in ruled.trades]
    assert plain.total_return == ruled.total_return


def test_tencent_symbols_send_new_bse_codes_to_bj(monkeypatch) -> None:
    seen: list[str] = []

    def fake_get(url, timeout):
        seen.append(url)
        return _Response("")

    monkeypatch.setattr(live.httpx, "get", fake_get)
    live._tencent_quotes(["920001", "600000", "832491", "000001"])
    assert seen and "bj920001" in seen[0] and "sh600000" in seen[0] and "bj832491" in seen[0] and "sz000001" in seen[0]
