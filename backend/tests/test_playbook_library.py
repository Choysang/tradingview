"""Playbook library: research panel correctness, registry integrity, no look-ahead.

All data is the deterministic SIM warehouse; nothing touches the network.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from backend.app.playbooks import base
from backend.app.playbooks.crowd import CROWD_COLUMNS as CROWD
from backend.app.playbooks.evaluate import _requires_history_missing
from backend.app.playbooks.panel import PANEL_COLUMNS, latest_date, research_panel
from backend.app.research import indicators
from backend.app.warehouse.store import Warehouse, active
from contracts.schema import PlaybookSpec

# Columns documented in playbooks/base.py, in documented order.
DOCUMENTED = (
    "code date open high low close volume amount "
    "prev_close pct_chg limit_ratio limit_up_price limit_down_price "
    "is_limit_up touched_limit is_broken is_limit_down one_word "
    "open_at_limit open_at_floor limit_streak "
    "ma5 ma10 ma20 ma60 ma120 ma250 vol_ma5 vol_ma20 "
    "ret_5 ret_20 ret_60 ret_120 ret_250 volatility_20 "
    "atr_14 high_20_prev high_55_prev high_60_prev high_250_prev low_20_prev "
    "macd_dif macd_dea macd_hist kdj_k kdj_d kdj_j rsi_6 rsi_14 "
    "boll_mid boll_upper boll_lower days_listed "
    "name board total_cap float_cap pe_ttm pb cap_est "
    "industry ind_limit_ups name_heat name_char lhb_on smart_buyers smart_score trip_buyers inst_net"
).split()
# Documented as rare on the SIM dataset:
# - dragon_return: a 4+ board dragon that pulls back 20% and re-ignites.
# - ice_point_reflex: needs a limit-down count at the top decile of its trailing 250 days (and > 0) followed by a
#   rising limit-up count with a buyable first board; SIM has limit-downs on only 10 of 780 days, so the rule is
#   correct (see test_playbook_library_v2.py hand-built panels) but never co-occurs there.
# - k_lower_shadow_volume: needs a lower shadow of 60% of a >4% range on 1.5x volume near a 20-day low; SIM bars have
#   no volume spikes on such days, so it never fires there (hand-built panels in test_playbook_library_tech.py).
RARE = {"dragon_return", "ice_point_reflex", "k_lower_shadow_volume"}


@pytest.fixture(scope="module")
def wh() -> Warehouse:
    return active()


@pytest.fixture(scope="module")
def panel(wh: Warehouse) -> pl.DataFrame:
    return research_panel(wh).collect()


def truncated_warehouse(wh: Warehouse, cutoff, root: Path) -> Warehouse:
    """A copy of the warehouse that has never seen a bar after `cutoff`."""
    copy = Warehouse(root, wh.dataset)
    copy.write("bars", wh.read("bars").filter(pl.col("date") <= cutoff))
    copy.write("index_bars", wh.read("index_bars").filter(pl.col("date") <= cutoff))
    copy.write("securities", wh.read("securities"))
    return copy


# --- research panel ---------------------------------------------------------------------


def test_panel_columns_match_the_documented_contract(panel: pl.DataFrame):
    assert panel.columns == DOCUMENTED
    assert tuple(panel.columns) == PANEL_COLUMNS
    keys = panel.select("code", "date")
    assert keys.sort("code", "date").equals(keys)
    assert keys.is_unique().all()
    assert latest_date(panel) == panel["date"].max()


def test_panel_indicators_match_research_indicators(panel: pl.DataFrame):
    code = panel["code"].unique().sort()[3]
    one = panel.filter(pl.col("code") == code).sort("date")
    closes, highs, lows = one["close"].to_list(), one["high"].to_list(), one["low"].to_list()
    dif, dea, hist = indicators.macd(closes)
    k, d, j = indicators.kdj(highs, lows, closes)
    upper, lower = indicators.boll(closes)
    expected = {
        "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
        "kdj_k": k, "kdj_d": d, "kdj_j": j,
        "rsi_6": indicators.rsi(closes, 6), "rsi_14": indicators.rsi(closes, 14),
        "atr_14": indicators.atr(highs, lows, closes, 14),
        "boll_upper": upper, "boll_lower": lower, "boll_mid": indicators.sma(closes, 20),
        "ma5": indicators.sma(closes, 5), "ma60": indicators.sma(closes, 60), "ma250": indicators.sma(closes, 250),
    }
    for column, reference in expected.items():
        mine = one[column].to_list()
        assert len(mine) == len(reference)
        for index, (a, b) in enumerate(zip(mine, reference)):
            assert (a is None) == (b is None), f"{column}[{index}] warm-up mismatch: {a} vs {b}"
            if a is not None:
                assert a == pytest.approx(b, abs=1e-8), f"{column}[{index}]"


def test_panel_limit_streak_and_cap_estimate(panel: pl.DataFrame):
    chains = panel.filter(pl.col("limit_streak") >= 3)
    assert chains.height > 0
    code = chains["code"][0]
    streak = 0
    for row in panel.filter(pl.col("code") == code).sort("date").iter_rows(named=True):
        streak = streak + 1 if row["is_limit_up"] else 0
        assert row["limit_streak"] == streak
    one = panel.filter(pl.col("code") == code).sort("date")
    last = one["close"][-1]
    assert one["cap_est"][-1] == pytest.approx(one["total_cap"][-1])
    assert one["cap_est"][10] == pytest.approx(one["total_cap"][10] * one["close"][10] / last)


def test_panel_has_no_lookahead(wh: Warehouse, panel: pl.DataFrame, tmp_path: Path):
    dates = panel["date"].unique().sort()
    cutoff = dates[500]
    cut = research_panel(truncated_warehouse(wh, cutoff, tmp_path)).collect()
    full = panel.filter(pl.col("date") <= cutoff).drop("cap_est")  # cap_est scales by the final close by design
    cut = cut.drop("cap_est")
    assert full.shape == cut.shape
    for column in full.columns:
        left, right = full[column], cut[column]
        if left.dtype.is_float():
            assert left.is_null().equals(right.is_null()), column
            assert (left.fill_null(0) - right.fill_null(0)).abs().max() < 1e-8, column
        else:
            assert left.equals(right), column


def test_panel_cache_is_reused_per_warehouse_version(wh: Warehouse):
    first, second = research_panel(wh).collect(), research_panel(wh).collect()
    assert first.equals(second)


# --- registry ---------------------------------------------------------------------------


def test_registry_is_complete_and_specs_validate():
    playbooks = base.catalog()
    ids = [pb.spec.id for pb in playbooks]
    assert len(ids) == len(set(ids)) >= 20
    for pb in playbooks:
        PlaybookSpec.model_validate(pb.spec.model_dump())
        assert pb.spec.rule and pb.spec.summary and pb.spec.source
        assert base.resolve(pb.spec.id) is pb
        for item in pb.spec.params:
            assert item.min <= item.value <= item.max and item.step > 0, (pb.spec.id, item.key)
        if pb.spec.source_url:
            assert pb.spec.source_url.startswith("https://")
        assert pb.params() == {item.key: item.value for item in pb.spec.params}
    assert {pb.spec.horizon for pb in playbooks} == {"short", "swing", "long"}
    with pytest.raises(ValueError):
        base.register(playbooks[0])


def test_requires_history_lists_value_and_the_crowd_sources():
    needing = {pb.spec.id: pb.requires_history for pb in base.catalog() if pb.requires_history}
    assert needing == {
        "value_pe_pb": ("pe_ttm", "pb"),
        "industry_follower": ("ind_limit_ups",), "industry_first_board": ("ind_limit_ups",),
        "name_cluster_follower": ("name_heat",),
        "smart_seat_follow": ("smart_buyers",), "institution_net_buy": ("inst_net",),
    }


@pytest.mark.parametrize("playbook_id", [pb.spec.id for pb in base.catalog()])
def test_signal_shape_and_sim_coverage(panel: pl.DataFrame, playbook_id: str):
    pb = base.resolve(playbook_id)
    signals = pb.signal(panel.lazy(), pb.params()).collect()
    assert signals.columns == ["code", "date", "score"]
    assert signals.is_empty() or signals["score"].min() > 0
    assert signals.select(pl.struct("code", "date").is_unique().all()).item()
    # SIM has no boards or Dragon-Tiger list: playbooks built on them cannot fire there.
    if playbook_id not in RARE and not _requires_history_missing(panel, tuple(c for c in pb.requires_history if c in CROWD)):
        assert signals.height > 0, f"{playbook_id} never fires on the SIM dataset"
    # Unknown override keys are ignored rather than injected into the signal.
    assert pb.params({"unknown_key": 1.0}) == pb.params()


@pytest.mark.parametrize("playbook_id", [pb.spec.id for pb in base.catalog()])
def test_signals_never_look_ahead(panel: pl.DataFrame, playbook_id: str):
    """Signals on a panel cut at D equal the full-panel signals up to D."""
    pb = base.resolve(playbook_id)
    params = pb.params()
    full = pb.signal(panel.lazy(), params).collect()
    dates = panel["date"].unique().sort()
    for cutoff in (dates[330], dates[560], dates[700]):
        cut = pb.signal(panel.filter(pl.col("date") <= cutoff).lazy(), params).collect()
        expected = full.filter(pl.col("date") <= cutoff)
        merged = expected.join(cut, on=["code", "date"], how="full", suffix="_cut", coalesce=True)
        assert merged.height == expected.height == cut.height, f"{playbook_id} @ {cutoff}: row sets differ"
        if merged.height:
            assert (merged["score"] - merged["score_cut"]).abs().max() < 1e-9


def test_factor_playbooks_rebalance_on_period_starts_only(panel: pl.DataFrame):
    monthly = base.resolve("momentum_12_1")
    signals = monthly.signal(panel.lazy(), monthly.params()).collect()
    month_firsts = (
        panel.select("date", (pl.col("date").dt.year() * 100 + pl.col("date").dt.month()).alias("m"))
        .group_by("m").agg(pl.col("date").min()).get_column("date")
    )
    assert set(signals["date"].unique()) <= set(month_firsts)
    weekly = base.resolve("short_reversal").signal(panel.lazy(), base.resolve("short_reversal").params()).collect()
    assert weekly["date"].n_unique() > signals["date"].n_unique()
