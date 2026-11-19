"""Native ext recorder, adapter level (no database): parsing per endpoint with units, the request plan, refusals, pacing, frame parity with the
legacy import. Saved answers only (fixtures/ext/recorded, copied from the old recorder's tests); nothing here touches the network."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import polars as pl
import pytest

from pulse.adapters.sources import ext_quotes as X
from pulse.adapters.sources import registry
from pulse.adapters.sources.http import SourceBlocked, TransportFailure
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_ext_record as R

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ext" / "recorded"
DAY = date(2026, 11, 6)
POLL = datetime(2026, 11, 6, 15, 1, 30)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def text(name: str) -> str:
    return fixture(name).decode("gbk")


def tencent_body() -> bytes:
    return fixture("tencent_etf_index.txt") + b"\n" + fixture("tencent_overseas.txt")


def rows_of(symbol: str, rows: list[dict]) -> dict:
    return next(r for r in rows if r["symbol"] == symbol)


# --- parsers on the saved answers, with units --------------------------------------------------------------------------------
def test_tencent_etf_row_units_and_stamps():
    rows = X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL)
    assert [r["asset"] for r in rows].count("etf") == 7 and [r["asset"] for r in rows].count("index") == 10
    etf = rows_of("sh510300", rows)
    assert etf["name"] == "沪深300ETF华泰柏瑞" and etf["host"] == X.QUOTE_HOST and etf["kind"] == "grid"
    assert (etf["price"], etf["prev_close"], etf["prev_kind"], etf["open"]) == (4.124, 4.195, "close", 4.113)
    assert (etf["high"], etf["low"], etf["bid1"], etf["ask1"]) == (4.169, 4.095, 4.124, 4.125)
    assert etf["volume"] == 12629370 and etf["amount"] == 5211672667            # lots as the vendor gives them; amount in yuan (field 35)
    assert etf["quote_ts"] == datetime(2026, 11, 6, 16, 14, 52) and etf["poll_ts"] == POLL
    assert etf["contract"] is None and etf["open_interest"] is None


def test_tencent_index_book_is_null_not_zero():
    index = rows_of("sh000001", X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL))
    assert index["name"] == "上证指数" and index["asset"] == "index"
    assert index["bid1"] is None and index["ask1"] is None and index["prev_kind"] == "close"


def test_tencent_skips_unknown_ticker_and_short_lines():
    assert X.parse_tencent('v_sh510300="1~x~510300~4~4";\nv_xx123="1";\n', "grid", POLL) == []


def test_tencent_overseas_futures_and_commodities_have_no_trusted_book_or_volume():
    rows = X.parse_tencent(text("tencent_overseas.txt"), "grid", POLL)
    assert {r["symbol"] for r in rows} == {"hf_HSI", "hf_GC", "hf_CL"}
    oil, hsi = rows_of("hf_CL", rows), rows_of("hf_HSI", rows)
    assert (oil["asset"], oil["price"], oil["prev_close"], oil["prev_kind"]) == ("commodity", 89.9, 90.83, "settle")
    assert (hsi["asset"], hsi["name"], hsi["price"], hsi["quote_ts"]) == ("future", "恒生指数期货", 24440.52, datetime(2026, 11, 9, 16, 14, 16))
    assert all(r[k] is None for r in rows for k in ("bid1", "ask1", "volume", "amount", "contract", "open_interest"))


def test_tencent_skips_a_short_overseas_line():
    assert X.parse_tencent('v_hf_GC="1,2";\nv_hf_CL="";\n', "grid", POLL) == []


def test_cffex_rows_units_and_last_modified_stamp():
    rows = X.parse_cffex(text("cffex_IF.txt"), "grid", POLL)
    assert [r["symbol"] for r in rows] == ["IF2611", "IF2612", "IF2703", "IF2706"]
    row = rows[1]
    assert (row["asset"], row["host"], row["contract"], row["name"]) == ("future", X.CFFEX_HOST, "IF2612", "IF2612")
    assert (row["open"], row["high"], row["low"], row["price"]) == (4037.4, 4063.2, 4022.0, 4023.6)
    assert (row["bid1"], row["ask1"], row["volume"], row["open_interest"]) == (4023.2, 4023.8, 52033, 173775)    # contracts
    assert row["amount"] is None                                                                               # the file has none: NULL, not a guess
    assert row["quote_ts"] == datetime(2026, 11, 9, 16, 16, 20)                                                # Last-Modified, in CST


def test_cffex_previous_settlement_is_last_minus_change_and_agrees_with_eastmoney():
    settlement = rows_of("IF2611", X.parse_cffex(text("cffex_IF.txt"), "grid", POLL))
    assert (settlement["prev_close"], settlement["prev_kind"]) == (4044.8, "settle")
    em = json.loads(fixture("eastmoney_ulist.txt"))["data"]["diff"]
    assert next(q for q in em if q["f12"] == "040120")["f28"] == settlement["prev_close"]


def test_cffex_main_contract_is_readable_from_the_open_interest():
    frame = pl.DataFrame(X.parse_cffex(text("cffex_IM.txt"), "grid", POLL), schema=X.ROW_SCHEMA)
    assert frame.sort("open_interest")["contract"][-1] == "IM2612"            # the main contract, not the front month IM2611


def test_cffex_zero_fields_before_the_open_are_null():
    body = X.CFFEX_COLUMNS + "\nIF2611,0.00,0.00,0.00,0.00,0.00,0.00,0,0.00,0,0,46000\n"
    row = X.parse_cffex(body, "grid", POLL)[0]
    assert all(row[k] is None for k in ("price", "open", "high", "low", "prev_close", "bid1", "ask1"))
    assert row["volume"] == 0 and row["open_interest"] == 46000 and row["quote_ts"] is None


@pytest.mark.parametrize("body", ["", "instrument,price\nIF2611,1\n", X.CFFEX_COLUMNS + "\nIF2611,1,2\n", X.CFFEX_COLUMNS + "\nXX2611,1,1,1,1,1,1,1,1,1,1,1\n"])
def test_cffex_changed_or_short_format_gives_no_rows(body: str):
    assert X.parse_cffex(body, "grid", POLL) == []


def test_cffex_bad_last_modified_leaves_the_time_null():
    assert X.parse_cffex("#last-modified: soon\n" + text("cffex_IF.txt").split("\n", 1)[1], "grid", POLL)[0]["quote_ts"] is None


def test_a50_minute_bars_units_and_stamps():
    rows = X.parse_minutes(fixture("eastmoney_trends_CN00Y.txt").decode("utf-8"), "close", POLL)
    assert len(rows) == 10 and {r["symbol"] for r in rows} == {"em_CN00Y"}
    first = rows[0]
    assert (first["asset"], first["name"], first["host"], first["kind"]) == ("future", "A50期指当月连续", X.EM_HOST, "minute")
    assert first["poll_ts"] == first["quote_ts"] == datetime(2026, 11, 6, 16, 45)        # the overnight session is in the series
    assert (first["open"], first["price"], first["high"], first["low"]) == (12756.0, 12759.0, 12759.0, 12756.0)
    assert (first["prev_close"], first["prev_kind"], first["volume"]) == (12752.0, "settle", 9)
    assert first["amount"] is None and first["bid1"] is None and first["contract"] is None


def test_cnh_minute_bars_have_no_volume():
    rows = X.parse_minutes(fixture("eastmoney_trends_USDCNH.txt").decode("utf-8"), "end", POLL)
    assert {r["symbol"] for r in rows} == {"em_USDCNH"} and {r["kind"] for r in rows} == {"minute"}
    assert (rows[0]["asset"], rows[0]["price"], rows[0]["prev_close"], rows[0]["prev_kind"]) == ("fx", 6.6701, 6.6738, "close")
    assert rows[0]["volume"] is None and rows[0]["poll_ts"] == datetime(2026, 11, 9, 6, 0)


@pytest.mark.parametrize("body", ['{"rc":0,"data":null}', '{"rc":102}', "null", '{"data":{"code":"OTHER","trends":["2026-11-09 09:30,1,1,1,1,1,1,1"]}}'])
def test_minute_reply_without_our_series_gives_no_rows(body: str):
    assert X.parse_minutes(body, "close", POLL) == []


def test_minute_garbage_is_a_parse_error_and_dashes_are_null():
    with pytest.raises(X.ExtParseError):
        X.parse_minutes("<html>", "close", POLL)
    body = '{"data":{"code":"USDCNH","preClose":"-","trends":["bad","2026-11-09 25:00,1,1,1,1,0,0,0","2026-11-09 09:30,-,-,-,-,0,0,0"]}}'
    rows = X.parse_minutes(body, "close", POLL)
    assert len(rows) == 1 and rows[0]["price"] is None and rows[0]["prev_close"] is None


def test_every_row_fits_the_row_schema():
    rows = (X.parse_tencent(tencent_body().decode("gbk"), "grid", POLL) + X.parse_cffex(text("cffex_IC.txt"), "grid", POLL)
            + X.parse_minutes(fixture("eastmoney_trends_CN00Y.txt").decode("utf-8"), "close", POLL))
    frame = pl.DataFrame(rows, schema=X.ROW_SCHEMA)
    assert frame.height == 20 + 4 + 10 and set(frame.columns) == set(X.ROW_SCHEMA)


# --- instruments and request plan -------------------------------------------------------------------------------------------
def test_contract_months_follow_the_exchange_listing():
    assert X.third_friday(2026, 11) == date(2026, 11, 20)
    assert X.contract_months(date(2026, 11, 8)) == ["2611", "2612", "2703", "2706"]
    assert X.contract_months(date(2026, 11, 21)) == ["2612", "2701", "2703", "2706"]
    assert X.contract_months(date(2026, 12, 21)) == ["2701", "2702", "2703", "2706"]


def test_symbol_lists_and_hosts():
    assert len(X.tencent_symbols()) == 20 and len(X.future_symbols(DAY)) == 16 and len(set(X.all_symbols(DAY))) == 38
    assert X.host_of("sh510300") == X.QUOTE_HOST and X.host_of("IF2612") == X.CFFEX_HOST and X.host_of("em_CN00Y") == X.EM_HOST
    assert [X.asset_of(s) for s in ("sh510300", "sh000001", "IF2612", "em_USDCNH", "hf_GC")] == ["etf", "index", "future", "fx", "commodity"]


def test_request_plan_one_batch_four_files_two_series():
    plan = X.request_plan(X.all_symbols(DAY))
    assert {h: len(r) for h, r in plan.items()} == {X.QUOTE_HOST: 1, X.CFFEX_HOST: 4, X.EM_HOST: 2}
    assert plan[X.CFFEX_HOST][0].url == "http://www.cffex.com.cn/quote_IF.txt" and plan[X.CFFEX_HOST][3].url.endswith("quote_IM.txt")
    assert plan[X.EM_HOST][0].url.endswith("secid=104.CN00Y") and plan[X.EM_HOST][1].url.endswith("secid=133.USDCNH")
    assert plan[X.QUOTE_HOST][0].url.startswith("https://qt.gtimg.cn/q=sh510300,") and plan[X.QUOTE_HOST][0].url.endswith("hf_CL")
    assert not any("sina" in r.url for reqs in plan.values() for r in reqs)


# --- registry ----------------------------------------------------------------------------------------------------------------
def test_endpoints_are_registered_under_native_sources():
    for host in X.HOSTS:
        sid, eid = X.HOST_ENDPOINT[host]
        spec = registry.spec(sid, eid)
        assert sid in registry.SOURCES and spec.contract_id == "CanonicalQuote" and spec.policy["dataset"] == "ext_quote_snapshots"
        assert spec.policy["rate_limit"].startswith("<=1 request") and "legacy" not in sid
    assert X.SRC_TENCENT != "legacy_recordings"


# --- the guarded fetcher ------------------------------------------------------------------------------------------------------
class Net:
    """A mock transport that records (monotonic time, host, headers) of every request and answers by host."""

    def __init__(self, answer):
        self.answer, self.log, self.mono = answer, [], [0.0]

    def clock(self) -> float:
        return self.mono[0]

    def sleep(self, s: float) -> None:
        self.mono[0] += s

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.log.append((self.mono[0], req.url.host, dict(req.headers), str(req.url)))
        return self.answer(req)

    def fetcher(self) -> X.ExtFetcher:
        return X.ExtFetcher(transport=httpx.MockTransport(self.handler), clock=self.clock, sleep=self.sleep,
                            now=lambda: datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc))


def ok(req: httpx.Request) -> httpx.Response:
    if req.url.host == X.QUOTE_HOST:
        return httpx.Response(200, content=tencent_body())
    if req.url.host == X.CFFEX_HOST:
        return httpx.Response(200, content=fixture(f"cffex_{str(req.url)[-6:-4]}.txt").split(b"\n", 1)[1], headers={"last-modified": "Mon, 09 Nov 2026 08:16:20 GMT"})
    return httpx.Response(200, content=fixture("eastmoney_trends_CN00Y.txt"))


def test_fetcher_keeps_last_modified_of_the_exchange_file_in_front_of_the_body():
    net = Net(ok)
    f = net.fetcher()
    cap = f.fetch(X.request_plan(X.all_symbols(DAY))[X.CFFEX_HOST][0])
    assert cap.source_id == X.SRC_CFFEX and cap.endpoint_id == X.EP_FILE and cap.request_key == "cffex:IF"
    assert cap.body.startswith(b"#last-modified: Mon, 09 Nov 2026 08:16:20 GMT\ninstrument,")
    rows = X.parse_capture(X.CFFEX_HOST, cap.body, "grid", POLL)
    assert rows[0]["quote_ts"] == datetime(2026, 11, 9, 16, 16, 20)
    tq = f.fetch(X.request_plan(X.all_symbols(DAY))[X.QUOTE_HOST][0])
    assert tq.body == tencent_body()                                                   # byte-exact, nothing prepended


def test_eastmoney_gets_its_referer_and_one_request_per_three_seconds_per_host():
    net = Net(ok)
    f = net.fetcher()
    plan = X.request_plan(X.all_symbols(DAY))
    for req in [*plan[X.EM_HOST], *plan[X.CFFEX_HOST], *plan[X.QUOTE_HOST], *plan[X.QUOTE_HOST]]:
        f.fetch(req)
    em = [e for e in net.log if e[1] == X.EM_HOST]
    assert all(e[2]["referer"] == "https://quote.eastmoney.com/" for e in em)
    assert em[1][0] - em[0][0] >= 3.0 - 1e-9
    cffex = [e[0] for e in net.log if e[1] == X.CFFEX_HOST]
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(cffex, cffex[1:])) and len(cffex) == 4
    tq = [e[0] for e in net.log if e[1] == X.QUOTE_HOST]
    assert tq[1] - tq[0] >= 1.0 - 1e-9


@pytest.mark.parametrize("response,reason", [
    (httpx.Response(403, content=b"denied"), "http_403"),
    (httpx.Response(429, content=b"slow"), "http_429"),
    (httpx.Response(456, content=b""), "http_456"),
    (httpx.Response(501, content=b""), "http_501"),
    (httpx.Response(302, headers={"location": "https://waf.tencent.com/x"}), "redirect_302"),
    (httpx.Response(302, headers={"location": "//push2delay.eastmoney.com/api"}), "redirect_302"),
    (httpx.Response(200, content=b"<html>blocked</html>"), "html_answer"),
    (httpx.Response(200, content=b"x" * 700 + b"https://waf.tencent.com/"), None),     # beyond the first 600 bytes: not a refusal
    (httpx.Response(200, content=b"x" * 100 + b"https://waf.tencent.com/"), "waf_tencent"),
    (httpx.Response(200, content=b"<script>location='https://elsewhere'</script>"), "html_answer"),
])
def test_a_refusal_closes_that_host_and_is_never_retried(response, reason):
    net = Net(lambda req: response)
    f = net.fetcher()
    req = X.request_plan(X.all_symbols(DAY))[X.QUOTE_HOST][0]
    if reason is None:
        assert f.fetch(req).status == 200
        return
    with pytest.raises(SourceBlocked) as err:
        f.fetch(req)
    assert reason in str(err.value) and f.blocked(X.QUOTE_HOST)
    if isinstance(err.value, X.RefusedAnswer):
        assert err.value.capture.url == req.url                                          # the page is kept as evidence
    n = len(net.log)
    with pytest.raises(SourceBlocked):
        f.fetch(req)
    assert len(net.log) == n                                                             # nothing more is sent to that host
    other = X.request_plan(X.all_symbols(DAY))[X.CFFEX_HOST][0]
    assert f.blocked(X.CFFEX_HOST) is None and other.host == X.CFFEX_HOST                # the other hosts are not closed


@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_other_error_statuses_are_plain_failures_not_blocks(status):
    net = Net(lambda req: httpx.Response(status, content=b"<html>x</html>"))
    f = net.fetcher()
    req = X.request_plan(X.all_symbols(DAY))[X.CFFEX_HOST][0]
    with pytest.raises(TransportFailure):
        f.fetch(req)
    assert f.blocked(X.CFFEX_HOST) is None
    with pytest.raises(TransportFailure):                                                # the next poll is the retry
        f.fetch(req)
    assert len(net.log) == 2


# --- frame parity with the legacy import ----------------------------------------------------------------------------------------
def _all_rows() -> list[dict]:
    return (X.parse_tencent(tencent_body().decode("gbk"), "grid", POLL) + X.parse_cffex(text("cffex_IF.txt"), "grid", POLL)
            + X.parse_minutes(fixture("eastmoney_trends_CN00Y.txt").decode("utf-8"), "close", POLL))


def test_native_frame_equals_what_the_legacy_import_reads_from_the_old_file(tmp_path):
    rows = _all_rows()
    day_dir = tmp_path / "intraday_ext" / DAY.isoformat()
    day_dir.mkdir(parents=True)
    pl.DataFrame(rows, schema=X.ROW_SCHEMA).write_parquet(day_dir / "quotes_ext.parquet")      # the old recorder's file shape
    rec = LegacyRecordings(tmp_path)
    (part,) = rec.parts("ext", DAY)
    legacy = rec.read_ext(part, DAY)
    native = X.ext_frame(rows, DAY, part.short)
    assert native.schema == legacy.schema and native.columns == legacy.columns           # columns, order and dtypes
    assert native.equals(legacy)
    assert native.schema == pl.Schema(X.EXT_FRAME_SCHEMA)


def test_frame_times_are_utc_instants_of_the_cst_stamps():
    rows = X.parse_cffex(text("cffex_IF.txt"), "grid", POLL)
    df = X.ext_frame(rows, DAY, "native/part.parquet")
    assert df["poll_at"][0] == datetime(2026, 11, 6, 7, 1, 30, tzinfo=timezone.utc)      # 15:01:30 CST
    assert df["quote_at"][0] == datetime(2026, 11, 9, 8, 16, 20, tzinfo=timezone.utc)    # 16:16:20 CST (Last-Modified)
    assert df["price"].dtype == pl.Decimal(24, 6) and df["volume"].dtype == pl.Float64 and df["amount"].null_count() == df.height


# --- schedule and cadence ---------------------------------------------------------------------------------------------------------
def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 6, h, m, s)


def test_grid_is_one_poll_a_minute_from_0900_to_1600_five_seconds_in():
    clock = R.Clock()
    assert R.due(at(9, 0, 4), clock) == [] and R.due(at(9, 0, 5), clock) == ["grid"]
    clock.last_grid = R.slot(at(9, 0, 5))
    assert R.due(at(9, 0, 40), clock) == [] and R.due(at(9, 1, 5), clock) == ["grid"]
    assert R.due(at(8, 59, 59), clock) == [] and R.due(at(16, 1, 0), R.Clock()) == []
    assert R.due(at(16, 0, 5), R.Clock()) == ["grid"]


def test_close_and_end_steps_run_once_in_their_windows():
    clock = R.Clock(last_grid=R.slot(at(15, 1, 30)))
    assert R.due(at(15, 1, 29), clock) == [] and R.due(at(15, 1, 30), clock) == ["close"]
    clock.done.add("close")
    assert R.due(at(15, 3, 0), clock) == []
    assert R.due(at(15, 4, 1), R.Clock(last_grid=R.slot(at(15, 4, 1)))) == []                  # the window is over: a missed close is not made up
    assert R.due(at(16, 0, 29), R.Clock(last_grid=R.slot(at(16, 0, 29)), done={"close"})) == []
    assert R.due(at(16, 0, 30), R.Clock(last_grid=R.slot(at(16, 0, 30)), done={"close"})) == ["end"]
    assert R.due(at(16, 0, 30), R.Clock(last_grid=R.slot(at(16, 0, 30)), done={"close", "end"})) == []


def test_finished_and_missed_slots():
    assert not R.finished(at(16, 0, 59)) and R.finished(at(16, 1, 0))
    assert R.missed(at(10, 0), at(10, 1)) == 0 and R.missed(at(10, 0), at(10, 4)) == 3
    assert R.missed(None, at(9, 0)) == 0 and R.missed(None, at(9, 31)) == 31


def test_cadence_is_a_parameter_and_cannot_go_below_the_grid():
    assert R.Cadence().every(X.CFFEX_HOST) == 60 and R.Cadence().every(X.QUOTE_HOST) == 60          # the old cadence
    assert R.Cadence(cffex_seconds=300).every(X.CFFEX_HOST) == 300
    with pytest.raises(ValueError):
        R.Cadence(cffex_seconds=30)
    with pytest.raises(ValueError):
        R.Cadence(grid_offset_seconds=60)


def test_cli_parser_accepts_the_documented_commands():
    from pulse.workers import rec_ext

    a = rec_ext._parser().parse_args(["record", "--until", "15:05", "--cffex-seconds", "300"])
    assert a.cmd == "record" and a.until.hour == 15 and a.cffex_seconds == 300 and a.tencent_seconds == 60
    b = rec_ext._parser().parse_args(["once", "--dry"])
    assert b.cmd == "once" and b.dry and b.cffex_seconds == 60
