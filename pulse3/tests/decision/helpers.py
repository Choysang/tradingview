"""Deterministic builders shared by the decision tests (synthetic, contract-valid)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid5

from pulse.domain.ranking.baseline import BaselinePolicy, FeatureSpec, RankingTarget
from pulse.domain.ranking.universe import InstrumentFacts

NS = UUID("11111111-2222-3333-4444-555555555555")
T0 = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
D = Decimal


def uid(name: str) -> UUID:
    return uuid5(NS, name)


def facts(n: int, **over) -> list[InstrumentFacts]:
    out = []
    for i in range(n):
        base = dict(
            instrument_id=uid(f"inst{i}"), venue_code="XSHG" if i % 2 else "XSHE", board_code="mainboard",
            instrument_type="equity", is_st=False, listed_sessions=500, price_raw=D("20") + i,
            circulating_market_cap_cny=D("5000000000"),
        )
        base.update(over)
        out.append(InstrumentFacts(**base))
    return out


TARGET = RankingTarget("rel_opportunity_5td_after_declared_costs", "5td", "cost-v1", "mainboard_equal_weight")


def policy(**kw) -> BaselinePolicy:
    feats = kw.pop("features", (
        FeatureSpec("mom20", "1", 1, D("2"), "price_momentum"),
        FeatureSpec("mom20_dup", "1", 1, D("1"), "price_momentum"),  # same source group -> dropped
        FeatureSpec("turnover_z", "1", -1, D("1"), "liquidity"),
        FeatureSpec("sector_rs", "1", 1, D("1"), "relative_strength"),
    ))
    return BaselinePolicy("baseline-v1", TARGET, feats, **kw)


def matrix(ids, n_missing: dict | None = None):
    m = {}
    for k, iid in enumerate(ids):
        m[iid] = {
            "mom20": D(k) * D("0.37") + D("1"),
            "mom20_dup": D(k) * D("0.37") + D("1.001"),
            "turnover_z": D((k * 7) % 5),
            "sector_rs": D((k * 3) % 7) - D("2.5"),
        }
    for iid, fid in (n_missing or {}).items():
        m[iid][fid] = None
    return m
