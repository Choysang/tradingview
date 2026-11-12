"""Review fix H1: sizing is None when NAV/price is unknown, 0 shares is insufficient, no forced one-lot minimum."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pulse.application import account_service as A
from pulse.domain.execution import LedgerReducer
from pulse.domain.plans.conditions import Tri
from pulse.domain.portfolio.actionability import ActionabilityInput, PlanLeg, QuoteView, evaluate_actionability

I1 = uuid.UUID(int=1)
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)


def _row(qty, quote=Decimal("10")):
    inp = ActionabilityInput(
        release_id=uuid.UUID(int=2), ranking_id=uuid.UUID(int=3), quote_snapshot_id=uuid.UUID(int=4), policy_version="p",
        checked_at=NOW, valid_until=NOW + timedelta(minutes=15), ranking_order=[(I1, 1)],
        plans={I1: [PlanLeg((uuid.UUID(int=5), 1), "buy", qty, Tri.TRUE)]}, quotes={I1: QuoteView(I1, quote, True, True)}, account=None)
    return evaluate_actionability(inp)["entries"][0]


def test_unknown_sizing_leg_is_unknown():
    e = _row(None)
    assert e["actionability_status"] == "unknown" and "SIZING_UNKNOWN" in e["reason_codes"] and e["checked_quantity_shares"] is None


def test_zero_sizing_leg_is_blocked_insufficient():
    e = _row(0)
    assert e["actionability_status"] == "blocked" and "INSUFFICIENT_SIZE" in e["reason_codes"] and e["checked_quantity_shares"] is None


def test_leg_quantity_rules():
    pct = {"kind": "portfolio_fraction", "fraction": "0.05"}
    assert A.leg_quantity(pct, Decimal("10"), Decimal("100000")) == 500
    assert A.leg_quantity(pct, Decimal("10"), Decimal("1000")) == 0                  # 5 shares -> 0 lots, NOT forced to 100
    assert A.leg_quantity(pct, None, Decimal("100000")) is None                      # no price
    assert A.leg_quantity(pct, Decimal("10"), None) is None                          # NAV unknown
    assert A.leg_quantity({"kind": "fixed_shares", "quantity_shares": 300}, None, None) == 300
    assert A.leg_quantity({"kind": "mystery"}, Decimal("10"), Decimal("1")) is None


def test_account_nav_uses_ledger_and_is_none_when_a_mark_is_missing():
    held, other = str(uuid.UUID(int=11)), str(uuid.UUID(int=12))
    lg = LedgerReducer(trading_day="2026-11-10", opening_cash="1000",
                       positions={held: {"quantity": 100, "cost": "500"}, other: {"quantity": 10, "cost": "50"}})
    views = {uuid.UUID(held): QuoteView(uuid.UUID(held), Decimal("6"), True, True)}
    assert A.account_nav(lg, views) is None                                           # `other` has no quote: not valued at 0
    views[uuid.UUID(other)] = QuoteView(uuid.UUID(other), None, None, None)
    assert A.account_nav(lg, views) is None                                           # a quote without a price is no mark
    views[uuid.UUID(other)] = QuoteView(uuid.UUID(other), Decimal("5"), True, True)
    assert A.account_nav(lg, views) == Decimal("1000") + Decimal("600") + Decimal("50")
