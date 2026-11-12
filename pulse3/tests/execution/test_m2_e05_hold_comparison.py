"""M2-E05: T / grid improvement is judged against a hold account on the same initial state and terminal price."""
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from pulse.domain.execution import (BacktestReplay, Leg, LegPlan, LegPlanStrategy, MinuteBarMatcher, PathScenario,
                                    compare_with_hold)

from .support import S, at, make_kernel, minute_bar

CASES = {c['case_id']: c for c in json.loads(
    (Path(__file__).with_name('golden_fixtures.json')).read_text(encoding='utf-8'))['cases']}


@pytest.mark.parametrize('case_id', ['B03', 'B04', 'B08'])
def test_golden_increment_equals_active_minus_hold_with_exact_bridge(case_id):
    c = CASES[case_id]
    rep = compare_with_hold(c['initial'], c['events'], c['marks'])
    assert rep.baseline_nav == D(c['passive_nav'])
    assert rep.incremental_pnl == D(c['expected_increment'])
    assert rep.bridge_holds()
    assert rep.active_fees == D(c['expected']['fees']) and rep.baseline_fees == 0


def test_golden_b03_unfinished_sell_keeps_the_inventory_shortfall_in_the_result():
    c = CASES['B03']
    rep = compare_with_hold(c['initial'], c['events'], c['marks'])
    assert rep.incremental_pnl == D('-305')  # sold 200 @10.5, left unbought while the price rose to 12
    assert rep.inventory_shortfall == {'SAMPLE_MAIN_001': 200}
    assert rep.inventory_value_delta == D('-2400') and rep.cash_delta == D('2095')


def test_dividend_streams_require_an_explicit_baseline():
    c = CASES['B05']
    with pytest.raises(ValueError):
        compare_with_hold(c['initial'], c['events'], c['marks'])


# -- T legs through kernel + minute matcher ------------------------------------------------------
PLAN = LegPlan('t1', S, (Leg('sell', 'sell', 200, D('10.8')), Leg('buyback', 'buy', 200, D('10.4'), after='sell')),
               prev_close=D('10'))


def run_plan(bars, *, path=PathScenario.UNKNOWN, expire=True):
    k = make_kernel(qty=1000, cost='10000', cash='100000')
    strat = LegPlanStrategy(PLAN)
    BacktestReplay(k, MinuteBarMatcher(), path=path, strategy=strat).run(bars)
    if expire:
        strat.expire(k, at('2026-11-10', '14:59'))
    return k, strat


def initial_of(k):
    return {'trading_day': '2026-11-10', 'opening_cash': '100000',
            'positions': {S: {'quantity': 1000, 'cost': '10000'}}, 'sale_proceeds_reusable': True}


def events_of(k):
    return [k.ledger.recorded_event(eid) for eid in k.ledger._events]


def test_completed_t_round_trip_is_measured_net_of_fees_against_hold():
    bars = [minute_bar('2026-11-10', '09:30', '10', '10.9', '10', '10.8', up='11', down='9'),
            minute_bar('2026-11-10', '09:31', '10.7', '10.8', '10.3', '10.5', up='11', down='9')]
    k, strat = run_plan(bars)
    report = strat.report(k)
    assert report.complete and report.net_share_delta == 0
    rep = compare_with_hold(initial_of(k), events_of(k), {S: '10.5'})
    assert rep.incremental_pnl == D('200') * (D('10.8') - D('10.4')) - D('10')  # 80 - 2 x 5 fees
    assert rep.active_fees == D('10') and rep.inventory_shortfall == {S: 0} and rep.bridge_holds()


def test_unfinished_t_leg_is_reported_and_priced_at_the_terminal_mark():
    bars = [minute_bar('2026-11-10', '09:30', '10', '10.9', '10', '10.8', up='11', down='9'),
            minute_bar('2026-11-10', '09:31', '10.7', '10.8', '10.5', '10.6', up='11', down='9')]  # never reaches 10.4
    k, strat = run_plan(bars)
    report = strat.report(k)
    assert not report.complete and report.net_share_delta == -200
    assert [(s.leg_id, s.status) for s in report.legs] == [('sell', 'filled'), ('buyback', 'cancelled')]
    rep = compare_with_hold(initial_of(k), events_of(k), {S: '12'})
    assert rep.inventory_shortfall == {S: 200}
    assert rep.incremental_pnl == D('200') * (D('10.8') - D('12')) - D('5')  # selling too early lost money
    assert k.ledger.reserved_sell(S) == 0  # cancel confirmed with cumulative fills released the rest


def test_same_bar_round_trip_needs_a_declared_path_and_the_path_changes_the_answer():
    bar = minute_bar('2026-11-10', '09:30', '10.5', '10.9', '10.3', '10.5', up='11', down='9')
    # unknown path: the buy-back is never assumed to happen after the sale inside the same bar
    k, strat = run_plan([bar], path=PathScenario.UNKNOWN, expire=False)
    assert [s.status for s in strat.report(k).legs] == ['filled', 'open']
    # open -> high -> low -> close: sell at the high, then the low allows the buy-back
    k2, s2 = run_plan([bar], path=PathScenario.OHLC, expire=False)
    assert s2.report(k2).complete
    # open -> low -> high -> close: the buy-back level is only reached before the sale, so it stays open
    k3, s3 = run_plan([bar], path=PathScenario.OLHC, expire=False)
    assert [s.status for s in s3.report(k3).legs] == ['filled', 'open']


def test_grid_chain_with_incomplete_last_leg_is_reported():
    plan = LegPlan('g', S, (Leg('s1', 'sell', 100, D('10.6')), Leg('b1', 'buy', 100, D('10.2'), after='s1'),
                            Leg('s2', 'sell', 100, D('10.6'), after='b1')), prev_close=D('10'))
    k = make_kernel(qty=1000, cost='10000')
    strat = LegPlanStrategy(plan)
    bars = [minute_bar('2026-11-10', '09:30', '10.5', '10.7', '10.5', '10.6', up='11', down='9'),
            minute_bar('2026-11-10', '09:31', '10.5', '10.6', '10.1', '10.2', up='11', down='9'),
            minute_bar('2026-11-10', '09:32', '10.2', '10.4', '10.1', '10.3', up='11', down='9')]
    BacktestReplay(k, MinuteBarMatcher(), strategy=strat).run(bars)
    strat.expire(k, at('2026-11-10', '14:59'))
    rep = strat.report(k)
    assert [(s.leg_id, s.status) for s in rep.legs] == [('s1', 'filled'), ('b1', 'filled'), ('s2', 'cancelled')]
    assert rep.net_share_delta == -100 + 100 - 0 and not rep.complete
