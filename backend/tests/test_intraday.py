from datetime import date, datetime

import httpx
import polars as pl

from backend.app.warehouse import intraday as I

# A real qt.gtimg.cn line (600000, 2026-10-30 close), decoded from GBK.
FIELDS = ["1", "浦发银行", "600000", "10.02", "9.99", "9.98", "648865", "361302", "287563",
          "10.01", "98", "10.00", "205", "9.99", "1531", "9.98", "880", "9.97", "458",
          "10.02", "372", "10.03", "589", "10.04", "2205", "10.05", "13046", "10.06", "4753",
          "", "20261030161441", "0.03", "0.30", "10.04", "9.90", "10.02/648865/647741483", "648865", "64774",
          "0.19", "6.45", "", "10.04", "9.90", "1.40", "3337.24", "3337.24", "0.44", "10.99", "8.99", "0.86",
          "-17793", "9.98", "6.17", "6.67", "", "", "-0.00", "64774.1483"]
QUOTE = 'v_sh600000="' + "~".join(FIELDS) + '";\nv_sz000001="";\n'
POLL = datetime(2026, 10, 30, 15, 1, 30)


def test_snapshot_maps_book_limits_and_amount():
    rows = I.parse_snapshot(QUOTE, "close", POLL)
    assert len(rows) == 1  # the empty line (no name) is skipped
    r = rows[0]
    assert (r["code"], r["name"], r["kind"]) == ("600000", "浦发银行", "close")
    assert (r["price"], r["prev_close"], r["open"], r["high"], r["low"]) == (10.02, 9.99, 9.98, 10.04, 9.90)
    assert (r["bid1_px"], r["bid1_lots"], r["bid5_px"], r["bid5_lots"]) == (10.01, 98, 9.97, 458)
    assert (r["ask1_px"], r["ask1_lots"], r["ask5_px"], r["ask5_lots"]) == (10.02, 372, 10.06, 4753)
    assert (r["limit_up"], r["limit_down"]) == (10.99, 8.99)
    assert r["amount_yuan"] == 647741483 and r["volume_lots"] == 648865
    assert r["quote_ts"] == datetime(2026, 10, 30, 16, 14, 41)
    assert (r["float_cap"], r["vwap"], r["turnover"]) == (3337.24, 9.98, 0.19)
    frame = pl.DataFrame(rows, schema=I.SNAPSHOT_SCHEMA)
    assert frame.height == 1


def test_zero_price_is_null_not_zero():
    fields = list(FIELDS)
    fields[3] = "0.00"
    rows = I.parse_snapshot('v_sh600000="' + "~".join(fields) + '";', "full", POLL)
    assert rows[0]["price"] is None


def test_minute_rows():
    payload = {"code": 0, "data": {"sh600000": {"data": {"date": "20261030",
                                                          "data": ["0930 9.98 6676 6662648.00", "0931 9.92 26374 26288847.00", "bad"]}}}}
    rows = I.parse_minute(payload, "600000")
    assert [r["minute"] for r in rows] == ["0930", "0931"]
    assert rows[1]["cum_lots"] == 26374 and rows[0]["date"] == date(2026, 10, 30)
    assert I.parse_minute({"data": {}}, "600000") == []


def test_hot_set_and_movers():
    frame = pl.DataFrame({"code": ["a", "b", "c", "d"], "price": [10.9, 9.0, 10.0, 9.5], "prev_close": [9.91, 10.0, 10.0, 10.0],
                          "limit_up": [10.9, 11.0, 11.0, 11.0], "limit_down": [8.92, 9.0, 9.0, 9.0],
                          "high": [10.9, 9.5, 11.0, 9.6], "low": [10.0, 9.0, 9.9, 9.4]})
    assert I.hot_codes(frame) == {"a", "b", "c"}  # at the limit, at the floor, touched the limit
    assert I.movers(frame) == {"a", "b", "d"}


def _bars(rows):
    return pl.DataFrame(rows, schema={"code": pl.Utf8, "date": pl.Date, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64}, orient="row")


def test_static_pool_last_three_sessions():
    d = [date(2026, 10, day) for day in (26, 27, 28, 29, 30)]
    rows = []
    for code, moves in {"600001": [0, 10, 0, 0, 0], "600002": [0, 0, 0, 10, 0], "600003": [0, 0, 0, 0, -10], "600004": [0] * 5}.items():
        close = 10.0
        for day, move in zip(d, moves):
            hi = close * (1 + move / 100) if move > 0 else close
            lo = close * (1 + move / 100) if move < 0 else close
            rows.append((code, day, hi, lo, close))
    pool = I.static_pool(_bars(rows))
    assert pool == {"600002", "600003"}  # 600001 touched on 10-27, outside the last 3 sessions


def test_schedule():
    c = I.Clock()
    at = lambda h, m, s=0: datetime(2026, 11, 2, h, m, s)
    assert I.due(at(9, 16, 1), c) == []  # before the minute offset
    assert I.due(at(9, 16, 3), c) == ["full"]
    assert set(I.due(at(9, 19, 31), c)) == {"full", "auction_hot"}
    c.last_full, c.last_auction = I.slot(at(9, 19, 31), 60), I.slot(at(9, 19, 31), 5)
    assert I.due(at(9, 19, 33), c) == []
    assert I.due(at(9, 19, 35), c) == ["auction_hot"]
    assert I.due(at(9, 25, 12), c) == ["auction_final"]
    assert I.due(at(9, 28), c) == []
    assert set(I.due(at(10, 0, 5), c)) == {"full", "hot"}
    assert I.due(at(12, 0, 5), c) == []
    assert "minute_hot" not in I.due(at(15, 3), c)  # waits until the after-hours session is over
    assert "minute_hot" not in I.due(at(15, 31), c)  # after Pulse's 15:35 daily sync
    assert "minute_hot" in I.due(at(16, 20), c)
    late = I.Clock()
    assert "auction_final" not in I.due(at(10, 0, 5), late)  # a late start skips passed one-shots


def test_after_hours_schedule():
    c = I.Clock()
    at = lambda h, m, s=0: datetime(2026, 11, 2, h, m, s)
    assert I.due(at(15, 4, 50), c) == []
    assert I.due(at(15, 5, 3), c) == []  # before the grid offset
    assert set(I.due(at(15, 5, 6), c)) == {"after_hot", "after_open"}
    c.last_after, c.done = I.slot(at(15, 5, 6), 60), {"after_open"}
    assert I.due(at(15, 5, 30), c) == []
    assert set(I.due(at(15, 15, 5), c)) == {"after_hot", "after_mid"}
    c.last_after, c.done = I.slot(at(15, 15, 5), 60), c.done | {"after_mid"}
    assert "pools" in I.due(at(15, 25, 5), c) and "after_hot" in I.due(at(15, 25, 5), c)
    c.last_after, c.done = I.slot(at(15, 30, 5), 60), c.done | {"pools"}
    assert I.due(at(15, 30, 6), c) == ["after_end"]
    assert I.due(at(15, 31, 10), c) == ["after_end"]  # a late after_end still runs before 15:33
    assert I.missed(at(15, 5, 0), at(15, 8, 0), 60) == 2  # 15:06, 15:07 in the after-hours window
    assert I.missed(at(14, 59, 0), at(15, 6, 0), 60) == 0  # close to after-hours is a break


def test_after_hours_steps_record_static_and_hot(monkeypatch):
    rec = I.Recorder.__new__(I.Recorder)
    rec.static, rec.hot, rec.bonds = {"600001"}, {"600002"}, {}
    seen = []
    rec.snapshot = lambda kind, codes: seen.append((kind, codes))
    rec.full = lambda kind: seen.append((kind, "all"))
    I.step(rec, "after_hot")
    I.step(rec, "after_mid")
    assert seen == [("after_hot", ["600001", "600002"]), ("after", "all")]


def test_minute_survives_garbage():
    for payload in (None, [], "x", {"data": "x"}, {"data": {"sh600000": {"data": {"date": "20261030", "data": "x"}}}}):
        assert I.parse_minute(payload, "600000") == []


def test_missed_slots():
    t = datetime(2026, 11, 2, 10, 0, 0)
    assert I.missed(None, t, 10) == 0
    assert I.missed(t, t.replace(second=10), 10) == 0
    assert I.missed(t, t.replace(second=40), 10) == 3  # :10, :20, :30
    assert I.missed(datetime(2026, 11, 2, 11, 30), datetime(2026, 11, 2, 13, 0), 10) == 0  # lunch is a break
    assert I.missed(datetime(2026, 11, 2, 9, 25), datetime(2026, 11, 2, 9, 30), 60) == 0  # auction to session


def test_nearest_prefers_names_closest_to_a_limit():
    frame = pl.DataFrame({"code": ["600001", "000001", "600002"], "price": [10.95, 10.5, 9.0],
                          "limit_up": [11.0, 11.0, 11.0], "limit_down": [9.0, 9.0, 9.0]})
    assert I.nearest(frame, {"600001", "000001", "600002"}, 2) == {"600001", "600002"}


def _recorder(tmp_path, handler):
    import httpx
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return I.Recorder(date(2026, 11, 2), ["600000"], tmp_path, client)


def test_pools_stop_on_html_and_survive_bad_json(tmp_path):
    import httpx
    rec = _recorder(tmp_path, lambda req: httpx.Response(200, text="<html>blocked</html>"))
    rec.pools()
    assert I.POOL_HOST in rec.blocked
    rec = _recorder(tmp_path / "b", lambda req: httpx.Response(200, content=b"null"))
    rec.pools()  # a null payload is skipped, not a crash
    assert rec.stats["pools"] == {} and I.POOL_HOST not in rec.blocked


def test_compact_merges_parts_and_dedups_minutes(tmp_path):
    import httpx
    rec = _recorder(tmp_path, lambda req: httpx.Response(500))
    row = {"code": "600000", "date": date(2026, 11, 2), "minute": "0930", "price": 10.0, "cum_lots": 1.0, "cum_amount_yuan": 1.0}
    for price in (10.0, 10.1):
        rec.minute_rows = [{**row, "price": price}]
        rec._flush_minute()
    snap = I.parse_snapshot(QUOTE, "full", POLL)
    rec.frames["quotes"] = [pl.DataFrame(snap, schema=I.SNAPSHOT_SCHEMA)] * 2
    summary = rec.finish("test")
    minute = pl.read_parquet(tmp_path / "2026-11-02" / "minute.parquet")
    assert minute.height == 1 and minute["price"][0] == 10.1
    assert pl.read_parquet(tmp_path / "2026-11-02" / "quotes.parquet").height == 2
    assert not list((tmp_path / "2026-11-02" / "minute").glob("part-*"))
    assert summary["end"] == "test" and rec.finish("again") == {}
    assert summary["minute_synth_codes"] == 0 and not summary["errors"]  # the quote is stamped after the close
    assert (tmp_path / "2026-11-02" / "minute_synth.parquet").exists()


def test_failures_in_a_row_block_the_host(tmp_path):
    import httpx
    rec = _recorder(tmp_path, lambda req: httpx.Response(502))
    for _ in range(I.MAX_FAILS_IN_ROW):
        rec._failed(I.MINUTE_HOST, RuntimeError("502"))
    assert I.MINUTE_HOST in rec.blocked


def test_bond_tickers_and_list():
    assert I.qticker("113050") == "sh113050" and I.qticker("123291") == "sz123291" and I.qticker("600000") == "sh600000"
    rows = [
        {"SECURITY_CODE": "113050", "CONVERT_STOCK_CODE": "600001", "LISTING_DATE": "2024-01-02 00:00:00", "DELIST_DATE": None},
        {"SECURITY_CODE": "123291", "CONVERT_STOCK_CODE": "300181", "LISTING_DATE": "2024-01-02", "DELIST_DATE": None},  # ChiNext stock
        {"SECURITY_CODE": "127120", "CONVERT_STOCK_CODE": "002550", "LISTING_DATE": None, "DELIST_DATE": None},  # not listed yet
        {"SECURITY_CODE": "128001", "CONVERT_STOCK_CODE": "002001", "LISTING_DATE": "2020-01-02", "DELIST_DATE": "2025-01-02"},  # gone
    ]
    assert I.bonds_of(rows, date(2026, 11, 2)) == {"113050": "600001"}


def test_hot_polls_carry_the_stocks_bonds(tmp_path):
    import httpx
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    rec = I.Recorder(date(2026, 11, 2), ["600001", "600002"], tmp_path, client, static={"600001"}, bonds={"113050": "600001", "110001": "600099"})
    assert rec.bonds == {"113050": "600001"}  # a bond whose stock is not recorded is dropped
    assert rec.with_bonds({"600001"}) == ["113050", "600001"]


def test_after_hours_fields_parse_and_short_lines_stay_null():
    f = ["1", "德明利", "001309", "207.78"] + ["0"] * 26 + ["20261102153000"] + ["0"] * 4 + ["207.78/182905/3800000000"] + ["0"] * 52
    f[47], f[48], f[51], f[58], f[59] = "228.56", "187.00", "205.0", "214.0134", "103"
    long_line = 'v_sz001309="' + "~".join(f) + '";'
    short_line = 'v_sz001309="' + "~".join(f[:52]) + '";'
    row = I.parse_snapshot(long_line, "after", datetime(2026, 11, 2, 15, 30, 5))[0]
    assert (row["after_lots"], row["after_amount_wan"]) == (103.0, 214.0134)
    short = I.parse_snapshot(short_line, "after", datetime(2026, 11, 2, 15, 30, 5))[0]
    assert short["after_lots"] is None and short["price"] == 207.78


def test_minute_order_puts_limit_names_first():
    snap = I.parse_snapshot(QUOTE, "close", POLL)[0]
    latest = pl.DataFrame([
        {**snap, "code": "600001", "price": 10.99, "high": 10.99},  # sealed limit-up, missing from the pools
        {**snap, "code": "600002", "price": 10.5, "high": 10.99},  # touched and opened: broken board
        {**snap, "code": "600003", "price": 8.99, "low": 8.99},  # limit-down
    ], schema=I.SNAPSHOT_SCHEMA)
    codes = {"600001", "600002", "600003", "600004", "600005", "600006", "600007"}
    pools = {"zt": {"600007", "300001"}, "zb": set()}  # a pool name outside the wanted codes is not fetched
    order = I.minute_order(codes, latest, pools, watched={"600005"})
    assert order == ["600001", "600007", "600002", "600003", "600005", "600004", "600006"]
    assert I.minute_order(codes, pl.DataFrame(schema=I.SNAPSHOT_SCHEMA), {}, set()) == sorted(codes)


def test_reparse_rebuilds_rows_with_the_stored_poll_time(tmp_path):
    import gzip
    day = tmp_path / "2026-10-30"
    (day / "raw").mkdir(parents=True)
    poll = datetime(2026, 10, 30, 15, 30, 5, 123000)
    old = pl.DataFrame(I.parse_snapshot(QUOTE, "after", poll), schema=I.SNAPSHOT_SCHEMA).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("after_amount_wan"))  # as recorded before the field was parsed
    other = old.with_columns(pl.lit("full").alias("kind"), pl.lit(datetime(2026, 10, 30, 10, 0, 2)).cast(pl.Datetime("ms")).alias("poll_ts"))
    pl.concat([old, other]).write_parquet(day / "quotes.parquet")
    with gzip.open(day / "raw" / "quotes.txt.gz", "wt", encoding="utf-8") as fh:
        fh.write("# after 15:30:05\n" + 'v_sh600000="' + "~".join([*FIELDS, "214.0134", "103"]) + '";\n')
    assert I.reparse(day) == {"quotes": 1}
    new = pl.read_parquet(day / "quotes.parquet").sort("kind")
    assert new["kind"].to_list() == ["after", "full"] and (day / "quotes.v0.parquet").exists()
    after = new.row(0, named=True)
    assert after["poll_ts"] == poll and (after["after_amount_wan"], after["after_lots"]) == (214.0134, 103.0)


def test_minute_fetch_rests_every_run_and_a_block_stops_it(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(I.time, "sleep", sleeps.append)
    seen: list[str] = []
    block_at = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["code"])
        return httpx.Response(501 if len(seen) == block_at[0] else 200, json={"data": {}})

    codes = [f"600{i:03d}" for i in range(I.MINUTE_BURST * 2 + 5)]
    _recorder(tmp_path / "short", handler).minutes(codes[:I.MINUTE_BURST], "hot")
    assert I.MINUTE_REST not in sleeps  # a list that fits in one run never rests
    seen.clear()
    _recorder(tmp_path / "long", handler).minutes(codes, "hot")
    assert sleeps.count(I.MINUTE_REST) == 2 and len(seen) == len(codes)  # at codes 100 and 200
    seen.clear()
    sleeps.clear()
    block_at[0] = I.MINUTE_BURST + 50
    rec = _recorder(tmp_path / "blocked", handler)
    rec.minutes(codes, "hot")
    assert sleeps.count(I.MINUTE_REST) == 1 and len(seen) == block_at[0] and I.MINUTE_HOST in rec.blocked
