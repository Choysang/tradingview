"""Forward books on a private PostgreSQL: LEGACY_FORWARD paper accounts on the one ledger, computed day by day by the kernel."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse.adapters.postgres.ledger_repo import LedgerRepo
from pulse.application import portfolio_service as PS
from pulse.domain.portfolio import forward_book as FB

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc


def build(conn):
    ids = [S.seed_instrument(conn, f"60099{i}", f"前向{i}") for i in range(1, 6)]
    snap = S.seed_manifest(conn)
    days = S.weekdays(date(2026, 10, 26), 12)                       # 10-26 .. 11-10 (Mon..Tue)
    for k, iid in enumerate(ids):
        S.seed_bars(conn, iid, snap, days, base=Decimal("10") + k, step=Decimal("0.1"))
    return ids, snap, days


def release(conn, ids, as_of):
    return S.publish(conn, slot="main", as_of=as_of, ranking=ids)


def test_two_books_are_registered_labelled_and_decide_then_execute_at_the_next_open(fresh_conn, principal):
    conn = fresh_conn
    ids, snap, days = build(conn)
    assert days[-1] == date(2026, 11, 10)
    release(conn, ids, datetime(2026, 11, 11, 4, 0, tzinfo=UTC))
    books = PS.register_forward_books(conn, [principal])
    assert len(books) == 2 and PS.register_forward_books(conn, [principal]) == books                     # idempotent
    rows = {r["book_key"]: r for r in conn.execute("SELECT * FROM execution.tracked_account")}
    assert set(rows) == {"main_h5", "shadow_h10"} and all(r["status_label"] == "检验中" and r["ranking_weight"] == "none" and r["tracking_kind"] == "LEGACY_FORWARD" for r in rows.values())
    assert rows["main_h5"]["rule"]["hold_days"] == 5 and rows["main_h5"]["rule"]["new_per_day"] == 4 and rows["main_h5"]["rule"]["per_name_cny"] == "50000"
    assert rows["shadow_h10"]["rule"]["hold_days"] == 10 and rows["shadow_h10"]["rule"]["new_per_day"] == 2
    ranking_rows = conn.execute("SELECT count(*) n FROM decision.ranking_entry").fetchone()["n"]

    d1 = PS.forward_daily(conn, datetime(2026, 11, 11, 5, 0, tzinfo=UTC))
    assert d1["main_h5"]["status"] == "decided" and d1["main_h5"]["buys"] == 4 and d1["shadow_h10"]["buys"] == 2
    assert PS.forward_daily(conn)["main_h5"]["status"] == "already_decided"                                  # one decision per session
    # no bar yet for the execution day: nothing is executed, nothing invented
    # the next session arrives; I3 opens at the limit-up price and cannot be bought
    nxt = date(2026, 11, 11)
    for k, iid in enumerate(ids):
        S.seed_bars(conn, iid, snap, [nxt], base=Decimal("10") + k + Decimal("1.2"), step=Decimal("0"))
    ref = conn.execute("SELECT close_price_raw c FROM market.bar_projection WHERE instrument_id=%s AND trade_date=%s", (ids[2], date(2026, 11, 10))).fetchone()["c"]
    limit_up = (ref * Decimal("1.1")).quantize(Decimal("0.01"))
    conn.execute("UPDATE market.bar_projection SET exchange_reference_price_raw=%s, open_price_raw=%s, high_price_raw=%s, low_price_raw=%s, close_price_raw=%s WHERE instrument_id=%s AND trade_date=%s",
                 (ref, limit_up, limit_up, limit_up - Decimal("0.5"), limit_up, ids[2], nxt))
    for k, iid in enumerate(ids):
        if k != 2:
            conn.execute("UPDATE market.bar_projection SET exchange_reference_price_raw=(SELECT close_price_raw FROM market.bar_projection p WHERE p.instrument_id=%s AND p.trade_date=%s) WHERE instrument_id=%s AND trade_date=%s",
                         (iid, date(2026, 11, 10), iid, nxt))
    release(conn, ids, datetime(2026, 11, 12, 4, 0, tzinfo=UTC))
    d2 = PS.forward_daily(conn, datetime(2026, 11, 12, 5, 0, tzinfo=UTC))
    assert d2["main_h5"]["status"] == "decided" and d2["main_h5"]["trade_date"] == "2026-11-11"

    main = PS.forward_account_id("main_h5")
    led = LedgerRepo(conn).load(main)
    held = {s: h.quantity for s, h in led.holdings.items() if h.quantity}
    open_price = lambda iid: conn.execute("SELECT open_price_raw o FROM market.bar_projection WHERE instrument_id=%s AND trade_date=%s", (iid, nxt)).fetchone()["o"]  # noqa: E731
    assert set(held) == {str(ids[0]), str(ids[1]), str(ids[3])}                                              # I3 (limit-up open) was not bought
    for k in (0, 1, 3):
        close_signal = conn.execute("SELECT close_price_raw c FROM market.bar_projection WHERE instrument_id=%s AND trade_date=%s", (ids[k], date(2026, 11, 10))).fetchone()["c"]
        assert held[str(ids[k])] == FB.lot_quantity(Decimal("50000"), close_signal)              # sized on the signal close, priced at the next open
    fills = [e for e in LedgerRepo(conn).events(main) if e["kind"] == "fill.booked"]
    assert len(fills) == 3 and all(Decimal(f["payload"]["fee"]) > 0 for f in fills)                           # costs from the user's account profile, not zero
    day1 = conn.execute("SELECT planned_buys FROM execution.tracked_book_day WHERE account_id=%s AND trade_date='2026-11-10'", (main,)).fetchone()["planned_buys"]
    assert day1["executed"] is True
    reasons = {r["instrument_id"]: r["reason"] for r in day1["results"]["buys"]}
    assert reasons[str(ids[2])] == "LIMIT_UP_OPEN" and reasons[str(ids[0])] is None
    opp = {(str(r["instrument_id"]), r["opportunity_version"]): r for r in conn.execute("SELECT * FROM research.opportunity_event WHERE account_id=%s", (main,))}
    assert opp[(str(ids[2]), 1)]["state"] == "triggered" and opp[(str(ids[2]), 2)]["state"] == "unfilled" and opp[(str(ids[2]), 2)]["reason_code"] == "LIMIT_UP_OPEN"
    assert opp[(str(ids[0]), 2)]["state"] == "filled" and opp[(str(ids[0]), 2)]["filled_quantity"] == held[str(ids[0])]
    # the second day's NAV is the ledger's own number with marks; the shadow bought its two
    nav = conn.execute("SELECT nav_cny, missing_marks FROM execution.tracked_book_day WHERE account_id=%s AND trade_date='2026-11-11'", (main,)).fetchone()
    assert nav["nav_cny"] is not None and nav["missing_marks"] == 0 and abs(nav["nav_cny"] - Decimal("1000000")) < Decimal("20000")
    sh = {s for s, h in LedgerRepo(conn).load(PS.forward_account_id("shadow_h10")).holdings.items() if h.quantity}
    assert sh == {str(ids[0]), str(ids[1])}
    # books never touch rankings or release tables
    assert conn.execute("SELECT count(*) n FROM decision.ranking_entry").fetchone()["n"] == ranking_rows * 2

    wire = PS.forward_books_wire(conn, principal)["books"]
    assert [b["book_key"] for b in wire] == ["main_h5", "shadow_h10"]
    b = wire[0]
    assert b["status_label"] == "检验中" and b["ranking_weight"] == "none" and b["latest"]["trade_date"] == date(2026, 11, 11)
    assert {h["name"] for h in b["latest"]["holdings"]} == {"前向1", "前向2", "前向4"} and len(b["history"]) == 2
    assert any("不参与排名权重" in n for n in b["notes"])
    assert PS.forward_books_wire(conn, uuid.uuid4())["books"] == []                                           # an ungranted user sees no book


def test_awaiting_bar_does_not_decide_a_new_day(fresh_conn, principal):
    conn = fresh_conn
    ids, snap, days = build(conn)
    release(conn, ids, datetime(2026, 11, 11, 4, 0, tzinfo=UTC))
    PS.register_forward_books(conn, [principal])
    PS.forward_daily(conn)
    release(conn, ids, datetime(2026, 11, 12, 4, 0, tzinfo=UTC))                                        # newer release, but still no bar after 11-10
    assert PS.forward_daily(conn)["main_h5"]["status"] == "already_decided"
