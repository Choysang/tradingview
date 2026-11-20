"""Eastmoney limit pools adapter (push2ex.eastmoney.com ``getTopicZTPool`` / ``getTopicZBPool`` / ``getTopicDTPool``).

Why: the old recorder fetched the vendor's own limit-up, broken-board and limit-down pools once after the close, so the seal facts that are
estimated from 60 s snapshots can be reconciled against the vendor's figures (first seal time ``fbt``, open count ``zbc``, who is sealed at
the close). The native recorder keeps that job as an OPTIONAL step (``--limit-pools``, off by default).
Endpoint (as the old recorder called it, no Referer needed): ``GET /<pool>?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&
pagesize=10000&sort=<sort>&date=<yyyymmdd>``; the answer is ``{"rc":0,"data":{"tc":<total>,"qdate":<yyyymmdd>,"pool":[...]}}`` and for a
date that is not a session the vendor answers the LAST session's pool, so ``qdate`` must be compared with the wanted day.
Units (checked on the 2026-11-02 recording and the 2026-11-20 capture): ``p`` and ``ztp`` are thousandths of a yuan, ``fbt`` / ``lbt`` are the
first / last seal time as an integer ``HHMMSS`` (92500 = 09:25:00), ``amount`` and ``fund`` yuan, ``ltsz`` / ``tshare`` yuan, ``zdp`` / ``hs`` /
``zf`` percent, ``lbc`` consecutive limit-up boards, ``zbc`` number of times the board opened. Limit-down pool fields ``fund`` / ``fba`` /
``days`` / ``oc`` are kept under the vendor's names (their meaning is not verified).
Invariants: a body that is not the expected JSON is ``NotData`` (an HTML page is a refusal, the caller stops the host); the bytes are kept by
the caller before parsing. Not covered: pools of other dates than the session, paging (``pagesize=10000`` covers a session; ``tc`` larger than
the rows is reported by the caller).
"""
from __future__ import annotations

import json

from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient

SOURCE_ID = "eastmoney_limit_pools"
ENDPOINT_ID = "pool"
SCHEMA_VERSION = "eastmoney-push2ex-pools-20261120"
MAPPING_VERSION = "1.0.0"
HOST = "push2ex.eastmoney.com"
URL = "https://" + HOST + "/{name}?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=10000&sort={sort}&date={day}"
POOLS = {"zt": ("getTopicZTPool", "fbt:asc"), "zb": ("getTopicZBPool", "fbt:asc"), "dt": ("getTopicDTPool", "fund:asc")}


def parse_pool(body: bytes) -> tuple[str, int, list[dict]]:
    """(qdate yyyymmdd, total count ``tc``, pool rows as the vendor sent them)."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NotData("pool_body_not_json") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise NotData("pool_body_has_no_data")
    pool = data.get("pool")
    if pool is None:                                    # the vendor sends null for an empty pool
        pool = []
    if not isinstance(pool, list) or "qdate" not in data:
        raise SchemaChanged("pool_shape")
    return str(data["qdate"]), int(data.get("tc") or 0), [r for r in pool if isinstance(r, dict)]


class EastmoneyPoolAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def pool(self, key: str, day) -> Capture:
        name, sort = POOLS[key]
        return self.client.request(source_id=SOURCE_ID, endpoint_id=ENDPOINT_ID, request_key=f"{key}:{day:%Y%m%d}",
                                   url=URL.format(name=name, sort=sort, day=f"{day:%Y%m%d}"))
