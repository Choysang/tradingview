from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.domain import inventory as L
from contracts.core import (
    TRANSITIONS, Action, ActionIntent, EventEnvelope, ExecutionReport, OpportunityRecord, OpportunityStatus,
    OrderPlan, OrderStatus, Position, PortfolioState, PricePolicy, QuantityPolicy, Session, Side, TriggerRef,
    UnfilledReason, can_transition,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)
SH = "CN.SH.600000"


def _state(cash: str = "100000", positions: tuple[Position, ...] = ()) -> PortfolioState:
    return PortfolioState(account_id="paper-1", ledger_version=0, as_of=T0, session_date="2026-11-02",
                          cash_available=Decimal(cash), positions=positions)


def test_event_time_fields_are_ordered_and_aware():
    ok = dict(event_id="e1", stream_id="quotes.SH", sequence=1, event_type="market.quote", instrument_id=SH,
              event_time=T0, received_at=T0 + timedelta(milliseconds=200), available_at=T0 + timedelta(milliseconds=240),
              recorded_at=T0 + timedelta(seconds=1), source_id="tencent", payload_ref="raw/1")
    assert EventEnvelope(**ok).available_at > EventEnvelope(**ok).received_at
    with pytest.raises(ValidationError):
        EventEnvelope(**{**ok, "available_at": T0})  # usable before we had it
    with pytest.raises(ValidationError):
        EventEnvelope(**{**ok, "event_time": datetime(2026, 11, 2, 9, 30)})  # naive time
    with pytest.raises(ValidationError):
        EventEnvelope(**{**ok, "published_at": T0 + timedelta(seconds=5)})  # received before it was published
    with pytest.raises(ValidationError):
        EventEnvelope(**{**ok, "instrument_id": "600000"})


def test_intent_needs_executable_price_and_ordered_times():
    base = dict(intent_id="i1", decision_time=T0, generated_at=T0, expires_at=T0 + timedelta(minutes=5),
                strategy_ref="trend.pullback@1.0.0", instrument_id=SH, action=Action.INCREASE_LONG, horizon_sessions=5,
                trigger=TriggerRef(rule_id="reclaim.vwap@1", status="FIRED"),
                price_policy=PricePolicy(type="bounded_limit", limit_price=Decimal("10.50")),
                quantity_policy=QuantityPolicy(type="portfolio_allocated"), data_snapshot_id="s1")
    assert ActionIntent(**base).price_policy.limit_price == Decimal("10.50")
    with pytest.raises(ValidationError):
        ActionIntent(**{**base, "expires_at": T0})
    with pytest.raises(ValidationError):
        PricePolicy(type="bounded_limit")


def test_buy_orders_come_in_lots_and_reports_stay_consistent():
    plan = dict(client_order_id="c1", idempotency_key="k1", intent_id="i1", account_id="paper-1", instrument_id=SH,
                side=Side.BUY, quantity=300, price_policy=PricePolicy(type="at_open"), session=Session.CONTINUOUS_AM)
    assert OrderPlan(**plan).quantity == 300
    with pytest.raises(ValidationError):
        OrderPlan(**{**plan, "quantity": 250})
    assert OrderPlan(**{**plan, "side": Side.SELL, "quantity": 250}).quantity == 250  # odd lots may be sold
    rep = dict(report_id="r1", order_id="o1", client_order_id="c1", order_quantity=300, event_time=T0)
    with pytest.raises(ValidationError):
        ExecutionReport(**rep, status=OrderStatus.FILLED, filled_quantity=200)
    with pytest.raises(ValidationError):
        ExecutionReport(**rep, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=100, last_fill_quantity=100)


def test_order_state_machine():
    assert can_transition(OrderStatus.CANCEL_PENDING, OrderStatus.FILLED)  # a cancel request can still lose the race
    assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELED)
    assert not can_transition(OrderStatus.CANCELED, OrderStatus.PARTIALLY_FILLED)
    assert all(new not in TRANSITIONS for new in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.EXPIRED))


def test_t_plus_one_example_from_the_design():
    """1000 sellable, buy 500 today, sell 500 old: 1000 left, 500 sellable, 500 not sellable today."""
    s = _state(positions=(Position(instrument_id=SH, total_qty=1000, sellable_qty=1000, acquired_today=0,
                                   cost_basis=Decimal("10000")),))
    s = L.reserve_buy(s, SH, 500, Decimal("5100"), T0)
    s = L.buy_fill(s, SH, 500, Decimal("10.00"), Decimal("5"), Decimal("5100"), T0)
    s = L.reserve_sell(s, SH, 500, T0)
    s = L.sell_fill(s, SH, 500, Decimal("10.20"), Decimal("7.10"), T0)
    p = s.position(SH)
    assert (p.total_qty, p.sellable_qty, p.acquired_today, p.reserved_sell_qty) == (1000, 500, 500, 0)
    assert s.cash_available == Decimal("100000") - Decimal("5005") + Decimal("5100") - Decimal("7.10")
    assert s.cash_reserved == 0 and s.ledger_version == 4
    with pytest.raises(L.LedgerError):
        L.reserve_sell(s, SH, 600, T0)  # today's 500 cannot be sold
    s = L.roll_session(s, "2026-11-03", T0 + timedelta(days=1))
    p = s.position(SH)
    assert (p.sellable_qty, p.acquired_today) == (1000, 0)


def test_ledger_refuses_double_sells_and_overdrafts():
    s = _state(cash="1000", positions=(Position(instrument_id=SH, total_qty=100, sellable_qty=100, acquired_today=0),))
    with pytest.raises(L.LedgerError):
        L.reserve_buy(s, SH, 100, Decimal("1001"), T0)
    s = L.reserve_sell(s, SH, 100, T0)
    with pytest.raises(L.LedgerError):
        L.reserve_sell(s, SH, 100, T0)  # already reserved by the first order
    with pytest.raises(L.LedgerError):
        L.roll_session(s, "2026-11-03", T0)  # an open sell reservation
    s = L.release_sell(s, SH, 100, T0)
    with pytest.raises(L.LedgerError):
        L.roll_session(s, "2026-11-02", T0)  # not a later session


def test_flat_position_leaves_the_book_and_inputs_are_not_mutated():
    s0 = _state(positions=(Position(instrument_id=SH, total_qty=100, sellable_qty=100, acquired_today=0),))
    s1 = L.sell_fill(L.reserve_sell(s0, SH, 100, T0), SH, 100, Decimal("10"), Decimal("1"), T0)
    assert s1.positions == () and s0.position(SH).total_qty == 100


def test_unbought_opportunities_keep_their_reason():
    base = dict(opportunity_id="op1", intent_id="i1", account_id="paper-1", instrument_id=SH, strategy_ref="board.first@1",
                decision_time=T0, target_quantity=500)
    assert OpportunityRecord(**base, status=OpportunityStatus.UNFILLED, unfilled_reason=UnfilledReason.LIMIT_UP_QUEUE,
                             reference_price=Decimal("11.00")).filled_quantity == 0
    with pytest.raises(ValidationError):
        OpportunityRecord(**base, status=OpportunityStatus.UNFILLED)
    with pytest.raises(ValidationError):
        OpportunityRecord(**base, status=OpportunityStatus.FILLED, filled_quantity=400, avg_fill_price=Decimal("11"))
