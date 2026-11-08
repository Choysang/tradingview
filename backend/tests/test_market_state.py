from datetime import date, datetime, timedelta

import polars as pl

from backend.app.domain import market_state as M
from backend.app.domain import triggers as T


def bars(days: int, ups_by_day) -> pl.DataFrame:
    """Ten stocks; on day i the first ups_by_day(i) close at +10%, stock 9 touches the limit and fails."""
    rows, d0 = [], date(2025, 1, 1)
    price = {c: 10.0 for c in range(10)}
    for i in range(days):
        d = d0 + timedelta(days=i)
        n = ups_by_day(i)
        for c in range(10):
            pc = price[c]
            up = round(pc * 1.1, 2)
            close, high = (up, up) if c < n else (pc, up if c == 9 else pc)
            rows.append({"code": f"60000{c}", "date": d, "high": high, "close": close})
            price[c] = close if c < n else pc
    return pl.DataFrame(rows)


def test_state_ranks_today_against_earlier_days_only():
    counts = M.daily_counts(bars(80, lambda i: 1 if i < 79 else 5))
    s = M.state_at(counts, date(2025, 1, 1) + timedelta(days=79))
    assert (s.limit_ups, s.heat) == (5, "hot") and s.failed_rate == 1 / 6
    early = M.state_at(counts, date(2025, 1, 1) + timedelta(days=30))
    assert early.heat == "unknown"  # under 60 earlier sessions
    cold = M.state_at(M.daily_counts(bars(80, lambda i: 4 if i < 79 else 0)), date(2025, 1, 1) + timedelta(days=79))
    assert cold.heat == "cold"


def test_sell_policy_v2():
    o, c = (datetime(2026, 11, 2, 9, 30), 10.0), (datetime(2026, 11, 2, 15, 0), 11.0)
    s1 = (datetime(2026, 11, 2, 10, 0), 11.0, "S1")
    assert T.sell_policy(True, "mid", s1, o, c)[1] == 10.0  # previous limit-up, not cold: the open
    assert T.sell_policy(True, "cold", s1, o, c) == s1  # cold: the intraday exit
    assert T.sell_policy(False, "hot", None, o, c)[1] == 11.0  # no exit fired: the close
    assert not T.B1_ACTIVE and not T.B3_ACTIVE and T.ACTIVE == {"B1": False, "B3": False}


def test_sell_policy_v3_keeps_names_sealed_at_the_open():
    o, c = (datetime(2026, 11, 2, 9, 30), 10.0), (datetime(2026, 11, 2, 15, 0), 11.0)
    s1 = (datetime(2026, 11, 2, 10, 0), 11.0, "S1")
    assert T.sell_policy_v3(False, s1, o, c)[1] == 10.0  # opened below the limit: the open
    assert T.sell_policy_v3(True, s1, o, c) == s1  # sealed at the open: the intraday exit
    assert T.sell_policy_v3(True, None, o, c)[1] == 11.0
