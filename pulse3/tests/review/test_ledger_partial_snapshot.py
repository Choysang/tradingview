"""Review fix H1/M1: the one ledger values an account with possibly missing marks (null, never 0)."""
from decimal import Decimal

from pulse.domain.execution import LedgerReducer


def _ledger():
    return LedgerReducer(trading_day="2026-11-10", opening_cash="1000",
                         positions={"A": {"quantity": 100, "cost": "500"}, "B": {"quantity": 200, "cost": "400"}})


def test_snapshot_partial_complete_matches_snapshot():
    lg = _ledger()
    full = lg.snapshot({"A": "6", "B": "3"})
    part = lg.snapshot_partial({"A": Decimal("6"), "B": Decimal("3")})
    assert part["missing_marks"] == []
    assert part["nav"] == Decimal(full["nav"]) and part["unrealized"] == Decimal(full["unrealized"])
    assert part["pnl_from_cost_origin"] == Decimal(full["pnl_from_cost_origin"])
    assert part["positions"]["A"]["market_value"] == Decimal("600")


def test_snapshot_partial_missing_mark_gives_null_nav_not_zero():
    lg = _ledger()
    part = lg.snapshot_partial({"A": Decimal("6"), "B": None})
    assert part["missing_marks"] == ["B"]
    assert part["nav"] is None and part["unrealized"] is None and part["pnl_from_cost_origin"] is None
    assert part["positions"]["B"]["market_value"] is None
    assert part["positions"]["A"]["market_value"] == Decimal("600")
    assert lg.snapshot_partial({"A": Decimal("6")})["missing_marks"] == ["B"]          # absent key == missing


def test_snapshot_partial_quantizes_to_ledger_scale():
    lg = LedgerReducer(trading_day="2026-11-10", opening_cash="10", positions={"A": {"quantity": 3, "cost": "1"}})
    part = lg.snapshot_partial({"A": Decimal("0.3333333333333333333")})
    assert part["positions"]["A"]["market_value"] == Decimal("1.000000000000")
    assert part["nav"].as_tuple().exponent == -12
