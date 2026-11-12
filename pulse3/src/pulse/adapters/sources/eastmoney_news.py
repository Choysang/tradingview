"""Eastmoney 7x24 fast-news feed adapter (``getFastNewsList``), spec FINAL §5.1, sources_sectors §4.

Why: one 24x7 news source feeding the claim pipeline. The endpoint is the public JSON list
used by the site's own 7x24 page; entitlement is NOT contractually confirmed (recorded in the
endpoint policy), so the client is rate limited and stops at the first block.

Invariants: this adapter only builds requests; the response bytes are kept as raw objects by
the caller before any parsing (parsing lives in ``domain.intelligence.feeds``); the cursor is
the ``sortEnd`` value the vendor returns, never a time guess.
Not covered: licensed push feeds, body text beyond the vendor's own ``summary`` field.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "eastmoney_news_724"
ENDPOINT_ID = "fast_news_list"
URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
SCHEMA_VERSION = "eastmoney-fast-news-recorded-20261111"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://kuaixun.eastmoney.com/"}
SOURCE_ROWS = {SOURCE_ID: ("Eastmoney 7x24 fast news", "https://kuaixun.eastmoney.com")}

POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "news_24x7", "empty_result": "success_empty is not an error",
    "pagination": "sortEnd cursor returned by the vendor; pageSize observed up to 50",
    "entitlement_status": "public_read_candidate_unconfirmed", "history_replay": "none verified; cursor walks back a bounded window",
    "rate_limit": "<=1 request/second, stop on 403/429/456/501",
    "revisions": "vendor exposes no revision id; changed content for the same code is a new revision",
}
SPEC = EndpointSpec(SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION, "EvidenceDocument", "poll", 1800, 300, None, POLICY, True, None)


class EastmoneyNewsAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, sort_end: str | None = None, page_size: int = 30) -> Capture:
        q = urlencode({"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": sort_end or "", "pageSize": page_size,
                       "req_trace": int(time.time() * 1000)})
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"page:{sort_end or 'latest'}:{page_size}",
                                   url=f"{URL}?{q}", headers=HEADERS)
