"""CLS (Cailian) telegraph roll adapter (``v1/roll/get_roll_list``), spec FINAL §5.1.

Why: a second, independent 7x24 outlet, so confirmation across outlets can be exercised on
real data. The catalog (source_catalog_v3) marks CLS "unconfigured, entitlement pending": this is
the public web-client JSON endpoint, NOT an official API, and it is registered as such.
The web endpoint expects a request signature (md5 of the sha1 of the sorted query string); it
is a request-integrity parameter of the public client, not a credential.

Invariants: only builds requests; raw bytes are stored by the caller first; cursor = vendor
``last_time`` of the oldest item seen. Rate limit and stop-on-block come from the shared client.
Not covered: licensed CLS products, subject/plate pages, the article body beyond ``content``.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "cls_telegraph"  # id from source_catalog_v3
ENDPOINT_ID = "roll_list_web"
URL = "https://www.cls.cn/v1/roll/get_roll_list"
SCHEMA_VERSION = "cls-roll-list-recorded-20261111"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://www.cls.cn/telegraph"}
SOURCE_ROWS = {SOURCE_ID: ("CLS telegraph (public web endpoint, not an official API)", "https://www.cls.cn/telegraph")}

POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "news_24x7", "empty_result": "success_empty is not an error",
    "pagination": "last_time cursor (epoch seconds of the oldest item)", "entitlement_status": "public_read_candidate_unconfirmed",
    "official_api": False, "rate_limit": "<=1 request/second, stop on 403/429/456/501",
    "note": "third-party proxy/web endpoints are not official APIs (catalog quality_notes)",
}
SPEC = EndpointSpec(SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION, "EvidenceDocument", "poll", 1800, 300, None, POLICY, True, None)


def signed_query(rn: int, last_time: int | None) -> str:
    params = {"app": "CailianpressWeb", "category": "", "last_time": last_time or "", "os": "web", "refresh_type": 1, "rn": rn, "sv": "8.4.6"}
    plain = urlencode(sorted(params.items()))
    sign = hashlib.md5(hashlib.sha1(plain.encode()).hexdigest().encode()).hexdigest()
    return f"{plain}&sign={sign}"


class ClsNewsAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, last_time: int | None = None, page_size: int = 30) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"page:{last_time or 'latest'}:{page_size}",
                                   url=f"{URL}?{signed_query(page_size, last_time)}", headers=HEADERS)
