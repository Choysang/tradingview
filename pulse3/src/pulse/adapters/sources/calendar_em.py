"""Eastmoney 财经日历 adapter (RPT_CPH_FECALENDAR on the public data-center API): economic releases, conferences, policy meetings.

Why: the old site's calendar store synced this report; pulse3 reads it into versioned calendar events (PEX07, D09). Only builds
requests and parses bytes; the caller stores the raw response first.
Invariants: a start time of 00:00:00 means "date only" (``date`` / ``date_range`` precision, never a fake minute); several minutes
listed for one code on one day keep the earliest, marked ``approx_minute``, with all minutes in the notes; the vendor compiles the
list (Choice) - it is not the issuer's notice (said in the notes); a refusal (403/429/456/501) is raised by the shared client, a
non-JSON body is ``NotData``, a body without the expected shape is ``SchemaChanged``; at most one request per second.
Not covered: expected/actual values of macro releases (this report has none), event-to-stock links (derived elsewhere).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pulse.domain.calendar.events import Observation
from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "eastmoney_calendar"
ENDPOINT_ID = "fe_calendar"
SCHEMA_VERSION = "eastmoney-datacenter-fecalendar-20261112"
MAPPING_VERSION = "1.0.0"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"Referer": "https://data.eastmoney.com/cjrl/default.html"}
COLUMNS = "FE_CODE,FE_NAME,FE_TYPE,START_DATE,END_DATE,CITY,SPONSOR_NAME"
CST = ZoneInfo("Asia/Shanghai")
SOURCE_ROWS = {SOURCE_ID: ("Eastmoney 财经日历 (Choice compilation)", "https://data.eastmoney.com/cjrl/default.html")}
POLICY = {"timezone": "Asia/Shanghai", "calendar_id": "event_calendar", "empty_result": "no rows is success_empty", "pagination": "pageNumber/pageSize",
          "entitlement_status": "public_read_candidate_unconfirmed", "rate_limit": "<=1 request/second, stop on 403/429/456/501",
          "compiled_by": "Eastmoney Choice, not the issuer's notice"}
SPEC = EndpointSpec(SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION, "CalendarEvent", "poll", 86400, 21600, None, POLICY, True, None)
PAGE_SIZE = 200
MAX_PAGES = 10


def query_url(start: date, days: int, page: int = 1) -> str:
    stop = start + timedelta(days=days)
    q = {"reportName": "RPT_CPH_FECALENDAR", "columns": COLUMNS, "pageNumber": page, "pageSize": PAGE_SIZE, "sortColumns": "START_DATE", "sortTypes": 1,
         "filter": f"(END_DATE>='{start}')(START_DATE<'{stop}')", "source": "WEB", "client": "WEB"}
    return f"{BASE}?{urlencode(q, quote_via=quote, safe='()')}"


class CalendarAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, start: date, days: int, page: int = 1) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"calendar:{start}:{days}:p{page}",
                                   url=query_url(start, days, page), headers=HEADERS)


def parse_page(body: bytes) -> tuple[list[dict], int]:
    """(rows, total pages). ``NotData`` for non-JSON, ``SchemaChanged`` for an unexpected shape."""
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise NotData("calendar answered non-JSON") from e
    if not isinstance(doc, dict) or "result" not in doc:
        raise SchemaChanged("calendar answer has no 'result'")
    res = doc["result"]
    if res is None:                                   # the vendor's valid empty answer
        return [], 1
    if not isinstance(res, dict) or not isinstance(res.get("data"), list):
        raise SchemaChanged("calendar result has no data list")
    rows = [r for r in res["data"] if isinstance(r, dict) and r.get("FE_CODE") and r.get("START_DATE") and r.get("FE_NAME")]
    return rows, int(res.get("pages") or 1)


def _category(kind: str, name: str) -> str:
    if "会议" in kind or "论坛" in name or "大会" in name or "博览会" in name:
        return "policy_meeting" if ("政治局" in name or "国务院" in name or "央行" in name or "央行" in kind) else "conference"
    if "数据" in kind or any(w in name for w in ("CPI", "PPI", "GDP", "PMI", "失业率", "零售", "产出", "贸易帐", "利率决议")):
        return "macro_release"
    if "指数" in name:
        return "index_adjustment"
    return "overseas" if not _is_cn(name) else "other"


def _is_cn(name: str) -> bool:
    return name.startswith(("中国", "国家", "全国", "上海", "深圳", "北京"))


def _region(name: str, city: str | None) -> str:
    for p, r in (("中国香港", "HK"), ("美国", "US"), ("日本", "JP"), ("韩国", "KR"), ("欧元区", "EU"), ("德国", "EU"), ("法国", "EU")):
        if name.startswith(p):
            return r
    return "CN" if _is_cn(name) or city else "GLOBAL"


def to_observations(rows: list[dict]) -> list[Observation]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{r['FE_CODE']}:{str(r['START_DATE'])[:10]}", []).append(r)
    out = []
    for key, g in sorted(groups.items()):
        g.sort(key=lambda r: str(r["START_DATE"]))
        r = g[0]
        start, end = datetime.fromisoformat(str(r["START_DATE"])), datetime.fromisoformat(str(r["END_DATE"] or r["START_DATE"]))
        times = sorted({str(x["START_DATE"])[11:16] for x in g})
        dated = start.time() == datetime.min.time()
        kind, city = str(r.get("FE_TYPE") or ""), r.get("CITY") or None
        notes = {"source_type": kind or None, "city": city, "sponsor": r.get("SPONSOR_NAME") or None, "compiled_by": "Eastmoney Choice, not the issuer's notice"}
        if dated:
            out.append(Observation(SOURCE_ID, key, str(r["FE_NAME"]).strip(), _category(kind, str(r["FE_NAME"])), _region(str(r["FE_NAME"]), city), None, start.date(),
                                   "date" if end.date() == start.date() else "date_range", notes=notes))
        else:
            multi = len(times) > 1
            if multi:
                notes["all_times_listed"] = times
            out.append(Observation(SOURCE_ID, key, str(r["FE_NAME"]).strip(), _category(kind, str(r["FE_NAME"])), _region(str(r["FE_NAME"]), city),
                                   start.replace(tzinfo=CST), None, "approx_minute" if multi else "minute", notes=notes))
    return out
