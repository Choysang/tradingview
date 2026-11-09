"""Research lab: indicators, pattern rules, factor IC, tear-sheet and HTTP smoke.

Every bar series is built by hand or from the deterministic simulator; nothing
here touches the network.
"""
from __future__ import annotations

from datetime import date, timedelta
import random
import threading
import time

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.app.core.engine import engine
from backend.app.main import app
from backend.app.market.history import HistoryProvider
from backend.app.market.simulation import synthetic_history
from backend.app.research import factor_lab, indicators, patterns, tearsheet
from backend.app.research.backtest import run as run_backtest
from contracts.schema import (
    BacktestRequest,
    DataProvenance,
    EquityPoint,
    FactorLabRequest,
    OHLCBar,
    ScreenerFilterRequest,
    TradeRecord,
)

CODE = "600000"  # main board: limit-up threshold 9.5%


def make_bars(closes, volumes=None, spread=0.01, opens=None, start=date(2025, 1, 1)) -> list[OHLCBar]:
    """Bars where open is the previous close unless overridden; high/low pad by `spread`."""
    bars, previous = [], closes[0]
    for index, close in enumerate(closes):
        opening = opens.get(index, previous) if opens else previous
        bars.append(
            OHLCBar(
                date=(start + timedelta(days=index)).isoformat(),
                open=opening,
                high=max(opening, close) * (1 + spread),
                low=min(opening, close) * (1 - spread),
                close=close,
                volume=(volumes[index] if volumes else 100.0),
            )
        )
        previous = close
    return bars


def hit_ids(bars: list[OHLCBar], code: str = CODE) -> set[str]:
    return {hit.pattern_id for hit in patterns.match(code, "测试", bars)}


def by_key(series, key):
    return next(s for s in series if s.key == key)


def provenance(mode: str = "SIM", coverage: int = 30) -> DataProvenance:
    return DataProvenance(mode=mode, source="test", as_of="2026-01-01T00:00:00", trade_date="2026-01-01", detail="test", coverage=coverage)


# --- indicators --------------------------------------------------------------------


def test_indicators_are_aligned_and_warm_up_is_none() -> None:
    bars = make_bars([float(v) for v in range(1, 41)])
    series = indicators.compute(bars)
    assert [s.key for s in series] == [
        "ma5", "ma10", "ma20", "ma60", "boll_upper", "boll_lower", "vol_ma5", "macd_dif", "macd_dea",
        "macd_hist", "kdj_k", "kdj_d", "kdj_j", "rsi6", "rsi14",
    ]
    assert all(len(s.values) == len(bars) for s in series)
    ma5 = by_key(series, "ma5").values
    assert ma5[3] is None and ma5[4] == 3.0 and ma5[-1] == 38.0
    assert all(v is None for v in by_key(series, "ma60").values)
    assert by_key(series, "macd_hist").style == "histogram"
    assert by_key(series, "vol_ma5").pane == "volume"


def test_macd_is_flat_for_constant_prices_and_positive_in_an_uptrend() -> None:
    flat = indicators.compute(make_bars([10.0] * 60))
    assert by_key(flat, "macd_hist").values[-1] == 0.0
    rising = indicators.compute(make_bars([10 + 0.2 * i for i in range(60)]))
    dif, dea, hist = (by_key(rising, k).values[-1] for k in ("macd_dif", "macd_dea", "macd_hist"))
    assert dif > dea > 0
    assert hist == pytest.approx(2 * (dif - dea), abs=0.01)


def test_kdj_seed_and_recursion() -> None:
    # Nine bars closing on their highest high: RSV = 100, so K = 2/3*50 + 100/3.
    bars = [OHLCBar(date=f"2025-01-{i + 1:02d}", open=10 + i, high=10 + i, low=9 + i, close=10 + i, volume=1) for i in range(9)]
    series = indicators.compute(bars)
    k, d, j = (by_key(series, key).values[-1] for key in ("kdj_k", "kdj_d", "kdj_j"))
    assert k == pytest.approx(66.667, abs=1e-3)
    assert d == pytest.approx(55.556, abs=1e-3)
    assert j == pytest.approx(88.889, abs=1e-3)
    assert by_key(series, "kdj_k").values[7] is None


def test_rsi_stays_within_bounds_and_saturates() -> None:
    rng = random.Random(7)
    closes = [50.0]
    for _ in range(120):
        closes.append(max(1.0, closes[-1] * (1 + rng.gauss(0, 0.03))))
    for key in ("rsi6", "rsi14"):
        values = [v for v in by_key(indicators.compute(make_bars(closes)), key).values if v is not None]
        assert values and all(0 <= v <= 100 for v in values)
    assert by_key(indicators.compute(make_bars([float(v) for v in range(1, 30)])), "rsi14").values[-1] == 100.0


def test_boll_collapses_on_constant_prices() -> None:
    series = indicators.compute(make_bars([8.0] * 30))
    assert by_key(series, "boll_upper").values[-1] == by_key(series, "boll_lower").values[-1] == 8.0


# --- patterns ----------------------------------------------------------------------


def test_every_definition_has_a_rule_and_an_honest_source() -> None:
    assert len(patterns.DEFINITIONS) == 12
    for definition in patterns.DEFINITIONS:
        assert definition.rule and definition.id in patterns._RULES
        assert definition.source_url == "" or definition.source_url.startswith("https://")
        if definition.source.startswith("Sequoia"):
            assert definition.source_url.startswith("https://github.com/sngyai/Sequoia/tree/195cccb8e6")
    sources = {d.id: d.source for d in patterns.DEFINITIONS}
    assert sources["ma_bull"].startswith("Pulse 改编") and sources["kdj_oversold"].startswith("Pulse 改编")
    assert "成交额" in next(d for d in patterns.DEFINITIONS if d.id == "volume_surge").rule


def test_ma_bull() -> None:
    assert "ma_bull" in hit_ids(make_bars([10 + 0.1 * i for i in range(100)]))
    assert "ma_bull" not in hit_ids(make_bars([20 - 0.1 * i for i in range(100)]))


def test_volume_surge() -> None:
    closes = [10.0] * 59 + [10.5]
    assert "volume_surge" in hit_ids(make_bars(closes, [100.0] * 59 + [250.0]))
    assert "volume_surge" in hit_ids(make_bars(closes, [100.0] * 59 + [250.0], opens={59: 10.5}))  # close == open is allowed
    assert "volume_surge" not in hit_ids(make_bars(closes, [100.0] * 59 + [250.0], opens={59: 10.8}))  # close below open
    assert "volume_surge" not in hit_ids(make_bars(closes, [100.0] * 59 + [150.0]))


def test_climax_limitdown_is_board_aware() -> None:
    surge = [100.0] * 59 + [450.0]
    assert "climax_limitdown" in hit_ids(make_bars([10.0] * 59 + [9.0], surge))
    assert "climax_limitdown" not in hit_ids(make_bars([10.0] * 59 + [9.0], [100.0] * 59 + [300.0]))  # only 3x volume
    assert "climax_limitdown" not in hit_ids(make_bars([10.0] * 59 + [9.5], surge))  # -5%
    assert "climax_limitdown" not in hit_ids(make_bars([10.0] * 59 + [9.0], surge), "300001")  # 20cm needs -19.5%
    assert "climax_limitdown" in hit_ids(make_bars([10.0] * 59 + [8.0], surge), "300001")


def test_parking_apron() -> None:
    base = [10.0] * 30 + [11.0]  # limit-up day d = 30, +10%
    opens = {31: 11.05}
    assert "parking_apron" in hit_ids(make_bars(base + [11.1, 11.2, 11.15], opens=opens))
    assert "parking_apron" in hit_ids(make_bars(base + [11.1, 11.2, 11.15, 11.15, 11.15], opens=opens))  # d+3 is still among the last 3
    assert "parking_apron" not in hit_ids(make_bars(base + [11.1, 11.2, 11.15] + [11.15] * 3, opens=opens))
    assert "parking_apron" not in hit_ids(make_bars(base + [11.1, 10.9, 11.15], opens=opens))  # d+2 closes below the limit-up close
    assert "parking_apron" not in hit_ids(make_bars(base + [11.1, 11.2, 11.15]))  # d+1 opens at, not above, the close


def _yearline_closes(low: float = 12.0) -> list[float]:
    closes = [10.0] * 260
    closes.append(9.8)  # index 260, below MA250
    closes += [10 + 6 * (i - 260) / 20 for i in range(261, 281)]  # climbs through MA250 to the peak 16.0 at 280
    closes += [16 - (16 - low) * (i - 280) / 20 for i in range(281, 301)]  # falls to `low` at 300
    closes += [low + 0.5 * (i - 300) / 19 for i in range(301, 320)]
    return closes


def _yearline_volumes(peak: float = 300.0) -> list[float]:
    volumes = [100.0] * 320
    volumes[280] = peak
    return volumes


def test_yearline_pullback() -> None:
    assert "yearline_pullback" in hit_ids(make_bars(_yearline_closes(), _yearline_volumes()))
    assert "yearline_pullback" not in hit_ids(make_bars(_yearline_closes(), _yearline_volumes(150.0)))  # volume only 1.5x
    assert "yearline_pullback" not in hit_ids(make_bars(_yearline_closes(13.5), _yearline_volumes()))  # fell only 16%
    assert "yearline_pullback" not in hit_ids(make_bars(_yearline_closes()[:200], [100.0] * 200))  # needs 250 bars


def test_platform_breakout() -> None:
    flat = [10.0] * 99 + [10.6]
    opens = {99: 9.8}
    hit = patterns.match(CODE, "测试", make_bars(flat, [100.0] * 99 + [200.0], opens=opens))
    assert "platform_breakout" in {h.pattern_id for h in hit}
    assert "platform_breakout" not in hit_ids(make_bars(flat, [100.0] * 99 + [150.0], opens=opens))
    spike = [10.0] * 89 + [12.0] + [10.0] * 9 + [10.6]  # a day 20% above MA60 inside the base
    assert "platform_breakout" not in hit_ids(make_bars(spike, [100.0] * 99 + [200.0], opens=opens))


def test_platform_breakout_reports_the_most_recent_breakout_day() -> None:
    closes = [10.0] * 95 + [10.6] * 5
    volumes = [100.0] * 95 + [200.0] + [100.0] * 4
    bars = make_bars(closes, volumes, opens={95: 9.8})
    hits = [h for h in patterns.match(CODE, "测试", bars) if h.pattern_id == "platform_breakout"]
    assert hits and hits[0].date == bars[95].date


def test_low_drawdown() -> None:
    steady = [10 * 1.01 ** i for i in range(70)]
    assert "low_drawdown" in hit_ids(make_bars(steady))
    shaken = steady[:40] + [v * 0.92 for v in steady[40:]]  # a -8% day inside the window
    assert "low_drawdown" not in hit_ids(make_bars(shaken))
    gap_and_fade = {50: steady[49] * 1.12}  # opens 12% higher and closes at the old trend: -10.7% intraday
    assert "low_drawdown" not in hit_ids(make_bars(steady, opens=gap_and_fade))
    assert "low_drawdown" not in hit_ids(make_bars([10 + 0.01 * i for i in range(70)]))  # not enough gain


def test_turtle_breakout() -> None:
    assert "turtle_breakout" in hit_ids(make_bars([10.0] * 60 + [10.5]))
    assert "turtle_breakout" not in hit_ids(make_bars([10.0] * 30 + [11.0] + [10.0] * 29 + [10.5]))  # an older 60-bar high is higher


def test_high_tight_flag() -> None:
    ups = [10 * 1.2 ** i for i in range(1, 5)]  # four 20cm limit-ups, 2.07x
    flag = [10.0] * 20 + ups + [ups[-1]] * 12
    assert "high_tight_flag" in hit_ids(make_bars(flag), "300001")
    assert "high_tight_flag" not in hit_ids(make_bars([10.0] * 20 + [12.0] * 16), "300001")  # one limit-up, 1.2x only
    assert "high_tight_flag" not in hit_ids(make_bars([10.0] * 30 + ups + [ups[-1]] * 2), "300001")  # surge is inside the last 10 bars, not the window


def test_low_atr_growth() -> None:
    steady = [10 * 1.003 ** i for i in range(80)]
    assert "low_atr_growth" in hit_ids(make_bars(steady, spread=0.002))
    assert "low_atr_growth" not in hit_ids(make_bars(steady, spread=0.05))


def _first_trigger(closes: list[float], pattern_id: str, spread: float = 0.01) -> int | None:
    """Smallest prefix length at which the latest bar triggers the pattern."""
    for size in range(40, len(closes) + 1):
        if pattern_id in hit_ids(make_bars(closes[:size], spread=spread)):
            return size
    return None


def test_macd_bottom_cross_fires_on_the_crossing_bar_only() -> None:
    closes = [100 - 0.5 * i for i in range(80)] + [60 + 0.6 * i for i in range(30)]
    size = _first_trigger(closes, "macd_bottom_cross")
    assert size is not None
    dif, dea, _ = indicators.macd(closes[:size])
    assert dif[-2] <= dea[-2] and dif[-1] > dea[-1] and dif[-1] < 0 and dea[-1] < 0
    assert "macd_bottom_cross" not in hit_ids(make_bars(closes[: size - 1]))
    assert _first_trigger([100 - 0.5 * i for i in range(80)], "macd_bottom_cross") is None


def test_kdj_oversold_fires_on_the_crossing_bar_only() -> None:
    closes = [100 - 0.8 * i for i in range(70)] + [44 + 0.9 * i for i in range(20)]
    size = _first_trigger(closes, "kdj_oversold")
    assert size is not None
    highs = [c * 1.01 for c in closes[:size]]
    lows = [c * 0.99 for c in closes[:size]]
    k, d, _ = indicators.kdj(highs, lows, closes[:size])
    assert k[-2] <= d[-2] and k[-1] > d[-1]
    assert _first_trigger([100 + 0.5 * i for i in range(90)], "kdj_oversold") is None


def test_scan_groups_hits_per_pattern_and_sorts_by_strength() -> None:
    frames = []
    for code, slope in (("600001", 0.1), ("600002", 0.2), ("600003", -0.1)):
        bars = make_bars([20 + slope * i for i in range(100)])
        frames.append(pl.DataFrame({
            "code": [code] * 100, "date": [b.date for b in bars], "open": [b.open for b in bars],
            "high": [b.high for b in bars], "low": [b.low for b in bars], "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }))
    summaries = patterns.scan(pl.concat(frames), {"600002": "强势股"})
    assert [s.pattern.id for s in summaries] == [d.id for d in patterns.DEFINITIONS]
    bull = next(s for s in summaries if s.pattern.id == "ma_bull")
    assert bull.hit_count == 2
    assert [h.code for h in bull.hits] == ["600002", "600001"]
    assert bull.hits[0].name == "强势股"


# --- history panel -----------------------------------------------------------------


def test_sim_panel_is_synthetic_deterministic_and_cached() -> None:
    history = HistoryProvider()
    history._live = False
    codes = [("600000", 10.0), ("000001", None)]
    frame, info = history.panel(codes, "SIM")
    assert info.mode == "SIM" and info.coverage == 2
    assert frame.columns == ["code", "date", "open", "high", "low", "close", "volume"]
    assert frame.height == 2 * 260
    again, _ = history.panel(codes, "SIM")
    assert again is frame


def test_sim_panel_cache_is_keyed_on_members_not_count() -> None:
    history = HistoryProvider()
    history._live = False
    first, _ = history.panel([("600000", 10.0), ("000001", None)], "SIM")
    second, _ = history.panel([("600000", 10.0), ("000002", None)], "SIM")  # same count, different member
    assert set(first["code"]) == {"600000", "000001"} and set(second["code"]) == {"600000", "000002"}


def history_now():
    from backend.app.market.history import _now

    return _now()


def test_live_panel_uses_every_cached_real_bar_series_and_starts_no_thread_offline() -> None:
    history = HistoryProvider()
    history._live = False
    # entries are only used on the day they were made
    real = DataProvenance(mode="LIVE", source="t", as_of=history_now().isoformat(), trade_date="2026-01-02", detail="d")
    history._cache["601000"] = (make_bars([30.0] * 5), real.model_copy(update={"as_of": "2026-01-02T15:00:00+08:00"}))
    history._cache["600000"] = (make_bars([10.0] * 5), real)
    history._cache["000001"] = (make_bars([9.0] * 5), real.model_copy(update={"mode": "SIM"}))
    history._cache["688001"] = (make_bars([20.0] * 5), real)  # cached, but not in today's top list
    frame, info = history.panel([("600000", 10.0), ("000001", 9.0), ("300001", 5.0)], "LIVE")
    assert info.mode == "LIVE" and info.coverage == 2
    assert set(frame["code"]) == {"600000", "688001"}
    assert "选择偏差" in info.detail and "已缓存真实日线的 2 只" in info.detail
    history._cache["300002"] = (make_bars([5.0] * 5), real)
    history._cache.pop("688001")  # same member count as before, different members
    swapped, _ = history.panel([], "LIVE")
    assert set(swapped["code"]) == {"600000", "300002"}


def test_bars_fetch_does_not_hold_the_provider_lock() -> None:
    history = HistoryProvider()
    history._live = True
    history._warmed.add(history_today())  # no warm-up thread in this test
    started, release = threading.Event(), threading.Event()

    def slow_fetch(code: str):
        started.set()
        release.wait(5)
        return None

    history._live_bars = slow_fetch  # type: ignore[method-assign]
    worker = threading.Thread(target=history.bars, args=("600000", 10.0))
    worker.start()
    assert started.wait(2)
    begun = time.perf_counter()
    history.panel([("000001", 5.0)], "LIVE")  # needs the lock while the fetch is still blocked
    assert time.perf_counter() - begun < 1.0
    release.set()
    worker.join(5)
    assert history.bars("600000")[1].mode == "SIM"


def history_today() -> str:
    from backend.app.market.history import _now

    return _now().date().isoformat()


# --- factor lab --------------------------------------------------------------------


def growth_panel(codes: int = 30, days: int = 150) -> pl.DataFrame:
    """Each stock compounds at its own constant rate, so every factor ranks perfectly."""
    start = date(2025, 1, 1)
    rows = []
    for c in range(codes):
        growth = 1 + (c - codes // 2) * 0.001
        price = 100.0
        for t in range(days):
            price *= growth
            rows.append((f"{600000 + c}", (start + timedelta(days=t)).isoformat(), price, price * 1.01, price * 0.99, price, 1000.0 + c))
    return pl.DataFrame(rows, schema=["code", "date", "open", "high", "low", "close", "volume"], orient="row")


def noise_panel(codes: int = 30, days: int = 160, seed: int = 3) -> pl.DataFrame:
    rng = random.Random(seed)
    start = date(2025, 1, 1)
    rows = []
    for c in range(codes):
        price = 50.0
        for t in range(days):
            price *= 1 + rng.gauss(0, 0.02)
            rows.append((f"{600000 + c}", (start + timedelta(days=t)).isoformat(), price, price * 1.01, price * 0.99, price, rng.uniform(500, 1500)))
    return pl.DataFrame(rows, schema=["code", "date", "open", "high", "low", "close", "volume"], orient="row")


def test_perfect_factor_gives_unit_ic_and_monotonic_quantiles() -> None:
    result = factor_lab.run(growth_panel(), FactorLabRequest(factor="momentum_20", horizon=5, quantiles=5, window=100), provenance("LIVE", 300))
    assert result.universe == 30 and result.days == 100
    assert result.ic_mean == pytest.approx(1.0, abs=1e-3)
    assert result.ic_positive_ratio == 100.0
    returns = [q.mean_return for q in result.quantile_returns]
    assert [q.quantile for q in result.quantile_returns] == [1, 2, 3, 4, 5]
    assert returns == sorted(returns) and returns[-1] > returns[0]
    assert result.long_short[-1].value > 0
    assert len(result.long_short) == 100 // 5
    assert result.top_turnover == 0.0  # the same names stay on top
    assert len(result.catalog) == 6 and not any("合成" in n for n in result.notes)
    assert any("选择偏差" in n for n in result.notes)  # LIVE always states the universe rule


def test_reversal_factor_is_signed() -> None:
    result = factor_lab.run(growth_panel(), FactorLabRequest(factor="reversal_5", horizon=5, window=60), provenance("LIVE", 300))
    assert result.ic_mean == pytest.approx(-1.0, abs=1e-3)


def test_random_walks_have_no_predictable_factor() -> None:
    result = factor_lab.run(noise_panel(), FactorLabRequest(factor="momentum_20", horizon=5, window=120), provenance("SIM"))
    assert result.days == 120
    assert abs(result.ic_mean) < 0.1
    assert any("合成" in note for note in result.notes)
    assert sum(q.count for q in result.quantile_returns) > 0


@pytest.mark.parametrize("factor", ["momentum_20", "reversal_5", "volatility_20", "volume_surge", "dist_high_60", "ma_gap_20"])
def test_every_factor_runs(factor: str) -> None:
    result = factor_lab.run(noise_panel(), FactorLabRequest(factor=factor, window=60), provenance("SIM"))
    assert result.days == 60 and result.ic_mean is not None and len(result.quantile_returns) == 5


def test_too_few_names_gives_null_ic() -> None:
    result = factor_lab.run(growth_panel(codes=5), FactorLabRequest(window=40), provenance("SIM"))
    assert result.ic_mean is None and all(p.ic is None for p in result.ic_series)
    assert result.quantile_returns == []


def test_empty_panel_is_reported_not_crashed() -> None:
    empty = growth_panel().clear()
    result = factor_lab.run(empty, FactorLabRequest(), provenance("SIM", 0))
    assert result.days == 0 and result.ic_series == [] and "无法评估" in result.notes[0]


# --- tear-sheet --------------------------------------------------------------------


DATES = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05"]
STRATEGY = [0.0, 10.0, 5.0, 8.0, 4.0, 20.0]


def known_equity() -> list[EquityPoint]:
    return [EquityPoint(date=d, strategy=s, benchmark=b, drawdown=0.0) for d, s, b in zip(DATES, STRATEGY, [0, 1, 2, 3, 4, 6])]


def trade(entry: str, exit_: str, pnl: float, pct: float) -> TradeRecord:
    return TradeRecord(entry_date=entry, exit_date=exit_, entry_price=10, exit_price=10, shares=100, pnl=pnl, return_pct=pct, costs=0)


def test_tearsheet_on_a_known_curve() -> None:
    trades = [
        trade(DATES[0], DATES[2], 500, 5), trade(DATES[1], DATES[2], -200, -2),
        trade(DATES[2], DATES[3], -100, -1), trade(DATES[3], DATES[5], 300, 3),
    ]
    m = tearsheet.metrics(known_equity(), trades, 100_000)
    assert m.annual_return == pytest.approx((1.2 ** (252 / 5) - 1) * 100, rel=1e-6)
    assert m.benchmark_annual_return == pytest.approx((1.06 ** (252 / 5) - 1) * 100, rel=1e-6)
    assert m.max_drawdown_days == 3  # 105, 108, 104 stay below the 110 peak
    assert m.profit_factor == pytest.approx(800 / 300, abs=1e-3)
    assert m.expectancy == pytest.approx(1.25)
    assert (m.best_trade, m.worst_trade) == (5, -2)
    assert m.max_consecutive_losses == 2
    assert m.avg_holding_days == pytest.approx((2 + 1 + 1 + 2) / 4)
    assert m.annual_volatility is not None and m.annual_volatility > 0
    assert m.sortino_ratio is not None and m.sortino_ratio > 0


def test_calmar_uses_the_worst_peak_to_trough_drop() -> None:
    m = tearsheet.metrics(known_equity(), [], 100_000)
    assert m.calmar_ratio == pytest.approx(m.annual_return / ((110 - 104) / 110 * 100), rel=1e-3)


def test_annualisation_counts_returns_between_bars_not_bars() -> None:
    two = [EquityPoint(date="2026-01-05", strategy=0.0, benchmark=0.0, drawdown=0.0),
           EquityPoint(date="2026-01-06", strategy=10.0, benchmark=5.0, drawdown=0.0)]
    m = tearsheet.metrics(two, [], 100_000)  # one daily return of +10%
    assert m.annual_return == pytest.approx((1.1 ** 252 - 1) * 100, rel=1e-9)
    assert m.benchmark_annual_return == pytest.approx((1.05 ** 252 - 1) * 100, rel=1e-9)
    assert tearsheet.metrics(two[:1], [], 100_000).annual_return is None  # no return yet


def test_tearsheet_leaves_unknowns_null() -> None:
    wins_only = [trade(DATES[0], DATES[1], 100, 1)]
    m = tearsheet.metrics(known_equity(), wins_only, 100_000)
    assert m.profit_factor is None and m.max_consecutive_losses == 0
    none = tearsheet.metrics([], [], 100_000)
    assert none.annual_return is None and none.expectancy is None and none.max_drawdown_days is None


def test_monthly_returns_group_month_ends() -> None:
    months = tearsheet.monthly(known_equity())
    assert [m.month for m in months] == ["2026-01", "2026-02"]
    assert months[0].value == pytest.approx(10.0)
    assert months[1].value == pytest.approx((1.2 / 1.1 - 1) * 100, abs=1e-3)


def test_backtest_response_carries_metrics_that_reconcile() -> None:
    bars = synthetic_history("600000", date(2026, 10, 20), count=260)
    result = run_backtest(BacktestRequest(strategy_id="momentum_ignition", code="600000"), bars, provenance("SIM", 260))
    assert result.metrics is not None and result.monthly_returns
    growth = 1.0
    for month in result.monthly_returns:
        growth *= 1 + month.value / 100
    assert (growth - 1) * 100 == pytest.approx(result.total_return, abs=0.05)
    assert result.metrics.max_consecutive_losses >= 0


# --- HTTP --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/v1/admin/mode", json={"mode": "SIM"})
        yield test_client


def test_patterns_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/lab/patterns?limit=5").json()
    assert body["provenance"]["mode"] == "SIM"
    assert body["scanned"] > 0 and body["universe"] >= body["scanned"]
    assert [p["pattern"]["id"] for p in body["patterns"]] == [d.id for d in patterns.DEFINITIONS]
    for summary in body["patterns"]:
        assert summary["hit_count"] >= len(summary["hits"]) and len(summary["hits"]) <= 5
        strengths = [h["strength"] for h in summary["hits"]]
        assert strengths == sorted(strengths, reverse=True)


def test_factors_endpoint(client: TestClient) -> None:
    body = client.post("/api/v1/lab/factors", json={"factor": "momentum_20", "horizon": 5, "quantiles": 5, "window": 120}).json()
    assert body["days"] == 120 and len(body["quantile_returns"]) == 5
    assert len(body["catalog"]) == 6 and body["ic_mean"] is not None
    assert any("合成" in note for note in body["notes"])
    assert client.post("/api/v1/lab/factors", json={"factor": "nope"}).status_code == 422


def test_pattern_scan_cache_survives_different_limits(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real_scan = patterns.scan
    monkeypatch.setattr(patterns, "scan", lambda *a, **k: calls.append(1) or real_scan(*a, **k))
    first = client.get("/api/v1/lab/patterns?limit=3").json()
    made = len(calls)
    second = client.get("/api/v1/lab/patterns?limit=40").json()
    again = client.get("/api/v1/lab/patterns?limit=3").json()
    assert len(calls) == made <= 1
    assert all(len(p["hits"]) <= 3 for p in first["patterns"]) and again["patterns"] == first["patterns"]
    assert [p["hit_count"] for p in second["patterns"]] == [p["hit_count"] for p in first["patterns"]]
    assert any(len(p["hits"]) > 3 for p in second["patterns"])


def test_stock_detail_includes_aligned_indicators(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine().history, "_live", False)
    code = engine().query(ScreenerFilterRequest(limit=1)).records[0].code
    body = client.get(f"/api/v1/stocks/{code}").json()
    assert body["history_provenance"]["mode"] == "SIM"
    assert len(body["indicators"]) == 15
    assert all(len(s["values"]) == len(body["bars"]) for s in body["indicators"])
    assert isinstance(body["patterns"], list)
