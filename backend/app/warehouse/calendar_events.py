"""Event calendar store: research seeds in, versioned CalendarEvent rows out, plus rule-computed expiries.

`import_seed` reads a research seed file (schema calendar_research_seed.v1) and appends one CalendarEvent per
event to `<data_dir>/calendar/events.jsonl`. first_seen_at is the import time: a seed's research dates prove
what the researcher saw, not what this system knew, so nothing is back-filled. Re-importing an unchanged
event adds nothing; a changed one becomes the next revision.
`expiries(year, month)` computes 股指期货 / 股指期权 (third Friday) and ETF option (fourth Wednesday) expiry
dates, moved to the next exchange trading day; they stay RULE_COMPUTED until the monthly notice confirms them.
`sync` pulls the Eastmoney 财经日历 (data.eastmoney.com/cjrl, report RPT_CPH_FECALENDAR, compiled from Choice):
economic releases at the minute (Beijing time), conferences, policy meetings and visits, for the next `days`
days. A time of 00:00:00 there means "date only", so those rows get DATE / DATE_RANGE precision, never a
minute. The event id is the source code plus the date, because the source sometimes lists one code on two
dates (2026-11-02: three US EIA weekly reports on both 11-11 and 11-12). It also lists some releases at two
minutes of one day (Hong Kong GDP at 14:30 and 16:30): those keep the earliest minute, marked approximate,
with both in notes. A changed row becomes a revision, a
row that disappears is left as it was (absence is not a cancellation). Region and category are mapped from
the name prefix, the source type and the city, and the raw type is kept in notes. One request per page, 1 s
apart; a refusal (403 / 429 / 501 or a non-JSON answer) stops the sync with an error.
Usage: python -m backend.app.warehouse.calendar_events import <seed.json> | sync [days] | expiries <year> <month>
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from contracts.calendar import CalendarEvent, EventCategory, Milestone, Precision, ScheduleStatus

from ..config import settings
from ..market.calendar import load_calendar

CATEGORY = {"election": EventCategory.OVERSEAS_POLICY, "earnings_call": EventCategory.OVERSEAS_MARKET,
            "earnings_release": EventCategory.OVERSEAS_MARKET, "conference": EventCategory.CONFERENCE,
            "macro_release": EventCategory.MACRO_RELEASE, "macro_release_bundle": EventCategory.MACRO_RELEASE,
            "derivatives_expiry": EventCategory.MARKET_RULE, "central_bank": EventCategory.OVERSEAS_POLICY,
            "market_session": EventCategory.OVERSEAS_MARKET}


def store_path() -> Path:
    return settings.data_dir / "calendar" / "events.jsonl"


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def from_seed(e: dict, seen: datetime) -> CalendarEvent:
    precision = Precision(e["event_precision"])
    region = e["region"] if e["region"] in ("CN", "HK", "US", "JP", "KR", "EU") else "GLOBAL"
    category = CATEGORY.get(e["category"], EventCategory.OVERSEAS_MARKET if region != "CN" else EventCategory.POLICY_MEETING)
    if region == "CN" and e["category"] == "central_bank":
        category = EventCategory.POLICY_MEETING
    milestones = []
    for m in e.get("milestones") or []:
        p = Precision(m["precision"])
        at = _dt(m.get("local")) if p in (Precision.MINUTE, Precision.APPROX_MINUTE) else None
        first = (m.get("local_dates") or [None])[0]
        milestones.append(Milestone(name=m["name"], event_at=at, event_date=date.fromisoformat(first) if first else None, precision=p))
    return CalendarEvent(
        event_id=e["id"], revision=0, name=e["name"], category=category, region=region,
        status=ScheduleStatus(e["status"]), event_date=date.fromisoformat(e["event_local_date"]),
        event_end_date=date.fromisoformat(e["event_local_end_date"]) if e.get("event_local_end_date") else None,
        event_at=_dt(e.get("event_local_datetime")) if precision in (Precision.MINUTE, Precision.APPROX_MINUTE) else None,
        event_timezone=e.get("event_timezone"), precision=precision, milestones=tuple(milestones), source_url=e["source_url"],
        source_announcement_date=date.fromisoformat(e["source_announcement_date"]) if e.get("source_announcement_date") else None,
        schedule_known_at=_dt(e.get("source_published_at")), first_seen_at=seen, notes=e.get("notes") or "")


def load(path: Path | None = None) -> dict[str, CalendarEvent]:
    """Latest revision of every event."""
    path = path or store_path()
    out: dict[str, CalendarEvent] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            ev = CalendarEvent.model_validate_json(line)
            if ev.event_id not in out or ev.revision > out[ev.event_id].revision:
                out[ev.event_id] = ev
    return out


def _content(ev: CalendarEvent) -> dict:
    return ev.model_dump(mode="json", exclude={"revision", "supersedes", "first_seen_at"})


def import_seed(seed: Path, path: Path | None = None, now: datetime | None = None) -> tuple[int, int]:
    """Append new events and changed revisions; returns (new, revised)."""
    now = now or datetime.now(timezone.utc)
    data = json.loads(seed.read_text(encoding="utf-8"))
    if data.get("schema_version") != "calendar_research_seed.v1":
        raise ValueError(f"unknown seed schema {data.get('schema_version')!r}")
    return append([from_seed(e, now) for e in data["events"]], path)


def append(events: list[CalendarEvent], path: Path | None = None) -> tuple[int, int]:
    """Append events not stored yet and changed ones as the next revision; returns (new, revised)."""
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load(path)
    new = revised = 0
    lines = []
    for ev in events:
        old = current.get(ev.event_id)
        if old is None:
            new += 1
        elif _content(old) != _content(ev):
            ev = ev.model_copy(update={"revision": old.revision + 1, "supersedes": old.revision})
            revised += 1
        else:
            continue
        current[ev.event_id] = ev  # a source listing one id twice yields one revision, not two rows of revision 0
        lines.append(ev.model_dump_json())
    if lines:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return new, revised


EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_PAGE = "https://data.eastmoney.com/cjrl/default.html"
EM_COLUMNS = "START_DATE,END_DATE,FE_CODE,FE_NAME,FE_TYPE,CONTENT,STD_TYPE_CODE,SPONSOR_NAME,CITY"
EM_PAGE_SIZE, EM_PACE, EM_MAX_PAGES = 500, 1.0, 10
CST = timezone(timedelta(hours=8))
# The source's CITY column holds a country or region for economic data and a city for meetings.
REGION_OF = {"中国": "CN", "中国香港": "HK", "美国": "US", "日本": "JP", "韩国": "KR", "欧盟": "EU", "欧元区": "EU",
             "德国": "EU", "法国": "EU", "意大利": "EU", "西班牙": "EU"}
CONFERENCE_TYPES = ("行业会议", "高峰论坛", "世界互联网大会")


def _region(name: str, city: str | None) -> str:
    if city:
        if city in REGION_OF:
            return REGION_OF[city]
        return "CN" if city.endswith(("市", "县", "省", "自治区")) else "GLOBAL"
    for place in sorted(REGION_OF, key=len, reverse=True):  # no CITY: the name prefix, longest first (中国香港 before 中国)
        if name.startswith(place):
            return REGION_OF[place]
    return "GLOBAL"


def _category(kind: str, name: str, region: str) -> EventCategory:
    if kind == "经济数据":
        if "利率决议" in name:
            return EventCategory.POLICY_MEETING if region == "CN" else EventCategory.OVERSEAS_POLICY
        return EventCategory.MACRO_RELEASE
    if kind in CONFERENCE_TYPES:
        return EventCategory.CONFERENCE
    if "财报" in name or "业绩" in name:
        return EventCategory.COMPANY if region == "CN" else EventCategory.OVERSEAS_MARKET
    if "指数" in name:
        return EventCategory.SHARE_INDEX
    if kind == "其他会议":
        return EventCategory.POLICY_MEETING if region == "CN" else EventCategory.OVERSEAS_POLICY
    return EventCategory.CONFERENCE if region == "CN" else EventCategory.OVERSEAS_MARKET


def _em_id(row: dict) -> str:
    return f"EM-{row['FE_CODE']}-{row['START_DATE'][:10].replace('-', '')}"


def from_em(row: dict, seen: datetime, times: tuple[str, ...] = ()) -> CalendarEvent:
    """One source row; `times` are the other minutes the source lists for the same id on that day."""
    start, end = datetime.fromisoformat(row["START_DATE"]), datetime.fromisoformat(row["END_DATE"])
    name, kind, city = str(row["FE_NAME"]).strip(), row.get("FE_TYPE") or "", row.get("CITY") or None
    region = _region(name, city)
    dated = start.time() == datetime.min.time()
    precision = (Precision.DATE if end.date() == start.date() else Precision.DATE_RANGE) if dated \
        else Precision.APPROX_MINUTE if times else Precision.MINUTE
    notes = "; ".join(x for x in (f"source type {kind or 'none'}", f"sponsor {row['SPONSOR_NAME']}" if row.get("SPONSOR_NAME") else "",
                                    f"place {city}" if city else "",
                                    f"source lists several times: {', '.join(times)}; the earliest is kept" if times else "",
                                    "compiled by Eastmoney Choice, not the issuer's notice") if x)
    return CalendarEvent(
        event_id=_em_id(row), revision=0, name=name, category=_category(kind, name, region),
        region=region, status=ScheduleStatus.OFFICIAL, event_date=start.date(),
        event_end_date=end.date() if end.date() != start.date() else None,
        event_at=None if dated else start.replace(tzinfo=CST), event_timezone="Asia/Shanghai", precision=precision,
        source_url=EM_PAGE, first_seen_at=seen, notes=notes)


def from_em_rows(rows: list[dict], seen: datetime) -> list[CalendarEvent]:
    """One event per id: when the source lists one release at several minutes of a day, the earliest one, marked
    approximate, so repeated syncs give the same content and no spurious revisions."""
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        by_id.setdefault(_em_id(r), []).append(r)
    out = []
    for group in by_id.values():
        group.sort(key=lambda r: r["START_DATE"])
        times = sorted({r["START_DATE"][11:16] for r in group})
        out.append(from_em(group[0], seen, tuple(times) if len(times) > 1 else ()))
    return out


def fetch_em(client: httpx.Client, start: date, days: int) -> list[dict]:
    stop = start + timedelta(days=days)
    rows: list[dict] = []
    for page in range(1, EM_MAX_PAGES + 1):
        params = {"reportName": "RPT_CPH_FECALENDAR", "columns": EM_COLUMNS, "pageNumber": str(page),
                  "pageSize": str(EM_PAGE_SIZE), "sortColumns": "START_DATE", "sortTypes": "1",
                  "filter": f"(END_DATE>='{start}')(START_DATE<'{stop}')", "source": "WEB", "client": "WEB"}
        response = client.get(EM_URL, params=params)
        if response.status_code in (403, 429, 501):
            raise RuntimeError(f"Eastmoney calendar refused: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Eastmoney calendar answered non-JSON (HTTP {response.status_code})") from exc
        result = (payload or {}).get("result") or {}
        rows += [r for r in (result.get("data") or []) if isinstance(r, dict) and r.get("FE_CODE") and r.get("START_DATE")]
        if page >= int(result.get("pages") or 1):
            break
        time.sleep(EM_PACE)
    return rows


def sync(days: int = 90, path: Path | None = None, now: datetime | None = None,
         client: httpx.Client | None = None) -> tuple[int, int]:
    """Pull the Eastmoney calendar for the next `days` days into the store; returns (new, revised)."""
    now = now or datetime.now(timezone.utc)
    own = client is None
    client = client or httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
    try:
        rows = fetch_em(client, now.astimezone(CST).date(), days)
    finally:
        if own:
            client.close()
    return append(from_em_rows(rows, now), path)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def expiries(year: int, month: int, is_trading_day=None) -> dict[str, date]:
    """Rule-computed expiry dates of one month, rolled forward over exchange holidays."""
    is_trading_day = is_trading_day or load_calendar().is_trading_day
    out = {}
    for name, weekday, n in (("index_futures_options", 4, 3), ("etf_options", 2, 4)):
        d = _nth_weekday(year, month, weekday, n)
        for _ in range(15):
            if is_trading_day(d):
                break
            d += timedelta(days=1)
        out[name] = d
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["import"] and len(argv) == 2:
        new, revised = import_seed(Path(argv[1]))
        print(f"calendar: {new} new, {revised} revised -> {store_path()}")
        return 0
    if argv[:1] == ["sync"] and len(argv) <= 2:
        new, revised = sync(int(argv[1]) if len(argv) == 2 else 90)
        print(f"calendar sync: {new} new, {revised} revised -> {store_path()}")
        return 0
    if argv[:1] == ["expiries"] and len(argv) == 3:
        print({k: v.isoformat() for k, v in expiries(int(argv[1]), int(argv[2])).items()})
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
