import unittest
from datetime import date
from decimal import Decimal as D

from account_model import (Account, CashPolicy, Position, ReservationStatus,
                           compare_t_equity, net_target_deltas)

DAY = date(2026, 11, 10)
SYMBOL = "XSHG:600000"


def account(cash="10000", quantity="1000", *, policy=CashPolicy.DOMESTIC_SALE_REUSABLE):
    return Account(trading_day=DAY, cash_available=D(cash),
                   positions={SYMBOL: Position(D(quantity), D(quantity))}, cash_policy=policy)


def sell(a, quantity="200", price="10.50", fee="5", order_id="sell-1", fill_id="sell-fill-1"):
    a.reserve_sell(order_id, SYMBOL, D(quantity))
    return a.apply_sell(fill_id=fill_id, order_id=order_id, quantity=D(quantity),
                        price=D(price), fee=D(fee))


class InventoryTests(unittest.TestCase):
    def test_positive_integral_quantities_required_for_actions(self):
        for quantity in (D("0"), D("-1"), D("0.5"), D("NaN"), D("Infinity")):
            for action in ("buy", "reserve"):
                with self.subTest(quantity=quantity, action=action):
                    a = account()
                    with self.assertRaises(ValueError):
                        if action == "buy":
                            a.apply_buy(fill_id="b", instrument_id=SYMBOL, quantity=quantity,
                                        price=D("10"), fee=D("1"))
                        else:
                            a.reserve_sell("s", SYMBOL, quantity)
                    self.assertEqual(a.cash_available, D("10000"))
                    self.assertEqual(a.position(SYMBOL).quantity, D("1000"))

    def test_floats_are_rejected_at_money_boundary(self):
        with self.assertRaises(TypeError):
            Account(trading_day=DAY, cash_available=10000.0)
        with self.assertRaises(TypeError):
            account().apply_buy(fill_id="b", instrument_id=SYMBOL, quantity=D("100"),
                                price=10.0, fee=D("1"))

    def test_position_invariants_are_enforced(self):
        for values in (("10", "11", "0"), ("10", "5", "6"), ("-1", "0", "0")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Position(*map(D, values))

    def test_today_buy_does_not_increase_sellable_until_next_day(self):
        a = account(quantity="100")
        a.apply_buy(fill_id="b", instrument_id=SYMBOL, quantity=D("200"), price=D("10"), fee=D("1"))
        self.assertEqual(a.position(SYMBOL).quantity, D("300"))
        self.assertEqual(a.position(SYMBOL).sellable_quantity, D("100"))
        with self.assertRaises(ValueError):
            a.reserve_sell("too-much", SYMBOL, D("200"))
        a.reserve_sell("old-inventory", SYMBOL, D("100"))
        a.advance_trading_day(date(2026, 11, 11))
        self.assertEqual(a.position(SYMBOL).available_to_sell, D("200"))

    def test_reservations_prevent_two_strategies_from_overselling(self):
        a = account(quantity="100")
        first = a.reserve_sell("A", SYMBOL, D("70"))
        self.assertEqual(a.reserve_sell("A", SYMBOL, D("70")), first)
        with self.assertRaises(ValueError):
            a.reserve_sell("B", SYMBOL, D("40"))
        with self.assertRaises(ValueError):
            a.reserve_sell("A", SYMBOL, D("60"))
        self.assertEqual(a.position(SYMBOL).available_to_sell, D("30"))

    def test_partial_fill_then_cancel_releases_only_remainder(self):
        a = account(quantity="100")
        a.reserve_sell("s", SYMBOL, D("80"))
        a.apply_sell(fill_id="s1", order_id="s", quantity=D("30"), price=D("10"), fee=D("1"))
        self.assertEqual(a.position(SYMBOL), Position(D("70"), D("70"), D("50")))
        cancelled = a.cancel_sell("s", confirmed_cumulative_filled=D("30"))
        self.assertEqual(cancelled.status, ReservationStatus.CANCELLED)
        self.assertEqual(a.position(SYMBOL).available_to_sell, D("70"))
        self.assertEqual(a.cancel_sell("s", confirmed_cumulative_filled=D("30")), cancelled)
        self.assertEqual(a.reserve_sell("s", SYMBOL, D("80")), cancelled)
        self.assertEqual(a.position(SYMBOL).reserved_sell_quantity, D("0"))

    def test_duplicate_sell_fill_does_not_double_book_cash_or_inventory(self):
        a = account()
        original = sell(a)
        repeated = a.apply_sell(fill_id="sell-fill-1", order_id="sell-1", quantity=D("200"),
                                price=D("10.50"), fee=D("5"))
        self.assertEqual(original, repeated)
        self.assertEqual(a.cash_available, D("12095"))
        self.assertEqual(a.position(SYMBOL).quantity, D("800"))
        with self.assertRaises(ValueError):
            a.apply_sell(fill_id="sell-fill-1", order_id="sell-1", quantity=D("200"),
                         price=D("11"), fee=D("5"))

    def test_duplicate_buy_fill_does_not_double_book(self):
        a = account()
        kwargs = dict(fill_id="buy-1", instrument_id=SYMBOL, quantity=D("100"), price=D("10"), fee=D("1"))
        a.apply_buy(**kwargs)
        a.apply_buy(**kwargs)
        self.assertEqual(a.cash_available, D("8999"))
        self.assertEqual(a.position(SYMBOL).quantity, D("1100"))
        self.assertEqual(a.fees_paid, D("1"))

    def test_overfill_and_fill_after_cancel_do_not_mutate_account(self):
        a = account()
        a.reserve_sell("s", SYMBOL, D("100"))
        with self.assertRaises(ValueError):
            a.apply_sell(fill_id="bad", order_id="s", quantity=D("101"), price=D("10"), fee=D("1"))
        self.assertEqual(a.position(SYMBOL).reserved_sell_quantity, D("100"))
        a.cancel_sell("s", confirmed_cumulative_filled=D("0"))
        with self.assertRaises(ValueError):
            a.apply_sell(fill_id="bad2", order_id="s", quantity=D("1"), price=D("10"), fee=D("1"))
        self.assertEqual(a.cash_available, D("10000"))

    def test_cancel_confirmation_with_missing_fills_preserves_reservation(self):
        a = account(quantity="100")
        a.reserve_sell("s", SYMBOL, D("100"))
        # Broker has filled 30, but its execution report has not arrived yet.
        with self.assertRaisesRegex(ValueError, "reconcile cumulative fills"):
            a.cancel_sell("s", confirmed_cumulative_filled=D("30"))
        self.assertEqual(a.position(SYMBOL).reserved_sell_quantity, D("100"))
        with self.assertRaises(ValueError):
            a.reserve_sell("another-strategy", SYMBOL, D("100"))
        # Reconcile the missing fill, then apply the same authoritative cancel.
        a.apply_sell(fill_id="late-fill", order_id="s", quantity=D("30"),
                     price=D("10"), fee=D("1"))
        a.cancel_sell("s", confirmed_cumulative_filled=D("30"))
        self.assertEqual(a.position(SYMBOL).available_to_sell, D("70"))
        self.assertEqual(a.cash_available, D("10299"))

    def test_same_day_or_backwards_inventory_settlement_is_rejected(self):
        for day in (DAY, date(2026, 11, 9)):
            with self.subTest(day=day), self.assertRaises(ValueError):
                account().advance_trading_day(day)


class CashPolicyTests(unittest.TestCase):
    def test_domestic_sale_proceeds_can_fund_another_trade_same_day(self):
        a = account(cash="0", quantity="100")
        sell(a, quantity="100", price="10", fee="1")
        a.apply_buy(fill_id="buy", instrument_id=SYMBOL, quantity=D("50"), price=D("10"), fee=D("1"))
        self.assertEqual(a.cash_available, D("498"))
        self.assertEqual(a.cash_unsettled, D("0"))
        self.assertEqual(a.position(SYMBOL).sellable_quantity, D("0"))

    def test_service_unsettled_policy_blocks_reuse_until_explicit_release(self):
        a = account(cash="0", quantity="100", policy=CashPolicy.UNSETTLED_UNTIL_RELEASE)
        sell(a, quantity="100", price="10", fee="1")
        self.assertEqual(a.cash_available, D("0"))
        self.assertEqual(a.cash_unsettled, D("999"))
        with self.assertRaises(ValueError):
            a.apply_buy(fill_id="buy", instrument_id=SYMBOL, quantity=D("50"), price=D("10"), fee=D("1"))
        a.release_unsettled_cash(D("501"))
        a.apply_buy(fill_id="buy", instrument_id=SYMBOL, quantity=D("50"), price=D("10"), fee=D("1"))
        self.assertEqual(a.cash_unsettled, D("498"))
        self.assertEqual(a.total_equity({SYMBOL: D("10")}), D("998"))

    def test_cash_bucket_policy_does_not_change_total_equity(self):
        immediate = account()
        deferred = account(policy=CashPolicy.UNSETTLED_UNTIL_RELEASE)
        sell(immediate)
        sell(deferred)
        marks = {SYMBOL: D("10")}
        self.assertEqual(immediate.total_equity(marks), deferred.total_equity(marks))
        before = deferred.total_equity(marks)
        deferred.release_unsettled_cash()
        self.assertEqual(deferred.total_equity(marks), before)

    def test_inventory_day_roll_does_not_assume_cash_release(self):
        a = account(policy=CashPolicy.UNSETTLED_UNTIL_RELEASE)
        sell(a)
        a.advance_trading_day(date(2026, 11, 11))
        self.assertEqual(a.cash_unsettled, D("2095"))

    def test_failed_buy_leaves_all_state_unchanged(self):
        a = account(cash="1000", quantity="0")
        with self.assertRaises(ValueError):
            a.apply_buy(fill_id="b", instrument_id=SYMBOL, quantity=D("100"), price=D("10"), fee=D("1"))
        self.assertEqual(a.cash_available, D("1000"))
        self.assertEqual(a.fees_paid, D("0"))
        self.assertEqual(a.position(SYMBOL).quantity, D("0"))

    def test_invalid_fee_and_cash_release_are_not_silent(self):
        a = account()
        with self.assertRaises(ValueError):
            a.apply_buy(fill_id="b", instrument_id=SYMBOL, quantity=D("1"), price=D("10"), fee=D("-1"))
        with self.assertRaises(ValueError):
            a.release_unsettled_cash(D("1"))
        self.assertEqual(a.cash_available, D("10000"))

    def test_fee_above_tiny_sale_proceeds_is_a_cash_debit_not_an_asset(self):
        a = account(cash="10", quantity="1", policy=CashPolicy.UNSETTLED_UNTIL_RELEASE)
        sell(a, quantity="1", price="1", fee="5")
        self.assertEqual(a.cash_available, D("6"))
        self.assertEqual(a.cash_unsettled, D("0"))


class NettingTests(unittest.TestCase):
    def test_opposing_sleeve_changes_net_to_no_account_order(self):
        deltas = net_target_deltas(strategy_targets={"A": {SYMBOL: D("150")}, "B": {SYMBOL: D("50")}},
                                   account_quantities={SYMBOL: D("200")})
        self.assertEqual(deltas, {SYMBOL: D("0")})

    def test_pending_unfilled_orders_reduce_new_delta(self):
        deltas = net_target_deltas(strategy_targets={"A": {SYMBOL: D("300")}, "B": {SYMBOL: D("200")}},
                                   account_quantities={SYMBOL: D("400")}, pending_deltas={SYMBOL: D("50")})
        self.assertEqual(deltas, {SYMBOL: D("50")})

    def test_absent_target_is_zero_and_negative_projected_position_is_rejected(self):
        self.assertEqual(net_target_deltas(strategy_targets={}, account_quantities={SYMBOL: D("100")}),
                         {SYMBOL: D("-100")})
        with self.assertRaises(ValueError):
            net_target_deltas(strategy_targets={}, account_quantities={SYMBOL: D("100")},
                              pending_deltas={SYMBOL: D("-101")})
        with self.assertRaises(ValueError):
            net_target_deltas(strategy_targets={"A": {SYMBOL: D("-1")}}, account_quantities={})


class TotalEquityTests(unittest.TestCase):
    def test_completed_t_roundtrip_deducts_both_fees(self):
        active = account()
        passive = active.fork()
        sell(active)
        active.apply_buy(fill_id="buy", instrument_id=SYMBOL, quantity=D("200"), price=D("10"), fee=D("5"))
        result = compare_t_equity(active, passive, {SYMBOL: D("9.80")})
        self.assertEqual(result.active_equity, D("19890"))
        self.assertEqual(result.passive_equity, D("19800"))
        self.assertEqual(result.incremental_equity, D("90"))
        self.assertEqual(result.incremental_fees, D("10"))
        self.assertEqual(active.position(SYMBOL).sellable_quantity, D("800"))

    def test_unbought_inventory_opportunity_cost_can_make_t_negative(self):
        active = account()
        passive = active.fork()
        sell(active)
        result = compare_t_equity(active, passive, {SYMBOL: D("12")})
        self.assertEqual(result.cash_delta, D("2095"))
        self.assertEqual(result.inventory_mark_value_delta, D("-2400"))
        self.assertEqual(result.incremental_equity, D("-305"))

    def test_partial_repurchase_uses_total_equity_not_closed_profit(self):
        active = account()
        passive = active.fork()
        sell(active)
        active.apply_buy(fill_id="buy", instrument_id=SYMBOL, quantity=D("100"), price=D("10"), fee=D("5"))
        result = compare_t_equity(active, passive, {SYMBOL: D("12")})
        self.assertEqual(result.incremental_equity, D("-110"))
        self.assertEqual(result.incremental_equity, result.cash_delta + result.inventory_mark_value_delta)

    def test_comparison_rejects_different_origins_or_valuation_day(self):
        with self.assertRaises(ValueError):
            compare_t_equity(account(cash="9999"), account(), {SYMBOL: D("10")})
        active, passive = account(), account()
        active.advance_trading_day(date(2026, 11, 11))
        with self.assertRaises(ValueError):
            compare_t_equity(active, passive, {SYMBOL: D("10")})

    def test_marks_must_cover_inventory_and_be_finite(self):
        a = account()
        with self.assertRaises(KeyError):
            a.total_equity({})
        with self.assertRaises(ValueError):
            a.total_equity({SYMBOL: D("NaN")})
        self.assertEqual(a.total_equity({SYMBOL: D("0")}), D("10000"))


if __name__ == "__main__":
    unittest.main()
