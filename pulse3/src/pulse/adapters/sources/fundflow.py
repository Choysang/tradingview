"""Eastmoney platform-computed fund-flow adapter (``fflow/daykline``), spec FINAL §5 / sources §7.

Why: provider-derived fund-flow observations. Eastmoney's own FAQ says these flows are computed by
its software (order-size classification); they are a PROVIDER METHOD, not an exchange fact, and are
stored under their own metric namespace and methodology version (never merged with another provider).

Invariants: only builds requests; raw JSON stored first by the caller. The ``push2his`` host answers
plain ``http`` but its TLS handshake timed out from the development host; the scheme is part of the
registered endpoint policy, not a silent downgrade of a credentialed call (no credentials are sent).
Not covered: intraday flow, northbound/southbound exchange statistics, ETF share changes.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .http import Capture, RateLimitedClient
from .registry import EndpointSpec

SOURCE_ID = "eastmoney_fund_flow"  # id from source_catalog_v3
ENDPOINT_ID = "fflow_daykline"
URL = "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
SCHEMA_VERSION = "eastmoney-fflow-daykline-recorded-20261111"
MAPPING_VERSION = "1.0.0"
HEADERS = {"Referer": "https://data.eastmoney.com/zjlx/"}
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
UT = "b2884a393a59ad64002292a3e90d46a5"  # public web-client constant of the site, not a credential
SOURCE_ROWS = {SOURCE_ID: ("Eastmoney fund flow (provider-computed)", "https://data.eastmoney.com/zjlx/")}

POLICY = {
    "timezone": "Asia/Shanghai", "calendar_id": "exchange_sessions", "entitlement_status": "public_read_candidate_unconfirmed",
    "scheme": "http (https handshake timed out from the dev host)", "measurement_class": "provider_derived",
    "methodology_note": "vendor FAQ: computed by the software from order and trade information; thresholds undisclosed",
    "rate_limit": "<=1 request/second, stop on 403/429/456/501",
}
SPEC = EndpointSpec(SOURCE_ID, ENDPOINT_ID, SCHEMA_VERSION, MAPPING_VERSION, "FundFlowObservation", "poll", 86400, 3600, None, POLICY, True, None)


def secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


class FundFlowAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def daykline(self, code: str, limit: int = 10) -> Capture:
        q = urlencode({"lmt": limit, "klt": 101, "secid": secid(code), "fields1": "f1,f2,f3,f7", "fields2": FIELDS2, "ut": UT}, safe=",")
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"daykline:{code}:{limit}", url=f"{URL}?{q}", headers=HEADERS)
