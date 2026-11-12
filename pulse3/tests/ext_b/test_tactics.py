"""Exit lab / tactic replay: judged against the same-inventory hold baseline through the kernel; T legs need a declared path."""
from __future__ import annotations

from decimal import Decimal as D

from pulse.domain.execution import PathScenario
from pulse.domain.market_rules import Board, Exchange, InstrumentSpec
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.playbooks.exits import ExitRules, variant
from pulse.domain.playbooks.tactics import run_exit_rules, run_intraday_t

from .synth import panel


def _world():
    days, raws, views = panel(3, 260, caps=False)
    vm = {v.instrument_id: v for v in views}
    specs = {k: InstrumentSpec(k, Exchange.SSE, Board.MAIN) for k in vm}
    lots = {k: (10000, D("100000")) for k in vm}
    return days, vm, specs, lots


def test_exit_rule_is_measured_against_hold_with_exact_bridge():
    days, vm, specs, lots = _world()
    res, rep = run_exit_rules(rules=ExitRules(holding_days=400, stop_loss_pct=4.0), label="紧止损", views=vm, specs=specs, sessions=days,
                              start=100, end=250, cash=D("200000"), lots=lots, profile=user_account_2026())
    c = res.comparison
    assert c.bridge_holds()
    assert c.baseline_fees == 0 and c.active_fees == res.fees
    assert c.incremental_pnl == c.active_nav - c.baseline_nav
    if res.fees > 0:                                          # a stop fired: inventory is lower than the hold account's
        assert any(q > 0 for q in c.inventory_shortfall.values())


def test_all_exit_variants_are_returned_not_just_the_best():
    days, vm, specs, lots = _world()
    base = ExitRules(holding_days=30)
    from pulse.domain.playbooks.exits import VARIANTS
    out = [run_exit_rules(rules=variant(base, k), label=l, views=vm, specs=specs, sessions=days, start=100, end=250, cash=D("200000"),
                          lots=lots, profile=user_account_2026())[0] for k, l, _ in VARIANTS]
    assert len(out) == len(VARIANTS) and all(r.comparison.bridge_holds() for r in out)


def test_intraday_t_unknown_path_cannot_complete_but_declared_paths_report_a_spread():
    days, vm, specs, lots = _world()
    kw = dict(mode="reverse", up_pct=1.0, rebuy_pct=1.0, share=0.5, views=vm, specs=specs, sessions=days, start=100, end=200,
              cash=D("200000"), lots=lots, profile=user_account_2026())
    unk = run_intraday_t(path=PathScenario.UNKNOWN, **kw)
    a = run_intraday_t(path=PathScenario.OHLC, **kw)
    b = run_intraday_t(path=PathScenario.OLHC, **kw)
    assert unk.legs_completed == 0                            # no favourable intrabar order is ever assumed
    assert a.comparison.bridge_holds() and b.comparison.bridge_holds()
    assert a.legs_attempted > 0
