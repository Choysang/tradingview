"""Eastmoney data-center Longhubang (龙虎榜) adapter: daily list + buy/sell seat rows, spec FINAL §5.2.

Why: the live seat-disclosure feed. Three report tables of the public data-center JSON API are read:
the per-stock daily list (``RPT_DAILYBILLBOARD_DETAILSNEW``) and the buy / sell seat tables
(``RPT_BILLBOARD_DAILYDETAILSBUY`` / ``...SELL``). Amounts are CNY yuan in this API; the raw unit is
declared in the mapping, never inferred from magnitude.

Invariants: only builds requests; raw JSON stored first by the caller; an empty trading day returns a
valid "no data" body that the parser classifies as ``success_empty``, not as an error.
Not covered: Eastmoney's seat win-rate statistics (``eastmoney_lhb_analytics``) are derived vendor
numbers and are deliberately not read; exchange-direct pages (SSE/SZSE) are not wired.
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "eastmoney_lhb_disclosure"
SCHEMA_VERSION = "eastmoney-datacenter-lhb-recorded-20261111"
MAPPING_VERSION = "1.0.0"
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"Referer": "https://data.eastmoney.com/stock/tradedetail.html"}
REPORTS = {"daily_list": "RPT_DAILYBILLBOARD_DETAILSNEW", "seat_buy": "RPT_BILLBOARD_DAILYDETAILSBUY", "seat_sell": "RPT_BILLBOARD_DAILYDETAILSSELL"}
RAW_UNIT = "CNY"
SOURCE_ROWS = {SOURCE_ID: ("Eastmoney data center LHB disclosure", "https://data.eastmoney.com/stock/tradedetail.html")}

POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "exchange_post_close_and_revisions", "empty_result": "no-data body is success_empty",
    "pagination": "pageNumber/pageSize (<=500), result.pages", "entitlement_status": "public_read_candidate_unconfirmed",
    "raw_unit": RAW_UNIT, "trade_scope": "unknown (vendor does not state auction-only)", "rate_limit": "<=1 request/second, stop on 403/429/456/501",
}
SPECS = {ep: EndpointSpec(SOURCE_ID, ep, SCHEMA_VERSION, MAPPING_VERSION, "SeatReport", "poll", 86400, 1800, None, {**POLICY, "report": rid}, True, None)
         for ep, rid in REPORTS.items()}


def query_url(endpoint_id: str, trade_date: str, page: int = 1, page_size: int = 200) -> str:
    q = {"reportName": REPORTS[endpoint_id], "columns": "ALL", "source": "WEB", "client": "WEB", "pageNumber": page, "pageSize": page_size,
         "sortColumns": "SECURITY_CODE", "sortTypes": 1, "filter": f"(TRADE_DATE='{trade_date}')"}
    return f"{BASE}?{urlencode(q, quote_via=quote, safe='()')}"


class LhbAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def page(self, endpoint_id: str, trade_date: str, page: int = 1, page_size: int = 200) -> Capture:
        return self.client.request(source_id=SOURCE_ID, endpoint_id=endpoint_id, request_key=f"{endpoint_id}:{trade_date}:p{page}:s{page_size}",
                                   url=query_url(endpoint_id, trade_date, page, page_size), headers=HEADERS)
