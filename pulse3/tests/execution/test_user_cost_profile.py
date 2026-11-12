from datetime import date
from decimal import Decimal as D

from pulse.domain.market_rules.costs import CostEngine, scaled, user_account_2026


def test_user_account_fees_on_50k_order():
    p = user_account_2026()
    assert p.covers(date(2026, 11, 11))
    buy, unknown = CostEngine.cumulative_fee(p, "buy", D("50000"))
    assert unknown == ()
    assert buy == D("25.50")  # commission 25 + transfer 0.50
    sell, _ = CostEngine.cumulative_fee(p, "sell", D("50000"))
    assert sell == D("50.50")  # + stamp duty 25


def test_minimum_commission_binds_on_small_leg():
    fee, _ = CostEngine.cumulative_fee(user_account_2026(), "buy", D("2000"))
    assert fee == D("5.02")


def test_stress_profiles_scale():
    fee, _ = CostEngine.cumulative_fee(scaled(user_account_2026(), 2), "buy", D("50000"))
    assert fee == D("51.00")
