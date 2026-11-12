"""Pure parsers for the six intelligence feeds (bytes -> typed records), spec sources_sectors §4.1 / DQ-SOURCE-01,09.

Why: HTTP 200 does not mean data. Each parser distinguishes (a) a valid, possibly empty payload,
(b) a non-data shell (login page, HTML where JSON is expected) -> ``NotData``, (c) a payload whose keys
no longer match the registered mapping -> ``SchemaChanged``. ``empty`` is only returned for a body the
vendor explicitly marks as "no data" (never for a failure).

Invariants: no IO, no clock; timestamps are converted to UTC from the vendor's declared zone
(Asia/Shanghai unless the field is epoch seconds); amounts stay as vendor strings/Decimals; missing
values stay None. Raw record dicts are kept untouched for the audit trail.
Not covered: HTTP, persistence, claim extraction.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pulse.domain.evidence.announcements import NotData, SchemaChanged

CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def _json(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace").lstrip("﻿").strip()
    if not text or text[0] not in "{[":
        raise NotData("body_is_not_json:login_or_html_page")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("body_is_not_valid_json") from exc


def _cst(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(UTC)


# ---- news ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NewsRecord:
    source_id: str
    source_record_id: str
    title: str
    body: str
    published_at: datetime | None
    published_time_precision: str
    stock_refs: tuple[str, ...]  # vendor refs verbatim, e.g. "1.605136" / "sz300302" / "90.BK0800"
    own_dispatch: bool  # item states the outlet itself as the reporter ("财联社11月11日电")
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedNews:
    records: tuple[NewsRecord, ...]
    next_cursor: str | None
    empty_success: bool


EM_REQUIRED = ("code", "title", "summary", "showTime")


def parse_eastmoney_news(body: bytes, source_id: str = "eastmoney_news_724") -> ParsedNews:
    doc = _json(body)
    if not isinstance(doc, dict) or "data" not in doc:
        raise SchemaChanged("missing_key:data")
    if str(doc.get("code")) != "1":
        raise SchemaChanged(f"vendor_code:{doc.get('code')}")
    data = doc["data"]
    if not isinstance(data, dict) or "fastNewsList" not in data:
        raise SchemaChanged("missing_key:fastNewsList")
    items = data["fastNewsList"] or []
    recs = []
    for it in items:
        missing = [k for k in EM_REQUIRED if k not in it]
        if missing:
            raise SchemaChanged("missing_record_keys:" + ",".join(missing))
        recs.append(NewsRecord(source_id, str(it["code"]), str(it["title"]), str(it["summary"]), _cst(it["showTime"]), "second",
                               tuple(str(s) for s in (it.get("stockList") or [])), False, it))
    sort_end = data.get("sortEnd")
    return ParsedNews(tuple(recs), str(sort_end) if sort_end else None, not recs)


CLS_REQUIRED = ("id", "ctime", "content")
_CLS_BRACKET = re.compile(r"^【(.+?)】")


def parse_cls_roll(body: bytes, source_id: str = "cls_telegraph") -> ParsedNews:
    doc = _json(body)
    if not isinstance(doc, dict) or "errno" not in doc:
        raise SchemaChanged("missing_key:errno")
    if doc["errno"] != 0:
        raise SchemaChanged(f"vendor_errno:{doc['errno']}")
    data = doc.get("data")
    if not isinstance(data, dict) or "roll_data" not in data:
        raise SchemaChanged("missing_key:roll_data")
    recs, oldest = [], None
    for it in data["roll_data"] or []:
        missing = [k for k in CLS_REQUIRED if k not in it]
        if missing:
            raise SchemaChanged("missing_record_keys:" + ",".join(missing))
        content = str(it["content"])
        title = str(it.get("title") or "")
        if not title:
            m = _CLS_BRACKET.match(content)
            title = m.group(1) if m else content[:40]
        ts = datetime.fromtimestamp(int(it["ctime"]), tz=UTC)
        refs = tuple(str(s.get("StockID")) for s in (it.get("stock_list") or []) if isinstance(s, dict) and s.get("StockID"))
        recs.append(NewsRecord(source_id, str(it["id"]), title, content, ts, "second", refs, bool(re.match(r"^财联社\d{1,2}月\d{1,2}日电", content)), it))
        oldest = int(it["ctime"]) if oldest is None else min(oldest, int(it["ctime"]))
    return ParsedNews(tuple(recs), str(oldest) if oldest is not None else None, not recs)


def stock_code_from_ref(ref: str) -> str | None:
    """A-share 6-digit code from a vendor ref (``1.605136`` / ``sz300302``); None for sectors, foreign tickers, indexes."""
    m = re.fullmatch(r"(?P<mk>[01])\.(?P<c>\d{6})", ref)
    if m:
        code, ok = m["c"], (m["c"][0] == "6" if m["mk"] == "1" else m["c"][0] in "0348")
        return code if ok else None  # "1.000001" is the SSE index, not an equity
    m = re.fullmatch(r"(?P<mk>sh|sz|bj)(?P<c>\d{6})", ref)
    if m:
        code = m["c"]
        ok = {"sh": code[0] == "6", "sz": code[0] in "03", "bj": code[0] in "489"}[m["mk"]]
        return code if ok else None
    return None


# ---- guba ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GubaPost:
    post_id: str
    bar_code: str
    title: str
    user_id: str | None
    published_at: datetime
    last_activity_at: datetime | None
    click_count: int | None
    comment_count: int | None
    forward_count: int | None
    pinned: bool
    bullish_bearish: int | None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedGuba:
    posts: tuple[GubaPost, ...]
    total_in_bar: int | None
    empty_success: bool


def parse_guba_list(body: bytes) -> ParsedGuba:
    text = body.decode("utf-8", errors="replace")
    marker = "var article_list="
    i = text.find(marker)
    if i < 0:
        raise NotData("no_article_list_literal:login_captcha_or_changed_page")
    try:
        doc, _ = json.JSONDecoder().raw_decode(text[i + len(marker):])
    except json.JSONDecodeError as exc:
        raise SchemaChanged("article_list_not_json") from exc
    if not isinstance(doc, dict) or "re" not in doc:
        raise SchemaChanged("missing_key:re")
    if doc.get("rc") not in (None, 1, 0) or (doc.get("rc") == 0 and not doc["re"] and "繁忙" in str(doc.get("me", ""))):
        raise NotData(f"vendor_busy:{doc.get('me')}")
    posts = []
    for p in doc["re"] or []:
        for k in ("post_id", "stockbar_code", "post_title", "post_publish_time"):
            if k not in p:
                raise SchemaChanged(f"missing_record_key:{k}")
        posts.append(GubaPost(
            str(p["post_id"]), str(p["stockbar_code"]), str(p["post_title"]), str(p["user_id"]) if p.get("user_id") else None,
            _cst(p["post_publish_time"]), _cst(p["post_last_time"]) if p.get("post_last_time") else None,
            p.get("post_click_count"), p.get("post_comment_count"), p.get("post_forward_count"), bool(p.get("post_top_status")),
            p.get("bullish_bearish"), p))
    return ParsedGuba(tuple(posts), doc.get("count"), not posts)


# ---- longhubang ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LhbDoc:
    rows: tuple[dict, ...]
    pages: int
    empty_success: bool


def parse_lhb(body: bytes, required: tuple[str, ...]) -> LhbDoc:
    doc = _json(body)
    if not isinstance(doc, dict) or "success" not in doc or "code" not in doc:
        raise SchemaChanged("missing_keys:success/code")
    if doc["code"] == 9201 and not doc.get("result"):  # vendor's explicit "no data for this filter"
        return LhbDoc((), 0, True)
    if not doc["success"] or not isinstance(doc.get("result"), dict):
        raise SchemaChanged(f"vendor_error:{doc.get('code')}:{doc.get('message')}")
    rows = doc["result"].get("data") or []
    for r in rows:
        missing = [k for k in required if k not in r]
        if missing:
            raise SchemaChanged("missing_record_keys:" + ",".join(missing))
    return LhbDoc(tuple(rows), int(doc["result"].get("pages") or 0), not rows)


LHB_LIST_REQUIRED = ("SECURITY_CODE", "TRADE_DATE", "TRADE_ID", "EXPLANATION")
LHB_SEAT_REQUIRED = ("SECURITY_CODE", "TRADE_DATE", "TRADE_ID", "OPERATEDEPT_NAME", "BUY", "SELL", "EXPLANATION")


def parse_lhb_list(body: bytes) -> LhbDoc:
    return parse_lhb(body, LHB_LIST_REQUIRED)


def parse_lhb_seats(body: bytes) -> LhbDoc:
    return parse_lhb(body, LHB_SEAT_REQUIRED)


# ---- fund flow -----------------------------------------------------------------------------------------
# fields2 order f51..f65 of fflow/daykline (verified on the recorded capture: main = large + super-large)
FFLOW_COLUMNS = ("date", "main_net", "small_net", "medium_net", "large_net", "super_large_net",
                 "main_ratio", "small_ratio", "medium_ratio", "large_ratio", "super_large_ratio", "close", "pct_chg", "f64", "f65")


@dataclass(frozen=True)
class FundFlowDay:
    code: str
    trade_date: str
    values: dict[str, Decimal | None]  # keys: FFLOW_COLUMNS[1:11]


@dataclass(frozen=True)
class ParsedFundFlow:
    code: str
    name: str | None
    days: tuple[FundFlowDay, ...]
    empty_success: bool


def _dec(s: str) -> Decimal | None:
    s = s.strip()
    if s in ("", "-", "--"):
        return None
    return Decimal(s)


def parse_fundflow(body: bytes) -> ParsedFundFlow:
    doc = _json(body)
    if not isinstance(doc, dict) or "rc" not in doc:
        raise SchemaChanged("missing_key:rc")
    if doc["rc"] != 0:
        raise SchemaChanged(f"vendor_rc:{doc['rc']}")
    data = doc.get("data")
    if data is None:
        return ParsedFundFlow("", None, (), True)
    if "klines" not in data or "code" not in data:
        raise SchemaChanged("missing_key:klines/code")
    days = []
    for line in data["klines"] or []:
        parts = line.split(",")
        if len(parts) != len(FFLOW_COLUMNS):
            raise SchemaChanged(f"kline_columns:{len(parts)}")
        vals = {k: _dec(v) for k, v in zip(FFLOW_COLUMNS[1:11], parts[1:11])}
        days.append(FundFlowDay(str(data["code"]), parts[0], vals))
    return ParsedFundFlow(str(data["code"]), data.get("name"), tuple(days), not days)
