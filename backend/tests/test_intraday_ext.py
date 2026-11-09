from datetime import date, datetime, time as dtime, timedelta
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import time

import httpx
import polars as pl
import pytest

from backend.app.warehouse import intraday_ext as X
from backend.app.warehouse.tencent import _Pacer

FIXTURES = Path(__file__).parent / "fixtures" / "intraday_ext"
DAY = date(2026, 11, 6)  # the session the fixtures were recorded for
POLL = datetime(2026, 11, 6, 15, 1, 30)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def text(name: str) -> str:
    return fixture(name).decode("gbk")


def tencent_body() -> bytes:
    return fixture("tencent_etf_index.txt") + b"\n" + fixture("tencent_overseas.txt")


@pytest.fixture(autouse=True)
def fast_pacers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(X, "_PACERS", {host: _Pacer(1000.0) for host in X.HOSTS})


def saved_answer(host: str, url: str) -> bytes:
    if host == X.QUOTE_HOST:
        return tencent_body()
    return fixture(f"eastmoney_trends_{url.split('.')[-1]}.txt") if host == X.EM_HOST else fixture(f"cffex_{url[-6:-4]}.txt")


def fake_fetcher(calls: list[str], overrides: dict | None = None):
    """Serves the saved responses; `overrides` maps a host to bytes, or to an exception to raise."""

    def fetch(host: str, url: str) -> bytes:
        calls.append(host)
        answer = (overrides or {}).get(host, saved_answer(host, url))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch


def recorder(tmp_path: Path, calls: list[str] | None = None, overrides: dict | None = None) -> X.Recorder:
    return X.Recorder(DAY, tmp_path, fake_fetcher([] if calls is None else calls, overrides))


def rows_of(symbol: str, rows: list[dict]) -> dict:
    return next(r for r in rows if r["symbol"] == symbol)


# --- parsers on the saved responses ---------------------------------------------------

def test_tencent_etf_and_index_rows():
    rows = X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL)
    assert [r["asset"] for r in rows].count("etf") == 7 and [r["asset"] for r in rows].count("index") == 10
    etf = rows_of("sh510300", rows)
    assert etf["name"] == "沪深300ETF华泰柏瑞" and etf["host"] == X.QUOTE_HOST and etf["kind"] == "grid"
    assert (etf["price"], etf["prev_close"], etf["prev_kind"], etf["open"]) == (4.124, 4.195, "close", 4.113)
    assert (etf["high"], etf["low"], etf["bid1"], etf["ask1"]) == (4.169, 4.095, 4.124, 4.125)
    assert etf["volume"] == 12629370 and etf["amount"] == 5211672667
    assert etf["quote_ts"] == datetime(2026, 11, 6, 16, 14, 52) and etf["poll_ts"] == POLL
    assert etf["contract"] is None and etf["open_interest"] is None


def test_tencent_index_has_null_book_not_zero():
    index = rows_of("sh000001", X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL))
    assert index["name"] == "上证指数" and index["asset"] == "index"
    assert (index["price"], index["prev_close"], index["open"]) == (3649.18, 3689.91, 3612.15)
    assert index["bid1"] is None and index["ask1"] is None
    assert index["amount"] == 772634566231


def test_tencent_skips_unknown_ticker_and_short_lines():
    body = 'v_pv_none_match="1";\nv_sh999999="1~~999999~1~1";\n'
    assert X.parse_tencent(body, "grid", POLL) == []


def test_tencent_overseas_futures_and_commodities():
    rows = X.parse_tencent(text("tencent_overseas.txt"), "grid", POLL)
    assert [r["symbol"] for r in rows] == ["hf_GC", "hf_CL", "hf_HSI"]  # hkHSI is in the answer but not asked for
    gold, oil, hsi = rows
    assert (gold["asset"], gold["name"], gold["host"], gold["contract"]) == ("commodity", "纽约黄金", X.QUOTE_HOST, None)
    assert (gold["price"], gold["high"], gold["low"], gold["open"]) == (4096.16, 4143.3, 4062.5, 4074.3)
    assert (gold["prev_close"], gold["prev_kind"]) == (4074.9, "settle")
    assert gold["quote_ts"] == datetime(2026, 11, 9, 16, 15, 4)
    assert gold["bid1"] is None and gold["ask1"] is None and gold["volume"] is None  # not trusted, see the docstring
    assert (oil["asset"], oil["price"], oil["prev_close"]) == ("commodity", 89.9, 90.83)
    assert (hsi["asset"], hsi["name"], hsi["price"], hsi["quote_ts"]) == (
        "future", "恒生指数期货", 24440.52, datetime(2026, 11, 9, 16, 14, 16))


def test_tencent_skips_a_short_overseas_line():
    assert X.parse_tencent('v_hf_GC="1,2";\nv_hf_CL="";\n', "grid", POLL) == []


def test_cffex_rows_from_the_exchange_file():
    rows = X.parse_cffex(text("cffex_IF.txt"), "grid", POLL)
    assert [r["symbol"] for r in rows] == ["IF2611", "IF2612", "IF2703", "IF2706"]
    row = rows[1]
    assert (row["asset"], row["host"], row["contract"], row["name"]) == ("future", X.CFFEX_HOST, "IF2612", "IF2612")
    assert (row["open"], row["high"], row["low"], row["price"]) == (4037.4, 4063.2, 4022.0, 4023.6)
    assert (row["bid1"], row["ask1"], row["volume"], row["open_interest"]) == (4023.2, 4023.8, 52033, 173775)
    assert row["amount"] is None  # the file has none: null, not a guess
    assert row["quote_ts"] == datetime(2026, 11, 9, 16, 16, 20)  # Last-Modified, in CST


def test_cffex_previous_settlement_is_last_minus_change():
    """Checked against Eastmoney's settlement field for the same contract on the same day (f28 of 8.040120)."""
    settlement = rows_of("IF2611", X.parse_cffex(text("cffex_IF.txt"), "grid", POLL))
    assert (settlement["prev_close"], settlement["prev_kind"]) == (4044.8, "settle")
    em = json.loads(fixture("eastmoney_ulist.txt"))["data"]["diff"]  # the one good ulist answer the server ever got
    assert next(q for q in em if q["f12"] == "040120")["f28"] == settlement["prev_close"]


def test_cffex_main_contract_can_be_read_from_the_open_interest():
    frame = pl.DataFrame(X.parse_cffex(text("cffex_IM.txt"), "grid", POLL), schema=X.SCHEMA)
    assert frame.sort("open_interest")["contract"][-1] == "IM2612"  # the main contract, not the front month IM2611


def test_cffex_zero_fields_before_the_open_are_null_and_there_is_no_settlement():
    body = X.CFFEX_COLUMNS + "\nIF2611,0.00,0.00,0.00,0.00,0.00,0.00,0,0.00,0,0,46000\n"
    row = X.parse_cffex(body, "grid", POLL)[0]
    assert all(row[k] is None for k in ("price", "open", "high", "low", "prev_close", "bid1", "ask1"))
    assert row["volume"] == 0 and row["open_interest"] == 46000 and row["quote_ts"] is None


@pytest.mark.parametrize("body", [
    "", "instrument,price\nIF2611,1\n", X.CFFEX_COLUMNS + "\nIF2611,1,2\n", X.CFFEX_COLUMNS + "\nXX2611,1,1,1,1,1,1,1,1,1,1,1\n",
])
def test_cffex_changed_or_short_format_gives_no_rows(body: str):
    assert X.parse_cffex(body, "grid", POLL) == []


def test_cffex_bad_last_modified_leaves_the_time_null():
    assert X.parse_cffex("#last-modified: soon\n" + text("cffex_IF.txt").split("\n", 1)[1], "grid", POLL)[0]["quote_ts"] is None


def test_minute_bars_of_the_a50():
    rows = X.parse_minutes(fixture("eastmoney_trends_CN00Y.txt").decode("utf-8"), "close", POLL)
    assert len(rows) == 10 and {r["symbol"] for r in rows} == {"em_CN00Y"}
    first = rows[0]
    assert (first["asset"], first["name"], first["host"], first["kind"]) == ("future", "A50期指当月连续", X.EM_HOST, "minute")
    assert first["poll_ts"] == first["quote_ts"] == datetime(2026, 11, 6, 16, 45)  # the overnight session is in the series
    assert (first["open"], first["price"], first["high"], first["low"]) == (12756.0, 12759.0, 12759.0, 12756.0)
    assert (first["prev_close"], first["prev_kind"], first["volume"]) == (12752.0, "settle", 9)
    assert first["amount"] is None and first["bid1"] is None and first["contract"] is None


def test_minute_bars_of_offshore_yuan():
    rows = X.parse_minutes(fixture("eastmoney_trends_USDCNH.txt").decode("utf-8"), "end", POLL)
    assert {r["symbol"] for r in rows} == {"em_USDCNH"} and {r["kind"] for r in rows} == {"minute"}
    assert (rows[0]["asset"], rows[0]["price"], rows[0]["prev_close"], rows[0]["prev_kind"]) == ("fx", 6.6701, 6.6738, "close")
    assert rows[0]["volume"] is None and rows[0]["poll_ts"] == datetime(2026, 11, 9, 6, 0)


@pytest.mark.parametrize("body", ['{"rc":0,"data":null}', '{"rc":102}', "null", '{"data":{"code":"OTHER","trends":["2026-11-09 09:30,1,1,1,1,1,1,1"]}}'])
def test_minute_reply_without_our_series_gives_no_rows(body: str):
    assert X.parse_minutes(body, "close", POLL) == []


def test_minute_bad_lines_are_skipped_and_dashes_are_null():
    body = '{"data":{"code":"USDCNH","preClose":"-","trends":["bad","2026-11-09 25:00,1,1,1,1,0,0,0","2026-11-09 09:30,-,-,-,-,0,0,0"]}}'
    rows = X.parse_minutes(body, "close", POLL)
    assert len(rows) == 1 and rows[0]["price"] is None and rows[0]["prev_close"] is None


def test_every_row_fits_the_schema():
    rows = (X.parse_tencent(tencent_body().decode("gbk"), "grid", POLL) + X.parse_cffex(text("cffex_IC.txt"), "grid", POLL)
            + X.parse_minutes(fixture("eastmoney_trends_CN00Y.txt").decode("utf-8"), "close", POLL))
    frame = pl.DataFrame(rows, schema=X.SCHEMA)
    assert frame.height == 20 + 4 + 10 and set(frame.columns) == set(X.SCHEMA)


# --- instruments ----------------------------------------------------------------------

def test_contract_months_follow_the_exchange_listing():
    assert X.third_friday(2026, 11) == date(2026, 11, 20)
    assert X.contract_months(date(2026, 11, 8)) == ["2611", "2612", "2703", "2706"]
    assert X.contract_months(date(2026, 11, 20)) == ["2611", "2612", "2703", "2706"]  # expiry day still trades
    assert X.contract_months(date(2026, 11, 21)) == ["2612", "2701", "2703", "2706"]  # expired month dropped
    assert X.contract_months(date(2026, 12, 21)) == ["2701", "2702", "2703", "2706"]  # year rolls over
    assert X.contract_months(date(2026, 10, 5)) == ["2610", "2611", "2612", "2703"]


def test_symbol_lists():
    tencent, futures = X.tencent_symbols(), X.future_symbols(DAY)
    assert len(tencent) == 20 and len(futures) == 16 and len(set(tencent + futures + list(X.EM_SYMBOLS))) == 38
    assert futures[:5] == ["IF2611", "IF2612", "IF2703", "IF2706", "IH2611"]
    assert {X.host_of(s) for s in tencent} == {X.QUOTE_HOST} and {X.host_of(s) for s in futures} == {X.CFFEX_HOST}
    assert {X.host_of(s) for s in X.EM_SYMBOLS} == {X.EM_HOST}
    assert [X.asset_of(s) for s in ("sz159919", "sz399006", "IM2612", "hf_GC", "hf_HSI", "em_USDCNH", "em_CN00Y")] == [
        "etf", "index", "future", "commodity", "future", "fx", "future"]


def test_the_requests_of_each_source_and_sina_is_gone():
    urls = [X.urls_for(X.QUOTE_HOST, X.tencent_symbols()), X.urls_for(X.CFFEX_HOST, X.future_symbols(DAY)),
            X.urls_for(X.EM_HOST, list(X.EM_SYMBOLS))]
    assert [len(u) for u in urls] == [1, 4, 2]
    assert urls[1][0] == "http://www.cffex.com.cn/quote_IF.txt" and urls[1][3].endswith("quote_IM.txt")
    assert urls[2][0].endswith("secid=104.CN00Y") and urls[2][1].endswith("secid=133.USDCNH")
    assert urls[2][0].startswith("https://push2his.eastmoney.com/")
    assert not any("sina" in u for group in urls for u in group)


# --- http -----------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,location,expected", [
    (200, b'v_sh000001="1~x";', "", False),
    (200, b"", "", False),
    (403, b"denied", "", True),
    (429, b"slow down", "", True),
    (501, b"", "", True),
    (302, b"", "https://waf.tencent.com/x", True),
    (200, b"<html>blocked</html>", "", True),
    (200, b"x" * 100 + b"https://waf.tencent.com/", "", True),
    (500, b"oops", "", False),
    (502, b"<html><title>502 Bad Gateway</title></html>", "", False),  # seen from push2delay: the next poll retries
    (503, b"<html>busy</html>", "", False),
    (404, b"<html>not found</html>", "", False),  # one missing file must not stop the host for the day
    (302, b"", "//push2delay.eastmoney.com/api", True),
])
def test_looks_blocked(status: int, body: bytes, location: str, expected: bool):
    assert X.looks_blocked(status, body, location) is expected


def fetcher_on(handler) -> X.HttpFetcher:
    return X.HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))


def test_http_fetcher_sends_the_eastmoney_referer_and_returns_bytes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = dict(request.headers)
        return httpx.Response(200, content=b'{"data":null}')

    fetch = fetcher_on(handler)
    assert fetch(X.EM_HOST, X.EM_URL + "104.CN00Y") == b'{"data":null}'
    fetch(X.QUOTE_HOST, X.QUOTE_URL + "sh000001")
    assert seen[X.EM_HOST]["referer"] == "https://quote.eastmoney.com/" and "referer" not in seen[X.QUOTE_HOST]


def test_http_fetcher_keeps_the_time_of_the_exchange_file():
    stamped = httpx.Response(200, content=b"body\n", headers={"last-modified": "Mon, 09 Nov 2026 08:16:20 GMT"})
    fetch = fetcher_on(lambda request: stamped)
    assert fetch(X.CFFEX_HOST, X.CFFEX_URL.format("IF")) == b"#last-modified: Mon, 09 Nov 2026 08:16:20 GMT\nbody\n"
    assert fetch(X.QUOTE_HOST, X.QUOTE_URL + "sh000001") == b"body\n"  # only there: the others carry their own times
    assert fetcher_on(lambda request: httpx.Response(200, content=b"body\n"))(X.CFFEX_HOST, X.CFFEX_URL.format("IF")) == b"body\n"


@pytest.mark.parametrize("response", [
    httpx.Response(403, content=b"no"), httpx.Response(429, content=b"no"), httpx.Response(501, content=b""),
    httpx.Response(302, headers={"location": "https://waf.tencent.com/block"}),
    httpx.Response(200, content=b"<!doctype html><title>blocked</title>"),
])
def test_http_fetcher_raises_blocked_with_the_evidence(response: httpx.Response):
    with pytest.raises(X.Blocked) as caught:
        fetcher_on(lambda request: response)(X.QUOTE_HOST, X.QUOTE_URL + "sh000001")
    assert caught.value.status == response.status_code


def test_http_fetcher_other_bad_status_is_a_plain_failure():
    with pytest.raises(X.HostError):
        fetcher_on(lambda request: httpx.Response(500, content=b"oops"))(X.QUOTE_HOST, X.QUOTE_URL + "sh000001")
    with pytest.raises(X.HostError):
        fetcher_on(lambda request: httpx.Response(502, content=b"<html>bad gateway</html>"))(X.EM_HOST, X.EM_URL)


# --- schedule -------------------------------------------------------------------------

def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 9, h, m, s)


def test_grid_has_one_poll_a_minute_from_0900_to_1600():
    slots = X.grid_slots(date(2026, 11, 9))
    assert len(slots) == 421 and slots[0] == at(9, 0, 5) and slots[-1] == at(16, 0, 5)
    assert all(X.due(t, X.Clock()) == ["grid"] or X.due(t, X.Clock()) == ["grid", "close"] for t in slots)


def test_nothing_is_due_outside_the_window_or_before_the_offset():
    clock = X.Clock()
    assert X.due(at(8, 59, 59), clock) == [] and X.due(at(9, 0, 4), clock) == []
    assert X.due(at(16, 1, 0), clock) == [] and X.due(at(20, 0, 0), clock) == []


def test_a_minute_is_polled_once():
    clock = X.Clock(last_grid=at(10, 30))
    assert X.due(at(10, 30, 20), clock) == [] and X.due(at(10, 31, 5), clock) == ["grid"]


def test_closing_snapshot_window_and_done():
    clock = X.Clock(last_grid=at(15, 1))
    assert X.due(at(15, 1, 29), clock) == []
    assert X.due(at(15, 1, 30), clock) == ["close"] and "close" in X.due(at(15, 3, 59), clock)
    assert "close" not in X.due(at(15, 4, 1), clock)
    clock.done.add("close")
    assert X.due(at(15, 1, 40), clock) == []


def test_the_minute_bars_are_fetched_once_more_after_the_last_grid_poll():
    clock = X.Clock(last_grid=at(16, 0))
    assert X.due(at(16, 0, 29), clock) == [] and X.due(at(16, 0, 30), clock) == ["end"]
    clock.done.add("end")
    assert X.due(at(16, 0, 40), clock) == [] and X.due(at(16, 1, 0), X.Clock()) == []


def test_the_clock_is_naive_cst():
    """The crash of 2026-11-09: an aware clock against a naive window start."""
    now = X._now()
    assert now.tzinfo is None and abs((now - datetime.now(X.CST).replace(tzinfo=None)).total_seconds()) < 5


def test_the_run_ends_after_the_16_00_poll_or_at_the_hard_stop():
    assert not X.finished(at(16, 0, 59)) and X.finished(at(16, 1, 0))
    alive, low = SimpleNamespace(disk_low=False), SimpleNamespace(disk_low=True)
    assert X._end_reason(alive, at(12, 0)) is None and X._end_reason(alive, at(16, 1)) == "done"
    assert X._end_reason(alive, at(16, 10)) == "stop time" and X._end_reason(low, at(12, 0)) == "disk low"


def test_missed_slots():
    assert X.missed(None, at(9, 0)) == 0 and X.missed(None, at(9, 5)) == 5  # a late start counts what it missed
    assert X.missed(at(10, 0), at(10, 1)) == 0 and X.missed(at(10, 0), at(10, 4)) == 3
    assert X.missed(at(10, 0), at(10, 0)) == 0


def test_advance_counts_missed_slots_and_remembers_the_close(tmp_path: Path):
    rec, clock = recorder(tmp_path), X.Clock()
    skipped = {at(10, 0, 5), at(10, 1, 5), at(13, 30, 5)}
    for t in X.grid_slots(date(2026, 11, 9)):
        if t not in skipped and "grid" in X.due(t, clock):
            X._advance(clock, rec, "grid", t)
    assert rec.stats["missed"] == 3
    X._advance(clock, rec, "close", at(15, 1, 30))
    assert json.loads((rec.dir / "done.json").read_text(encoding="utf-8")) == ["close"]


# --- the whole run on a fake clock ------------------------------------------------------

@pytest.fixture
def fake_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`run()` with a fake wall clock and the saved responses; 2026-11-09 was lost to a crash only this path has."""
    state: dict = {}
    calls: list[str] = []
    monkeypatch.setenv("PULSE_INTRADAY_EXT_DIR", str(tmp_path))
    monkeypatch.setattr(X, "_now", lambda: state["t"])
    monkeypatch.setattr(X, "_tick", lambda: state.update(t=state["t"] + timedelta(seconds=1)))
    monkeypatch.setattr(X, "load_calendar", lambda: SimpleNamespace(is_trading_day=lambda d: True))
    monkeypatch.setattr(X, "HttpFetcher", lambda client: fake_fetcher(calls))
    root = logging.getLogger()
    handlers = list(root.handlers)

    def go(start: datetime) -> dict:
        state["t"] = start
        assert X.run() == 0
        return json.loads((tmp_path / start.date().isoformat() / "summary.json").read_text(encoding="utf-8"))

    yield go
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()


def test_run_records_a_whole_day(fake_run, tmp_path: Path):
    summary = fake_run(datetime(2026, 11, 6, 8, 58, 1))
    assert summary["end"] == "done" and summary["polls"] == {"grid": 421, "close": 1, "end": 1}
    assert summary["missed"] == 0 and summary["error_count"] == 0 and summary["blocked"] == {}
    assert summary["never_returned"] == [] and summary["restarted"] is False
    frame = pl.read_parquet(tmp_path / "2026-11-06" / "quotes_ext.parquet")
    assert frame.filter(pl.col("kind") != "minute").height == 422 * 36
    assert frame.filter(pl.col("kind") == "minute").height == 20  # two fetches of the same bars merge into one series
    quotes = frame.filter(pl.col("kind") == "grid")
    assert quotes["poll_ts"].min() == datetime(2026, 11, 6, 9, 0, 5)
    assert quotes["poll_ts"].dt.time().max() < dtime(16, 1)
    assert frame.filter(pl.col("kind") == "close")["poll_ts"].dt.time().unique().to_list() == [dtime(15, 1, 30)]


def test_run_writes_an_honest_summary_when_the_loop_dies(fake_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(rec: X.Recorder, clock: X.Clock) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(X, "_loop", boom)
    with pytest.raises(RuntimeError):
        fake_run(datetime(2026, 11, 6, 8, 58, 1))
    assert json.loads((tmp_path / "2026-11-06" / "summary.json").read_text(encoding="utf-8"))["end"] == "crash"


def test_a_bookkeeping_bug_does_not_cost_the_polls(fake_run, monkeypatch: pytest.MonkeyPatch):
    def boom(previous: datetime | None, current: datetime) -> int:
        raise TypeError("bug")

    monkeypatch.setattr(X, "missed", boom)
    summary = fake_run(datetime(2026, 11, 6, 8, 58, 1))
    assert summary["end"] == "done" and summary["polls"] == {"grid": 421, "close": 1, "end": 1}
    assert summary["error_count"] == 421 and "advance grid" in summary["errors"][0]


def test_tencent_down_at_0931_does_not_end_the_day(fake_run, monkeypatch: pytest.MonkeyPatch):
    def fetch(host: str, url: str) -> bytes:
        if host == X.QUOTE_HOST and X._now().time() < dtime(9, 40):
            raise X.HostError("HTTP 500")
        return saved_answer(host, url)

    monkeypatch.setattr(X, "HttpFetcher", lambda client: fetch)
    summary = fake_run(datetime(2026, 11, 6, 9, 25, 0))
    assert summary["end"] == "done" and summary["failed_requests"] == 15


def stale_until(limit: dtime):
    """Tencent answers yesterday's quotes until `limit`, as a stale cache would, then today's."""
    today = fixture("tencent_etf_index.txt").decode("gbk")

    def fetch(host: str, url: str) -> bytes:
        if host == X.QUOTE_HOST and X._now().time() < limit:
            return today.replace("20261106", "20261105").encode("gbk")
        return saved_answer(host, url)

    return fetch


def test_one_stale_answer_at_0931_does_not_end_the_day(fake_run, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(X, "HttpFetcher", lambda client: stale_until(dtime(9, 32)))
    assert fake_run(datetime(2026, 11, 6, 9, 25, 0))["end"] == "done"


def test_quotes_without_a_usable_stamp_give_no_verdict(fake_run, monkeypatch: pytest.MonkeyPatch):
    body = fixture("tencent_etf_index.txt").decode("gbk").replace("20261106", "notatime")

    def fetch(host: str, url: str) -> bytes:
        return body.encode("gbk") if host == X.QUOTE_HOST else saved_answer(host, url)

    monkeypatch.setattr(X, "HttpFetcher", lambda client: fetch)
    assert fake_run(datetime(2026, 11, 6, 9, 25, 0))["end"] == "done"


def test_run_ends_the_day_on_stale_quotes_even_with_fresh_overseas_stamps(fake_run):
    summary = fake_run(datetime(2026, 11, 7, 9, 30, 0))  # the fixture's hf_ lines say 11-09, its A-share lines 11-06
    assert summary["end"] == "quotes not stamped today: not a trading day"


def test_a_start_after_the_window_does_not_claim_the_day_is_done(fake_run):
    summary = fake_run(datetime(2026, 11, 6, 16, 5, 0))
    assert summary["end"] == "nothing recorded: started after the window" and summary["polls"] == {}


def test_a_restart_says_so_and_the_day_stays_in_one_file(fake_run, tmp_path: Path):
    day_dir = tmp_path / "2026-11-06"
    day_dir.mkdir()
    (day_dir / "done.json").write_text('["close"]', encoding="utf-8")
    summary = fake_run(datetime(2026, 11, 6, 15, 59, 0))
    assert summary["restarted"] is True and summary["polls"] == {"grid": 2, "end": 1}  # no second close snapshot


def test_run_started_late_counts_the_minutes_it_missed(fake_run):
    summary = fake_run(datetime(2026, 11, 6, 10, 0, 0))
    assert summary["end"] == "done" and summary["missed"] == 60 and summary["polls"]["grid"] == 421 - 60


def test_run_ends_after_five_polls_when_the_quotes_are_not_stamped_today(fake_run):
    summary = fake_run(datetime(2026, 11, 9, 8, 58, 1))  # the saved quotes carry 2026-11-06
    assert summary["end"] == "quotes not stamped today: not a trading day" and summary["polls"] == {"grid": 36}


# --- recorder -------------------------------------------------------------------------

def test_grid_poll_makes_five_requests_and_the_close_adds_the_minute_bars(tmp_path: Path):
    calls: list[str] = []
    rec = recorder(tmp_path, calls)
    rows = rec.poll("grid", POLL)
    assert sorted(calls) == sorted([X.QUOTE_HOST, *[X.CFFEX_HOST] * 4])
    assert len(rows) == 36
    assert rec.stats["polls"] == {"grid": 1}
    assert rec.stats["rows_by_host"] == {X.QUOTE_HOST: 20, X.CFFEX_HOST: 16}
    calls.clear()
    assert len(rec.poll("close", POLL)) == 36 + 20
    assert calls.count(X.EM_HOST) == 2 and len(calls) == 7
    assert rec.seen == set(rec.symbols)
    calls.clear()
    assert len(rec.poll("end", POLL)) == 20
    assert calls == [X.EM_HOST, X.EM_HOST]  # the last fetch asks nothing else


def test_waf_answer_stops_that_host_for_the_day_and_the_others_go_on(tmp_path: Path):
    calls: list[str] = []
    block = X.Blocked("qt.gtimg.cn refused: HTTP 501", 501, b"<html>waf.tencent.com</html>")
    rec = recorder(tmp_path, calls, {X.QUOTE_HOST: block})
    first = rec.poll("close", POLL)
    assert X.QUOTE_HOST in rec.blocked and X.EM_HOST not in rec.blocked
    assert {r["host"] for r in first} == {X.CFFEX_HOST, X.EM_HOST}
    calls.clear()
    rec.poll("close", POLL)
    assert X.QUOTE_HOST not in calls and len(calls) == 6  # no request to the blocked host, no retry
    raw = X.raw_polls(rec.dir / "raw" / f"{X.QUOTE_HOST}.txt.gz")
    assert len(raw) == 1 and raw[0][2] == 501 and "waf.tencent.com" in raw[0][3]  # the refusal is kept as evidence


def test_a_refusal_stops_the_rest_of_that_hosts_queue_in_the_same_poll(tmp_path: Path):
    calls: list[str] = []
    block = X.Blocked("www.cffex.com.cn refused: HTTP 403", 403, b"no")
    rec = recorder(tmp_path, calls, {X.CFFEX_HOST: block})
    rec.poll("grid", POLL)
    assert calls.count(X.CFFEX_HOST) == 1 and X.CFFEX_HOST in rec.blocked  # requests of a host run one after the other


def failing(host: str, plan):
    """A fetcher whose answers for `host` follow plan(n), n counting that host's requests from 1."""
    calls = {"n": 0, "all": []}

    def fetch(h: str, url: str) -> bytes:
        calls["all"].append(h)
        if h == host:
            calls["n"] += 1
            if plan(calls["n"]):
                raise X.HostError("HTTP 500")
        return saved_answer(h, url)

    return fetch, calls


def test_twenty_failures_in_a_row_stop_a_host_but_a_success_resets_the_count(tmp_path: Path):
    fetch, calls = failing(X.QUOTE_HOST, lambda n: n != 20)  # fails 19 times, answers once, fails again
    rec = X.Recorder(DAY, tmp_path, fetch)
    for _ in range(25):
        rec.poll("grid", POLL)
    assert rec.fails[X.QUOTE_HOST] == 5 and X.QUOTE_HOST not in rec.blocked  # the 20th poll reset the count
    for _ in range(15):
        rec.poll("grid", POLL)
    assert X.QUOTE_HOST in rec.blocked and "20 failures" in rec.blocked[X.QUOTE_HOST]
    before = calls["all"].count(X.QUOTE_HOST)
    rec.poll("grid", POLL)
    assert calls["all"].count(X.QUOTE_HOST) == before and rec.stats["failed_requests"] == 39
    assert rec.stats["error_count"] == 39 and len(rec.stats["errors"]) <= X.MAX_ERRORS


def test_a_host_with_four_requests_also_gets_twenty_polls(tmp_path: Path):
    fetch, _ = failing(X.CFFEX_HOST, lambda n: True)
    rec = X.Recorder(DAY, tmp_path, fetch)
    for _ in range(19):
        rec.poll("grid", POLL)
    assert X.CFFEX_HOST not in rec.blocked and rec.fails[X.CFFEX_HOST] == 76
    rec.poll("grid", POLL)
    assert X.CFFEX_HOST in rec.blocked and "80 failures" in rec.blocked[X.CFFEX_HOST]


@pytest.mark.parametrize("host,garbage", [
    (X.CFFEX_HOST, b"garbage that is not a quote"),
    (X.EM_HOST, b"not json"),
    (X.EM_HOST, b'{"data":[1,2]}'),  # the wrong shape
])
def test_an_unparsable_answer_counts_as_a_failure_not_a_crash(tmp_path: Path, host: str, garbage: bytes):
    rec = recorder(tmp_path, overrides={host: garbage})
    rows = rec.poll("close", POLL)
    assert rows and host not in {r["host"] for r in rows} and rec.fails[host] >= 1 and rec.stats["error_count"] >= 1


def shut(rec: X.Recorder) -> None:
    for pool in rec.pools.values():
        pool.shutdown(wait=True, cancel_futures=True)


def test_a_slow_host_cannot_hold_the_grid_or_take_the_others_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(X, "POLL_BUDGET", 0.2)

    def fetch(host: str, url: str) -> bytes:
        if host == X.CFFEX_HOST:
            time.sleep(0.6)
        return saved_answer(host, url)

    rec = X.Recorder(DAY, tmp_path, fetch)
    start = time.monotonic()
    for _ in range(3):
        rows = rec.poll("grid", POLL)
        assert {r["host"] for r in rows} == {X.QUOTE_HOST}  # the stuck host does not starve Tencent
    assert time.monotonic() - start < 1.5
    assert rec.stats["truncated"] == {X.CFFEX_HOST: 12} and rec.fails[X.CFFEX_HOST] == 12
    shut(rec)


def test_a_host_that_never_answers_in_time_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(X, "POLL_BUDGET", 0.01)

    def fetch(host: str, url: str) -> bytes:
        if host == X.QUOTE_HOST:
            time.sleep(0.05)
        return saved_answer(host, url)

    rec = X.Recorder(DAY, tmp_path, fetch)
    for _ in range(X.MAX_FAILS_IN_ROW):
        rec.poll("grid", POLL)
    assert X.QUOTE_HOST in rec.blocked and "over the poll budget" in rec.blocked[X.QUOTE_HOST]
    shut(rec)


def test_a_locked_raw_file_does_not_cost_the_poll_its_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def locked(*args, **kwargs):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(X.gzip, "open", locked)
    rec = recorder(tmp_path)
    assert len(rec.poll("grid", POLL)) == 36 and len(rec.buffer) == 36 and "raw" in rec.stats["errors"][0]


def test_trading_day_check_reads_the_quote_stamps(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", POLL)
    assert rec.trading_today() is True
    stale = X.Recorder(date(2026, 11, 9), tmp_path, fake_fetcher([]))  # the feed still shows Friday
    stale.poll("grid", datetime(2026, 11, 9, 9, 31, 5))
    assert stale.trading_today() is False
    assert X.Recorder(DAY, tmp_path / "empty", fake_fetcher([])).trading_today() is None  # nothing polled yet
    down = recorder(tmp_path / "down", overrides={X.QUOTE_HOST: X.HostError("HTTP 500")})
    assert down.poll("grid", POLL) and down.trading_today() is None  # no A-share quotes in this poll: no verdict


def test_disk_guard_stops_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    rec = recorder(tmp_path, calls)
    monkeypatch.setattr(rec, "_free_gb", lambda: X.MIN_FREE_GB - 0.1)
    assert rec.poll("grid", POLL) == [] and calls == [] and rec.disk_low
    assert X._end_reason(rec, at(10, 0)) == "disk low"


def test_part_files_merge_into_one_sorted_file_and_the_summary_counts(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.poll("grid", datetime(2026, 11, 6, 9, 1, 5))
    rec.flush(force=True)
    assert len(list((rec.dir / "parts").glob("part-*.parquet"))) == 1 and rec.buffer == []
    rec.poll("close", datetime(2026, 11, 6, 15, 1, 30))
    rec.stats["missed"] = 2
    summary = rec.finish("done")
    merged = pl.read_parquet(rec.dir / "quotes_ext.parquet")
    assert merged.height == 3 * 36 + 20 and merged.schema == pl.Schema(X.SCHEMA)
    assert merged.select("symbol", "poll_ts", "kind").equals(merged.select("symbol", "poll_ts", "kind").sort("symbol", "poll_ts", "kind"))
    assert merged["kind"].value_counts().sort("kind").rows() == [("close", 36), ("grid", 72), ("minute", 20)]
    assert list((rec.dir / "parts").glob("part-*.parquet")) == []
    stored = json.loads((rec.dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["end"] == "done" and stored["polls"] == {"grid": 2, "close": 1}
    assert stored["rows"] == {"grid": 72, "close": 56} and stored["missed"] == 2 and stored["blocked"] == {}
    assert stored["symbols"] == 38 and stored["never_returned"] == []
    assert summary["error_count"] == 0


def test_finish_twice_and_a_restart_keep_every_row(tmp_path: Path):
    first = recorder(tmp_path)
    first.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    first.finish("stop time")
    assert first.finish("again") == {}
    second = recorder(tmp_path)  # a restart: same day directory, the merged file already exists
    second.poll("grid", datetime(2026, 11, 6, 9, 1, 5))
    second.finish("done")
    assert pl.read_parquet(first.dir / "quotes_ext.parquet").height == 2 * 36
    assert X.compact(first.dir) == 2 * 36  # nothing left to merge


def test_a_crash_keeps_the_flushed_parts_and_compact_recovers_them(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.flush(force=True)
    rec.poll("grid", datetime(2026, 11, 6, 9, 1, 5))  # in memory only: lost with the process
    assert not (rec.dir / "quotes_ext.parquet").exists()
    assert X.compact(rec.dir) == 36 and pl.read_parquet(rec.dir / "quotes_ext.parquet").height == 36


def test_compact_merges_a_file_written_before_a_new_column(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.flush(force=True)
    pl.read_parquet(next((rec.dir / "parts").glob("part-*.parquet"))).drop("open_interest").write_parquet(rec.dir / "quotes_ext.parquet")
    rec.poll("grid", datetime(2026, 11, 6, 9, 1, 5))
    rec.flush(force=True)
    assert X.compact(rec.dir) == 2 * 36 and "open_interest" in pl.read_parquet(rec.dir / "quotes_ext.parquet").columns


def test_a_smoke_test_leaves_the_days_summary_alone(tmp_path: Path):
    rec = recorder(tmp_path)
    (rec.dir / "summary.json").write_text('{"end": "done"}', encoding="utf-8")
    rec.poll("once", POLL)
    rec.finish("once")
    assert json.loads((rec.dir / "summary.json").read_text(encoding="utf-8")) == {"end": "done"}
    assert (rec.dir / "summary_once.json").exists()


def test_raw_file_keeps_every_response_as_received(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.poll("close", datetime(2026, 11, 6, 15, 1, 30))
    polls = X.raw_polls(rec.dir / "raw" / f"{X.QUOTE_HOST}.txt.gz")
    assert [(p[0], p[1], p[2]) for p in polls] == [("grid", "09:00:05", 200), ("close", "15:01:30", 200)]
    assert polls[0][3].strip() == tencent_body().decode("gbk").strip()
    assert len(X.raw_polls(rec.dir / "raw" / f"{X.CFFEX_HOST}.txt.gz")) == 8  # four files per poll, each with its time
    em = X.raw_polls(rec.dir / "raw" / f"{X.EM_HOST}.txt.gz", "utf-8")
    assert len(em) == 2 and "A50期指当月连续" in em[0][3]  # only the close fetched the minute bars


def test_output_root_follows_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PULSE_INTRADAY_EXT_DIR", str(tmp_path))
    assert X.output_root() == tmp_path
    monkeypatch.delenv("PULSE_INTRADAY_EXT_DIR")
    assert X.output_root().name == "intraday_ext"
