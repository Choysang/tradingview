"""W06 plans: 3-valued conditions, trigger policy, compiler, versions, T and grid (M3-E03/E04)."""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.domain.plans import conditions as C
from pulse.domain.plans.bodies import (
    BodyError,
    GridBody,
    GridState,
    TLeg,
    TPlanBody,
    TProgress,
    apply_t_fills,
    body_ref,
    levels_crossed,
    re_anchor,
    t_outcome,
)
from pulse.domain.plans.compiler import CompileError, StrategyOutput, attach_validation, compile_plan
from pulse.domain.plans.conditions import StaticContext, Observation as Obs, Tri
from pulse.domain.plans.trigger import TriggerState, decide_trigger
from pulse.domain.plans.versions import (
    LateResult,
    PlanVersionError,
    check_append,
    judge_late_result,
    next_version,
    ref,
)

from types import SimpleNamespace

from .helpers import T0, uid
from .test_ranking import _meta

INST = uid("inst0")
CLOCK = C.bar_close_clock("1m", "cn-session-v1")
QCLOCK = {"kind": "quote"}


def macd():
    return C.feature_operand(feature_id="macd.hist", instrument_id=INST, timeframe="1d", field_path="hist", unit="CNY")


def px():
    return C.feature_operand(feature_id="px.close", instrument_id=INST, timeframe="1d", field_path="close", unit="CNY")


def sig():
    return C.feature_operand(feature_id="macd.signal", instrument_id=INST, timeframe="1d", field_path="hist", unit="CNY")


def key(op):
    return StaticContext.key(op)


def ctx(**kw):
    return StaticContext(now=T0, **kw)


def obs(v, confirmed=True, at=T0):
    return Obs(D(v), at, confirmed)


# ------------------------------------------------------------------ conditions


def test_missing_value_is_unknown_not_zero():
    cond = C.compare("gt", macd(), C.constant("0", "CNY"), CLOCK)
    assert C.evaluate(cond, ctx()).value == Tri.UNKNOWN  # missing MACD must not compare as 0 (0 > 0 would be FALSE)
    below = C.compare("lt", macd(), C.constant("1", "CNY"), CLOCK)
    assert C.evaluate(below, ctx()).value == Tri.UNKNOWN  # nor 0 < 1 -> TRUE
    c = ctx(values={key(macd()): obs("-0.2")})
    assert C.evaluate(C.compare("lt", macd(), C.constant("0", "CNY"), CLOCK), c).value == Tri.TRUE


def test_bar_close_clock_needs_confirmed_bar():
    cond = C.compare("gt", px(), C.constant("10", "CNY"), CLOCK)
    c = ctx(values={key(px()): obs("11", confirmed=False)})
    r = C.evaluate(cond, c)
    assert r.value == Tri.UNKNOWN and r.unknown_reasons[0].startswith("unconfirmed_bar")
    # a quote clock accepts the live value
    assert C.evaluate(C.compare("gt", px(), C.constant("10", "CNY"), QCLOCK), c).value == Tri.TRUE


def test_kleene_all_any_not():
    t = C.compare("gt", px(), C.constant("10", "CNY"), QCLOCK)
    f = C.compare("lt", px(), C.constant("10", "CNY"), QCLOCK)
    u = C.compare("gt", macd(), C.constant("0", "CNY"), QCLOCK)
    c = ctx(values={key(px()): obs("11")})
    assert C.evaluate(C.all_of(QCLOCK, t, u), c).value == Tri.UNKNOWN
    assert C.evaluate(C.all_of(QCLOCK, f, u), c).value == Tri.FALSE  # FALSE decides over UNKNOWN
    assert C.evaluate(C.any_of(QCLOCK, t, u), c).value == Tri.TRUE
    assert C.evaluate(C.any_of(QCLOCK, f, u), c).value == Tri.UNKNOWN
    assert C.evaluate(C.not_(QCLOCK, u), c).value == Tri.UNKNOWN
    assert C.evaluate(C.not_(QCLOCK, f), c).value == Tri.TRUE


def test_cross_needs_two_confirmed_points():
    cond = C.crosses("above", px(), sig(), CLOCK, max_age_ms=120_000)
    prev_t = T0 - timedelta(seconds=60)
    full = ctx(values={key(px()): obs("11"), key(sig()): obs("10")},
               previous_values={key(px()): obs("9", at=prev_t), key(sig()): obs("10", at=prev_t)})
    assert C.evaluate(cond, full).value == Tri.TRUE
    only_current = ctx(values={key(px()): obs("11"), key(sig()): obs("10")})
    assert C.evaluate(cond, only_current).value == Tri.UNKNOWN  # a single value never proves a cross
    unconf = ctx(values={key(px()): obs("11"), key(sig()): obs("10")},
                 previous_values={key(px()): obs("9", False, prev_t), key(sig()): obs("10", True, prev_t)})
    assert C.evaluate(cond, unconf).value == Tri.UNKNOWN
    stale_t = T0 - timedelta(seconds=600)
    stale = ctx(values={key(px()): obs("11"), key(sig()): obs("10")},
                previous_values={key(px()): obs("9", at=stale_t), key(sig()): obs("10", at=stale_t)})
    assert C.evaluate(cond, stale).value == Tri.UNKNOWN
    already_above = ctx(values={key(px()): obs("11"), key(sig()): obs("10")},
                        previous_values={key(px()): obs("11", at=prev_t), key(sig()): obs("10", at=prev_t)})
    assert C.evaluate(cond, already_above).value == Tri.FALSE
    below = C.crosses("below", px(), sig(), CLOCK, 120_000)
    down = ctx(values={key(px()): obs("9"), key(sig()): obs("10")},
               previous_values={key(px()): obs("11", at=prev_t), key(sig()): obs("10", at=prev_t)})
    assert C.evaluate(below, down).value == Tri.TRUE


def test_session_event_nodes_and_unit_check():
    s = C.within_session(QCLOCK, "continuous_auction")
    assert C.evaluate(s, ctx()).value == Tri.UNKNOWN
    assert C.evaluate(s, ctx(session="continuous_auction")).value == Tri.TRUE
    assert C.evaluate(s, ctx(session="closing_auction")).value == Tri.FALSE
    ev = C.event_state(QCLOCK, uid("ev"), 2, "confirmed")
    assert C.evaluate(ev, ctx()).value == Tri.UNKNOWN
    assert C.evaluate(ev, ctx(events={str(uid("ev")): (1, "confirmed")})).value == Tri.UNKNOWN  # revision too old
    assert C.evaluate(ev, ctx(events={str(uid("ev")): (2, "confirmed")})).value == Tri.TRUE
    assert C.evaluate(ev, ctx(events={str(uid("ev")): (3, "retracted")})).value == Tri.FALSE
    bad = C.compare("gt", px(), C.constant("10", "percent"), QCLOCK)
    with pytest.raises(C.ConditionError):
        C.validate_condition(bad)


# ------------------------------------------------------------------ trigger policy


def pol(**kw):
    p = {"trigger_id": "t", "fire_mode": "on_transition_to_true", "unknown_policy": "do_not_fire",
         "rearm": {"mode": "never", "cooldown_ms": 0}, "max_fires": 1, "min_interval_ms": 0,
         "require_final_bar": True, "pending_order_policy": "block_retrigger"}
    p.update(kw)
    contracts.check("TriggerPolicy", p)
    return p


def fire(p, st, v, *, at=T0, final=True, pending=False):
    return decide_trigger(p, st, v, now=at, trade_date=date(2026, 11, 10), bar_final=final, pending_order=pending)


def test_trigger_unknown_never_fires_and_bar_must_be_final():
    p, st = pol(), TriggerState()
    d, st = fire(p, st, Tri.UNKNOWN)
    assert not d.fire and d.reason == "unknown_do_not_fire"
    d, st = fire(p, st, Tri.TRUE, final=False)
    assert not d.fire and d.reason == "bar_not_final"
    d, st = fire(p, st, Tri.TRUE)
    assert d.fire
    d, st = fire(p, st, Tri.TRUE, at=T0 + timedelta(minutes=1))
    assert not d.fire and d.reason == "not_a_transition"


def test_single_fire_never_rearms():
    p, st = pol(), TriggerState()
    d, st = fire(p, st, Tri.TRUE)
    assert d.fire
    d, st = fire(p, st, Tri.FALSE, at=T0 + timedelta(minutes=1))
    d, st = fire(p, st, Tri.TRUE, at=T0 + timedelta(minutes=2))
    assert not d.fire and d.reason == "rearm_never"


def test_rearm_after_false_cooldown_and_per_day_count():
    p = pol(rearm={"mode": "after_false", "cooldown_ms": 0}, max_fires=2)
    st = TriggerState()
    d, st = fire(p, st, Tri.TRUE)
    assert d.fire
    d, st = fire(p, st, Tri.TRUE, at=T0 + timedelta(minutes=1))
    assert not d.fire  # same state, not a transition
    d, st = fire(p, st, Tri.FALSE, at=T0 + timedelta(minutes=2))
    d, st = fire(p, st, Tri.TRUE, at=T0 + timedelta(minutes=3))
    assert d.fire
    d, st = fire(p, st, Tri.FALSE, at=T0 + timedelta(minutes=4))
    d, st = fire(p, st, Tri.TRUE, at=T0 + timedelta(minutes=5))
    assert not d.fire and d.reason == "max_fires_per_day"
    # next trading date: the per-day count resets
    d, st2 = decide_trigger(p, st, Tri.TRUE, now=T0 + timedelta(days=1), trade_date=date(2026, 11, 11),
                            bar_final=True, pending_order=False)
    assert d.fire

    rp = pol(fire_mode="repeated_while_true", rearm={"mode": "after_cooldown", "cooldown_ms": 300_000}, max_fires=5)
    st = TriggerState()
    d, st = fire(rp, st, Tri.TRUE)
    assert d.fire
    d, st = fire(rp, st, Tri.TRUE, at=T0 + timedelta(minutes=1))
    assert not d.fire and d.reason == "cooldown"
    d, st = fire(rp, st, Tri.TRUE, at=T0 + timedelta(minutes=6))
    assert d.fire
    mi = pol(fire_mode="repeated_while_true", rearm={"mode": "after_false", "cooldown_ms": 0}, min_interval_ms=60_000, max_fires=9)
    st = TriggerState()
    d, st = fire(mi, st, Tri.TRUE)
    d, st = fire(mi, st, Tri.FALSE, at=T0 + timedelta(seconds=10))
    d, st = fire(mi, st, Tri.TRUE, at=T0 + timedelta(seconds=20))
    assert not d.fire and d.reason == "min_interval"


def test_pending_order_policies():
    block, net = pol(), pol(pending_order_policy="net_target_delta")
    d, _ = fire(block, TriggerState(), Tri.TRUE, pending=True)
    assert not d.fire and d.reason == "pending_order_block"
    d, st = fire(net, TriggerState(), Tri.TRUE, pending=True)
    assert d.fire and d.net_target_delta
    # a transition blocked by a pending order is not lost: it fires once the order clears
    d, st = fire(block, TriggerState(), Tri.TRUE, pending=True)
    d, st = fire(block, st, Tri.TRUE, at=T0 + timedelta(seconds=5), pending=False)
    assert d.fire


# ------------------------------------------------------------------ compiler + versions


def strat(role, lifecycle="active", **payload):
    n = uid(f"s-{role}")
    return StrategyOutput(role, {"instance_id": str(uid(f"i-{role}")), "strategy_id": str(n), "strategy_version": 1},
                          lifecycle, payload)


def outputs(lifecycle="active"):
    entry = C.compare("lte", px(), C.constant("10", "CNY"), CLOCK)
    exit_ = C.compare("gte", px(), C.constant("11", "CNY"), CLOCK)
    inval = C.compare("lt", px(), C.constant("9", "CNY"), CLOCK)
    return [
        strat("entry", lifecycle, entry_conditions=entry, trigger_policy=pol(),
              entry_price_zone={"lower_price_raw": "9.5", "upper_price_raw": "10", "currency": "CNY", "adjustment_mode": "raw"}),
        strat("exit", lifecycle, exit_conditions=exit_, invalidation=inval),
        strat("allocation", lifecycle, sizing={"kind": "fixed_shares", "quantity_shares": 100}),
        strat("holding", lifecycle, holding_horizon={"horizon_id": "5td", "unit": "trading_days", "value": 5}),
        strat("execution", lifecycle, replace_policy="keep_existing"),
    ]


def compiled(lifecycle="active", **kw):
    return compile_plan(plan_id=uid("plan"), instrument_id=INST, release_id=uid("rel"), outputs=outputs(lifecycle),
                        valid_from=T0, expires_at=T0 + timedelta(days=5), available_at=T0,
                        validation_policy_version="val-v1", **kw)


def test_compile_emits_contract_valid_watching_plan():
    r = compiled()
    contracts.check("TradePlan", r.plan)
    contracts.check("getTradePlanResponse", {"meta": _meta(SimpleNamespace(release_id=uid("rel"))), "data": r.plan})
    assert r.plan["state"] == "watching" and r.plan["validation"]["status"] == "pending"
    assert r.plan["replace_policy"] == "keep_existing" and len(r.plan["strategy_refs"]) == 5


def test_compile_requires_roles_and_valid_trees():
    outs = [o for o in outputs() if o.role != "exit"]
    with pytest.raises(CompileError, match="exit"):
        compile_plan(plan_id=uid("p"), instrument_id=INST, release_id=uid("r"), outputs=outs, valid_from=T0,
                     expires_at=T0 + timedelta(days=1), available_at=T0, validation_policy_version="v")
    bad = outputs()
    bad[0].payload["entry_conditions"] = C.compare("gt", px(), C.constant("1", "percent"), CLOCK)
    with pytest.raises(CompileError, match="unit"):
        compile_plan(plan_id=uid("p"), instrument_id=INST, release_id=uid("r"), outputs=bad, valid_from=T0,
                     expires_at=T0 + timedelta(days=1), available_at=T0, validation_policy_version="v")


def test_actionable_needs_passed_validation_and_admitted_strategies():
    r = compiled(lifecycle="shadow")
    assert r.not_admitted  # shadow strategies are not admitted for actionable plans
    lifecycles = {s["instance_id"]: "shadow" for s in r.plan["strategy_refs"]}
    with pytest.raises(PlanVersionError, match="not admitted"):
        attach_validation([r.plan], status="passed", result_ref="val://1", validated_at=T0, available_at=T0,
                          evaluation_id=None, strategy_lifecycles=lifecycles)
    ok = compiled()
    lifecycles = {s["instance_id"]: "active" for s in ok.plan["strategy_refs"]}
    v2 = attach_validation([ok.plan], status="passed", result_ref="val://1", validated_at=T0, available_at=T0 + timedelta(seconds=1),
                           evaluation_id=None, strategy_lifecycles=lifecycles)
    contracts.check("TradePlan", v2)
    assert v2["state"] == "actionable" and v2["plan_version"] == 2 and v2["supersedes"] == ref(ok.plan)
    failed = attach_validation([ok.plan], status="failed", result_ref="val://2", validated_at=T0, available_at=T0,
                               evaluation_id=None, strategy_lifecycles=lifecycles)
    assert failed["state"] == "watching"  # a failed validation never becomes actionable


def test_versions_append_only_and_supersede_chain():
    p1 = compiled().plan
    hist = [p1]
    p2 = next_version(p1, state="watching", available_at=T0 + timedelta(minutes=1), expires_at="2026-11-16T02:00:00Z")
    check_append(hist, p2)
    hist.append(p2)
    contracts.check("TradePlan", p2)
    with pytest.raises(PlanVersionError):  # skipping a version
        check_append(hist, {**p2, "plan_version": 4})
    with pytest.raises(PlanVersionError):  # not superseding the latest
        check_append(hist, {**p2, "plan_version": 3, "supersedes": ref(p1)})
    p3 = next_version(p2, state="expired", available_at=T0 + timedelta(minutes=2))
    check_append(hist, p3)
    hist.append(p3)
    with pytest.raises(PlanVersionError, match="illegal"):  # expired is terminal
        check_append(hist, next_version(p3, state="watching", available_at=T0 + timedelta(minutes=3)))
    with pytest.raises(PlanVersionError, match="backwards"):
        check_append(hist[:2], next_version(p2, state="superseded", available_at=T0 - timedelta(minutes=5)))


def test_slow_model_result_cannot_overwrite_newer_plan():
    p1 = compiled().plan
    p2 = next_version(p1, state="watching", available_at=T0 + timedelta(minutes=5))
    hist = [p1, p2]
    now = T0 + timedelta(minutes=10)
    slow = LateResult(based_on_plan_version=1, analysis_as_of=T0 + timedelta(minutes=1), input_revoked=False,
                      critical_data_valid_until=None)
    a = judge_late_result(hist, slow, now=now)
    assert not a.may_replace_formal_plan and a.keep_as_history
    assert "superseded_by_newer_plan_version" in a.reasons
    fresh = LateResult(2, T0 + timedelta(minutes=6), False, None)
    assert judge_late_result(hist, fresh, now=now).may_replace_formal_plan
    revoked = LateResult(2, T0 + timedelta(minutes=6), True, now - timedelta(seconds=1))
    r = judge_late_result(hist, revoked, now=now)
    assert not r.may_replace_formal_plan and {"input_revoked", "critical_data_stale"} <= set(r.reasons)


# ------------------------------------------------------------------ T plan and grid


def tleg(side, qty=300):
    return TLeg(side, qty, D("9.9"), D("10.1"), "cond:abc")


def tbody(**kw):
    base = dict(body_version=1, direction="sell_first", base_sellable_shares=500, lot_size=100,
                first_leg=tleg("sell"), second_leg=tleg("buy"), leftover_policy="carry",
                expires_at=T0 + timedelta(hours=3))
    base.update(kw)
    return TPlanBody(**base)


def test_t_plan_legs_leftover_and_sellable_accounting():
    b = tbody()
    with pytest.raises(BodyError, match="sellable"):
        tbody(base_sellable_shares=200)
    with pytest.raises(BodyError, match="lot"):
        tbody(first_leg=tleg("sell", 250), second_leg=tleg("buy", 250))
    with pytest.raises(BodyError, match="direction"):
        tbody(first_leg=tleg("buy"), second_leg=tleg("sell"))
    p = apply_t_fills(b, TProgress(), sold=300)
    o = t_outcome(b, p, now=T0)
    assert o.status == "in_progress" and o.leftover_shares == 300 and o.exposure == "underweight"
    assert o.sellable_after_shares == 200
    p = apply_t_fills(b, p, bought=300)
    o = t_outcome(b, p, now=T0)
    assert o.status == "complete" and o.sellable_after_shares == 200  # buy-back does NOT restore sellable
    with pytest.raises(BodyError):  # cannot buy back more than sold
        apply_t_fills(b, TProgress(sold_shares=100), bought=200)
    # expiry with only the sell leg done: leftover is reported, orders reconciled first
    p = apply_t_fills(b, TProgress(), sold=300, bought=100)
    o = t_outcome(b, p, now=T0 + timedelta(hours=4))
    assert o.status == "expired_with_leftover" and o.leftover_shares == 200 and o.exposure == "underweight"
    assert o.actions[0] == "reconcile_then_cancel_unfilled_orders"
    flat = t_outcome(b, TProgress(), now=T0 + timedelta(hours=4))
    assert flat.status == "expired_flat"
    # buy-first: bought today is not sellable; the sell leg draws on OLD inventory only
    bf = tbody(direction="buy_first", first_leg=tleg("buy"), second_leg=tleg("sell"))
    o = t_outcome(bf, apply_t_fills(bf, TProgress(), bought=300), now=T0)
    assert o.exposure == "overweight" and o.sellable_after_shares == 500


def grid(**kw):
    base = dict(body_version=1, anchor_source="last_close", anchor_price_raw=D("10"), step_unit="ratio",
                step_value=D("0.02"), atr_value=None, levels_below=3, levels_above=3, qty_per_level=100, lot_size=100,
                max_inventory_shares=300, cash_budget_cny=D("3000"), daily_sellable_quota_shares=200,
                round_trip_fee_ratio=D("0.004"), min_net_edge_ratio=D("0.002"), gap_policy="fill_at_open",
                multi_level_policy="all_crossed_levels", intrabar_path="open_low_high_close",
                re_anchor_trigger="price_outside_band")
    base.update(kw)
    return GridBody(**base)


def test_grid_levels_validation_and_fee_threshold():
    g = grid()
    assert g.buy_prices() == [D("9.80"), D("9.60"), D("9.40")] and g.sell_prices() == [D("10.20"), D("10.40"), D("10.60")]
    with pytest.raises(BodyError, match="fee"):
        grid(step_value=D("0.004"))
    with pytest.raises(BodyError, match="atr"):
        grid(step_unit="atr", step_value=D("1"))
    with pytest.raises(BodyError, match="max_inventory"):
        grid(max_inventory_shares=200)
    with pytest.raises(BodyError, match="cash"):
        grid(cash_budget_cny=D("1000"))
    with pytest.raises(BodyError, match="intrabar"):
        grid(intrabar_path="whatever")


def test_grid_re_anchor_is_a_new_version_history_unchanged():
    g1 = grid(cash_budget_cny=D("4000"))
    h1 = body_ref("grid", g1)
    with pytest.raises(BodyError, match="cash"):  # a re-anchor that the budget cannot fund is refused
        re_anchor(grid(), new_anchor_price_raw=D("11"), anchor_source="vwap")
    g2 = re_anchor(g1, new_anchor_price_raw=D("11"), anchor_source="vwap")
    assert g2.body_version == 2 and g2.supersedes_hash and h1.endswith(g2.supersedes_hash)
    assert g1.buy_prices()[0] == D("9.80") and g2.buy_prices()[0] == D("10.78")  # old levels never moved
    assert body_ref("grid", g1) == h1 != body_ref("grid", g2)


def test_grid_gap_multi_level_and_caps():
    g = grid()
    state = GridState(inventory_shares=0, sellable_shares=200, cash_available_cny=D("5000"), sold_today_shares=0)
    # one bar sweeping three buy levels: all crossed, limited by max inventory (300 = 3 levels)
    got = levels_crossed(g, state, open_=D("10"), low=D("9.3"), high=D("10.0"))
    assert [(x.side, x.level_index) for x in got] == [("buy", 1), ("buy", 2), ("buy", 3)]
    one = grid(multi_level_policy="one_level_per_bar")
    assert len(levels_crossed(one, state, open_=D("10"), low=D("9.3"), high=D("10.0"))) == 1
    # gap down through level 1 and 2: filled at the open, not at the (unreachable) level price
    gap = levels_crossed(g, state, open_=D("9.5"), low=D("9.4"), high=D("9.6"))
    assert [(x.level_index, x.price_raw, x.gapped) for x in gap if x.side == "buy"][:2] == [(1, D("9.5"), True), (2, D("9.5"), True)]
    skip = levels_crossed(grid(gap_policy="skip_gapped_levels"), state, open_=D("9.5"), low=D("9.4"), high=D("9.6"))
    assert all(not x.gapped for x in skip)
    # sells draw only on old sellable inventory and the daily quota
    sells = levels_crossed(g, GridState(0, 100, D("0"), 0), open_=D("10"), low=D("10"), high=D("10.7"))
    assert [(x.side, x.level_index) for x in sells] == [("sell", 1)]
    quota = levels_crossed(g, GridState(300, 300, D("0"), 200), open_=D("10"), low=D("10"), high=D("10.7"))
    assert quota == []
