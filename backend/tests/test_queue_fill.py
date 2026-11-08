from datetime import datetime, timedelta

import polars as pl

from backend.app.domain.queue_fill import simulate
from contracts.core import FillScenario as F

T0 = datetime(2026, 11, 2, 10, 0, 0)
LIMIT = 11.0


def _snaps(rows: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    """rows: (price, bid1_px, bid1_lots, cumulative volume_lots), one every 10 s from T0."""
    return pl.DataFrame({"quote_ts": [T0 + timedelta(seconds=10 * i) for i in range(len(rows))],
                         "price": [r[0] for r in rows], "limit_up": [LIMIT] * len(rows), "bid1_px": [r[1] for r in rows],
                         "bid1_lots": [r[2] for r in rows], "volume_lots": [r[3] for r in rows]})


END = T0 + timedelta(hours=5)


def test_sealed_board_with_a_big_queue_never_fills():
    s = _snaps([(LIMIT, LIMIT, 10_000, 50_000 + 50 * i) for i in range(30)])
    res = simulate(s, T0 + timedelta(seconds=15), 100, END)
    assert all(r.filled_lots == 0 and not r.broken for r in res.values())
    assert res[F.BASE].queue_ahead == 10_000


def test_scenarios_are_ordered_and_base_follows_patch_c():
    # queue grows from 1000 to 1200 as we arrive; 1300 lots then trade at the limit
    rows = [(LIMIT, LIMIT, 1000, 0), (LIMIT, LIMIT, 1200, 0), (LIMIT, LIMIT, 1300, 0)]
    rows += [(LIMIT, LIMIT, 1300 - 100 * i, 100 * i) for i in range(1, 14)]
    res = simulate(_snaps(rows), T0 + timedelta(seconds=5), 100, END)
    assert res[F.OPTIMISTIC].queue_ahead == 1000
    assert res[F.BASE].queue_ahead == 1200  # max(bid1 before, bid1 after)
    assert res[F.PESSIMISTIC].queue_ahead == 1300
    assert res[F.OPTIMISTIC].filled_lots == res[F.BASE].filled_lots == 100
    assert res[F.PESSIMISTIC].filled_lots == 0  # 1300 traded is exactly the queue, nothing left for us
    assert res[F.OPTIMISTIC].fill_time <= res[F.BASE].fill_time


def test_volume_while_the_price_is_off_the_limit_does_not_consume_the_queue():
    res = simulate(_snaps([(10.9, 10.9, 50, 0), (10.9, 10.9, 50, 5_000), (LIMIT, LIMIT, 100, 5_050)]), T0, 10, END)
    assert res[F.PESSIMISTIC].filled_lots == 0  # sealing interval: only the optimistic and base cases count it
    assert res[F.BASE].filled_lots == 10


def test_a_broken_board_fills_everyone_and_says_so():
    rows = [(LIMIT, LIMIT, 5_000, 0), (LIMIT, LIMIT, 5_000, 100), (10.7, 10.69, 300, 9_000)]
    res = simulate(_snaps(rows), T0 + timedelta(seconds=5), 200, END)
    assert all(r.broken and r.filled_lots == 200 for r in res.values())
    assert res[F.BASE].fill_time == T0 + timedelta(seconds=20)


def test_shrinking_bid_without_volume_counts_as_cancels_only_when_optimistic():
    rows = [(LIMIT, LIMIT, 1000, 0), (LIMIT, LIMIT, 1000, 0), (LIMIT, LIMIT, 200, 0), (LIMIT, LIMIT, 200, 300)]
    res = simulate(_snaps(rows), T0 + timedelta(seconds=5), 100, END)
    assert res[F.OPTIMISTIC].filled_lots == 100
    assert res[F.BASE].filled_lots == 0 and res[F.PESSIMISTIC].filled_lots == 0


def test_order_expires_and_late_arrival_has_no_fill():
    rows = [(LIMIT, LIMIT, 100, 100 * i) for i in range(10)]
    res = simulate(_snaps(rows), T0 + timedelta(seconds=5), 100, T0 + timedelta(seconds=15))
    assert res[F.BASE].filled_lots == 0  # only one interval inside the order's life
    res = simulate(_snaps(rows), T0 + timedelta(hours=1), 100, END)
    assert all(r.filled_lots == 0 and r.fill_time is None for r in res.values())
