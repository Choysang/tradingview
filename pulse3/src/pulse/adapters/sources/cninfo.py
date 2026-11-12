"""cninfo official announcement adapter (POST hisAnnouncement/query), M1-E01 official event chain.

Why: cninfo is the statutory disclosure platform; an announcement here confirms
the issuer's OWN publication. The endpoint is a public query used by the site
(entitlement not contractually confirmed - recorded in the endpoint policy), so
the client is rate limited and stops at the first block.

Invariants: the whole window is paged until ``hasMore`` is false or a page cap
is hit; a capped listing is marked ``window_fully_paged=False`` so no
disappearance (withdrawal) is inferred from it; the response bytes of every page
are kept as raw objects by the caller before parsing.
Not covered: PDF download, other exchanges' disclosure sites.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .http import Capture, RateLimitedClient

SOURCE_ID = "cninfo_announcements"
ENDPOINT_ID = "his_announcement_query"
URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
SCHEMA_VERSION = "cninfo-his-announcement-recorded-20261111"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
           "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
           "X-Requested-With": "XMLHttpRequest"}

ENDPOINT_POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "announcement_24x7", "empty_result": "success_empty is not an error",
    "lookback_days": 3, "pagination": "pageNum/pageSize(max observed 30), hasMore flag",
    "entitlement_status": "public_read_candidate_unconfirmed",
    "history_replay": "seDate window query; withdrawn announcements disappear from listings (inferred only after a fully paged window)",
    "rate_limit": "<=1 request/second, stop on 403/429/501",
}


def form_body(page: int, se_date: str, page_size: int = 30, column: str = "szse") -> str:
    return urlencode({"pageNum": page, "pageSize": page_size, "column": column, "tabName": "fulltext", "plate": "",
                      "stock": "", "searchkey": "", "secid": "", "category": "", "trade": "",
                      "seDate": se_date, "sortName": "", "sortType": "", "isHLtitle": "true"}, safe="~")


class CninfoAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, page: int, se_date: str, page_size: int = 30) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID,
                                   request_key=f"query:{se_date}:p{page}:s{page_size}", url=URL, method="POST",
                                   headers=HEADERS, form=form_body(page, se_date, page_size))
