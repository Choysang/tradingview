"""Engine, factor and screener behaviour on the deterministic simulation frame."""
from __future__ import annotations

import pytest
import polars as pl

from backend.app.core.engine import MarketEngine
from contracts.schema import MacroPoolType, ScreenerFilterRequest, TurnoverRegime


@pytest.fixture(scope="module")
def market() -> MarketEngine:
    return MarketEngine()


def test_overview_invariants(market: MarketEngine) -> None:
    overview = market.overview()
    suspended = sum(1 for record in market.query(
        ScreenerFilterRequest(limit=6000, include_suspended=True, exclude_new=False)
    ).records if record.is_suspended)
    assert overview.total_stocks == overview.provenance.coverage
    assert overview.total_limit_up + overview.total_broken_board <= overview.total_stocks
    assert 0 <= overview.broken_rate <= 100
    assert 0 <= overview.sentiment_score <= 100
    # Breadth describes trading names only; suspended rows are excluded.
    assert overview.breadth.up + overview.breadth.down + overview.breadth.flat == overview.total_stocks - suspended
    assert sum(overview.breadth.buckets) == overview.total_stocks - suspended
    assert len(overview.breadth.buckets) == len(overview.breadth.bucket_labels)
    assert overview.max_limit_height >= 1


def test_ladder_tiers_are_consistent(market: MarketEngine) -> None:
    overview = market.overview()
    assert [tier.count for tier in overview.limit_ladder_tiers][0] > 0
    assert sum(tier.count for tier in overview.limit_ladder_tiers) == overview.total_limit_up
    assert overview.limit_ladder_tiers[0].promotion_rate is None  # no denominator for 1板
    for tier in overview.limit_ladder_tiers[1:]:
        assert tier.promotion_rate is None or 0 <= tier.promotion_rate <= 100


def test_theme_strength_is_ordered(market: MarketEngine) -> None:
    themes = market.overview().hot_themes
    assert themes
    strengths = [theme.strength for theme in themes]
    assert strengths == sorted(strengths, reverse=True)
    for theme in themes:
        assert theme.stock_count >= theme.limit_up_count
        assert theme.leader_code


def test_turnover_ratio_matches_definition(market: MarketEngine) -> None:
    response = market.query(ScreenerFilterRequest(limit=50))
    checked = 0
    for record in response.records:
        if record.turnover_yesterday and record.turnover_today is not None:
            expected = record.turnover_today / record.turnover_yesterday
            assert record.turnover_ratio_yd == pytest.approx(expected, rel=1e-6)
            checked += 1
    assert checked > 0


def test_composite_score_is_weighted_sum(market: MarketEngine) -> None:
    record = market.query(ScreenerFilterRequest(limit=1)).records[0]
    weighted = sum((factor.score or 0) * factor.weight for factor in record.factors)
    assert record.composite_score == pytest.approx(weighted, abs=0.02)


def test_pool_filters(market: MarketEngine) -> None:
    limit_up = market.query(ScreenerFilterRequest(pool_type=MacroPoolType.LIMIT_UP_LADDER, limit=6000))
    assert all(record.is_limit_up for record in limit_up.records)
    broken = market.query(ScreenerFilterRequest(pool_type=MacroPoolType.BROKEN_OR_ATTACK, limit=6000))
    assert all(record.ever_limit_up_today and not record.is_limit_up for record in broken.records)
    growth = market.query(ScreenerFilterRequest(pool_type=MacroPoolType.CONTINUOUS_GROWTH, limit=6000))
    assert all((record.growth_streak or 0) >= 3 for record in growth.records)


def test_turnover_regimes_partition(market: MarketEngine) -> None:
    total = market.query(ScreenerFilterRequest(limit=6000)).total_matched
    parts = [
        market.query(ScreenerFilterRequest(turnover_regime=regime, limit=6000)).total_matched
        for regime in (TurnoverRegime.SHRINK_ACCEL, TurnoverRegime.NORMAL_HAND, TurnoverRegime.EXPAND_DIV)
    ]
    # Every record either falls in one regime or has no computable yesterday turnover.
    assert sum(parts) <= total
    unknown = total - sum(parts)
    records = market.query(ScreenerFilterRequest(limit=6000)).records
    assert unknown == sum(1 for record in records if record.turnover_ratio_yd is None)


def test_sorting_and_pagination(market: MarketEngine) -> None:
    first = market.query(ScreenerFilterRequest(sort_by="amount_today", sort_desc=True, limit=5))
    second = market.query(ScreenerFilterRequest(sort_by="amount_today", sort_desc=True, offset=5, limit=5))
    amounts = [record.amount_today for record in first.records]
    assert amounts == sorted(amounts, reverse=True)
    assert {record.code for record in first.records}.isdisjoint({record.code for record in second.records})


def test_search_and_codes(market: MarketEngine) -> None:
    sample = market.query(ScreenerFilterRequest(limit=1)).records[0]
    by_code = market.query(ScreenerFilterRequest(codes=[sample.code], limit=10))
    assert by_code.total_matched == 1
    assert by_code.records[0].name == sample.name
    by_name = market.query(ScreenerFilterRequest(search=sample.name[:2], limit=6000))
    assert by_name.total_matched >= 1


def test_range_validation_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError):
        ScreenerFilterRequest(price_min=50, price_max=10)


def test_seal_ratio_only_exists_for_sealed_boards(market: MarketEngine) -> None:
    records = market.query(ScreenerFilterRequest(limit=6000)).records
    for record in records:
        if record.is_limit_up:
            assert record.seal_to_amount_ratio is not None
        else:
            assert record.seal_to_amount_ratio is None
        assert record.composite_score is not None


def test_suspended_and_new_listings_are_opt_in(market: MarketEngine) -> None:
    default = market.query(ScreenerFilterRequest(limit=6000))
    everything = market.query(
        ScreenerFilterRequest(limit=6000, include_suspended=True, exclude_new=False)
    )
    assert everything.total_matched > default.total_matched
    assert all(not record.is_suspended and not record.is_new for record in default.records)
    assert any(record.is_new for record in everything.records)
    assert any(record.is_suspended for record in everything.records)


def test_new_listings_never_count_as_limit_up(market: MarketEngine) -> None:
    records = market.query(
        ScreenerFilterRequest(limit=6000, include_suspended=True, exclude_new=False)
    ).records
    for record in records:
        if record.days_since_listing is not None and record.days_since_listing < 5:
            assert record.is_limit_up is False
            assert not record.limit_streak


def test_breadth_ignores_suspended_names(market: MarketEngine) -> None:
    overview = market.overview()
    breadth = overview.breadth
    suspended = market.query(
        ScreenerFilterRequest(limit=6000, include_suspended=True, exclude_new=False)
    ).records
    suspended_count = sum(1 for record in suspended if record.is_suspended)
    assert breadth.up + breadth.down + breadth.flat == overview.total_stocks - suspended_count


def test_growth_pool_respects_doji_tolerance(market: MarketEngine) -> None:
    strict = market.query(
        ScreenerFilterRequest(pool_type=MacroPoolType.CONTINUOUS_GROWTH, min_growth_streak=5, limit=6000)
    )
    tolerant = market.query(
        ScreenerFilterRequest(
            pool_type=MacroPoolType.CONTINUOUS_GROWTH,
            min_growth_streak=5,
            allow_doji_tolerance=True,
            limit=6000,
        )
    )
    assert all((record.growth_streak or 0) >= 5 for record in strict.records)
    assert tolerant.total_matched >= strict.total_matched
    assert {record.code for record in strict.records} <= {record.code for record in tolerant.records}


def test_screener_handles_full_market_size(market: MarketEngine) -> None:
    """Replicate the frame to ~5,300 rows and keep the filter pass interactive."""
    base = market._frame
    expected = market.query(ScreenerFilterRequest(pool_type=MacroPoolType.LIMIT_UP_LADDER, limit=1)).total_matched
    copies = [
        base.with_columns((pl.col("code") + pl.lit(f"{index:03d}")).alias("code"))
        for index in range(20)
    ]
    market._frame = pl.concat(copies)
    try:
        market.query(ScreenerFilterRequest(limit=10))  # warm-up
        response = market.query(
            ScreenerFilterRequest(pool_type=MacroPoolType.LIMIT_UP_LADDER, limit=100)
        )
        assert response.total_matched == expected * 20
        assert len(response.records) == 100
        assert response.execution_ms < 150
    finally:
        market._frame = base


def test_observations_only_state_facts(market: MarketEngine) -> None:
    record = market.query(ScreenerFilterRequest(pool_type=MacroPoolType.LIMIT_UP_LADDER, limit=1)).records[0]
    notes = market.observations(record)
    assert notes
    assert all("建议" not in note and "买入" not in note for note in notes)


def test_range_filters_let_unknowns_through() -> None:
    from backend.app.core.engine import _between

    frame = pl.DataFrame({"value": [None, 1.0, 3.0]})
    assert frame.filter(_between("value", 2.0, None, None)).height == 2  # null + 3.0
    assert frame.filter(_between("value", None, 2.0, None)).height == 2  # null + 1.0
    assert frame.filter(_between("value", 0, 100, 100)).height == 3  # neutral defaults


def test_early_seal_and_seal_span_filters(market: MarketEngine) -> None:
    early = market.query(ScreenerFilterRequest(limit=6000, only_early_seal=True))
    assert all(record.early_seal for record in early.records)
    assert all(record.is_limit_up for record in early.records)

    spanned = market.query(ScreenerFilterRequest(limit=6000, min_seal_span_minutes=30))
    assert all((record.seal_span_minutes or 0) >= 30 for record in spanned.records)
