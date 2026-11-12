"""Reproduce C27 against the old repository; do not import in the new system.

Run from the old repo with this environment's audit Python and PYTHONPATH=.
The identifier is only a synthetic account fixture, not a stock recommendation.
"""
from datetime import datetime, timezone
from decimal import Decimal
from contracts.core import PortfolioState
from backend.app.domain.inventory import reserve_buy

now = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
state = PortfolioState(account_id='SAMPLE', ledger_version=0, as_of=now,
                       session_date='2026-11-10', cash_available=Decimal('10000'))
before = reserve_buy(state, 'CN.SH.600000', 100, Decimal('1000'), now)
after = reserve_buy(before, 'CN.SH.600000', -50, Decimal('-500'), now)
assert before.cash_available == Decimal('9000')
assert after.cash_available == Decimal('9500')
assert after.positions[0].pending_buy_qty == 50
print('C27 reproduced: a negative reserve operation decreases an existing reservation without raising.')
