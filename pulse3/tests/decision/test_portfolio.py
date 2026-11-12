"""W06 portfolio: coordinator and ActionabilitySnapshot (M3-E04)."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.domain.plans.conditions import Tri
from pulse.domain.portfolio.actionability import ActionabilityInput, PlanLeg, QuoteView, evaluate_actionability
from pulse.domain.portfolio.coordinator import (
    AccountSnapshot,
    PositionView,
    PriorityPolicy,
    Proposal,
    coordinate,
)

from .helpers import T0, uid
from .test_ranking import _meta

A, B, E = uid("inst0"), uid("inst1"), uid("inst2")


def acct(cash="100000", pos=None, **kw):
    return AccountSnapshot(uid("acct"), 7, D(cash), pos if pos is not None else {}, **kw)


def test_empty_position_does_not_block_a_normal_buy():
    r = coordinate([Proposal("p1", "sleeveA", "entry", "new_buy", A, 500, D("10"))], acct())
    d = r.decisions[0]
    assert d.approved_shares == 500 and d.reason_codes == ()


def test_shared_sellable_inventory_between_base_trim_and_t():
    pos = {A: PositionView(quantity_shares=1000, sellable_shares=500)}
    props = [
        Proposal("a-t", "t_sleeve", "holding", "t_sell", A, 300),
        Proposal("b-trim", "base_sleeve", "holding", "trim", A, 300),
    ]
    r = coordinate(props, acct(pos=pos))
    got = {d.proposal_id: d for d in r.decisions}
    # declared order: trim is before t_sell in the default priority; total approved never exceeds 500
    assert got["b-trim"].approved_shares == 300
    assert got["a-t"].approved_shares == 200 and "REDUCED_TO_SHARED_SELLABLE" in got["a-t"].reason_codes
    assert sum(d.approved_shares for d in r.decisions) <= 500
    assert r.remaining_sellable[A] == 0
    # a different declared priority flips who wins; models do not bid by "strength"
    r2 = coordinate(props, acct(pos=pos), PriorityPolicy(order=("exit", "t_sell", "trim", "t_buy", "new_buy", "add_buy")))
    got2 = {d.proposal_id: d for d in r2.decisions}
    assert got2["a-t"].approved_shares == 300 and got2["b-trim"].approved_shares == 200


def test_sleeves_are_attribution_only_and_cash_is_shared():
    props = [Proposal("s1", "slv1", "entry", "new_buy", A, 600, D("10")), Proposal("s2", "slv2", "entry", "new_buy", B, 600, D("10"))]
    r = coordinate(props, acct(cash="8000"))  # cash for ~800 shares in total, not 600+600
    total = sum(d.approved_shares for d in r.decisions)
    assert total <= 700 and r.cash_reserved_cny + r.remaining_cash_cny == D("8000")
    attributed = sum(q for m in r.sleeve_attribution.values() for q in m.values())
    assert attributed == total  # sleeves only describe the one physical account's result
    assert r.decisions[1].reason_codes in (("REDUCED_TO_AVAILABLE_CASH",), ("INSUFFICIENT_CASH",))


def test_exit_before_new_buy_on_same_instrument_and_no_proceeds_reuse():
    pos = {A: PositionView(1000, 1000)}
    props = [Proposal("buy", "s", "entry", "add_buy", A, 100, D("10")), Proposal("exit", "s", "exit", "exit", A, 1000)]
    r = coordinate(props, acct(cash="0", pos=pos))
    got = {d.proposal_id: d for d in r.decisions}
    assert got["exit"].approved_shares == 1000
    assert got["buy"].approved_shares == 0 and got["buy"].reason_codes == ("EXIT_PRIORITY_CONFLICT",)
    # sale proceeds are not reusable cash this cycle (cash_reuse_policy = next_session)
    r2 = coordinate([Proposal("e2", "s", "exit", "exit", A, 1000), Proposal("b2", "s", "entry", "new_buy", B, 100, D("10"))],
                    acct(cash="0", pos=pos))
    assert {d.proposal_id: d.reason_codes for d in r2.decisions}["b2"] == ("INSUFFICIENT_CASH",)
    with pytest.raises(ValueError):
        PriorityPolicy(cash_reuse_policy="same_day")


def test_permissions_and_selection_cannot_order():
    r = coordinate([Proposal("p", "s", "entry", "new_buy", A, 100, D("10"))], acct(buy_permitted=frozenset({B})))
    assert r.decisions[0].reason_codes == ("NO_BUY_PERMISSION",)
    with pytest.raises(ValueError):
        Proposal("p", "s", "selection", "new_buy", A, 100, D("10"))


# ------------------------------------------------------------------ actionability


def build(account=None, plans=None, quotes=None, order=None):
    order = order or [(A, 1), (B, 2), (E, None)]
    return ActionabilityInput(
        release_id=uid("rel"), ranking_id=uid("rk"), quote_snapshot_id=uid("quote"), policy_version="act-v1",
        checked_at=T0, valid_until=T0 + timedelta(seconds=90), ranking_order=order,
        plans=plans if plans is not None else {}, quotes=quotes if quotes is not None else {}, account=account)


def leg(iid, side="buy", qty=500, trig=Tri.TRUE, name=None):
    return PlanLeg((uid(f"plan-{iid}"), 1), side, qty, trig, name, D("10"))


def test_buy_checks_cash_permission_and_empty_position_ok():
    q = {A: QuoteView(A, D("10"), True, True), B: QuoteView(B, D("10"), False, True), E: QuoteView(E, D("10"), True, True)}
    plans = {A: [leg(A)], B: [leg(B)], E: [leg(E, trig=Tri.FALSE)]}
    out = evaluate_actionability(build(acct(cash="3000"), plans, q))
    contracts.check("ActionabilitySnapshot", out)
    contracts.check("getAccountActionabilityResponse", {"meta": {**_meta(type("R", (), {"release_id": uid("rel")})), "actionability_id": out["actionability_id"]}, "data": out})
    by = {e["instrument_id"]: e for e in out["entries"]}
    a = by[str(A)]
    assert a["actionability_status"] == "eligible" and a["checked_quantity_shares"] == 200  # cash limited, lot rounded
    assert "REDUCED_TO_AVAILABLE_CASH" in a["reason_codes"] and a["account_action_rank"] == 1
    assert by[str(B)]["actionability_status"] == "blocked" and by[str(B)]["reason_codes"] == ["NO_COUNTERPARTY_LIMIT_UP"]
    assert by[str(B)]["frozen_action_rank"] == 2 and by[str(B)]["account_action_rank"] is None  # research rank kept
    assert by[str(E)]["actionability_status"] == "not_triggered"
    broke = evaluate_actionability(build(acct(cash="500"), plans, q))
    assert {e["instrument_id"]: e["reason_codes"] for e in broke["entries"]}[str(A)] == ["INSUFFICIENT_CASH"]
    noperm = evaluate_actionability(build(acct(buy_permitted=frozenset()), {A: [leg(A)]}, {A: q[A]}, order=[(A, 1)]))
    assert noperm["entries"][0]["reason_codes"] == ["NO_BUY_PERMISSION"]


def test_sell_needs_sellable_inventory_and_t_legs_checked_separately():
    q = {A: QuoteView(A, D("10"), True, True)}
    pos = {A: PositionView(quantity_shares=300, sellable_shares=100, reserved_sell_shares=0)}
    plans = {A: [leg(A, "sell", 300, name="sell_leg"), leg(A, "buy", 300, name="buyback_leg")]}
    out = evaluate_actionability(build(acct(cash="100000", pos=pos), plans, q, order=[(A, 1)]))
    e = out["entries"]
    assert [x["side"] for x in e] == ["sell", "buy"]
    assert e[0]["actionability_status"] == "eligible" and e[0]["checked_quantity_shares"] == 100
    assert "REDUCED_TO_SELLABLE" in e[0]["reason_codes"] and "LEG:sell_leg" in e[0]["reason_codes"]
    assert e[1]["actionability_status"] == "eligible" and "LEG:buyback_leg" in e[1]["reason_codes"]
    none = evaluate_actionability(build(acct(pos={A: PositionView(300, 0)}), plans, q, order=[(A, 1)]))
    assert none["entries"][0]["reason_codes"][-1] == "NO_SELLABLE_INVENTORY"
    # a sell without any account is unknown, not eligible; a missing quote is unknown
    pub = evaluate_actionability(build(None, {A: [leg(A, "sell")]}, q, order=[(A, 1)]))
    assert pub["entries"][0]["actionability_status"] == "unknown"
    assert evaluate_actionability(build(acct(), {A: [leg(A)]}, {}, order=[(A, 1)]))["entries"][0]["reason_codes"] == ["QUOTE_MISSING"]


def test_snapshot_binding_is_deterministic_and_valid_window_enforced():
    q = {A: QuoteView(A, D("10"), True, True)}
    i1 = build(acct(), {A: [leg(A)]}, q, order=[(A, 1)])
    s1, s2 = evaluate_actionability(i1), evaluate_actionability(i1)
    assert s1 == s2 and s1["account_version"] == 7 and s1["quote_snapshot_id"] == str(uid("quote"))
    assert evaluate_actionability(build(acct(), {A: [leg(A)]}, q, order=[(A, 1)]))["actionability_id"] == s1["actionability_id"]
    other = ActionabilityInput(**{**i1.__dict__, "account": AccountSnapshot(uid("acct"), 8, D("100000"), {})})
    assert evaluate_actionability(other)["actionability_id"] != s1["actionability_id"]
    with pytest.raises(ValueError):
        evaluate_actionability(ActionabilityInput(**{**i1.__dict__, "valid_until": T0}))
