"""Warehouse provider parsing, sync jobs and the data API. No network: the
Tencent fetchers are replaced with deterministic fakes and every test writes to
its own temporary lake."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
import threading

import httpx
import polars as pl
import pytest
from fastapi.testclient import TestClient

from contracts.schema import SyncRequest
from backend.app.api import data as data_api
from backend.app.main import app
from backend.app.warehouse import store as store_module
from backend.app.warehouse import sync, tencent
from backend.app.warehouse.store import Warehouse

TODAY = date.today()


def weekdays(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


class FakeTencent:
    """Deterministic stand-in for the network functions in `tencent`."""

    def __init__(self) -> None:
        self.bar_calls: list[tuple[str, date, date]] = []
        self.quote_calls: list[list[str]] = []
        self.blocked = False
        self.adjust: dict[str, tuple[date, float]] = {}
        self.universe: list[str] = []
        self.fail_bars = False
        self.gate: threading.Event | None = None

    def close(self, code: str, day: date) -> float:
        value = 10 + int(code) % 7 + (day.toordinal() % 5) * 0.1
        ex = self.adjust.get(code)
        return round(value * ex[1], 4) if ex and day < ex[0] else round(value, 4)

    def fetch_bars(self, code, start, end, client):
        self.bar_calls.append((code, start, end))
        if self.gate is not None:
            self.gate.wait(5)
        if self.blocked:
            raise tencent.TencentBlocked("waf")
        if self.fail_bars:
            raise tencent.TencentError("boom")
        days = weekdays(start, end)
        if not days:
            return pl.DataFrame(schema=tencent.BAR_SCHEMA)
        closes = [self.close(code, d) for d in days]
        return pl.DataFrame(
            {
                "code": [code] * len(days),
                "date": days,
                "open": closes,
                "high": [c + 0.1 for c in closes],
                "low": [c - 0.1 for c in closes],
                "close": closes,
                "volume": [100000.0] * len(days),
                "amount": [None] * len(days),
            },
            schema=tencent.BAR_SCHEMA,
        )

    def fetch_index(self, symbol, start, end, client):
        days = weekdays(start, end)
        closes = [3000.0 + i for i in range(len(days))]
        return pl.DataFrame(
            {
                "symbol": [symbol] * len(days),
                "date": days,
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": [1e9] * len(days),
            },
            schema=tencent.INDEX_SCHEMA,
        )

    def securities(self, codes: list[str]) -> pl.DataFrame:
        rows = [
            {
                "code": code, "name": f"测试{code}", "board": tencent.board_of(code), "total_cap": 100.0,
                "float_cap": 80.0, "pe_ttm": None, "pb": 2.0, "as_of": TODAY.isoformat(), "list_date": None,
            }
            for code in codes
        ]
        return pl.DataFrame(rows, schema=tencent.SECURITY_SCHEMA)

    def list_securities(self, client):
        return self.securities(self.universe)

    def fetch_quotes(self, codes, client):
        return self.securities(codes)

    def fetch_quote_bars(self, codes, client):
        """Today's closing quote. The previous close is computed on today's
        adjustment basis, like the exchange's ex-right reference price."""
        self.quote_calls.append(list(codes))
        days = weekdays(TODAY - timedelta(days=10), TODAY)
        day, prev = days[-1], days[-2]
        rows = [
            {
                "code": code, "date": day, "open": self.close(code, day), "high": self.close(code, day) + 0.1,
                "low": self.close(code, day) - 0.1, "close": self.close(code, day), "volume": 100000.0,
                "amount": 1.5, "prev_close": self.close(code, prev),
            }
            for code in codes
        ]
        return pl.DataFrame(rows, schema=tencent.QUOTE_BAR_SCHEMA)


@pytest.fixture()
def fake(tmp_path, monkeypatch) -> FakeTencent:
    fake = FakeTencent()
    lake = Warehouse(tmp_path / "warehouse" / "live", "live")
    monkeypatch.setattr(store_module, "settings", SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(sync, "_store", lambda: lake)
    monkeypatch.setattr(sync, "_client", lambda: httpx.Client())
    monkeypatch.setattr(tencent, "fetch_bars", fake.fetch_bars)
    monkeypatch.setattr(tencent, "fetch_index", fake.fetch_index)
    monkeypatch.setattr(tencent, "list_securities", fake.list_securities)
    monkeypatch.setattr(tencent, "fetch_quotes", fake.fetch_quotes)
    monkeypatch.setattr(tencent, "fetch_quote_bars", fake.fetch_quote_bars)
    monkeypatch.setattr(sync, "_JOB", None)
    monkeypatch.setattr(sync, "_RUNNING", False)
    monkeypatch.setattr(sync, "session_cut", lambda now=None: None)  # wall clock must not change results
    monkeypatch.setattr(sync, "BAR_SOURCE", "tencent")  # the fakes stand in for the tencent fetchers
    fake.lake = lake  # type: ignore[attr-defined]
    yield fake
    thread = sync._THREAD
    if thread is not None:
        if fake.gate is not None:
            fake.gate.set()
        thread.join(10)


# --- provider parsing ---------------------------------------------------------------------

def quote_line(ticker: str, name: str, code: str, price: str, pe="", cap_float="", cap_total="", pb="") -> str:
    fields = [""] * 50
    fields[0], fields[1], fields[2], fields[3] = "1", name, code, price
    fields[39], fields[44], fields[45], fields[46] = pe, cap_float, cap_total, pb
    return f'v_{ticker}="{"~".join(fields)}";'


def test_parse_quotes_keeps_priced_rows_and_nulls_unknowns() -> None:
    text = "\n".join(
        [
            quote_line("sh600000", "浦发银行", "600000", "9.50", pe="5.2", cap_float="2800.5", cap_total="2900.1", pb="0.55"),
            quote_line("sz300750", "宁德时代", "300750", "200.1", pe="", cap_float="0", cap_total="9000", pb=""),
            quote_line("sz000002", "", "000002", "7.0"),           # no name
            quote_line("sz000004", "停牌股", "000004", "0.00"),     # no price
            'v_pv_none_match="1";',
            quote_line("bj920001", "北交样本", "920001", "12.3", pe="0"),
        ]
    )
    rows = {row["code"]: row for row in tencent.parse_quotes(text, "2026-10-30")}
    assert set(rows) == {"600000", "300750", "920001"}
    assert rows["600000"]["pe_ttm"] == 5.2 and rows["600000"]["float_cap"] == 2800.5 and rows["600000"]["pb"] == 0.55
    assert rows["300750"]["pe_ttm"] is None and rows["300750"]["pb"] is None and rows["300750"]["float_cap"] is None
    assert rows["300750"]["total_cap"] == 9000.0
    assert rows["920001"]["pe_ttm"] is None  # 0 means unknown, never a value
    assert (rows["600000"]["board"], rows["300750"]["board"], rows["920001"]["board"]) == ("10cm", "20cm", "30cm")
    assert rows["600000"]["as_of"] == "2026-10-30" and rows["600000"]["list_date"] is None


def test_new_bse_codes_route_to_bj_not_sh() -> None:
    assert tencent.market_of("920001") == "bj"
    assert tencent.market_of("688981") == "sh"
    assert tencent.market_of("430047") == "bj"
    assert tencent.market_of("000001") == "sz"
    assert tencent.board_of("688981") == "20cm"


def test_list_securities_batches_and_filters(monkeypatch) -> None:
    seen: list[int] = []

    def fake_get(client, url, params=None):
        tickers = url.removeprefix(tencent.QUOTE_URL).split(",")
        seen.append(len(tickers))
        body = "\n".join(
            quote_line(t, "样本", t[2:], "10.0") for t in tickers if t in {"sh600000", "sz000001", "sz300750", "bj920001"}
        )
        return SimpleNamespace(content=body.encode("gbk"))

    monkeypatch.setattr(tencent, "_get", fake_get)
    frame = tencent.list_securities(httpx.Client())
    assert frame["code"].to_list() == ["000001", "300750", "600000", "920001"]
    assert max(seen) == tencent.QUOTE_BATCH
    assert sum(seen) == len(tencent.candidate_codes())


def test_legacy_bse_codes_are_dropped_when_the_920_alias_exists() -> None:
    rows = [
        {"code": "920000", "name": "安徽凤凰"}, {"code": "832000", "name": "安徽凤凰"},
        {"code": "430017", "name": "星昊医药"}, {"code": "600000", "name": "浦发银行"},
    ]
    frame = pl.DataFrame(rows)
    assert sorted(tencent._drop_legacy_bse_aliases(frame)["code"].to_list()) == ["430017", "600000", "920000"]


def test_list_securities_rejects_mostly_failed_runs(monkeypatch) -> None:
    def broken(client, url, params=None):
        raise tencent.TencentError("down")

    monkeypatch.setattr(tencent, "_get", broken)
    with pytest.raises(tencent.TencentError):
        tencent.list_securities(httpx.Client())


def test_windows_cover_range_in_two_year_chunks() -> None:
    start, end = date(2020, 3, 5), date(2026, 10, 30)
    chunks = tencent.windows(start, end)
    assert chunks[0][0] == start and chunks[-1][1] == end
    assert all((b - a).days < 731 for a, b in chunks)
    assert all(chunks[i][1] + timedelta(days=1) == chunks[i + 1][0] for i in range(len(chunks) - 1))
    assert tencent.windows(date(2026, 1, 1), date(2025, 1, 1)) == []


def test_parse_kline_converts_lots_to_shares() -> None:
    payload = {"data": {"sh600000": {"qfqday": [["2024-01-02", "10", "10.5", "10.8", "9.9", "1234", {"x": 1}], ["bad"], ["2024-01-03", "0", "0", "0", "0", "0"]]}}}
    rows = tencent.parse_kline(payload, "sh600000")
    assert rows == [(date(2024, 1, 2), 10.0, 10.8, 9.9, 10.5, 123400.0)]
    assert tencent.parse_kline({"data": []}, "sh600000") == []


def test_fetch_bars_chunks_requests_and_dedups(monkeypatch) -> None:
    calls: list[tuple[date, date]] = []

    def fake_kline(client, ticker, first, last):
        calls.append((first, last))
        return [(last, 10.0, 11.0, 9.0, 10.5, 500.0), (last, 10.0, 11.0, 9.0, 10.6, 500.0)]  # duplicate day

    monkeypatch.setattr(tencent, "_kline", fake_kline)
    frame = tencent.fetch_bars("600000", date(2021, 1, 1), date(2026, 1, 1), httpx.Client())
    assert len(calls) == len(tencent.windows(date(2021, 1, 1), date(2026, 1, 1))) >= 3
    assert frame.height == len(calls) and frame["close"].to_list()[0] == 10.6
    assert frame.schema == pl.Schema(tencent.BAR_SCHEMA)
    assert frame["amount"].null_count() == frame.height
    assert frame["date"].is_sorted()


# --- sync jobs -------------------------------------------------------------------------------

def backfill(codes=None, years=1) -> SyncRequest:
    return SyncRequest(kind="backfill", years=years, codes=codes)


def test_backfill_writes_all_three_tables(fake) -> None:
    job = sync.run_sync(backfill(["600000", "000001", "300750"]))
    assert job.status == "done", job.message
    assert job.done == job.total == 3 and job.progress == 1.0 and job.errors == 0
    bars = fake.lake.read("bars")
    assert sorted(bars["code"].unique().to_list()) == ["000001", "300750", "600000"]
    assert fake.lake.read("index_bars")["symbol"].n_unique() == len(tencent.INDEX_TICKERS)
    assert fake.lake.read("securities")["code"].to_list() == ["000001", "300750", "600000"]
    assert len(fake.lake.files("bars")) == 1
    assert sync.current_job().id == job.id  # type: ignore[union-attr]
    assert "回填完成" in job.message


def test_backfill_batches_about_200_codes_per_file(fake) -> None:
    fake.universe = [f"{600000 + i}" for i in range(450)]
    job = sync.run_sync(backfill(years=1))
    assert job.status == "done", job.message
    assert fake.lake.read("bars")["code"].n_unique() == 450
    assert len(fake.lake.files("bars")) == 3  # 200 + 200 + 50


def test_backfill_is_resumable(fake) -> None:
    sync.run_sync(backfill(["600000", "000001"]))
    fake.bar_calls.clear()
    job = sync.run_sync(backfill(["600000", "000001", "300750"]))
    assert job.status == "done"
    assert [call[0] for call in fake.bar_calls] == ["300750"]
    assert "跳过已最新 2 只" in job.message


def test_backfill_failure_becomes_failed_job(fake) -> None:
    fake.fail_bars = True
    job = sync.run_sync(backfill(["600000", "000001"]))
    assert job.status == "failed" and job.finished_at is not None
    assert "回填失败" in job.message
    assert fake.lake.read("bars") is None


def test_listing_failure_becomes_failed_job(fake, monkeypatch) -> None:
    def broken(client):
        raise tencent.TencentError("证券列表不可信")

    monkeypatch.setattr(tencent, "list_securities", broken)
    job = sync.run_sync(backfill())
    assert job.status == "failed" and "不可信" in job.message


def test_progress_callback_reports_and_survives_errors(fake) -> None:
    seen: list[float] = []

    def hook(job):
        seen.append(job.progress)
        raise RuntimeError("callback bug")

    job = sync.run_sync(backfill(["600000", "000001"]), hook)
    assert job.status == "done" and seen[-1] == 1.0 and seen == sorted(seen)


def test_daily_detects_an_ex_right_day_and_refetches_the_whole_history(fake) -> None:
    fake.universe = ["600000", "000001"]
    sync.run_sync(backfill(years=1))
    first_date = fake.lake.read("bars").filter(pl.col("code") == "600000")["date"].min()
    stored_old = fake.lake.read("bars").filter((pl.col("code") == "600000") & (pl.col("date") == first_date))["close"][0]

    fake.adjust["600000"] = (weekdays(TODAY - timedelta(days=10), TODAY)[-1], 0.9)  # ex-dividend on the quote day
    fake.universe = ["600000", "000001", "300750"]                                     # 300750 newly listed
    fake.bar_calls.clear()
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "done", job.message
    assert "重拉 1 只" in job.message and "新增 1 只" in job.message
    assert fake.quote_calls[-1] == ["000001", "600000"]  # stored codes read through the batch quote

    by_code: dict[str, list[tuple[date, date]]] = {}
    for code, start, end in fake.bar_calls:
        by_code.setdefault(code, []).append((start, end))
    assert by_code["600000"] == [(first_date, by_code["600000"][0][1])]       # straight to the full history
    assert "000001" not in by_code                                            # closing quote was enough
    assert by_code["300750"][0][0] < first_date + timedelta(days=15)          # new code gets the full window

    after = fake.lake.read("bars").filter((pl.col("code") == "600000") & (pl.col("date") == first_date))["close"][0]
    assert after == pytest.approx(stored_old * 0.9, rel=1e-6)  # whole history on one adjustment basis


def test_daily_without_adjustment_needs_no_kline_requests(fake) -> None:
    fake.universe = ["600000"]
    sync.run_sync(backfill(years=1))
    fake.bar_calls.clear()
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "done" and "重拉 0 只" in job.message, job.message
    assert fake.bar_calls == []
    today = fake.lake.read("bars").filter(pl.col("code") == "600000").sort("date").tail(1)
    assert today["amount"][0] is None  # one amount definition per table: the estimate, like kline rows


def test_plan_daily_sorts_quotes_into_append_gap_and_readjusted() -> None:
    d1, d2, d3 = date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)
    tail = {"A": {d1: 10.0, d2: 10.5}, "B": {d1: 20.0}, "C": {d1: 5.0, d2: 5.0}, "D": {d1: 8.0, d2: 8.0}}

    def row(code, prev_close, volume=1e5):
        return {"code": code, "date": d3, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume": volume, "amount": 1.0, "prev_close": prev_close}

    quotes = pl.DataFrame([row("A", 10.5), row("B", 20.0), row("C", 4.6), row("D", 8.0, volume=0.0)],
                          schema=tencent.QUOTE_BAR_SCHEMA)
    bars, refetch = sync.plan_daily(quotes, tail)
    assert bars["code"].to_list() == ["A"] and bars.columns == list(tencent.BAR_SCHEMA)
    assert refetch == {"B": "gap", "C": "readjusted"}  # D is suspended: nothing to do


def test_backfill_stops_at_once_when_the_provider_blocks(fake) -> None:
    fake.universe = [f"60{index:04d}" for index in range(120)]
    fake.blocked = True
    job = sync.run_sync(backfill(years=1))
    assert job.status == "failed" and "waf" in job.message
    assert len(fake.bar_calls) < 30  # stopped instead of walking the whole list


def test_waf_page_is_raised_without_retries(monkeypatch) -> None:
    calls = []

    class Client:
        def get(self, url, params=None):
            calls.append(url)
            return httpx.Response(501, content=b'<html><script>window.location.href="https://waf.tencent.com/501page.html"')

    with pytest.raises(tencent.TencentBlocked):
        tencent._get(Client(), tencent.KLINE_URL)
    assert len(calls) == 1


def test_parse_quote_bars_reads_today_ohlc_volume_and_amount() -> None:
    fields = [""] * 50
    fields[1], fields[2], fields[3], fields[4], fields[5] = "浦发银行", "600000", "10.02", "9.99", "9.98"
    fields[30], fields[33], fields[34], fields[36], fields[37] = "20261030150003", "10.04", "9.90", "648865", "64774"
    row = tencent.parse_quote_bars(f'v_sh600000="{"~".join(fields)}";')[0]
    assert row["date"] == date(2026, 10, 30) and row["close"] == 10.02 and row["prev_close"] == 9.99
    assert row["volume"] == 64886500 and row["amount"] == pytest.approx(6.4774)


def test_session_cut_keeps_unfinished_bars_out() -> None:
    from datetime import datetime as dt
    cst = sync.CST
    assert sync.session_cut(dt(2026, 10, 30, 10, 30, tzinfo=cst)) == date(2026, 10, 30)  # Friday, mid-session
    assert sync.session_cut(dt(2026, 10, 30, 15, 30, tzinfo=cst)) is None                # after the close
    assert sync.session_cut(dt(2026, 10, 31, 10, 30, tzinfo=cst)) is None                # Saturday


def test_intraday_daily_sync_stores_nothing_for_today(fake, monkeypatch) -> None:
    fake.universe = ["600000"]
    sync.run_sync(backfill(years=1))
    quote_day = weekdays(TODAY - timedelta(days=10), TODAY)[-1]
    before = fake.lake.read("bars").filter(pl.col("date") < quote_day).height
    lake_files = len(fake.lake.files("bars"))
    monkeypatch.setattr(sync, "session_cut", lambda now=None: quote_day)
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "done", job.message
    bars = fake.lake.read("bars")
    assert bars.filter(pl.col("date") >= quote_day).height == 1  # only the bar stored by the earlier (after-close) backfill
    assert bars.filter(pl.col("date") < quote_day).height == before
    assert len(fake.lake.files("bars")) == lake_files  # nothing new written


def test_daily_after_a_long_pause_refetches_the_whole_history(fake) -> None:
    fake.universe = ["600000"]
    sync.run_sync(backfill(years=1))
    first = fake.lake.read("bars")["date"].min()
    # Keep only bars older than the overlap window, as if the last sync ran two months ago.
    old = fake.lake.read("bars").filter(pl.col("date") < TODAY - timedelta(days=60)).drop("ingested_at")
    for path in fake.lake.files("bars"):
        path.unlink()
    fake.lake.write("bars", old)
    fake.bar_calls.clear()
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "done", job.message
    assert [(code, start) for code, start, _ in fake.bar_calls] == [("600000", first)]
    days = fake.lake.read("bars")["date"].to_list()
    assert days == sorted(set(days)) and len(days) == len(weekdays(first, TODAY))  # no hole left


def test_backfill_does_not_refetch_codes_listed_after_the_window(fake, monkeypatch) -> None:
    fake.universe = ["600000", "300750"]
    late = TODAY - timedelta(days=100)

    def listed_late(code, start, end, client):
        return fake.fetch_bars(code, max(start, late) if code == "300750" else start, end, client)

    monkeypatch.setattr(tencent, "fetch_bars", listed_late)
    sync.run_sync(backfill(years=1))
    fake.bar_calls.clear()
    job = sync.run_sync(backfill(years=1))
    assert job.status == "done" and "跳过已最新 2 只" in job.message, job.message
    assert fake.bar_calls == []


def test_interrupted_backfill_does_not_refetch_codes_it_already_has(fake, monkeypatch) -> None:
    # 300750 listed late, 600009 has no bars at all (delisted or no Tencent data):
    # neither ever looks covered by its stored span, so only the record saves them.
    fake.universe = ["300750", "600009", "600000"]
    late = TODAY - timedelta(days=100)
    others_done = threading.Event()
    state = {"block": True}

    def fetch(code, start, end, client):
        if code == "600000" and state["block"]:
            others_done.wait(5)  # refuse only after the other two have been processed
            raise tencent.TencentBlocked("waf")
        if code == "600009":
            fake.bar_calls.append((code, start, end))
            return pl.DataFrame(schema=tencent.BAR_SCHEMA)
        return fake.fetch_bars(code, max(start, late) if code == "300750" else start, end, client)

    monkeypatch.setattr(tencent, "fetch_bars", fetch)
    job = sync.run_sync(backfill(years=1), lambda job: others_done.set() if job.done >= 2 else None)
    assert job.status == "failed" and "waf" in job.message
    assert fake.lake.read_doc("meta", "backfill_fetched")["codes"] == ["300750", "600009"]

    state["block"] = False
    fake.bar_calls.clear()
    job = sync.run_sync(backfill(years=1))
    assert job.status == "done", job.message
    assert [code for code, _, _ in fake.bar_calls] == ["600000"]
    assert fake.lake.read_doc("meta", "backfill_fetched")["codes"] == []  # done: the window takes over


def test_cli_waits_out_a_refusal_and_resumes(fake, monkeypatch) -> None:
    fake.universe = ["600000", "600001"]
    fake.blocked = True
    waits: list[float] = []

    def reopen(deadline: float) -> bool:
        waits.append(deadline)
        fake.blocked = False
        return True

    monkeypatch.setattr(sync, "_wait_until_open", reopen)
    assert sync.main(["--kind", "backfill", "--years", "1", "--wait-hours", "2"]) == 0
    assert len(waits) == 1 and fake.lake.coverage()[0] == 2


def test_cli_without_wait_stops_on_a_refusal(fake, monkeypatch) -> None:
    fake.universe = ["600000"]
    fake.blocked = True
    monkeypatch.setattr(sync, "sleep", lambda seconds: pytest.fail("must not wait"))
    assert sync.main(["--kind", "backfill", "--years", "1"]) == 1


def test_wait_until_open_probes_until_the_host_answers(monkeypatch) -> None:
    answers = iter([False, False, True])
    monkeypatch.setattr(sync, "PROBE_EVERY", 0.0)
    monkeypatch.setattr(sync, "sleep", lambda seconds: None)
    monkeypatch.setattr(sync, "_client", lambda: httpx.Client())
    monkeypatch.setattr(tencent, "kline_open", lambda client: next(answers))
    monkeypatch.setattr(sync, "BAR_SOURCE", "tencent")
    assert sync._wait_until_open(sync.monotonic() + 60)
    assert next(answers, "exhausted") == "exhausted"


def test_kline_open_reads_the_waf_page_as_closed() -> None:
    def client(status: int, body: bytes) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body)))

    assert tencent.kline_open(client(200, b'{"code":0}'))
    assert not tencent.kline_open(client(501, b'window.location.href="https://waf.tencent.com/501page.html"'))
    assert not tencent.kline_open(client(200, b'<script>location="https://waf.tencent.com/x"</script>'))


def test_daily_on_empty_warehouse_without_securities_fails_cleanly(fake) -> None:
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "failed"


def test_sync_compacts_when_there_are_many_files(fake, monkeypatch) -> None:
    monkeypatch.setattr(sync, "BATCH_CODES", 1)
    monkeypatch.setattr(sync, "COMPACT_ABOVE", 2)
    sync.run_sync(backfill(["600000", "000001", "300750", "688981"]))
    assert len(fake.lake.files("bars")) == 1
    assert fake.lake.read("bars")["code"].n_unique() == 4


def test_compact_keeps_one_deduplicated_file(tmp_path) -> None:
    lake = Warehouse(tmp_path, "live")
    day = date(2026, 10, 29)
    for close in (1.0, 2.0, 3.0):
        lake.write("bars", pl.DataFrame({"code": ["600000"], "date": [day], "close": [close]}))
    assert len(lake.files("bars")) == 3
    lake.compact("bars")
    assert len(lake.files("bars")) == 1
    frame = lake.read("bars")
    assert frame.height == 1 and frame["close"][0] == 3.0  # newest ingest wins


def test_compaction_never_pulls_files_from_under_a_reader(tmp_path) -> None:
    lake = Warehouse(tmp_path / "lake", "live")
    for close in (1.0, 2.0):
        lake.write("bars", pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 5)], "close": [close]}))
    pending = lake.scan("bars")  # a research request listed the two batch files
    lake.compact("bars")
    assert pending.collect()["close"].to_list() == [2.0]  # still readable, newest ingest wins
    assert len(lake.files("bars")) == 1 and lake.read("bars")["close"].to_list() == [2.0]
    lake.version()  # tolerates retired files


def test_only_one_job_at_a_time(fake) -> None:
    fake.gate = threading.Event()
    request = backfill(["600000"])
    first = sync.start_sync(request)
    assert first.status == "running"
    again, started = sync.begin_sync(request)
    assert not started and again.id == first.id
    assert sync.start_sync(request).id == first.id
    fake.gate.set()
    sync._THREAD.join(10)  # type: ignore[union-attr]
    assert sync.current_job().status == "done"  # type: ignore[union-attr]
    assert sync.begin_sync(request)[1] is True  # slot is free again
    sync._THREAD.join(10)  # type: ignore[union-attr]


# --- api ---------------------------------------------------------------------------------------

def test_status_reports_sim_fallback_then_live(fake, monkeypatch) -> None:
    client = TestClient(app)
    sim = Warehouse(fake.lake.root.parent / "sim", "sim")
    monkeypatch.setattr(data_api, "active", lambda: sim)
    body = client.get("/api/v1/data/warehouse").json()
    assert body["active"] == "sim"
    assert body["provenance"]["mode"] == "SIM" and body["provenance"]["status"] == "fallback"
    assert "尚未同步真实历史" in body["provenance"]["detail"] and "sync" in body["provenance"]["detail"]
    assert body["notes"] and body["datasets"] == {"live": [], "sim": []}

    fake.universe = [f"{600000 + i}" for i in range(3)]
    assert sync.run_sync(backfill(years=1)).status == "done"
    monkeypatch.setattr(data_api, "active", lambda: fake.lake)
    body = client.get("/api/v1/data/warehouse").json()
    assert body["active"] == "live"
    assert body["provenance"]["mode"] == "LIVE" and body["provenance"]["source"] == "新浪日线 · 按比例前复权（数据仓）"
    assert body["coverage_codes"] == 3 and body["latest_trade_date"] == body["provenance"]["trade_date"]
    assert {table["name"] for table in body["datasets"]["live"]} == {"bars", "index_bars", "securities"}
    assert body["job"]["status"] == "done"


def test_sync_endpoint_returns_job_then_409_while_running(fake) -> None:
    client = TestClient(app)
    fake.gate = threading.Event()
    payload = {"kind": "backfill", "years": 1, "codes": ["600000"]}
    first = client.post("/api/v1/data/sync", json=payload)
    assert first.status_code == 200 and first.json()["status"] == "running"
    second = client.post("/api/v1/data/sync", json=payload)
    assert second.status_code == 409 and "已有同步任务" in second.json()["detail"]
    fake.gate.set()
    sync._THREAD.join(10)  # type: ignore[union-attr]
    assert client.post("/api/v1/data/sync", json={**payload, "years": 99}).status_code == 422

def test_today_st_names_are_not_applied_to_history() -> None:
    from backend.app.warehouse.features import daily_features

    bars = pl.DataFrame({
        "code": ["600001"] * 3, "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        "open": [10.0, 10.0, 10.55], "high": [10.0, 10.55, 10.6], "low": [10.0, 10.0, 10.5],
        "close": [10.0, 10.55, 10.6], "volume": [1e6] * 3,
    })
    securities = pl.DataFrame({"code": ["600001"], "name": ["*ST样本"]})  # became ST later
    frame = daily_features(bars.lazy(), securities.lazy()).collect()
    assert frame["limit_ratio"].to_list() == [0.10] * 3
    assert not frame["is_limit_up"].any()  # +5.5% is not a limit-up on the main board


# --- the bar source and its adjustment method ----------------------------------------

def test_a_new_adjustment_method_refetches_every_stored_code(fake, monkeypatch):
    fake.universe = ["600000", "600001"]
    assert sync.run_sync(backfill(years=1)).status == "done"
    assert fake.lake.read_doc("meta", "bars_method") == {"method": "tencent-fq"}
    fake.bar_calls.clear()
    assert sync.run_sync(backfill(years=1)).status == "done" and fake.bar_calls == []  # same method: all covered
    monkeypatch.setitem(sync.BARS_METHOD, "tencent", "other-method")
    job = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert job.status == "failed" and "复权方式" in job.message  # daily refuses to mix adjustments
    assert sync.run_sync(backfill(years=1)).status == "done"
    assert sorted(code for code, _, _ in fake.bar_calls) == ["600000", "600001"]
    assert fake.lake.read_doc("meta", "bars_method") == {"method": "other-method"}


def test_a_failed_code_during_a_method_switch_keeps_the_old_method_and_is_retried(fake, monkeypatch):
    fake.universe = ["600000", "600001"]
    assert sync.run_sync(backfill(years=1)).status == "done"
    monkeypatch.setitem(sync.BARS_METHOD, "tencent", "other-method")
    failing = {"600001"}

    def fetch(code, start, end, client):
        if code in failing:
            fake.bar_calls.append((code, start, end))
            raise tencent.TencentError("boom")
        return fake.fetch_bars(code, start, end, client)

    monkeypatch.setattr(tencent, "fetch_bars", fetch)
    fake.bar_calls.clear()
    job = sync.run_sync(backfill(years=1))
    assert job.status == "failed" and "回填未完成" in job.message, job.message
    assert fake.lake.read_doc("meta", "bars_method") == {"method": "tencent-fq"}  # the switch is not declared
    assert fake.lake.read_doc("meta", "backfill_fetched") == {
        "start": (TODAY - timedelta(days=365)).isoformat(), "codes": ["600000"], "method": "other-method",
    }
    daily = sync.run_sync(SyncRequest(kind="daily", years=1))
    assert daily.status == "failed" and "复权方式" in daily.message  # daily still refuses to mix adjustments

    failing.clear()
    fake.bar_calls.clear()
    job = sync.run_sync(backfill(years=1))
    assert job.status == "done", job.message
    assert [code for code, _, _ in fake.bar_calls] == ["600001"]  # only the code not yet fetched under the new method
    assert fake.lake.read_doc("meta", "bars_method") == {"method": "other-method"}
    assert fake.lake.read_doc("meta", "backfill_fetched")["codes"] == []


def test_a_failed_code_without_stored_bars_does_not_block_the_switch(fake, monkeypatch):
    fake.universe = ["600000", "600001"]

    def fetch(code, start, end, client):
        if code == "600001":
            raise tencent.TencentError("boom")
        return fake.fetch_bars(code, start, end, client)

    monkeypatch.setattr(tencent, "fetch_bars", fetch)
    job = sync.run_sync(backfill(years=1))  # fresh warehouse: no old rows to leave behind
    assert job.status == "done", job.message
    assert fake.lake.read_doc("meta", "bars_method") == {"method": "tencent-fq"}


def test_a_refetched_code_loses_old_method_rows_outside_the_new_window(fake, monkeypatch):
    fake.universe = ["600000", "600001"]
    assert sync.run_sync(backfill(years=2)).status == "done"
    assert fake.lake.read("bars")["date"].min() < TODAY - timedelta(days=400)
    monkeypatch.setitem(sync.BARS_METHOD, "tencent", "other-method")
    assert sync.run_sync(backfill(years=1)).status == "done"
    bars = fake.lake.read("bars")
    assert bars["date"].min() >= TODAY - timedelta(days=365)  # no old-method bar survives before the new window
    assert sorted(bars["code"].unique().to_list()) == ["600000", "600001"]


def test_replace_codes_drops_every_old_row_of_the_codes_and_survives_a_crash(tmp_path, monkeypatch):
    lake = Warehouse(tmp_path / "live", "live")

    def frame(code: str, days: list[date], close: float) -> pl.DataFrame:
        return pl.DataFrame(
            {"code": [code] * len(days), "date": days, "close": [close] * len(days)},
            schema={"code": pl.String, "date": pl.Date, "close": pl.Float64},
        )

    first, second = date(2026, 1, 5), date(2026, 1, 6)
    lake.write("bars", pl.concat([frame("A", [first, second], 1.0), frame("B", [first, second], 1.0)]))

    def crash(table, paths):
        raise OSError("disk")

    with monkeypatch.context() as patch:
        patch.setattr(lake, "_retire", crash)
        with pytest.raises(OSError):
            lake.replace_codes("bars", frame("A", [second], 2.0), ["A"])
    crashed = lake.read("bars").filter(pl.col("code") == "A")
    assert crashed["date"].to_list() == [first, second]  # old and new side by side: never neither
    assert crashed["close"].to_list() == [1.0, 2.0]

    lake.replace_codes("bars", frame("A", [second], 2.0), ["A"])
    bars = lake.read("bars")
    assert bars.filter(pl.col("code") == "A").select("date", "close").rows() == [(second, 2.0)]
    assert bars.filter(pl.col("code") == "B")["date"].to_list() == [first, second]

    lake.replace_codes("bars", pl.DataFrame(schema=bars.schema), ["B"])  # empty frame: the code is dropped
    assert lake.read("bars")["code"].unique().to_list() == ["A"]


def test_years_beyond_what_one_sina_request_returns_are_refused(fake, monkeypatch):
    from backend.app.warehouse import sina

    fake.universe = ["600000"]
    monkeypatch.setattr(sync, "BAR_SOURCE", "sina")
    assert sina.bars_needed(TODAY - timedelta(days=365 * sina.MAX_YEARS)) < sina.MAX_BARS  # the cap is honest
    for kind in ("backfill", "daily"):
        job = sync.run_sync(SyncRequest(kind=kind, years=sina.MAX_YEARS + 1))
        assert job.status == "failed" and "最多约" in job.message, job.message
    assert fake.bar_calls == [] and fake.lake.read("bars") is None and fake.lake.read_doc("meta", "backfill_window") is None


def test_sina_factors_divide_prices_from_each_ex_date_on():
    from backend.app.warehouse import sina

    raw = pl.DataFrame({
        "date": [date(2024, 6, 13), date(2024, 6, 14), date(2025, 1, 2)],
        "open": [11.0, 10.0, 10.0], "high": [11.0, 10.0, 10.0], "low": [11.0, 10.0, 10.0], "close": [11.0, 10.0, 10.0],
        "volume": [1.0, 1.0, 1.0],
    })
    factors = sina.parse_factors(
        'var sz000001qfq={"total":2,"data":[{"d":"2024-06-14", "f":"1.0000"},{"d":"2020-01-01", "f":"1.1000"}]}'
    )
    adjusted = sina.adjust(raw, factors)
    assert adjusted["close"].to_list() == pytest.approx([10.0, 10.0, 10.0])  # 11 / 1.1 before the ex-date
    assert sina.adjust(raw, sina.parse_factors("var x={}")).equals(raw)  # no factor file: unadjusted


def test_sina_kline_parsing_and_block_pages():
    from backend.app.warehouse import sina

    frame = sina.parse_kline('[{"day":"2026-10-30","open":"1.0","high":"2.0","low":"0.5","close":"1.5","volume":"300"}]')
    assert frame.row(0) == (date(2026, 10, 30), 1.0, 2.0, 0.5, 1.5, 300.0)
    assert sina.parse_kline("null").is_empty()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(456, content=b"<html>blocked")))
    with pytest.raises(tencent.ProviderBlocked):
        sina._get(client, sina.KLINE_URL)
    assert sina.bars_needed(TODAY - timedelta(days=365 * 3)) <= sina.MAX_BARS


def test_a_tencent_block_on_the_stale_index_does_not_stop_a_sina_job(monkeypatch):
    from backend.app.warehouse import sina

    def refuse(*args, **kwargs):
        raise tencent.TencentBlocked("waf")

    monkeypatch.setattr(tencent, "fetch_index", refuse)
    frame = sina.fetch_index("000985", TODAY - timedelta(days=30), TODAY, httpx.Client())
    assert frame.is_empty() and frame.columns == list(tencent.INDEX_SCHEMA)
