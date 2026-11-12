"""M2-E03: trading day, T+1, settlement, corporate actions and non-fillable states replay deterministically."""
from datetime import date
from decimal import Decimal as D

import pytest

from pulse.domain.execution import (BacktestReplay, DailyBarMatcher, MinuteBarMatcher, OrderIntent,
                                    QueueScenario)
from pulse.domain.market_rules import (Board, Exchange, InstrumentSpec, PriceLimitRegistry, RuleNotVerified,
                                       eligible_on_for_buy, validate_quantity)

from .support import CAL, S, SPEC, at, daily_bar, make_kernel, minute_bar


def order(k, oid, side, qty, price, when, prev='10'):
    return k.submit(OrderIntent(oid, S, side, qty, D(price)), expected_account_version=k.ledger.account_version,
                    at=when, prev_close=D(prev))


# -- versioned price limits ---------------------------------------------------------------
def spec(ex, board, st=False):
    return InstrumentSpec('X', ex, board, is_risk_warning=st)


def test_price_limits_by_board_and_effective_date():
    reg = PriceLimitRegistry()
    d = date(2026, 11, 10)
    assert reg.band(spec(Exchange.SSE, Board.MAIN), d, D('10')).limit_up == D('11.00')
    assert reg.band(spec(Exchange.SZSE, Board.CHINEXT), d, D('10')).limit_down == D('8.00')
    assert reg.band(spec(Exchange.SSE, Board.STAR), d, D('10')).ratio == D('0.20')
    assert reg.band(spec(Exchange.BSE, Board.BSE), d, D('10')).limit_up == D('13.00')


def test_sse_main_board_risk_warning_5_percent_before_and_10_percent_from_2026_07_06():
    reg, st = PriceLimitRegistry(), spec(Exchange.SSE, Board.MAIN, st=True)
    before, after = reg.band(st, date(2026, 7, 3), D('10')), reg.band(st, date(2026, 7, 6), D('10'))
    assert (before.limit_up, before.limit_down) == (D('10.50'), D('9.50'))
    assert (after.limit_up, after.limit_down) == (D('11.00'), D('9.00'))


def test_unverified_combinations_raise_instead_of_guessing():
    reg = PriceLimitRegistry()
    szse_st = spec(Exchange.SZSE, Board.MAIN, st=True)
    with pytest.raises(RuleNotVerified):
        reg.band(szse_st, date(2026, 11, 10), D('10'))  # no row at all after the switch date
    with pytest.raises(RuleNotVerified):
        reg.band(szse_st, date(2026, 1, 5), D('10'))  # unverified row, strict by default
    assert reg.band(szse_st, date(2026, 1, 5), D('10'), strict=False).ratio == D('0.05')


def test_kernel_rejects_price_outside_the_versioned_band_and_missing_reference():
    k = make_kernel()
    assert order(k, 'o1', 'sell', 100, '11.01', at('2026-11-10', '09:20')).reason == 'price_outside_limits'
    assert order(k, 'o2', 'sell', 100, '10.005', at('2026-11-10', '09:20')).reason == 'price_not_on_tick'
    res = k.submit(OrderIntent('o3', S, 'sell', 100, D('10')), expected_account_version=0, at=at('2026-11-10', '09:20'))
    assert res.reason == 'reference_price_missing'
    assert k.ledger.orders == {}


def test_lot_sizes():
    assert validate_quantity(SPEC, 'buy', 150) == 'buy_not_lot_multiple'
    assert validate_quantity(SPEC, 'sell', 50, position_quantity=250) is None  # the odd lot
    assert validate_quantity(SPEC, 'sell', 150, position_quantity=250) is not None
    star = InstrumentSpec('Y', Exchange.SSE, Board.STAR)
    assert validate_quantity(star, 'buy', 199) == 'star_buy_below_200' and validate_quantity(star, 'buy', 201) is None


# -- T+1, sellability, settlement ------------------------------------------------------------
def test_t_plus_1_buy_is_not_sellable_until_the_next_trading_day_and_skips_non_trading_days():
    assert eligible_on_for_buy(SPEC, date(2026, 11, 13), CAL) == date(2026, 11, 16)  # Friday -> Monday
    k = make_kernel(qty=0)
    assert order(k, 'b', 'buy', 100, '10', at('2026-11-10', '09:30')).accepted
    BacktestReplay(k, DailyBarMatcher()).run([daily_bar('2026-11-10', '10', '10.2', '9.9', '10.1', up='11', down='9')])
    assert k.ledger.holdings[S].quantity == 100 and k.ledger.sellable(S) == 0
    assert order(k, 's', 'sell', 100, '10', at('2026-11-10', '15:01')).accepted is False
    k.advance_session(date(2026, 11, 11), at('2026-11-11', '09:00'))
    assert k.ledger.sellable(S) == 100 and order(k, 's', 'sell', 100, '10', at('2026-11-11', '09:20')).accepted


def test_intraday_t_buying_back_does_not_restore_used_sellable_quantity():
    k = make_kernel(qty=1000, cost='10000')
    assert order(k, 's', 'sell', 200, '10.5', at('2026-11-10', '09:20')).accepted
    run = BacktestReplay(k, MinuteBarMatcher())
    run.run([minute_bar('2026-11-10', '09:30', '10.4', '10.6', '10.3', '10.5', up='11', down='9')])
    assert order(k, 'b', 'buy', 200, '10.3', at('2026-11-10', '09:31')).accepted
    run.run([minute_bar('2026-11-10', '09:31', '10.4', '10.5', '10.2', '10.3', up='11', down='9')])
    h = k.ledger.holdings[S]
    assert h.quantity == 1000 and k.ledger.sellable(S) == 800  # bought-back 200 not sellable today
    res = order(k, 's2', 'sell', 900, '10', at('2026-11-10', '09:33'))
    assert not res.accepted and 'insufficient' in res.reason
    k.advance_session(date(2026, 11, 11), at('2026-11-11', '09:00'))
    assert k.ledger.sellable(S) == 1000


def test_dividend_payment_after_session_change_is_asset_conversion_not_second_income():
    k = make_kernel(qty=1000, cost='10000', cash='0')

    def seq():
        return k.ledger.sequence + 1
    k.apply_event({'event_id': 'd1', 'sequence': seq(), 'kind': 'dividend.accrued',
                   'effective_at': at('2026-11-10', '10:00').isoformat(),
                   'payload': {'action_id': 'div', 'instrument_id': S, 'entitlement_quantity': 1000,
                               'cash_per_share': '0.2', 'entitlement_snapshot_id': 'rec'}})
    nav_before = k.ledger.snapshot({S: '10'})
    k.advance_session(date(2026, 11, 11), at('2026-11-11', '09:00'))
    k.apply_event({'event_id': 'd2', 'sequence': seq(), 'kind': 'dividend.paid',
                   'effective_at': at('2026-11-11', '10:00').isoformat(), 'payload': {'action_id': 'div'}})
    after = k.ledger.snapshot({S: '10'})
    assert (after['nav'], after['dividend_income']) == (nav_before['nav'], nav_before['dividend_income'])
    assert D(after['cash_settled']) == 200 and D(after['dividend_receivable']) == 0


# -- non-fillable states -----------------------------------------------------------------------
def test_limit_up_queue_is_never_assumed_filled_and_explicit_scenarios_are_recorded():
    locked = daily_bar('2026-11-10', '11', '11', '11', '11', up='11', down='9')
    k = make_kernel(qty=0)
    assert order(k, 'b', 'buy', 200, '11', at('2026-11-10', '09:20')).accepted
    log = BacktestReplay(k, DailyBarMatcher()).run([locked])
    assert log.fills == [] and [m.reason for _, m in log.no_fills] == ['limit_queue_unknown']
    assert k.ledger.holdings.get(S) is None and k.ledger.orders['b'].filled == 0

    k2 = make_kernel(qty=0)
    order(k2, 'b', 'buy', 200, '11', at('2026-11-10', '09:20'))
    half = QueueScenario('assume_half_of_order_fills_at_limit_up', D('0.5'))
    log2 = BacktestReplay(k2, DailyBarMatcher(), queue=half).run([locked])
    assert [(f.quantity, f.price, f.scenario_id) for f in log2.fills] == [(100, D('11'), half.scenario_id)]


def test_limit_down_queue_blocks_sells_and_zero_range_bar_without_limits_is_treated_as_possibly_locked():
    k = make_kernel(qty=1000)
    order(k, 's', 'sell', 500, '9', at('2026-11-10', '09:20'))
    log = BacktestReplay(k, DailyBarMatcher()).run([daily_bar('2026-11-10', '9', '9', '9', '9', up='11', down='9')])
    assert log.fills == [] and k.ledger.holdings[S].quantity == 1000
    k2 = make_kernel(qty=1000)
    order(k2, 's', 'sell', 500, '9', at('2026-11-10', '09:20'))
    log2 = BacktestReplay(k2, DailyBarMatcher()).run([daily_bar('2026-11-10', '9', '9', '9', '9')])  # limits unknown
    assert log2.fills == []


def test_suspension_and_untouched_limits_produce_no_fills_with_reasons():
    k = make_kernel(qty=1000)
    order(k, 's', 'sell', 100, '10.5', at('2026-11-10', '09:20'))
    log = BacktestReplay(k, DailyBarMatcher()).run([
        daily_bar('2026-11-10', '10', '10', '10', '10', vol=0, suspended=True, up='11', down='9'),
        daily_bar('2026-11-11', '10', '10.4', '9.9', '10.2', up='11', down='9')])
    assert [m.reason for _, m in log.no_fills] == ['suspended', 'limit_not_touched']
    assert k.ledger.orders['s'].filled == 0


def test_volume_cap_gives_partial_fill_in_lot_steps_shared_across_orders_on_the_bar():
    k = make_kernel(qty=1000)
    order(k, 's1', 'sell', 400, '10', at('2026-11-10', '09:20'))
    order(k, 's2', 'sell', 400, '10', at('2026-11-10', '09:20'))
    log = BacktestReplay(k, DailyBarMatcher()).run(
        [daily_bar('2026-11-10', '10.1', '10.3', '10', '10.2', vol=5000, up='11', down='9')])
    assert [(f.order_id, f.quantity) for f in log.fills] == [('s1', 400), ('s2', 100)]  # 10% of 5000 = 500 shares


def test_delisting_style_unsold_inventory_is_kept_not_force_liquidated():
    k = make_kernel(qty=1000, cost='10000')
    order(k, 's', 'sell', 1000, '9', at('2026-11-10', '09:20'))
    bars = [daily_bar(f'2026-11-1{d}', '9', '9', '9', '9', up='11', down='9') for d in (0, 1, 2)]
    log = BacktestReplay(k, DailyBarMatcher()).run(bars)
    assert log.fills == [] and len(log.no_fills) == 3
    assert k.ledger.holdings[S].quantity == 1000 and k.ledger.cash_available() == D('100000')
    with pytest.raises(KeyError):
        k.ledger.snapshot({})  # a missing mark is never valued as zero
    assert D(k.ledger.snapshot({S: '9'})['nav']) == 109000  # declared last mark, no fabricated cash
