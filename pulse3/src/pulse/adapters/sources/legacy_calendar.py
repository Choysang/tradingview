"""Legacy calendar store as a declared source (legacy rows #114 / #115 / #174): the old system's ``events.jsonl``, read only.

Why: the old event calendar (research seeds, Eastmoney calendar rows, rule-computed expiries) is a versioned JSON-lines store. The
forward-log M3 catalyst rule read it from the old install path; pulse3 imports it as a dataset so the rule can read pulse3 data.
Invariants: the file is only read; every revision line is kept (the latest revision per event is chosen at read time, by what was
visible at the cutoff); a naive timestamp of the store is Asia/Shanghai time; a line without the required keys makes the whole file
unreadable (nothing is imported from a half-understood file); the import time is the dataset ``available_at`` (the store content is
not available to pulse3 earlier).
Not covered: syncing the Eastmoney calendar natively (``calendar_service.sync`` does that into the pulse3 calendar tables), editing.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import polars as pl

SOURCE_ID = "legacy_calendar_store"
ENDPOINT_ID = "calendar_events_jsonl"
SCHEMA_VERSION = "legacy-calendar-store-1.0.0"
MAPPING_VERSION = "legacy-calendar-store-map-v1"
CST = ZoneInfo("Asia/Shanghai")
REQUIRED = ("event_id", "revision", "name", "category", "status", "event_date", "first_seen_at")
POLICY = {"timezone": "Asia/Shanghai", "mode": "legacy_store_import_read_only", "files": "events.jsonl (one JSON object per revision)",
          "available_at": "time of import into pulse3", "entitlement_status": "own_file", "rate_limit": "none (file import)",
          "retire": "after the M6 cutover the pulse3 calendar sync (calendar_service) is the only writer"}
SCHEMA = {"event_id": pl.String, "revision": pl.Int64, "supersedes": pl.String, "name": pl.String, "category": pl.String, "region": pl.String,
          "status": pl.String, "event_date": pl.Date, "event_end_date": pl.Date, "event_at": pl.Datetime("us", "UTC"), "event_timezone": pl.String,
          "precision": pl.String, "source_url": pl.String, "source_announcement_date": pl.String, "first_seen_at": pl.Datetime("us", "UTC"),
          "result_known_at": pl.Datetime("us", "UTC"), "themes": pl.String, "instruments": pl.String, "notes": pl.String}


class StoreUnreadable(ValueError):
    pass


def _instant(v: str | None) -> datetime | None:
    if v is None:
        return None
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=CST)).astimezone(timezone.utc)


def _date(v: str | None) -> date | None:
    return None if v is None else date.fromisoformat(v)


def parse_store(raw: bytes) -> pl.DataFrame:
    rows = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            missing = [k for k in REQUIRED if e.get(k) is None]
            if missing:
                raise StoreUnreadable(f"line {n}: missing {missing}")
            rows.append({
                "event_id": str(e["event_id"]), "revision": int(e["revision"]), "supersedes": e.get("supersedes"), "name": e["name"], "category": e["category"],
                "region": e.get("region"), "status": e["status"], "event_date": _date(e["event_date"]), "event_end_date": _date(e.get("event_end_date")),
                "event_at": _instant(e.get("event_at")), "event_timezone": e.get("event_timezone"), "precision": e.get("precision"),
                "source_url": e.get("source_url"), "source_announcement_date": e.get("source_announcement_date"),
                "first_seen_at": _instant(e["first_seen_at"]), "result_known_at": _instant(e.get("result_known_at")),
                "themes": json.dumps(e.get("themes") or [], ensure_ascii=False), "instruments": json.dumps(e.get("instruments") or [], ensure_ascii=False),
                "notes": e.get("notes")})
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, StoreUnreadable):
                raise
            raise StoreUnreadable(f"line {n}: {exc}") from exc
    if not rows:
        raise StoreUnreadable("the store has no events")
    return pl.DataFrame(rows, schema=SCHEMA)
