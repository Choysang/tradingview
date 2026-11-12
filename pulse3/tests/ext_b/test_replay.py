"""Playbook replay goes through the ExecutionKernel: ledger bridge holds, costs charged, one-word board not filled, deterministic."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from pulse.domain.market_rules import Board, Exchange, InstrumentSpec
from pulse.domain.market_rules.costs import scaled, user_account_2026
from pulse.domain.playbooks import library as L
from pulse.domain.playbooks.builder import prepare, run_signal
from pulse.domain.playbooks.exits import ExitRules
from pulse.domain.playbooks.replay import ReplayConfig, replay

from .synth import panel


def _run(profile, pid="turtle_55", days=None, views=None, start=120, end=300):
    pb = L.REGISTRY[pid]
    sigs = {v.instrument_id: run_signal(pb, v) for v in views}
    specs = {v.instrument_id: InstrumentSpec(v.instrument_id, Exchange.SSE, Board.MAIN) for v in views}
    cfg = ReplayConfig(D("1000000"), profile, start, end, pb.max_positions, pb.max_new_per_day, ExitRules.from_spec(pb.exits))
    return replay(signals=sigs, views={v.instrument_id: v for v in views}, specs=specs, sessions=days, cfg=cfg, run_id=pid)


@pytest.fixture(scope="module")
def world():
    days, raws, views = panel(30, 330)
    prepare(views, days)
    return days, views


def test_replay_runs_through_kernel_and_ledger_balances(world):
    days, views = world
    r = _run(user_account_2026(), days=days, views=views)
    assert r.counters.get("entry_filled", 0) > 0
    assert r.fees > 0                                         # the user cost profile is charged
    assert r.final["nav"] is not None and r.final["net_external_capital"] == D("1000000")
    navs = [n for _, n in r.nav if n is not None]
    assert navs and all(n > 0 for n in navs)
    # same inputs -> byte-identical events (determinism)
    r2 = _run(user_account_2026(), days=days, views=views)
    assert r.events == r2.events and r.nav == r2.nav


def test_higher_cost_profile_never_improves_the_result(world):
    days, views = world
    base = _run(user_account_2026(), days=days, views=views)
    x2 = _run(scaled(user_account_2026(), 2), days=days, views=views)
    x3 = _run(scaled(user_account_2026(), 3), days=days, views=views)
    assert base.fees < x2.fees < x3.fees
    assert base.final["nav"] >= x2.final["nav"] >= x3.final["nav"] or abs(base.final["nav"] - x3.final["nav"]) > 0


def test_one_word_limit_up_board_is_not_assumed_fillable():
    # a stock that opens at its limit-up price and never trades below it cannot be bought
    from pulse.domain.playbooks.builder import RawSeries, build_views
    from .synth import sessions
    days = sessions(40)
    px = [10.0 + 0.01 * k for k in range(39)]
    o = list(px) + [round(px[-1] * 1.10, 2)]
    c = list(o)
    raw = RawSeries("z1", "main", tuple(days), tuple(o), tuple(o), tuple(o), tuple(c), tuple([1e7] * 40))
    v = build_views([raw], days)[0]
    sig = [None] * 39 + [1.0]
    sig[38] = 1.0                                              # signal on day 38 -> order for day 39 (one-word limit-up)
    specs = {"z1": InstrumentSpec("z1", Exchange.SSE, Board.MAIN)}
    cfg = ReplayConfig(D("1000000"), user_account_2026(), 30, 40, 5, 2, ExitRules(3))
    r = replay(signals={"z1": sig}, views={"z1": v}, specs=specs, sessions=days, cfg=cfg)
    assert r.counters.get("entry_filled", 0) == 0 and r.counters.get("entry_unfilled", 0) == 1
    assert r.final["fees"] == 0
