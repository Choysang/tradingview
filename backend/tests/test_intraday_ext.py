from datetime import date, datetime
import json
from pathlib import Path
import re
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


def sina_body() -> bytes:
    """What Sina would answer for the configured symbols: the saved lines minus the expired IF2609 quote."""
    dated = b"\n".join(l for l in fixture("sina_dated.txt").splitlines() if b"nf_IF2609" not in l)
    return fixture("sina_main_overseas.txt") + b"\n" + dated


@pytest.fixture(autouse=True)
def fast_pacers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(X, "_PACERS", {host: _Pacer(1000.0) for host in X.HOSTS})


def fake_fetcher(calls: list[str], overrides: dict | None = None):
    """Serves the saved responses; `overrides` maps a host to bytes, or to an exception to raise."""
    answers = {X.QUOTE_HOST: fixture("tencent_etf_index.txt"), X.SINA_HOST: sina_body(), **(overrides or {})}

    def fetch(host: str, url: str) -> bytes:
        calls.append(host)
        answer = answers[host]
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
    assert etf["contract"] is None


def test_tencent_index_has_null_book_not_zero():
    index = rows_of("sh000001", X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL))
    assert index["name"] == "上证指数" and index["asset"] == "index"
    assert (index["price"], index["prev_close"], index["open"]) == (3649.18, 3689.91, 3612.15)
    assert index["bid1"] is None and index["ask1"] is None
    assert index["amount"] == 772634566231


def test_tencent_skips_unknown_ticker_and_short_lines():
    body = 'v_pv_none_match="1";\nv_sh999999="1~~999999~1~1";\n'
    assert X.parse_tencent(body, "grid", POLL) == []


def test_nf_dated_contract_row():
    rows = X.parse_sina(text("sina_dated.txt"), "grid", POLL)
    row = rows_of("nf_IF2612", rows)
    assert row["asset"] == "future" and row["contract"] == "IF2612" and row["name"] == "沪深300指数期货2612"
    assert (row["open"], row["high"], row["low"], row["price"]) == (4015.0, 4049.0, 4000.0, 4018.4)
    assert (row["prev_close"], row["prev_kind"]) == (4080.2, "settle")
    assert (row["bid1"], row["ask1"], row["volume"]) == (4018.0, 4018.6, 73520)
    assert row["quote_ts"] == datetime(2026, 11, 6, 15, 0, 0)
    assert row["amount"] == pytest.approx(296140994.2 * 300)  # sum(price x volume) times the 300 yuan multiplier
    assert rows_of("nf_IM2611", rows)["amount"] == pytest.approx(363399326.6 * 200)


def test_futures_prev_close_is_the_settlement_the_limits_are_built_on():
    """The exchange's +-10% limits are 1.1 / 0.9 times the previous settlement: field 14, not field 13."""
    body = text("sina_main_overseas.txt") + text("sina_dated.txt")
    rows = {r["symbol"]: r for r in X.parse_sina(body, "grid", POLL)}
    for match in re.finditer(r'hq_str_(nf_I[A-Z]\d+)="([^"]*)"', body):
        symbol, fields = match.group(1), match.group(2).split(",")
        limit_up, limit_down = float(fields[9]), float(fields[10])
        if symbol.endswith("2609"):
            continue  # expired contract, stale quote
        prev = rows[symbol]["prev_close"]
        assert limit_up == pytest.approx(prev * 1.1, abs=0.5), symbol
        assert limit_down == pytest.approx(prev * 0.9, abs=0.5), symbol


def test_continuous_series_names_the_dated_contract_behind_it():
    body = text("sina_main_overseas.txt") + text("sina_dated.txt")
    rows = X.with_main_contracts(X.parse_sina(body, "grid", POLL))
    assert rows_of("nf_IF0", rows)["contract"] == "IF2612"  # the main contract, not the front month IF2611
    assert rows_of("nf_IF2611", rows)["contract"] == "IF2611"
    assert rows_of("nf_IH0", rows)["contract"] is None  # IH2612 is not in the fixture: no match, no guess


def test_continuous_series_ambiguous_match_stays_null():
    rows = X.parse_sina(text("sina_main_overseas.txt") + text("sina_dated.txt"), "grid", POLL)
    twin = {**rows_of("nf_IF2612", rows), "symbol": "nf_IF2703", "contract": "IF2703"}
    resolved = X.with_main_contracts([*rows, twin])
    assert rows_of("nf_IF0", resolved)["contract"] is None


def test_overseas_futures_and_commodities():
    rows = X.parse_sina(text("sina_main_overseas.txt"), "grid", POLL)
    a50 = rows_of("hf_CHA50CFD", rows)
    assert (a50["asset"], a50["name"], a50["contract"]) == ("future", "富时中国A50期货", None)
    assert (a50["price"], a50["high"], a50["low"], a50["open"]) == (12800.64, 12831.0, 12737.0, 12756.0)
    assert (a50["prev_close"], a50["prev_kind"]) == (12752.0, "settle")
    assert a50["quote_ts"] == datetime(2026, 11, 7, 5, 14, 59)
    assert a50["bid1"] is None and a50["ask1"] is None and a50["volume"] is None  # not trusted, see the docstring
    gold, oil, hsi = (rows_of(s, rows) for s in ("hf_GC", "hf_CL", "hf_HSI"))
    assert (gold["asset"], gold["name"], gold["price"], gold["prev_close"]) == ("commodity", "纽约黄金", 4083.17, 4113.5)
    assert (oil["asset"], oil["price"]) == ("commodity", 90.238)
    assert (hsi["asset"], hsi["quote_ts"]) == ("future", datetime(2026, 11, 7, 2, 37, 21))


def test_usdcnh_row_derives_the_previous_close():
    fx = rows_of("fx_susdcnh", X.parse_sina(text("sina_main_overseas.txt"), "grid", POLL))
    assert (fx["asset"], fx["price"], fx["bid1"], fx["ask1"]) == ("fx", 6.6736, 6.6736, 6.6739)
    assert (fx["open"], fx["high"], fx["low"]) == (6.665, 6.6748, 6.661)
    assert fx["prev_close"] == pytest.approx(6.6648) and fx["prev_kind"] == "derived"
    assert fx["quote_ts"] == datetime(2026, 11, 7, 5, 58, 52)


def test_zero_fields_are_null_not_zero():
    fields = ["0.000"] * 36 + ["2026-11-09", "08:59:00", "0", "0"] + [""] * 8 + ["0.000", "沪深300指数期货2611"]
    rows = X.parse_sina('var hq_str_nf_IF2611="' + ",".join(fields) + '";', "grid", POLL)
    row = rows[0]
    assert all(row[k] is None for k in ("price", "open", "high", "low", "prev_close", "bid1", "ask1"))
    assert row["volume"] == 0 and row["contract"] == "IF2611"


def test_sina_skips_empty_and_short_answers():
    body = 'var hq_str_nf_IF2609="";\nvar hq_str_hf_GC="1,2";\nvar hq_str_zz_unknown="a,b";\n'
    assert X.parse_sina(body, "grid", POLL) == []


def test_every_row_fits_the_schema():
    rows = X.parse_tencent(text("tencent_etf_index.txt"), "grid", POLL) + X.parse_sina(sina_body().decode("gbk"), "grid", POLL)
    frame = pl.DataFrame(rows, schema=X.SCHEMA)
    assert frame.height == 17 + 9 + 5 and set(frame.columns) == set(X.SCHEMA)


# --- instruments ----------------------------------------------------------------------

def test_contract_months_follow_the_exchange_listing():
    assert X.third_friday(2026, 11) == date(2026, 11, 20)
    assert X.contract_months(date(2026, 11, 8)) == ["2611", "2612", "2703", "2706"]
    assert X.contract_months(date(2026, 11, 20)) == ["2611", "2612", "2703", "2706"]  # expiry day still trades
    assert X.contract_months(date(2026, 11, 21)) == ["2612", "2701", "2703", "2706"]  # expired month dropped
    assert X.contract_months(date(2026, 12, 21)) == ["2701", "2702", "2703", "2706"]  # year rolls over
    assert X.contract_months(date(2026, 10, 5)) == ["2610", "2611", "2612", "2703"]


def test_symbol_lists():
    tencent, sina = X.tencent_symbols(), X.sina_symbols(DAY)
    assert len(tencent) == 17 and len(sina) == 25 and len(set(tencent + sina)) == 42
    assert sina[:5] == ["nf_IF0", "nf_IF2611", "nf_IF2612", "nf_IF2703", "nf_IF2706"]
    assert {X.host_of(s) for s in tencent} == {X.QUOTE_HOST} and {X.host_of(s) for s in sina} == {X.SINA_HOST}
    assert [X.asset_of(s) for s in ("sz159919", "sz399006", "nf_IM0", "hf_GC", "fx_susdcnh")] == [
        "etf", "index", "future", "commodity", "fx"]


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
])
def test_looks_blocked(status: int, body: bytes, location: str, expected: bool):
    assert X.looks_blocked(status, body, location) is expected


def fetcher_on(handler) -> X.HttpFetcher:
    return X.HttpFetcher(httpx.Client(transport=httpx.MockTransport(handler)))


def test_http_fetcher_sends_the_sina_referer_and_returns_bytes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = dict(request.headers)
        return httpx.Response(200, content=b'var hq_str_x="1";')

    fetch = fetcher_on(handler)
    assert fetch(X.SINA_HOST, X.SINA_URL + "nf_IF0") == b'var hq_str_x="1";'
    fetch(X.QUOTE_HOST, X.QUOTE_URL + "sh000001")
    assert seen[X.SINA_HOST]["referer"] == "https://finance.sina.com.cn" and "referer" not in seen[X.QUOTE_HOST]


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


# --- recorder -------------------------------------------------------------------------

def test_poll_makes_one_request_per_host_and_records_every_instrument(tmp_path: Path):
    calls: list[str] = []
    rec = recorder(tmp_path, calls)
    rows = rec.poll("grid", POLL)
    assert sorted(calls) == sorted([X.QUOTE_HOST, X.SINA_HOST])
    assert len(rows) == 17 + 9 + 5
    assert rows_of("nf_IF0", rows)["contract"] == "IF2612"
    assert rec.stats["polls"] == {"grid": 1} and rec.stats["rows_by_host"] == {X.QUOTE_HOST: 17, X.SINA_HOST: 14}
    assert len(rec.seen) == 31


def test_waf_answer_stops_that_host_for_the_day_and_the_other_goes_on(tmp_path: Path):
    calls: list[str] = []
    block = X.Blocked("qt.gtimg.cn refused: HTTP 501", 501, b"<html>waf.tencent.com</html>")
    rec = recorder(tmp_path, calls, {X.QUOTE_HOST: block})
    first = rec.poll("grid", POLL)
    assert X.QUOTE_HOST in rec.blocked and X.SINA_HOST not in rec.blocked
    assert {r["host"] for r in first} == {X.SINA_HOST}
    calls.clear()
    rec.poll("grid", POLL)
    assert calls == [X.SINA_HOST]  # no request to the blocked host, no retry
    raw = X.raw_polls(rec.dir / "raw" / f"{X.QUOTE_HOST}.txt.gz")
    assert len(raw) == 1 and raw[0][2] == 501 and "waf.tencent.com" in raw[0][3]  # the refusal is kept as evidence


def test_twenty_failures_in_a_row_stop_a_host_but_a_success_resets_the_count(tmp_path: Path):
    calls: list[str] = []
    flaky = {"n": 0}
    answers = {X.QUOTE_HOST: fixture("tencent_etf_index.txt"), X.SINA_HOST: sina_body()}

    def fetch(host: str, url: str) -> bytes:
        calls.append(host)
        if host == X.SINA_HOST:
            flaky["n"] += 1
            if flaky["n"] != 20:  # fails 19 times, answers once, fails again
                raise X.HostError("HTTP 500")
        return answers[host]

    rec = X.Recorder(DAY, tmp_path, fetch)
    for _ in range(25):
        rec.poll("grid", POLL)
    assert rec.fails[X.SINA_HOST] == 5 and X.SINA_HOST not in rec.blocked  # the 20th poll reset the count
    for _ in range(15):
        rec.poll("grid", POLL)
    assert X.SINA_HOST in rec.blocked and "20 failures" in rec.blocked[X.SINA_HOST]
    before = calls.count(X.SINA_HOST)
    rec.poll("grid", POLL)
    assert calls.count(X.SINA_HOST) == before and rec.stats["failed_requests"] == 39
    assert rec.stats["error_count"] == 39 and len(rec.stats["errors"]) <= X.MAX_ERRORS


def test_an_unparsable_answer_counts_as_a_failure_not_a_crash(tmp_path: Path):
    rec = recorder(tmp_path, overrides={X.SINA_HOST: b"garbage that is not a quote"})
    rows = rec.poll("grid", POLL)
    assert {r["host"] for r in rows} == {X.QUOTE_HOST} and rec.fails[X.SINA_HOST] == 1


def test_a_slow_host_cannot_hold_the_grid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(X, "POLL_BUDGET", 0.2)
    answers = {X.QUOTE_HOST: fixture("tencent_etf_index.txt"), X.SINA_HOST: sina_body()}

    def fetch(host: str, url: str) -> bytes:
        if host == X.SINA_HOST:
            time.sleep(1.0)
        return answers[host]

    rec = X.Recorder(DAY, tmp_path, fetch)
    start = time.monotonic()
    rows = rec.poll("grid", POLL)
    assert time.monotonic() - start < 0.9
    assert {r["host"] for r in rows} == {X.QUOTE_HOST} and rec.stats["truncated"] == {X.SINA_HOST: 1}
    rec.pool.shutdown(wait=True)


def test_trading_day_check_reads_the_quote_stamps(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", POLL)
    assert rec.trading_today()
    stale = X.Recorder(date(2026, 11, 9), tmp_path, fake_fetcher([]))  # the feed still shows Friday
    stale.poll("grid", datetime(2026, 11, 9, 9, 31, 5))
    assert not stale.trading_today()
    assert not X.Recorder(DAY, tmp_path / "empty", fake_fetcher([])).trading_today()  # nothing polled yet


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
    assert merged.height == 3 * 31 and merged.schema == pl.Schema(X.SCHEMA)
    assert merged.select("symbol", "poll_ts", "kind").equals(merged.select("symbol", "poll_ts", "kind").sort("symbol", "poll_ts", "kind"))
    assert merged["kind"].value_counts().sort("kind").rows() == [("close", 31), ("grid", 62)]
    assert list((rec.dir / "parts").glob("part-*.parquet")) == []
    stored = json.loads((rec.dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["end"] == "done" and stored["polls"] == {"grid": 2, "close": 1}
    assert stored["rows"] == {"grid": 62, "close": 31} and stored["missed"] == 2 and stored["blocked"] == {}
    assert stored["symbols"] == 42 and len(stored["never_returned"]) == 11
    assert "nf_IH2612" in stored["never_returned"] and "nf_IF2611" not in stored["never_returned"]
    assert summary["error_count"] == 0


def test_finish_twice_and_a_restart_keep_every_row(tmp_path: Path):
    first = recorder(tmp_path)
    first.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    first.finish("stop time")
    assert first.finish("again") == {}
    second = recorder(tmp_path)  # a restart: same day directory, the merged file already exists
    second.poll("grid", datetime(2026, 11, 6, 9, 1, 5))
    second.finish("done")
    assert pl.read_parquet(first.dir / "quotes_ext.parquet").height == 2 * 31
    assert X.compact(first.dir) == 2 * 31  # nothing left to merge


def test_a_crash_keeps_the_flushed_parts_and_compact_recovers_them(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.flush(force=True)
    rec.poll("grid", datetime(2026, 11, 6, 9, 1, 5))  # in memory only: lost with the process
    assert not (rec.dir / "quotes_ext.parquet").exists()
    assert X.compact(rec.dir) == 31 and pl.read_parquet(rec.dir / "quotes_ext.parquet").height == 31


def test_raw_file_keeps_every_response_as_received(tmp_path: Path):
    rec = recorder(tmp_path)
    rec.poll("grid", datetime(2026, 11, 6, 9, 0, 5))
    rec.poll("close", datetime(2026, 11, 6, 15, 1, 30))
    polls = X.raw_polls(rec.dir / "raw" / f"{X.QUOTE_HOST}.txt.gz")
    assert [(p[0], p[1], p[2]) for p in polls] == [("grid", "09:00:05", 200), ("close", "15:01:30", 200)]
    assert polls[0][3].strip() == text("tencent_etf_index.txt").strip()
    assert len(X.raw_polls(rec.dir / "raw" / f"{X.SINA_HOST}.txt.gz")) == 2


def test_output_root_follows_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PULSE_INTRADAY_EXT_DIR", str(tmp_path))
    assert X.output_root() == tmp_path
    monkeypatch.delenv("PULSE_INTRADAY_EXT_DIR")
    assert X.output_root().name == "intraday_ext"
