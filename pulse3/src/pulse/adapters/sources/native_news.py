"""Step 2 cutover readiness: source and endpoint registration of the native 7x24 news recorder (lane C, rows #131/#187).

Why: until the M6 cutover the old recorder polls Tonghuashun / Eastmoney / CLS 7x24 and pulse3 imports its message files
(``legacy_news_recorder``). At the cutover pulse3's own recorder (``application.native_news_recorder``) takes over and writes the
SAME document store (``intelligence.document_revision`` through ``IntelligenceService``), with the same raw-object format and the
same ``<origin>:<item id>`` record ids, under the native source id ``native_news_recorder``. Claims and the alert scan read claims,
so they work unchanged.
Registered here: the document source/endpoint (``native_news_recorder/news_messages``) and the one feed that had no adapter yet
(``ths_news_724/push_stock``, ``ths_news``). Eastmoney (``eastmoney_news_724/fast_news_list``) and CLS (``cls_telegraph/roll_list_web``)
keep their existing ids: ``IntelligenceService`` already registers them and the recorder reuses their adapters, so they are not
declared a second time.
Invariants: ``register`` only declares specs; it never opens a connection; cadence and breaker numbers below are the old
recorder's (``backend/app/warehouse/news.py``): Tonghuashun every 10 s, Eastmoney and CLS every 30 s, day window 08:00-22:00,
3 consecutive bad answers stop that feed for the day. Here a block (403/429/456/501) stops that host at once, without a retry.
Not covered: the guba follow-up fetches and ``watch.json`` of the old recorder (guba is the community lane's, ``watch.json``
feeds the old intraday recorder only).
"""
from __future__ import annotations

from . import ths_news

SOURCE_ID = "native_news_recorder"
EP_MESSAGES = "news_messages"
SCHEMA_VERSION = "native-news-recorder-20261119"
MAPPING_VERSION = "1.0.0"
RAW_FORMAT = "legacy-news-revision-v1"  # the lossless per-revision raw object keeps the legacy importer's format, so one parser reads both

#: origin ids, identical to the old recorder's ``source_id`` column, so ``source_record_id`` (``<origin>:<item id>``) matches
ORIGIN_THS, ORIGIN_EM, ORIGIN_CLS = "ths.724", "em.724", "cls.telegraph"
POLL_EVERY_SECONDS = {ORIGIN_THS: 10, ORIGIN_EM: 30, ORIGIN_CLS: 30}
BREAK_AFTER = 3  # consecutive bad answers (HTML, vendor error, 5xx, timeout) that stop a feed for the day
STOP_AT = "22:00"  # the old scheduled task started at 08:00 and the process stopped at 22:00 Asia/Shanghai
START_AT = "08:00"

POLICY = {
    "timezone": "Asia/Shanghai", "mode": "native_recorder", "calendar_id": "news_24x7", "entitlement_status": "own_recording_of_public_feeds",
    "source_times": "as the feed states them (epoch seconds or Asia/Shanghai text), converted to UTC; recorded as source_published_at / source_modified_at",
    "first_seen_at": "received_at: the response time of our own poll (the only time trading may use)",
    "available_at": "document write time, right after the poll",
    "revisions": "revision_id = <item>@<sha256(title+body)[:12]>, supersedes = the previous revision id; a changed title/body appends a document revision",
    "cadence_seconds": POLL_EVERY_SECONDS, "window": f"{START_AT}-{STOP_AT} Asia/Shanghai, weekdays",
    "rate_limit": "<=1 request/second per host (one client per host), stop that host for the day on 403/429/456/501, no retry",
    "replaces": "legacy_news_recorder (import of the old recorder's message files) after the M6 cutover",
}


def register(add, spec_cls, sources: dict) -> None:
    """Declare this lane's sources (`sources[id] = (title, base_url)`) and endpoints (`add(spec_cls(...))`)."""
    sources[SOURCE_ID] = ("Native 7x24 news recorder (own polling of public feeds)", "native://pulse3/news-recorder")
    sources.update(ths_news.SOURCE_ROWS)
    add(spec_cls(SOURCE_ID, EP_MESSAGES, SCHEMA_VERSION, MAPPING_VERSION, "EvidenceDocument", "poll", 1800, 10, None, POLICY, True, None))
    add(spec_cls(ths_news.SOURCE_ID, ths_news.ENDPOINT_ID, ths_news.SCHEMA_VERSION, ths_news.MAPPING_VERSION, "EvidenceDocument", "poll", 1800, 10,
                 None, ths_news.POLICY, True, None))
