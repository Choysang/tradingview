"""Tonghuashun 7x24 push feed adapter (``news/push/stock``) for the native news recorder.

Why: the old recorder's first and fastest source (probe 2026-11-02: first seen 1.8-8 s after publication, items carry linked
stock codes). pulse3 had no Tonghuashun adapter; the native recorder needs one to replace the old process at the cutover.
This is the public web page's JSON endpoint, NOT an official API, and it is registered as such (see ``native_news``).

Invariants: only builds the request (raw bytes are kept by the caller first); no credentials, no signature; item mapping is the
old recorder's: ``ctime`` (epoch seconds) is the publication time, ``digest`` the body, ``import == "1"`` the level "important",
the ``stock`` list the linked codes. The feed has no revision time.
Not covered: tag/field (sector) lists, push pages beyond page 1, licensed Tonghuashun products.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .http import Capture, RateLimitedClient
from .news_items import BadAnswer, bare_codes, epoch_utc, load_json

SOURCE_ID = "ths_news_724"
ENDPOINT_ID = "push_stock"
URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
SCHEMA_VERSION = "ths-push-stock-recorded-20261119"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://news.10jqka.com.cn/realtimenews.html"}
SOURCE_ROWS = {SOURCE_ID: ("Tonghuashun 7x24 push feed (public web endpoint, not an official API)", "https://news.10jqka.com.cn")}
POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "news_24x7", "empty_result": "success_empty is not an error",
    "pagination": "page=1 only (newest items); pagesize 30", "entitlement_status": "public_read_candidate_unconfirmed", "official_api": False,
    "rate_limit": "<=1 request/second per host; the native recorder polls every 10 s; stop on 403/429/456/501",
    "revisions": "vendor exposes no revision time; changed title/digest for the same id is a new revision",
}


class ThsNewsAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, page: int = 1, page_size: int = 30) -> Capture:
        q = urlencode({"page": page, "tag": "", "track": "website", "pagesize": page_size})
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"page:{page}:{page_size}", url=f"{URL}?{q}", headers=HEADERS)


def recorder_items(body: bytes) -> list[dict]:
    j = load_json(body)
    rows = (j.get("data") or {}).get("list") if isinstance(j, dict) else None
    if not isinstance(rows, list):
        raise BadAnswer("ths: no data.list")
    return [{"item_id": str(x["id"]), "title": x.get("title") or "", "body": x.get("digest") or "", "url": x.get("url"),
             "source_published_at": epoch_utc(x.get("ctime")), "source_modified_at": None,
             "codes": bare_codes([s.get("stockCode", "") for s in x.get("stock") or [] if isinstance(s, dict)]), "comments": None,
             "level": "important" if str(x.get("import")) == "1" else None} for x in rows if isinstance(x, dict) and x.get("id")]
