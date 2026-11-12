"""Official announcement normalization (cninfo ``hisAnnouncement/query``), spec FINAL §5.1, M1-E01/E02/E04.

Why: a listed-company announcement is the issuer's own factual publication.
The system keeps the original document id, title, publish time, URL and the
revision / withdrawal relation. One announcement mirrored by several portals is
ONE origin (``origin_group`` is derived from issuer + date + normalized title,
never from the portal), and an official announcement confirms only its own
publication, not the truth of its contents.

Invariants: ``published_at`` is the source's timestamp with an explicit
precision (midnight-CST stamps are ``date`` precision, never invented seconds);
``available_at`` is the platform's actual normalization time; a changed body
for an existing id is a new revision (``corrected``), a disappeared id is a new
revision ``deleted_at_source`` only when the listing window was fully paged;
title keywords produce *relation hints*, never resolved links; an HTML/login
page or a response missing required keys is NOT data (``NotData``/``SchemaChanged``).
Not covered: HTTP, persistence, PDF text extraction, claim truth verification.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
SOURCE_ID = "cninfo_announcements"
STATIC_BASE = "http://static.cninfo.com.cn/"
DOC_NS = uuid.UUID("a0b1c2d3-4e5f-4a6b-8c7d-0e1f2a3b4c5d")
REQUIRED_KEYS = ("announcementId", "secCode", "announcementTitle", "announcementTime", "adjunctUrl")


class NotData(ValueError):
    """HTTP 200 body that is a login/captcha/HTML page or otherwise not an API payload."""


class SchemaChanged(ValueError):
    """JSON payload lacks keys the registered mapping depends on."""


@dataclass(frozen=True)
class ParsedPage:
    total_records: int | None
    has_more: bool
    records: tuple[dict, ...]
    empty_success: bool


def parse_response(body: bytes) -> ParsedPage:
    text = body.decode("utf-8", errors="replace").lstrip("﻿").strip()
    if not text or text[0] not in "{[":
        raise NotData("body_is_not_json:login_or_html_page")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("body_is_not_valid_json") from exc
    if not isinstance(doc, dict) or "announcements" not in doc:
        raise SchemaChanged("missing_key:announcements")
    anns = doc["announcements"]
    if anns is None:
        anns = []
    if not isinstance(anns, list):
        raise SchemaChanged("announcements_not_a_list")
    for rec in anns:
        missing = [k for k in REQUIRED_KEYS if k not in rec]
        if missing:
            raise SchemaChanged("missing_record_keys:" + ",".join(missing))
    return ParsedPage(doc.get("totalAnnouncement"), bool(doc.get("hasMore")), tuple(anns), not anns)


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title)
    t = re.sub(r"<[^>]+>", "", t)  # highlight markup
    return re.sub(r"\s+", "", t)


_RELATIONS = [
    ("withdraws", re.compile(r"撤回|撤销(?!.*(?:风险警示|退市))")),
    ("corrects", re.compile(r"更正|修订|修正")),
    ("supplements", re.compile(r"补充")),
    ("cancels", re.compile(r"取消|终止")),
]
_CATEGORY_RULES = [
    ("company.earnings_guidance", re.compile(r"业绩预告|业绩快报|业绩说明|年度报告|半年度报告|季度报告|年报|季报")),
    ("company.capital_action", re.compile(r"回购|分红|权益分派|利润分配|减持|增持|股权激励|定向增发|可转债|配股")),
    ("company.contract_order", re.compile(r"中标|合同|订单|框架协议")),
    ("company.ma_governance", re.compile(r"收购|重组|并购|诉讼|仲裁|控制权|董事会决议|股东大会|权益变动")),
]


def relation_hints(title: str) -> list[dict]:
    t = normalize_title(title)
    hints = []
    for kind, rx in _RELATIONS:
        if rx.search(t):
            stripped = re.sub(r"(?:关于|的)?(?:更正|修订|修正|补充|撤回|撤销|取消|终止)(?:公告|说明|的公告)?", "", t)
            hints.append({"relation": kind, "target_title_hint": stripped, "resolved_document_id": None})
    return hints


def event_category(title: str) -> str | None:
    t = normalize_title(title)
    for cat, rx in _CATEGORY_RULES:
        if rx.search(t):
            return cat
    return None


def publish_time(ms: int | None) -> tuple[datetime | None, str]:
    if ms is None:
        return None, "unknown"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    local = dt.astimezone(CST)
    if local.hour == 0 and local.minute == 0 and local.second == 0:
        return dt, "date"
    return dt, "second"


@dataclass(frozen=True)
class AnnouncementDoc:
    source_record_id: str
    sec_code: str
    sec_name: str | None
    title: str
    url: str
    doc_type: str | None
    published_at: datetime | None
    published_time_precision: str
    document_status: str
    revision: int
    document_id: uuid.UUID
    origin_group: str
    content_hash: str
    category: str | None
    hints: tuple[dict, ...]
    field_quality: dict = field(default_factory=dict)
    raw_record: dict = field(default_factory=dict)


def content_hash(rec: dict) -> str:
    keys = ("announcementId", "secCode", "announcementTitle", "announcementTime", "adjunctUrl", "adjunctSize", "adjunctType")
    return hashlib.sha256(json.dumps({k: rec.get(k) for k in keys}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def document_uuid(source_record_id: str) -> uuid.UUID:
    return uuid.uuid5(DOC_NS, f"{SOURCE_ID}:{source_record_id}")


def normalize_record(rec: dict, *, revision: int = 1, document_status: str = "published") -> AnnouncementDoc:
    sid = str(rec["announcementId"])
    title = normalize_title(rec["announcementTitle"])
    published, precision = publish_time(rec.get("announcementTime"))
    day = published.astimezone(CST).date().isoformat() if published else "unknown"
    origin = f"issuer:{rec['secCode']}|{day}|{hashlib.sha1(title.encode()).hexdigest()[:12]}"
    fq = {
        "published_at": {"status": "observed" if published else "missing", "reason": None if published else "source_value_null",
                         "raw_object_id": None, "value_origin": "primary_source" if published else "not_available",
                         "precision_note": None if precision == "second" else f"time precision is {precision}"},
        "title": {"status": "observed", "reason": None, "raw_object_id": None, "value_origin": "primary_source"},
    }
    url = STATIC_BASE + rec["adjunctUrl"].lstrip("/") if rec.get("adjunctUrl") else None
    return AnnouncementDoc(
        source_record_id=sid, sec_code=str(rec["secCode"]), sec_name=rec.get("secName"), title=title,
        url=url or "", doc_type=rec.get("adjunctType"), published_at=published, published_time_precision=precision,
        document_status=document_status, revision=revision, document_id=document_uuid(sid), origin_group=origin,
        content_hash=content_hash(rec), category=event_category(title), hints=tuple(relation_hints(title)),
        field_quality=fq, raw_record=rec)


def next_revision(previous: AnnouncementDoc | None, current: AnnouncementDoc) -> tuple[int, str] | None:
    """(revision, status) to append for ``current``; None when it equals the stored latest version."""
    if previous is None:
        return 1, "published"
    if previous.content_hash == current.content_hash and previous.document_status != "deleted_at_source":
        return None
    return previous.revision + 1, "corrected"


def detect_deleted(previous_ids: Iterable[str], current_ids: Iterable[str], *, window_fully_paged: bool) -> set[str]:
    """Ids that vanished from a fully paged window. Partial windows prove nothing."""
    if not window_fully_paged:
        return set()
    return set(previous_ids) - set(current_ids)


def date_windows(start: datetime, end: datetime, days: int = 1) -> list[tuple[str, str]]:
    """cninfo ``seDate`` windows ('YYYY-MM-DD~YYYY-MM-DD') in CST, overlapping by nothing."""
    out = []
    d = start.astimezone(CST).date()
    last = end.astimezone(CST).date()
    while d <= last:
        e = min(d + timedelta(days=days - 1), last)
        out.append((d.isoformat(), e.isoformat()))
        d = e + timedelta(days=1)
    return out
