"""Dragon-Tiger list (龙虎榜) sync from the Eastmoney datacenter.

Two tables in the live warehouse, money in 亿元 (yuan / 1e8), dates as `pl.Date`:
* `lhb_stocks` - one row per (stock, day, listing reason): close, pct change,
  turnover, the list's buy / sell / net amounts and its share of the day's volume.
* `lhb_seats` - the buy and sell seats (营业部) behind each listing. Several
  institutions share the one seat name "机构专用", so `rank` (order by amount
  within code / day / side / reason) keeps them as separate rows.

Unknown or non-numeric fields are null, never 0. Convertible bonds and B shares
are dropped: only A-share codes are kept.

A day is stored only when its stock list *and* seat details were both fetched;
fetched days (empty ones included, holidays and quiet days have no list) are
recorded in the doc `meta/lhb_dates`, so a rerun resumes without refetching.
The list publishes in the evening: today is not fetched before 18:00 Asia/Shanghai,
and an empty answer for today is not recorded (it may just not be published yet).

Politeness: one host, ~2 requests/s. HTTP 403 / 429 / 501, a redirect or an HTML
page raises `LhbBlocked` at once (no retries) and the job stops, keeping the days
already fetched; a run of consecutive failing days stops it too.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import re
import sys
import time
from typing import Any, Callable

import httpx
import polars as pl

from .store import Warehouse, warehouse
from .tencent import _Pacer, new_client

LOG = logging.getLogger("pulse.warehouse.lhb")

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_STOCKS = "RPT_DAILYBILLBOARD_DETAILSNEW"
REPORT_SEATS = {"buy": "RPT_BILLBOARD_DAILYDETAILSBUY", "sell": "RPT_BILLBOARD_DAILYDETAILSSELL"}
PAGE_SIZE = 500
MAX_PAGES = 40
RETRIES = 2
BACKOFF = 0.5
BATCH_DAYS = 20
MAX_CONSECUTIVE_ERRORS = 10
NO_DATA_CODE = 9201  # "返回数据为空": the normal answer for a day without a list
PUBLISH_HOUR = 18
CST = timezone(timedelta(hours=8))
INDEX_SYMBOL = "000985"
DATES_DOC = ("meta", "lhb_dates")
_PACER = _Pacer(2.0)

A_SHARE = re.compile(r"(00|30|60|68|4|8|92)\d*")
YI = 1e8

STOCK_SCHEMA: dict[str, Any] = {
    "code": pl.Utf8,
    "date": pl.Date,
    "name": pl.Utf8,
    "reason": pl.Utf8,
    "close": pl.Float64,
    "pct_chg": pl.Float64,
    "turnover_rate": pl.Float64,
    "amount": pl.Float64,
    "net_buy": pl.Float64,
    "buy_amt": pl.Float64,
    "sell_amt": pl.Float64,
    "deal_ratio": pl.Float64,
    "free_cap": pl.Float64,
}
SEAT_SCHEMA: dict[str, Any] = {
    "code": pl.Utf8,
    "date": pl.Date,
    "side": pl.Utf8,
    "seat": pl.Utf8,
    "seat_code": pl.Utf8,
    "rank": pl.Int32,
    "reason": pl.Utf8,
    "buy": pl.Float64,
    "sell": pl.Float64,
    "net": pl.Float64,
}

ProgressHook = Callable[[str], None]


class LhbError(RuntimeError):
    """A request kept failing after its retries."""


class LhbBlocked(LhbError):
    """The host refuses us; retrying only extends the block."""


@dataclass(frozen=True)
class LhbSummary:
    days_total: int  # candidate days in the window
    days_skipped: int  # already stored
    days_fetched: int  # fetched this run (empty days included)
    days_empty: int
    stock_rows: int
    seat_rows: int
    errors: int
    blocked: bool
    message: str

    @property
    def ok(self) -> bool:
        return not self.blocked and self.errors == 0


# --- parsing ---------------------------------------------------------------------

def is_a_share(code: str) -> bool:
    return len(code) == 6 and code.isdigit() and A_SHARE.fullmatch(code) is not None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and number == number and abs(number) != float("inf") else None


def _yi(value: Any) -> float | None:
    number = _number(value)
    return number / YI if number is not None else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_stocks(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """A-share rows keyed by (code, date, reason); the last duplicate wins."""
    out: dict[tuple[str, date, str], dict[str, Any]] = {}
    for row in rows:
        code, day = _text(row.get("SECURITY_CODE")), _day(row.get("TRADE_DATE"))
        if not is_a_share(code) or day is None:
            continue
        reason = _text(row.get("EXPLANATION"))
        out[(code, day, reason)] = {
            "code": code,
            "date": day,
            "name": _text(row.get("SECURITY_NAME_ABBR")) or None,
            "reason": reason,
            "close": _number(row.get("CLOSE_PRICE")),
            "pct_chg": _number(row.get("CHANGE_RATE")),
            "turnover_rate": _number(row.get("TURNOVERRATE")),
            "amount": _yi(row.get("ACCUM_AMOUNT")),
            "net_buy": _yi(row.get("BILLBOARD_NET_AMT")),
            "buy_amt": _yi(row.get("BILLBOARD_BUY_AMT")),
            "sell_amt": _yi(row.get("BILLBOARD_SELL_AMT")),
            "deal_ratio": _number(row.get("DEAL_AMOUNT_RATIO")),
            "free_cap": _yi(row.get("FREE_MARKET_CAP")),
        }
    return pl.DataFrame(list(out.values()), schema=STOCK_SCHEMA)


def parse_seats(rows: list[dict[str, Any]], side: str) -> pl.DataFrame:
    """Seat rows of one side. Within a (code, date, reason) group seats are ranked
    by the side's amount (largest first) so that identical seat names stay distinct
    and a refetch reproduces the same ranks."""
    parsed: list[dict[str, Any]] = []
    for row in rows:
        code, day = _text(row.get("SECURITY_CODE")), _day(row.get("TRADE_DATE"))
        seat = _text(row.get("OPERATEDEPT_NAME"))
        if not is_a_share(code) or day is None or not seat:
            continue
        parsed.append(
            {
                "code": code,
                "date": day,
                "side": side,
                "seat": seat,
                "seat_code": _text(row.get("OPERATEDEPT_CODE")) or None,
                "reason": _text(row.get("EXPLANATION")),
                "buy": _yi(row.get("BUY")),
                "sell": _yi(row.get("SELL")),
                "net": _yi(row.get("NET")),
            }
        )
    amount = "buy" if side == "buy" else "sell"

    def order(item: dict[str, Any]) -> tuple:
        value = item[amount]
        return item["code"], item["date"], item["reason"], value is None, -(value or 0.0), item["seat"], item["seat_code"] or ""

    counters: dict[tuple[str, date, str], int] = {}
    for item in sorted(parsed, key=order):
        group = (item["code"], item["date"], item["reason"])
        item["rank"] = counters.get(group, 0)
        counters[group] = item["rank"] + 1
    return pl.DataFrame(parsed, schema=SEAT_SCHEMA)


# --- http ------------------------------------------------------------------------------

def _blocked(response: httpx.Response) -> bool:
    return (
        response.status_code in (403, 429, 501)
        or 300 <= response.status_code < 400
        or response.content[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))
    )


def _get(client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            _PACER.wait()
            response = client.get(URL, params=params)
            if _blocked(response):
                raise LhbBlocked(f"东方财富拒绝访问（HTTP {response.status_code}，限流或风控页），已停止请求，稍后再试")
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                last = LhbError("返回了无法解析的数据")
            else:
                last = LhbError(f"HTTP {response.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
        if attempt < RETRIES:
            time.sleep(BACKOFF * (attempt + 1))
    raise LhbError(f"东方财富请求失败：{last}") from last


def fetch_report(client: httpx.Client, report: str, day: date) -> list[dict[str, Any]]:
    """All rows of one report for one day; a day without data is an empty list."""
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = _get(
            client,
            {
                "reportName": report,
                "columns": "ALL",
                "pageSize": PAGE_SIZE,
                "pageNumber": page,
                # Ties on the first key would reorder between pages and drop rows.
                "sortColumns": "SECURITY_CODE,TRADE_ID" if report == REPORT_STOCKS else "SECURITY_CODE,OPERATEDEPT_CODE,BUY,SELL",
                "sortTypes": "1,1" if report == REPORT_STOCKS else "1,1,-1,-1",
                "filter": f"(TRADE_DATE='{day.isoformat()}')",
            },
        )
        result = payload.get("result")
        if not payload.get("success"):
            if payload.get("code") == NO_DATA_CODE:
                return rows
            raise LhbError(f"{report} 返回失败：{payload.get('message')}")
        if not isinstance(result, dict):
            return rows
        rows.extend(result.get("data") or [])
        if page >= int(result.get("pages") or 0):
            return rows
    raise LhbError(f"{report} {day} 页数超过 {MAX_PAGES}，疑似分页异常")


def fetch_day(client: httpx.Client, day: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(stocks, seats) of one day. Seats are only requested when the day has a list."""
    stocks = parse_stocks(fetch_report(client, REPORT_STOCKS, day))
    if stocks.is_empty():
        return stocks, pl.DataFrame(schema=SEAT_SCHEMA)
    seats = [parse_seats(fetch_report(client, report, day), side) for side, report in REPORT_SEATS.items()]
    return stocks, pl.concat(seats)


# --- trading days ----------------------------------------------------------------------

def latest_day(now: datetime | None = None) -> date:
    """Newest day whose list is published: today from 18:00 Shanghai, else yesterday."""
    local = (now or datetime.now(CST)).astimezone(CST)
    return local.date() if local.hour >= PUBLISH_HOUR else local.date() - timedelta(days=1)


def candidate_days(store: Warehouse, start: date, end: date) -> list[date]:
    """Trading days in [start, end]: dates of the stored index bars where known,
    plus weekdays after the newest index bar (a non-trading weekday there just
    yields an empty list)."""
    known: list[date] = []
    lazy = store.scan("index_bars")
    if lazy is not None:
        symbols = lazy.filter(pl.col("symbol") == INDEX_SYMBOL)
        frame = symbols.select("date").collect()
        if frame.is_empty():
            frame = lazy.select("date").collect()
        known = sorted({day for day in frame["date"].to_list() if start <= day <= end})
    cursor = known[-1] + timedelta(days=1) if known else start
    tail = []
    while cursor <= end:
        if cursor.weekday() < 5:
            tail.append(cursor)
        cursor += timedelta(days=1)
    return known + tail


def _stored_days(store: Warehouse) -> set[date]:
    days: set[date] = set()
    doc = store.read_doc(*DATES_DOC) or {}
    for text in doc.get("dates", []):
        parsed = _day(text)
        if parsed is not None:
            days.add(parsed)
    lazy = store.scan("lhb_stocks")
    if lazy is not None:
        days.update(lazy.select(pl.col("date").unique()).collect()["date"].to_list())
    return days


# --- sync ------------------------------------------------------------------------------

class _Pending:
    """Fetched days waiting for the next batch write. Seats go first, then stocks,
    then the date record, so a recorded or listed day always has its seats."""

    def __init__(self, store: Warehouse) -> None:
        self.store = store
        self.stocks: list[pl.DataFrame] = []
        self.seats: list[pl.DataFrame] = []
        self.days: list[date] = []

    def add(self, day: date, stocks: pl.DataFrame, seats: pl.DataFrame, record: bool) -> None:
        if not stocks.is_empty():
            self.stocks.append(stocks)
            self.seats.append(seats)
        if record:
            self.days.append(day)

    def full(self) -> bool:
        return len(self.days) >= BATCH_DAYS

    def flush(self) -> None:
        if self.seats:
            self.store.write("lhb_seats", pl.concat(self.seats))
        if self.stocks:
            self.store.write("lhb_stocks", pl.concat(self.stocks))
        if self.days:
            doc = self.store.read_doc(*DATES_DOC) or {}
            merged = {day.isoformat() for day in self.days} | set(doc.get("dates", []))
            self.store.write_doc(*DATES_DOC, {"dates": sorted(merged)})
        self.stocks, self.seats, self.days = [], [], []


def run_lhb_sync(
    years: int = 3,
    days: int | None = None,
    store: Warehouse | None = None,
    client: httpx.Client | None = None,
    on_progress: ProgressHook | None = None,
    now: datetime | None = None,
) -> LhbSummary:
    """Fetch the Dragon-Tiger list of the last `years` years (or the most recent
    `days` trading days) into the warehouse. Never raises on provider trouble:
    a block or a run of failures ends the job with the reason in the summary."""
    store = store or warehouse("live")
    owned = client is None
    client = client or new_client()
    say = on_progress or (lambda _message: None)
    end = latest_day(now)
    today = (now or datetime.now(CST)).astimezone(CST).date()
    window = candidate_days(store, end - timedelta(days=365 * years), end)
    if days is not None:
        window = window[-days:] if days > 0 else []
    done = _stored_days(store)
    todo = [day for day in window if day not in done]
    pending = _Pending(store)
    stock_rows = seat_rows = empty = fetched = errors = streak = 0
    blocked = False
    reason = ""
    try:
        for index, day in enumerate(todo, 1):
            try:
                stocks, seats = fetch_day(client, day)
            except LhbBlocked as exc:
                blocked, reason = True, str(exc)
                break
            except LhbError as exc:
                errors += 1
                streak += 1
                LOG.warning("lhb %s failed: %s", day, exc)
                say(f"{day} 获取失败：{exc}")
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    reason = f"连续 {streak} 天请求失败，已停止（{exc}）"
                    break
                continue
            streak = 0
            fetched += 1
            empty += stocks.is_empty()
            stock_rows += stocks.height
            seat_rows += seats.height
            # Today's list may simply not be published yet: leave it unrecorded.
            pending.add(day, stocks, seats, record=not stocks.is_empty() or day < today)
            say(f"龙虎榜 {index}/{len(todo)} {day}：个股 {stocks.height} 条，席位 {seats.height} 条")
            if pending.full():
                pending.flush()
    finally:
        pending.flush()
        if owned:
            client.close()
    parts = [f"龙虎榜{'同步中断' if reason else '同步完成'}：窗口 {len(window)} 个交易日"]
    parts.append(f"已存在跳过 {len(window) - len(todo)} 天，本次拉取 {fetched} 天（其中无榜单 {empty} 天）")
    parts.append(f"写入个股 {stock_rows} 行、席位 {seat_rows} 行")
    if errors:
        parts.append(f"失败 {errors} 天")
    if reason:
        parts.append(f"{reason}；已写入的数据保留，重新运行会从断点续传")
    return LhbSummary(len(window), len(window) - len(todo), fetched, empty, stock_rows, seat_rows, errors, blocked, "；".join(parts))


# --- cli -------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the Eastmoney Dragon-Tiger list into the live warehouse")
    parser.add_argument("--years", type=int, default=3, help="how many years of trading days to cover")
    parser.add_argument("--days", type=int, default=None, help="only the most recent N trading days")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Pipes (CI, subprocess captures) are read as UTF-8; terminals keep their own codec.
            stream.reconfigure(errors="replace", **({} if stream.isatty() else {"encoding": "utf-8"}))
    summary = run_lhb_sync(years=args.years, days=args.days, on_progress=lambda message: print(message, flush=True))
    print(summary.message, flush=True)
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
