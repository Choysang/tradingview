from decimal import Decimal as D

from pulse.domain.execution import (BacktestReplay, DailyBarMatcher, OrderIntent, PathScenario)

from .support import S, at, daily_bar, make_kernel


def test_submit_and_daily_fill_smoke():
    k = make_kernel(cash='100000', qty=1000)
    r = k.submit(OrderIntent('s1', S, 'sell', 300, D('10.5')), expected_account_version=k.ledger.account_version,
                 at=at('2026-11-10', '09:20'), prev_close=D('10'))
    assert r.accepted, r.reason
    run = BacktestReplay(k, DailyBarMatcher())
    log = run.run([daily_bar('2026-11-10', '10.2', '10.8', '10.1', '10.6', up='11', down='9')])
    assert log.fills and log.fills[0].quantity == 300
    assert k.ledger.holdings[S].quantity == 700
