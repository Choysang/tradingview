"""LHB comparison against real pulse3 seat reports (PostgreSQL, recorded Eastmoney bodies) and a synthetic old warehouse; read only."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from intel import helpers as h
from pulse.adapters.postgres import instrument_repo
from pulse.adapters.sources.lhb import LhbAdapter
from pulse.application import lhb_compare as lc
from pulse.domain.instruments.identity import InstrumentRecord
from pulse.domain.intelligence import feeds, seats
from pulse.workers import lhb_compare as worker

from . import support as sup

pytestmark = pytest.mark.pg
D = date(2026, 11, 10)
UTC = timezone.utc


@pytest.fixture
def loaded(svc, conn, clock):
    """The recorded 2026-11-10 list stored by pulse3's own ingest; returns its (stocks, seats) in the old store's row format."""
    lst = feeds.parse_lhb_list(h.recorded("lhb_daily_list_20261110.json")).rows
    recs = [InstrumentRecord.build(str(r["SECURITY_CODE"]), str(r.get("SECURITY_NAME_ABBR") or "x")) for r in lst if seats.is_equity_code(str(r["SECURITY_CODE"]))]
    with conn.transaction():
        conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES ('legacy_warehouse','legacy warehouse','urn:test',%s)"
                     " ON CONFLICT (source_id) DO NOTHING", (datetime(2026, 11, 1, tzinfo=UTC),))
        instrument_repo.upsert_instruments(conn, recs, datetime(2026, 11, 1, tzinfo=UTC), ("legacy_warehouse",), "lhb-compare-tests")
    svc.ingest_lhb(LhbAdapter(h.client(now=clock)), [D])
    return sup.old_rows_from_recorded(h.recorded, D)


def _run(conn, root):
    return lc.run_compare(conn, root, D, D)


def test_recorded_day_matches_its_old_warehouse_copy(loaded, conn, tmp_path):
    stocks, seat_rows = loaded
    sup.write_old(tmp_path, stocks, seat_rows)
    c = _run(conn, tmp_path)
    s = c.sessions[0]
    assert s.verdict == "SAME", s.reasons
    assert s.old == s.new and s.old.stocks > 3 and s.old.institution_rows > 0 and s.old.lhasa_rows > 0
    assert c.all_same and c.same_tail == 1 and not c.ready_to_retire


def test_differences_are_found_per_stock_and_seat(loaded, conn, tmp_path):
    stocks, seat_rows = loaded
    codes = sorted({s["code"] for s in stocks})
    drop_stock, change_stock = codes[0], codes[1]
    lst = [s for s in stocks if s["code"] != drop_stock] + [{"code": "603999", "date": D, "reason": "ghost"}]
    rows = [dict(r) for r in seat_rows if r["code"] != drop_stock]
    victim = next(r for r in rows if r["code"] == change_stock and r["side"] == "buy")
    victim["buy"] = victim["buy"] * 1.2  # 20% off
    within = next(r for r in rows if r["code"] == codes[2] and r["side"] == "sell")
    within["sell"] = within["sell"] * 1.004  # inside the 0.5% tolerance
    gone = next(r for r in rows if r["code"] == codes[3])
    rows.remove(gone)
    sup.write_old(tmp_path, lst, rows)
    s = _run(conn, tmp_path).sessions[0]
    assert s.verdict == "DIFF"
    assert s.stocks_only_new == [drop_stock] and s.stocks_only_old == ["603999"]
    amount = [d for d in s.diffs if d.kind == "amount"]
    assert [(d.code, d.seat) for d in amount] == [(change_stock, victim["seat"])] and amount[0].new != amount[0].old
    assert any(d.kind == "seat_only_new" and d.code == gone["code"] and d.seat == gone["seat"] for d in s.diffs)
    assert not any(d.code == codes[2] and d.kind == "amount" for d in s.diffs)
    ghost = next(d for d in s.diffs if d.code == "603999")
    assert "not registered" in ghost.note  # 603999 is no registered instrument in pulse3


def test_session_missing_on_the_new_side_and_the_newest_revision_wins(loaded, conn, svc, clock, tmp_path):
    stocks, seat_rows = loaded
    other = date(2026, 11, 11)
    sup.write_old(tmp_path, stocks + [{**stocks[0], "date": other}], seat_rows)
    c = lc.run_compare(conn, tmp_path, D, other)
    assert [s.verdict for s in c.sessions] == ["SAME", "DIFF"] and "old side only" in c.sessions[1].reasons[0] and c.same_tail == 0
    # a changed disclosure appends revision 2: the comparison reads revision 2, so the unchanged old copy now differs
    doc = json.loads(h.recorded("lhb_seat_buy_20261110.json"))
    doc["result"]["data"][0]["BUY"] = (doc["result"]["data"][0]["BUY"] or 0) + 5_000_000
    transport = h.transport(overrides={"RPT_BILLBOARD_DAILYDETAILSBUY": (200, json.dumps(doc).encode())})
    svc.ingest_lhb(LhbAdapter(h.client(now=clock, transport_=transport)), [D])
    s = _run(conn, tmp_path).sessions[0]
    assert s.verdict == "DIFF" and any(d.kind == "amount" for d in s.diffs)


def test_cli_reports_json_exit_codes_and_writes_nothing(loaded, conn, fresh_dsn, tmp_path, capsys):
    stocks, seat_rows = loaded
    sup.write_old(tmp_path, stocks, seat_rows)
    before = (conn.execute("SELECT count(*) AS n FROM intelligence.seat_report").fetchone()["n"], sorted(p.name for p in tmp_path.rglob("*")))
    args = ["--since", "2026-11-10", "--until", "2026-11-10", "--legacy-warehouse", str(tmp_path), "--dsn", fresh_dsn]
    assert worker.run(args + ["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sessions"][0]["verdict"] == "SAME" and out["ready_to_retire"] is False
    assert worker.run(args) == 0 and "2026-11-10 SAME" in capsys.readouterr().out
    after = (conn.execute("SELECT count(*) AS n FROM intelligence.seat_report").fetchone()["n"], sorted(p.name for p in tmp_path.rglob("*")))
    assert before == after
    sup.write_old(tmp_path, stocks[:-1], seat_rows, name="b.parquet")  # same keys: newest ingest wins, still SAME
    assert worker.run(args) == 0
    capsys.readouterr()
    assert worker.run(["--since", "2026-11-10", "--until", "2026-11-10", "--legacy-warehouse", str(tmp_path / "missing"), "--dsn", fresh_dsn]) == 2
    assert "cannot compare" in capsys.readouterr().err


def test_database_session_is_read_only(fresh_dsn):
    import psycopg
    from pulse.adapters.postgres import db

    c = db.connect(fresh_dsn, autocommit=True)
    try:
        c.execute("SET default_transaction_read_only = on")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("CREATE TABLE public.should_not_exist(x int)")
    finally:
        c.close()


def test_reports_without_a_code_alias_are_counted_not_silently_dropped(loaded, conn, tmp_path):
    stocks, seat_rows = loaded
    sup.write_old(tmp_path, stocks, seat_rows)
    conn.execute("DELETE FROM core.instrument_alias")
    c = _run(conn, tmp_path)
    assert c.dropped["new_reports_without_code"] == 55 and c.sessions[0].verdict == "DIFF"
    assert "new_reports_without_code=55" in lc.render(c)
