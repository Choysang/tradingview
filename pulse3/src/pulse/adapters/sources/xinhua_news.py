"""Xinhua Finance / Xinhuanet Authority News Adapter (Tier-1 Official Outlet).

Why: Multi-source cross-verification requires a Tier-1 central state media anchor
alongside CNINFO statutory disclosures (Tier-1), CLS telegraph (Tier-2), and
Eastmoney / Tonghuashun commercial feeds (Tier-3).
Invariants:
* Strictly uses ``RateLimitedClient`` (<= 1 request/second per host, stop immediately
  on 403/429/456/501 with zero retry storms).
* Cursor traversal uses ``skip`` (never ``break``) so pinned/top stories never stall
  downstream incremental items.
* Raw payload is captured before parsing; missing fields remain empty/None, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .http import Capture, RateLimitedClient

CST = ZoneInfo("Asia/Shanghai")
SOURCE_ID = "xinhua_finance"
ENDPOINT_ID = "fortune_roll_json"
URL = "https://www.news.cn/fortune/datasource/page.json"
SCHEMA_VERSION = "xinhua-fortune-roll-20261126"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://www.news.cn/fortune/"}
SOURCE_ROWS = {
    SOURCE_ID: ("Xinhuanet Finance official public roll (Tier-1 central media)", "https://www.news.cn/fortune/")
}

POLICY = {
    "timezone": "Asia/Shanghai",
    "calendar_id": "news_24x7",
    "authority_tier": "tier1_official",
    "empty_result": "success_empty is not an error",
    "cursor_semantics": "skip seen ids (never break on pinned top items)",
    "rate_limit": "<=1 request/second per host, stop on 403/429/456/501",
}


def register(add, endpoint_spec_cls, sources: dict) -> None:
    """Register Tier-1 Xinhua Finance endpoint into the central source registry without circular import."""
    sources.update(SOURCE_ROWS)
    add(endpoint_spec_cls(
        SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION,
        "EvidenceDocument", "poll", 3600, 300, None, POLICY, True, None,
    ))

_CODE_RE = re.compile(r"(?<!\d)((?:60[0135]\d{3}|00[0123]\d{3}|30[01]\d{3}|688\d{3}))(?!\d)")
_DT_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})[\sT]+(\d{2}):(\d{2})(?::(\d{2}))?")


def _parse_cst(text: str | None) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    m = _DT_RE.search(text.strip())
    if not m:
        return None
    yr, mo, dy, hh, mm, ss = (
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), int(m.group(6) or 0),
    )
    return datetime(yr, mo, dy, hh, mm, ss, tzinfo=CST).astimezone(timezone.utc)


class XinhuaNewsAdapter:
    """Rate-limited adapter for Xinhuanet Finance public JSON feed."""

    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self) -> Capture:
        return self.client.request(
            source_id=SOURCE_ID,
            endpoint_id=ENDPOINT_ID,
            request_key="fortune_roll:latest",
            url=URL,
            headers=HEADERS,
        )


def recorder_items(body: bytes, seen_ids: set[str] | None = None) -> tuple[list[dict], int]:
    """Parse Xinhua Finance roll JSON into canonical news recorder items.

    Returns ``(new_items, skipped_by_cursor)``. Crucially uses ``skip`` instead of ``break``
    when encountering an already-seen ``item_id``, preventing pinned headline articles from
    starving newer dispatches lower in the feed.
    """
    from .news_items import BadAnswer, bare_codes

    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadAnswer(f"xinhua invalid json: {type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise BadAnswer("xinhua root is not a JSON object")
    rows = doc.get("datasource")
    if rows is None and isinstance(doc.get("data"), dict):
        rows = doc["data"].get("list")
    if not isinstance(rows, list):
        raise BadAnswer("xinhua missing datasource/list array")

    seen = seen_ids or set()
    items: list[dict] = []
    skipped = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw_id = str(r.get("contentId") or r.get("docid") or r.get("id") or "").strip()
        title = str(r.get("title") or r.get("showTitle") or "").strip()
        summary = str(r.get("summary") or r.get("abstract") or r.get("description") or "").strip()
        if not raw_id and title:
            raw_id = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        if not raw_id or not title:
            continue
        if raw_id in seen:
            skipped += 1
            continue  # REDLINE: skip, never break!
        pub_at = _parse_cst(str(r.get("publishTime") or r.get("pubtime") or ""))
        found_codes = _CODE_RE.findall(f"{title} {summary}")
        items.append({
            "item_id": raw_id,
            "title": title,
            "body": summary or title,
            "url": r.get("publishUrl") or r.get("linkUrl") or None,
            "source_published_at": pub_at,
            "source_modified_at": None,
            "codes": bare_codes(found_codes),
            "comments": None,
            "level": "official_tier1",
        })
    return items, skipped
