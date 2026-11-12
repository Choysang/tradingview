"""M2-E04: CostProfile (Decimal), weighted_average_gross authority, external-flow TWR."""
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pulse.domain.execution import (ExecutionReport, LedgerReducer, OrderIntent, true_time_weighted_return,
                                    twr_by_flow_segments)
from pulse.domain.market_rules import (CostEngine, CostProfileRegistry, a_share_documented_partial,
                                       synthetic_fixed_fee)

from .support import S, at, make_kernel

CASES = {c['case_id']: c for c in json.loads(
    (Path(__file__).with_name('golden_fixtures.json')).read_text(encoding='utf-8'))['cases']}


def test_cumulative_minimum_fee_is_charged_once_across_partial_fills_golden_b09():
    case = CASES['B09']
    k = make_kernel(cash=case['initial']['opening_cash'], qty=0)
    assert k.submit(OrderIntent('b', S, 'buy', 100, D('10')), expected_account_version=0,
                    at=at('2026-11-10', '09:30'), prev_close=D('10')).accepted
    results = [k.ingest_execution(ExecutionReport(f'f{i}', 'b', q, D('10'), at('2026-11-10', '10:00', i)))
               for i, q in enumerate((30, 30, 40), start=1)]
    fees = [D(r.event['payload']['fee']) for r in results]
    assert fees == [D('5'), D('0'), D('0')] and all(r.fee_status == 'complete' for r in results)
    snap = k.ledger.snapshot(case['marks'])
    for key, expected in case['expected'].items():
        if isinstance(expected, str):
            assert D(snap[key]) == D(expected), key


def test_unknown_fee_components_are_flagged_never_booked_as_zero_silently():
    profile = a_share_documented_partial()
    inc = CostEngine.increment(profile, 'sell', D('1000000'), D('0'))
    assert inc.fee == D('34.10')  # 0.00341% exchange handling fee, the only documented rate
    assert inc.status == 'incomplete'
    assert set(inc.unknown_components) == {'commission', 'stamp_duty', 'transfer', 'regulatory'}
    assert CostEngine.increment(profile, 'buy', D('1000000'), D('0')).unknown_components == \
        ('commission', 'transfer', 'regulatory')  # stamp duty is sell-only


def test_kernel_reports_incomplete_fee_status_and_books_only_the_known_part():
    k = make_kernel(qty=0, costs=CostProfileRegistry((a_share_documented_partial(),)))
    k.submit(OrderIntent('b', S, 'buy', 100, D('10')), expected_account_version=0, at=at('2026-11-10', '09:30'),
             prev_close=D('10'))
    res = k.ingest_execution(ExecutionReport('f1', 'b', 100, D('10'), at('2026-11-10', '10:00')))
    assert res.fee_status == 'incomplete' and 'commission' in res.unknown_fee_components
    assert res.event['payload']['fee'] == '0.03'  # 1000 x 0.00341% = 0.0341 -> 0.03


def test_commission_that_includes_exchange_fee_does_not_double_count():
    profile = replace(a_share_documented_partial(), commission_includes_exchange_fee=True)
    inc = CostEngine.increment(profile, 'buy', D('1000000'), D('0'))
    assert inc.fee == D('0') and 'exchange_handling' not in inc.unknown_components


def test_fill_granularity_cannot_change_total_fee_and_rounding_tail_reconciles():
    profile = replace(a_share_documented_partial(), rules=(a_share_documented_partial().rules[0],))
    total, _ = CostEngine.cumulative_fee(profile, 'buy', D('123456.78'))
    charged, notional = D(0), D(0)
    for part in (D('41152.26'), D('41152.26'), D('41152.26')):
        notional += part
        charged += CostEngine.increment(profile, 'buy', notional, charged).fee
    assert charged == total == D('4.21')  # 123456.78 x 0.0000341 = 4.2098 -> 4.21
    with pytest.raises(ValueError):
        CostEngine.increment(profile, 'buy', D('1'), D('99'))  # a lower cumulative requires an appended adjustment


def test_cost_profile_registry_resolves_by_effective_date():
    old = replace(synthetic_fixed_fee('5'), version='v-old', effective_to=date(2026, 7, 6))
    new = replace(synthetic_fixed_fee('3'), version='v-new', effective_from=date(2026, 7, 6))
    reg = CostProfileRegistry((old, new))
    assert reg.resolve(date(2026, 7, 3)).version == 'v-old' and reg.resolve(date(2026, 7, 6)).version == 'v-new'


def test_realized_pnl_is_weighted_average_gross_lots_only_decide_eligibility():
    k = make_kernel(qty=1000, cost='10000', cash='100000')
    k.submit(OrderIntent('b', S, 'buy', 200, D('12')), expected_account_version=0, at=at('2026-11-10', '09:30'),
             prev_close=D('12'))
    k.ingest_execution(ExecutionReport('fb', 'b', 200, D('12'), at('2026-11-10', '10:00')))
    k.advance_session(date(2026, 11, 11), at('2026-11-11', '09:00'))
    k.submit(OrderIntent('s', S, 'sell', 100, D('13')), expected_account_version=k.ledger.account_version,
             at=at('2026-11-11', '09:30'), prev_close=D('12'))
    k.ingest_execution(ExecutionReport('fs', 's', 100, D('13'), at('2026-11-11', '10:00')))
    # average cost (10000 + 2400) / 1200 = 10.3333...; FIFO would have released 10.00 per share.
    snap = k.ledger.snapshot({S: '13'})
    assert D(snap['realized_gross']) == D('1300') - D('1033.333333333333')
    assert D(snap['realized_gross']) != D('300')  # the FIFO figure is not an authority


def test_external_flow_twr_matches_golden_b07_and_flows_are_not_income():
    case = CASES['B07']
    twr = twr_by_flow_segments(case['initial'], case['events'],
                               marks_at=lambda e: {S: '10'} if e is None else {S: '11'},
                               end_marks=case['marks'])
    assert twr == D(case['expected_twr']) == D('0.21')
    b = LedgerReducer(**case['initial'])
    before = b.snapshot({S: '10'})
    b.apply(case['events'][0])
    after = b.snapshot({S: '10'})
    assert after['pnl_from_cost_origin'] == before['pnl_from_cost_origin'] and D(after['nav']) == D(before['nav']) + 9000


def test_withdrawal_and_empty_account_segments():
    initial = {'trading_day': '2026-11-10', 'opening_cash': '0', 'positions': {S: {'quantity': 1000, 'cost': '10000'}},
               'sale_proceeds_reusable': True}
    deposit = {'event_id': 'e1', 'sequence': 1, 'kind': 'capital.deposit', 'effective_at': '2026-11-10T10:00:00+08:00',
               'payload': {'amount': '10000'}}
    withdraw = {'event_id': 'e2', 'sequence': 2, 'kind': 'capital.withdraw', 'effective_at': '2026-11-10T11:00:00+08:00',
                'payload': {'amount': '5000'}}
    marks = {None: '10', 'e1': '11', 'e2': '12'}
    twr = twr_by_flow_segments(initial, [deposit, withdraw], lambda e: {S: marks[e and e['event_id']]}, {S: '12'})
    # NAV 10000 -> 11000 | +10000 -> 21000 -> 22000 | -5000 -> 17000 -> 17000+... computed by hand below
    # seg1 11000/10000 ; seg2 (21000 -> 12*1000+10000=22000) 22000/21000 ; seg3 (22000-5000=17000 -> 17000) 1
    assert abs(twr - (D('1.1') * D('22000') / D('21000') - 1)) < D('1e-25')
    with pytest.raises(ValueError):
        true_time_weighted_return([('0', '100')])
