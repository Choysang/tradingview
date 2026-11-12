"""Helpers for the data-plane tests: recorded fixtures, resolvers, fixed clocks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pulse.domain.instruments.identity import InstrumentRecord

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / "fixtures" / "data" / "recorded"
LEGACY_MINI = ROOT / "fixtures" / "data" / "legacy_mini"
PACKAGE_MAPPINGS = ROOT / "spec_package" / "database" / "source_mapping_examples.json"

NOW = datetime(2026, 11, 11, 7, 20, tzinfo=timezone.utc)  # 15:20 CST, right after the recorded captures
NAMES = {"000001": "平安银行", "600000": "浦发银行", "002415": "海康威视"}


def recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def instruments() -> dict[str, InstrumentRecord]:
    return {c: InstrumentRecord.build(c, n) for c, n in NAMES.items()}


def make_resolver(source_id: str):
    """Alias resolver equivalent over the three fixture instruments (what instrument_repo.alias_resolver returns)."""
    table = {}
    for rec in instruments().values():
        table[rec.symbols[source_id]] = str(rec.instrument_id)

    def resolve(symbol: str, trade_date: str, market: str | None) -> str:
        try:
            return table[symbol]
        except KeyError:
            raise ValueError(f"unresolved_identifier:{source_id}:{symbol}") from None

    return resolve


def context(spec_or_profile: dict, **extra) -> dict:
    base = {k: spec_or_profile[k] for k in ("source_id", "endpoint_id", "source_schema_version", "mapping_version")}
    t = NOW.isoformat().replace("+00:00", "Z")
    return {**base, "first_seen_at": t, "ingested_at": t, "available_at": t, "raw_object_id": "00000000-0000-4000-8000-0000000000aa",
            **extra}


def load_json_after_var(name: str) -> dict:
    return json.loads(recorded(name).decode("utf-8").split("=", 1)[1])


# ---- recorded HTTP transport ---------------------------------------------------------------------
def mock_transport(*, overrides: dict | None = None, calls: list | None = None):
    """httpx.MockTransport that answers the three sources from the recorded fixtures."""
    import re
    import httpx

    overrides = overrides or {}

    def handler(request: "httpx.Request") -> "httpx.Response":
        url = str(request.url)
        if calls is not None:
            calls.append((request.method, url, request.content.decode() if request.content else ""))
        for pattern, responder in overrides.items():
            if re.search(pattern, url + request.content.decode()):
                return responder(request) if callable(responder) else responder
        if "hq.sinajs.cn" in url:
            return httpx.Response(200, content=recorded("sina_hq.txt"), headers={"content-type": "text/javascript; charset=GBK"})
        if "CN_MarketData.getKLineData" in url:
            sym = re.search(r"symbol=(\w+)", url).group(1)
            scale = re.search(r"scale=(\d+)", url).group(1)
            name = f"sina_kline_{scale}_{sym}.json"
            if not (RECORDED / name).exists():
                name = "sina_kline_5_sz000001.json" if scale != "240" else f"sina_kline_240_{sym}.json"
            return httpx.Response(200, content=recorded(name), headers={"content-type": "application/json"})
        if "qt.gtimg.cn" in url:
            return httpx.Response(200, content=recorded("tencent_qt.txt"), headers={"content-type": "application/x-javascript; charset=GBK"})
        if "fqkline/get" in url:
            sym = re.search(r"param=(\w+),day", url).group(1)
            return httpx.Response(200, content=recorded(f"tencent_day_raw_{sym}.json"), headers={"content-type": "application/json"})
        if "mkline" in url:
            per = re.search(r",(m\d+),", url).group(1)
            return httpx.Response(200, content=recorded(f"tencent_mkline_{per}_sz000001.json"), headers={"content-type": "application/json"})
        if "minute/query" in url:
            return httpx.Response(200, content=recorded("tencent_minute_query_sz000001.json"), headers={"content-type": "application/json"})
        if "cninfo.com.cn/new/hisAnnouncement/query" in url:
            page = re.search(r"pageNum=(\d+)", request.content.decode()).group(1)
            name = f"cninfo_page{page}.json"
            body = recorded(name) if (RECORDED / name).exists() else b'{"announcements":null,"hasMore":false,"totalAnnouncement":0}'
            return httpx.Response(200, content=body, headers={"content-type": "application/json;charset=UTF-8"})
        return httpx.Response(404, content=b"unmapped")

    return httpx.MockTransport(handler)
