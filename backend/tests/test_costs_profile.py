"""Cost profiles of the backtest engine: the frozen baseline is unchanged; the user's account profile (2026-11-08)."""
from __future__ import annotations

import pytest

from backend.app.playbooks.backtest import DEFAULT_COSTS, USER_COSTS, USER_ORDER_YUAN, Costs


def _round_trip(c: Costs) -> float:
    buy = (1 + c.slippage_bp / 1e4) * (1 + (c.commission_bp + c.transfer_bp) / 1e4)
    sell = (1 - c.slippage_bp / 1e4) * (1 - (c.commission_bp + c.stamp_bp + c.transfer_bp) / 1e4)
    return 1 - sell / buy


def test_engine_baseline_is_unchanged() -> None:
    assert DEFAULT_COSTS == Costs(commission_bp=2.5, stamp_bp=5.0, slippage_bp=5.0, transfer_bp=0.0)
    assert _round_trip(DEFAULT_COSTS) == pytest.approx(0.0020, abs=2e-5)


def test_user_account_profile() -> None:
    assert (USER_COSTS.commission_bp, USER_COSTS.stamp_bp, USER_COSTS.transfer_bp) == (5.0, 5.0, 0.1)
    assert _round_trip(USER_COSTS) == pytest.approx(0.00252, abs=2e-5)
    # the 5-yuan minimum commission does not bind at the user's order size
    assert USER_ORDER_YUAN * USER_COSTS.commission_bp / 1e4 >= 5.0
