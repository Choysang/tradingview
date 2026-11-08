"""Dragon-Tiger list and Sina board sync. No network: both providers are replaced
by `httpx.MockTransport` handlers, pacing and backoff are switched off, and every
test writes to its own temporary lake."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re

import httpx
import polars as pl
import pytest

from backend.app.warehouse import boards, lhb
from backend.app.warehouse.store import Warehouse
from backend.app.warehouse.tencent import _Pacer

CST = timezone(timedelta(hours=8))
FRIDAY = date(2026, 10, 30)
EVENING = datetime(2026, 10, 30, 20, 0, tzinfo=CST)
MORNING = datetime(2026, 10, 30, 9, 30, tzinfo=CST)


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(lhb, "_PACER", _Pacer(1000.0))
    monkeypatch.setattr(lhb, "BACKOFF", 0.0)
    monkeypatch.setattr(boards, "BACKOFF", 0.0)
    monkeypatch.setattr(boards, "_PACERS", {host: _Pacer(1000.0) for host in boards._PACERS})


@pytest.fixture
def store(tmp_path) -> Warehouse:
    return Warehouse(tmp_path / "live", "live")


# --- Eastmoney fake --------------------------------------------------------------------

def stock_row(code: str, day: date, reason: str = "日涨幅偏离值达到7%的前5只证券", **extra) -> dict:
    row = {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": f"名称{code}",
        "TRADE_DATE": f"{day.isoformat()} 00:00:00",
        "EXPLANATION": reason,
        "CLOSE_PRICE": 12.5,
        "CHANGE_RATE": 10.0057,
        "TURNOVERRATE": 2.8119,
        "ACCUM_AMOUNT": 3_116_403_391,
        "BILLBOARD_NET_AMT": 460_963_059.86,
        "BILLBOARD_BUY_AMT": 803_520_809.71,
        "BILLBOARD_SELL_AMT": 342_557_749.85,
        "DEAL_AMOUNT_RATIO": 36.77,
        "FREE_MARKET_CAP": 113_382_381_121.59,
    }
    row.update(extra)
    return row


def seat_row(code: str, day: date, name: str, buy: float, sell: float, reason: str = "日涨幅偏离值达到7%的前5只证券", seat_code="1") -> dict:
    return {
        "SECURITY_CODE": code,
        "TRADE_DATE": f"{day.isoformat()} 00:00:00",
        "OPERATEDEPT_CODE": seat_code,
        "OPERATEDEPT_NAME": name,
        "EXPLANATION": reason,
        "BUY": buy,
        "SELL": sell,
        "NET": buy - sell,
    }


class FakeEastmoney:
    """Serves the three reports from per-day dictionaries, paged like the real API."""

    def __init__(self) -> None:
        self.stocks: dict[date, list[dict]] = {}
        self.buys: dict[date, list[dict]] = {}
        self.sells: dict[date, list[dict]] = {}
        self.requests: list[dict[str, str]] = []
        self.status = 200
        self.status_from: int | None = None  # request number (1-based) from which `status` applies
        self.body: bytes | None = None

    def add_day(self, day: date, codes: list[str], seats: int = 2) -> None:
        self.stocks[day] = [stock_row(code, day) for code in codes]
        self.buys[day] = [seat_row(code, day, f"营业部{n}", 100.0 - n, 5.0) for code in codes for n in range(seats)]
        self.sells[day] = [seat_row(code, day, f"营业部{n}", 5.0, 100.0 - n) for code in codes for n in range(seats)]

    def days_requested(self) -> list[str]:
        return [re.search(r"'([\d-]+)'", request["filter"]).group(1) for request in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if self.status_from is None or len(self.requests) >= self.status_from:
            if self.status != 200:
                return httpx.Response(self.status, text="nope")
            if self.body is not None:
                return httpx.Response(200, content=self.body)
        day = date.fromisoformat(re.search(r"'([\d-]+)'", params["filter"]).group(1))
        table = {lhb.REPORT_STOCKS: self.stocks, **{lhb.REPORT_SEATS["buy"]: self.buys, lhb.REPORT_SEATS["sell"]: self.sells}}[params["reportName"]]
        rows = table.get(day, [])
        if not rows:
            return httpx.Response(200, json={"version": None, "result": None, "success": False, "message": "返回数据为空", "code": 9201})
        size, page = int(params["pageSize"]), int(params["pageNumber"])
        pages = -(-len(rows) // size)
        return httpx.Response(
            200,
            json={"result": {"pages": pages, "count": len(rows), "data": rows[(page - 1) * size : page * size]}, "success": True, "code": 0},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def sync(fake: FakeEastmoney, store: Warehouse, **kwargs):
    kwargs.setdefault("days", 4)
    kwargs.setdefault("now", EVENING)
    return lhb.run_lhb_sync(store=store, client=fake.client(), **kwargs)


# --- LHB parsing -----------------------------------------------------------------------

def test_parse_stocks_converts_money_to_yi_and_keeps_percentages():
    frame = lhb.parse_stocks([stock_row("600519", FRIDAY)])
    row = frame.row(0, named=True)
    assert row["date"] == FRIDAY and row["name"] == "名称600519"
    assert row["amount"] == pytest.approx(31.16403391)
    assert row["net_buy"] == pytest.approx(4.6096305986)
    assert row["buy_amt"] == pytest.approx(8.0352080971)
    assert row["sell_amt"] == pytest.approx(3.4255774985)
    assert row["free_cap"] == pytest.approx(1133.82381121)
    assert row["pct_chg"] == pytest.approx(10.0057) and row["turnover_rate"] == pytest.approx(2.8119)
    assert row["deal_ratio"] == pytest.approx(36.77) and row["close"] == 12.5


def test_parse_stocks_keeps_a_shares_only_and_nulls_unknown_numbers():
    rows = [
        stock_row("118058", FRIDAY),  # convertible bond
        stock_row("200030", FRIDAY),  # B share
        stock_row("900920", FRIDAY),  # B share
        stock_row("920157", FRIDAY),  # BSE
        stock_row("832000", FRIDAY),  # legacy BSE
        stock_row("300001", FRIDAY, CLOSE_PRICE=None, ACCUM_AMOUNT="-", FREE_MARKET_CAP="n/a", CHANGE_RATE=float("nan")),
        stock_row("00001", FRIDAY),  # not 6 digits
    ]
    frame = lhb.parse_stocks(rows)
    assert sorted(frame["code"].to_list()) == ["300001", "832000", "920157"]
    unknown = frame.filter(pl.col("code") == "300001").row(0, named=True)
    assert unknown["close"] is None and unknown["amount"] is None
    assert unknown["free_cap"] is None and unknown["pct_chg"] is None
    assert unknown["net_buy"] is not None  # fields that are present survive


def test_parse_stocks_one_row_per_reason():
    rows = [stock_row("600519", FRIDAY, "A"), stock_row("600519", FRIDAY, "B"), stock_row("600519", FRIDAY, "A", CLOSE_PRICE=13.0)]
    frame = lhb.parse_stocks(rows)
    assert sorted(frame["reason"].to_list()) == ["A", "B"]
    assert frame.filter(pl.col("reason") == "A")["close"].item() == 13.0


def test_parse_seats_ranks_identical_institution_seats_apart():
    rows = [
        seat_row("600519", FRIDAY, "机构专用", 10_000_000, 0, seat_code="0"),
        seat_row("600519", FRIDAY, "机构专用", 30_000_000, 0, seat_code="0"),
        seat_row("600519", FRIDAY, "某某证券营业部", 20_000_000, 0),
        seat_row("600519", FRIDAY, "", 1, 0),  # no seat name
        seat_row("118058", FRIDAY, "机构专用", 1, 0),  # bond
    ]
    frame = lhb.parse_seats(rows, "buy").sort("rank")
    assert frame["side"].unique().to_list() == ["buy"]
    assert frame["seat"].to_list() == ["机构专用", "某某证券营业部", "机构专用"]
    assert frame["rank"].to_list() == [0, 1, 2]
    assert frame["buy"].to_list() == pytest.approx([0.3, 0.2, 0.1])
    assert frame["seat_code"].to_list() == ["0", "1", "0"]
    rerun = lhb.parse_seats(list(reversed(rows)), "buy").sort("rank")
    assert rerun["buy"].to_list() == frame["buy"].to_list()  # ranks are reproducible


def test_parse_seats_sell_side_ranks_by_sell_amount():
    frame = lhb.parse_seats([seat_row("600519", FRIDAY, "A", 0, 5e7), seat_row("600519", FRIDAY, "B", 0, 9e7)], "sell").sort("rank")
    assert frame["seat"].to_list() == ["B", "A"]


# --- LHB sync --------------------------------------------------------------------------

def test_sync_writes_both_tables_and_records_empty_days(store):
    fake = FakeEastmoney()
    fake.add_day(date(2026, 10, 29), ["600519", "000001"])
    fake.add_day(FRIDAY, ["300750"], seats=3)
    summary = sync(fake, store)  # 27th and 28th have no list
    assert summary.ok and summary.days_total == 4 and summary.days_fetched == 4 and summary.days_empty == 2
    stocks, seats = store.read("lhb_stocks"), store.read("lhb_seats")
    assert stocks.height == 3 and summary.stock_rows == 3
    assert seats.height == (2 * 2 + 3) * 2 and summary.seat_rows == seats.height
    assert set(seats["side"]) == {"buy", "sell"}
    assert stocks["date"].dtype == pl.Date
    assert store.read_doc("meta", "lhb_dates")["dates"] == ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    # Empty days cost one request (the stock list); days with a list also ask for both seat sides.
    assert len(fake.requests) == 2 + 3 + 3


def test_second_run_fetches_nothing(store):
    fake = FakeEastmoney()
    fake.add_day(FRIDAY, ["600519"])
    sync(fake, store)
    fake.requests.clear()
    summary = sync(fake, store)
    assert fake.requests == []
    assert summary.days_fetched == 0 and summary.days_skipped == 4 and summary.ok


def test_rerun_after_partial_fetch_only_fetches_missing_days(store):
    fake = FakeEastmoney()
    fake.add_day(FRIDAY, ["600519"])
    sync(fake, store, days=1)
    fake.requests.clear()
    sync(fake, store, days=4)
    assert sorted(set(fake.days_requested())) == ["2026-10-27", "2026-10-28", "2026-10-29"]


def test_stored_stock_days_count_as_done_even_without_the_doc(store):
    store.write("lhb_stocks", lhb.parse_stocks([stock_row("600519", FRIDAY)]))
    fake = FakeEastmoney()
    sync(fake, store, days=1)
    assert fake.requests == []


def test_paging_collects_every_page(store, monkeypatch):
    monkeypatch.setattr(lhb, "PAGE_SIZE", 2)
    fake = FakeEastmoney()
    codes = ["600519", "000001", "300750", "688001", "601318"]
    fake.add_day(FRIDAY, codes, seats=2)
    summary = sync(fake, store, days=1)
    assert summary.stock_rows == 5 and summary.seat_rows == 20
    pages = [int(request["pageNumber"]) for request in fake.requests if request["reportName"] == lhb.REPORT_STOCKS]
    assert pages == [1, 2, 3]
    assert store.read("lhb_stocks").height == 5


def test_blocked_stops_at_once_and_keeps_written_days(store):
    fake = FakeEastmoney()
    for day in (date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), FRIDAY):
        fake.add_day(day, ["600519"])
    fake.status, fake.status_from = 403, 7  # two full days = 6 requests, the 7th is refused
    summary = sync(fake, store)
    assert summary.blocked and not summary.ok
    assert len(fake.requests) == 7  # no retry after the refusal, no further days
    assert summary.days_fetched == 2 and "拒绝访问" in summary.message
    assert store.read("lhb_stocks").height == 2
    assert store.read_doc("meta", "lhb_dates")["dates"] == ["2026-10-27", "2026-10-28"]
    fake.status = 200
    fake.requests.clear()
    assert sync(fake, store).ok
    assert sorted(set(fake.days_requested())) == ["2026-10-29", "2026-10-30"]


@pytest.mark.parametrize("status", [429, 501])
def test_other_refusal_codes_block(store, status):
    fake = FakeEastmoney()
    fake.status = status
    summary = sync(fake, store)
    assert summary.blocked and len(fake.requests) == 1


def test_html_page_counts_as_blocked(store):
    fake = FakeEastmoney()
    fake.body = b"<!DOCTYPE html><html><body>captcha</body></html>"
    summary = sync(fake, store)
    assert summary.blocked and len(fake.requests) == 1 and store.read("lhb_stocks") is None


def test_consecutive_failing_days_stop_the_job(store, monkeypatch):
    monkeypatch.setattr(lhb, "MAX_CONSECUTIVE_ERRORS", 2)
    fake = FakeEastmoney()
    fake.status = 500
    summary = sync(fake, store)
    assert not summary.blocked and summary.errors == 2 and summary.days_fetched == 0
    assert len(fake.requests) == 2 * (lhb.RETRIES + 1)  # server errors are retried, then the day is skipped
    assert "连续 2 天" in summary.message
    assert store.read_doc("meta", "lhb_dates") is None  # failed days are not recorded


def test_failed_day_is_skipped_and_retried_next_run(store):
    fake = FakeEastmoney()
    fake.add_day(date(2026, 10, 29), ["600519"])
    fake.add_day(FRIDAY, ["600519"])
    original = fake.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        if "2026-10-28" in request.url.params["filter"]:
            return httpx.Response(500, text="boom")
        return original(request)

    client = httpx.Client(transport=httpx.MockTransport(flaky))
    summary = lhb.run_lhb_sync(days=4, store=store, client=client, now=EVENING)
    assert summary.errors == 1 and not summary.ok and not summary.blocked
    assert "2026-10-28" not in store.read_doc("meta", "lhb_dates")["dates"]
    assert "2026-10-29" in store.read_doc("meta", "lhb_dates")["dates"]


def test_today_is_not_fetched_before_the_evening_publish(store):
    fake = FakeEastmoney()
    fake.add_day(FRIDAY, ["600519"])
    summary = sync(fake, store, days=2, now=MORNING)
    assert "2026-10-30" not in fake.days_requested()
    assert summary.days_total == 2 and store.read("lhb_stocks") is None


def test_empty_answer_for_today_is_not_recorded_but_past_empty_days_are(store):
    fake = FakeEastmoney()
    summary = sync(fake, store, days=2)
    assert summary.days_empty == 2
    assert store.read_doc("meta", "lhb_dates")["dates"] == ["2026-10-29"]
    fake.add_day(FRIDAY, ["600519"])
    sync(fake, store, days=2)
    assert store.read("lhb_stocks").height == 1


def test_trading_days_come_from_index_bars_then_weekdays(store):
    index_days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 23), date(2026, 10, 26)]  # 22nd is a "holiday"
    store.write(
        "index_bars",
        pl.DataFrame(
            {"symbol": "000985", "date": index_days, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        ),
    )
    days = lhb.candidate_days(store, date(2026, 10, 19), FRIDAY)
    assert days == index_days + [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), FRIDAY]
    assert lhb.candidate_days(Warehouse(store.root.parent / "other", "live"), date(2026, 10, 24), date(2026, 10, 28)) == [
        date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)
    ]


def test_latest_day_waits_for_the_evening():
    assert lhb.latest_day(MORNING) == date(2026, 10, 29)
    assert lhb.latest_day(EVENING) == FRIDAY


def test_batches_write_every_batch_days(store, monkeypatch):
    monkeypatch.setattr(lhb, "BATCH_DAYS", 2)
    fake = FakeEastmoney()
    for day in (date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), FRIDAY):
        fake.add_day(day, ["600519"])
    sync(fake, store)
    assert len(store.files("lhb_stocks")) == 2


# --- Sina boards -----------------------------------------------------------------------

NODES = [
    "行情中心",
    [
        [
            "A股",
            [
                ["新浪行业", [["玻璃行业", "", "new_blhy"], ["传媒娱乐", "", "new_cmyl"]]],
                ["概念板块", [["华为汽车", "", "gn_hwqc"], ["BC电池", "", "gn_BCdc"]]],
                ["地域板块", [["新疆维吾尔自治区", "", "diyu_650000"]]],
            ],
        ]
    ],
]


def member(code: str) -> dict:
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return {"symbol": f"{prefix}{code}", "code": code, "name": f"名{code}"}


class FakeSina:
    def __init__(self) -> None:
        self.members: dict[str, list[dict]] = {
            "new_blhy": [member("600001"), member("600002")],
            "new_cmyl": [member("000001")],
            "gn_hwqc": [member(f"60{n:04d}") for n in range(1, 251)],
            "gn_BCdc": [member("300750"), member("123456"), member("sh0")],  # a bond-like code and junk are dropped
        }
        self.nodes: list = NODES
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.status = 200
        self.status_for: str | None = None  # node id that is refused

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append((request.url.path.rsplit(".", 1)[-1], params))
        if request.url.path.endswith("getHQNodes"):
            return httpx.Response(200, content=json.dumps(self.nodes, ensure_ascii=False).encode())
        node = params["node"]
        if self.status != 200 and self.status_for in (None, node):
            return httpx.Response(self.status, text="refused")
        rows = self.members.get(node, [])
        size, page = int(params["num"]), int(params["page"])
        return httpx.Response(200, json=rows[(page - 1) * size : page * size])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def member_requests(self, node: str) -> int:
        return sum(1 for kind, params in self.requests if kind == "getHQNodeData" and params["node"] == node)


def boards_sync(fake: FakeSina, store: Warehouse, **kwargs):
    return boards.run_boards_sync(store=store, client=fake.client(), today=date(2026, 10, 31), **kwargs)


def test_parse_nodes_reads_industry_and_concept_groups_only():
    parsed = boards.parse_nodes(NODES)
    assert [(b.kind, b.board_id, b.name) for b in parsed] == [
        ("industry", "new_blhy", "玻璃行业"),
        ("industry", "new_cmyl", "传媒娱乐"),
        ("concept", "gn_hwqc", "华为汽车"),
        ("concept", "gn_BCdc", "BC电池"),
    ]
    assert boards.parse_nodes([]) == [] and boards.parse_nodes({"unexpected": 1}) == []
    dup = ["x", [["概念板块", [["A", "", "gn_a"], ["A again", "", "gn_a"], ["bad"], ["B", "", ""]]]]]
    assert [b.board_id for b in boards.parse_nodes(dup)] == ["gn_a"]


def test_parse_members_filters_codes_and_falls_back_to_symbol():
    rows = [member("600519"), {"symbol": "sz000001", "name": "x"}, member("123456"), member("600519"), {"code": ""}, "junk"]
    assert boards.parse_members(rows) == ["600519", "000001"]
    assert boards.parse_members(None) == []


def test_members_are_paged_until_a_short_page(store):
    fake = FakeSina()
    codes = boards.fetch_members(fake.client(), "gn_hwqc")
    assert len(codes) == 250 and len(set(codes)) == 250
    assert fake.member_requests("gn_hwqc") == 3
    fake.requests.clear()
    assert boards.fetch_members(fake.client(), "new_cmyl") == ["000001"] and fake.member_requests("new_cmyl") == 1


def test_paging_stops_when_the_server_ignores_the_page_number(monkeypatch):
    monkeypatch.setattr(boards, "PAGE_SIZE", 2)
    fake = FakeSina()
    fake.members["new_blhy"] = [member("600001"), member("600002")]

    def always_first(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fake.members["new_blhy"])

    client = httpx.Client(transport=httpx.MockTransport(always_first))
    assert boards.fetch_members(client, "new_blhy") == ["600001", "600002"]


def test_full_sync_stores_snapshot_and_replaces_the_previous_one(store):
    fake = FakeSina()
    summary = boards_sync(fake, store)
    assert summary.ok and summary.complete and summary.boards_done == 4
    frame = store.read("boards")
    assert frame.height == summary.rows == 2 + 1 + 250 + 1
    assert set(frame["kind"]) == {"industry", "concept"}
    assert frame["as_of"].dtype == pl.Date and frame["as_of"].unique().to_list() == [date(2026, 10, 31)]
    row = frame.filter(pl.col("code") == "300750").row(0, named=True)
    assert (row["kind"], row["board_id"], row["board_name"]) == ("concept", "gn_BCdc", "BC电池")
    fake.members["new_blhy"] = [member("600001")]  # 600002 left the board
    boards_sync(fake, store)
    frame = store.read("boards")
    assert frame.filter(pl.col("board_id") == "new_blhy")["code"].to_list() == ["600001"]


def test_limit_run_appends_and_never_replaces(store):
    fake = FakeSina()
    boards_sync(fake, store)
    full = store.read("boards").height
    summary = boards_sync(fake, store, limit=1)
    assert not summary.complete and not summary.ok and summary.boards_done == 1 and "--limit" in summary.message
    assert store.read("boards").height == full
    assert boards_sync(FakeSina(), Warehouse(store.root.parent / "fresh", "live"), limit=2).rows == 3


def test_blocked_stops_at_once_and_keeps_snapshot(store):
    fake = FakeSina()
    boards_sync(fake, store)
    before = store.read("boards").height
    fake.requests.clear()
    fake.status, fake.status_for = 456, "gn_hwqc"
    fake.members["new_blhy"] = [member("600001")]
    summary = boards_sync(fake, store)
    assert summary.blocked and not summary.ok and summary.boards_done == 2
    assert fake.member_requests("gn_hwqc") == 1  # refused once, not retried, later boards untouched
    assert fake.member_requests("gn_BCdc") == 0
    assert "旧快照未被替换" in summary.message
    after = store.read("boards")
    assert after.height == before  # old rows still there (appended partial = same keys)


def test_failed_board_makes_the_snapshot_incomplete(store):
    fake = FakeSina()
    fake.status, fake.status_for = 500, "new_cmyl"
    summary = boards_sync(fake, store)
    assert summary.errors == 1 and not summary.complete and not summary.blocked
    assert summary.boards_done == 3
    assert store.read("boards") is not None
