"""Eastmoney Guba (stock-bar) post-list adapter, spec FINAL §5 / sources_sectors §6.

Why: community attention samples for listed instruments. The list page embeds the posts as a JSON
literal (``var article_list=``); there is no verified public bulk API (catalog quality note), so
this reads the public HTML page the site itself serves, newest-post order, one page per request.

Invariants: only builds requests; raw HTML is stored first by the caller; the platform sort
(``f`` = by publish time) is part of the declared sampling scope. Read-only: there is no posting
or account code anywhere in this package.
Not covered: comment threads, per-post pages, likes (the list carries click and comment counts).
"""
from __future__ import annotations

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "eastmoney_guba_comments"  # id from source_catalog_v3
ENDPOINT_ID = "post_list_by_publish_time"
BASE = "https://guba.eastmoney.com"
SCHEMA_VERSION = "eastmoney-guba-list-recorded-20261111"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://guba.eastmoney.com/"}
SORT = "publish_time_desc"
SOURCE_ROWS = {SOURCE_ID: ("Eastmoney Guba stock-bar posts (sampled, read-only)", "https://guba.eastmoney.com")}

POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "community_24x7", "coverage": "sampled_not_exhaustive",
    "pagination": "page n of list,<code>,f_<n>.html (about 80 posts per page)", "entitlement_status": "public_read_candidate_unconfirmed",
    "community_policy": {"mode": "read_only", "external_posting_enabled": False}, "rate_limit": "<=1 request/second, stop on 403/429/456/501",
}
SPEC = EndpointSpec(SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION, "CommunityObservation", "poll", 3600, 600, None, POLICY, True, None)


def list_url(code: str, page: int = 1) -> str:
    return f"{BASE}/list,{code},f.html" if page <= 1 else f"{BASE}/list,{code},f_{page}.html"


class GubaAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, code: str, page: int = 1) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"list:{code}:p{page}", url=list_url(code, page),
                                   headers=HEADERS)
