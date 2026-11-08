"""Real daily bars from Tencent, for detail charts and derived metrics.

AkShare's history endpoint goes through Eastmoney, which some networks block;
this module uses the same quote host as the snapshot enrichment. Bars are
forward-adjusted (`qfq`) and in the contract's schema, so they can stand in for
any daily-bar consumer.
"""
from __future__ import annotations

import httpx

from contracts.schema import OHLCBar

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def market(code: str) -> str:
    # BSE first: new BSE codes start with 92, which would otherwise match the SH "9" prefix.
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    return "sz"


def _ticker(code: str) -> str:
    return f"{market(code)}{code}"


def parse(payload: dict, ticker: str) -> list[OHLCBar]:
    """Tencent rows are [date, open, close, high, low, volume(手)]."""
    node = (payload.get("data") or {}).get(ticker) or {}
    rows = node.get("qfqday") or node.get("day") or []
    bars: list[OHLCBar] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            bars.append(
                OHLCBar(
                    date=str(row[0])[:10],
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]) * 100,  # 报价为“手”，契约用“股”
                )
            )
        except (TypeError, ValueError):
            continue
    return bars


def fetch_daily(code: str, count: int = 320, client: httpx.Client | None = None) -> list[OHLCBar] | None:
    """Fetch the newest `count` daily bars, or None when unavailable."""
    ticker = _ticker(code)
    own = client is None
    http = client or httpx.Client(timeout=8)
    try:
        response = http.get(
            KLINE_URL, params={"param": f"{ticker},day,,,{count},qfq"}
        )
        if response.status_code != 200:
            return None
        bars = parse(response.json(), ticker)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            http.close()
    return bars or None
