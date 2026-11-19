"""Native reference-data adapters, market side: ratio-adjusted daily bars, index bars, board membership (Step 2, lane D).

Why: until the cutover the old system fetched these on the server and pulse3 only imported its parquet warehouse.
Ported behaviour (read from ``backend/app/warehouse/{sina,tencent,boards}.py``, NOT the Tencent qfq endpoint the
brief assumed): the old daily bars are Sina RAW klines (``quotes.sina.cn`` ``CN_MarketDataService.getKLineData``,
scale 240, up to 1023 bars) divided by Sina's own factor file (``qfq.js``: from date ``d`` on, adjusted = raw / f), a
RATIO forward adjustment that keeps every daily return equal to the raw one. Tencent's fqkline ``qfq`` subtracts the
cumulative dividend instead (old docstring, verified 2026-10-31), which inflates old percentage moves, so it is not used
for stocks. Index bars: Sina kline per index, Tencent fqkline for 000985 (Sina's 000985 series stops in 2016). Boards:
``Market_Center.getHQNodes`` (industry / concept lists) + ``getHQNodeData`` (members, paged by 100).

Invariants: every function that touches the network returns a ``Capture`` with the raw bytes untouched; a body that is
an HTML page or a 3xx answer for a data call is a BLOCK (circuit opened, ``SourceBlocked``); parsing is pure and
validates shapes (drift -> ``SchemaChanged``, not data -> ``NotData``); prices are float64 exactly like the old
warehouse so the legacy and native series are comparable cell by cell; volume is SHARES (Sina kline) or lots x 100
(Tencent index), turnover amount is never produced (NULL, not 0).
Not covered: Level-2, hfq, ex-right event extraction (a changed factor file simply yields changed past values and the
service records them as new revisions), listing dates (``ref_listing.py``).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .http import Capture, RateLimitedClient, SourceBlocked

SINA_SOURCE = "sina_ref"
TENCENT_SOURCE = "tencent_ref"
SCHEMA_VERSION = "ref-market-20261119"
MAPPING_VERSION = "1.0.0"
ADJUSTMENT_VERSION = "sina-ratio-qfq-v1"

EP_KLINE, EP_FACTOR, EP_INDEX = "adj_kline", "adj_factor", "index_kline"
EP_NODES, EP_MEMBERS = "board_nodes", "board_members"
OUT_ADJUSTED, OUT_INDEX, OUT_BOARDS = "adjusted_bars", "index_bars", "boards"

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
FACTOR_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
NODES_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes"
MEMBERS_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
               "?page={page}&num={num}&sort=symbol&asc=1&node={node}&symbol=&_s_r_a=page")
TENCENT_INDEX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
REFERER = {"Referer": "https://finance.sina.com.cn"}

MAX_BARS = 1023                      # one Sina kline request returns at most this many bars (about 4 years)
PAGE_SIZE = 100
MAX_PAGES = 40
BOARD_KINDS = {"新浪行业": "industry", "概念板块": "concept"}
#: index code -> (kline ticker, source). 000985 comes from Tencent because Sina's series stops in 2016.
INDEX_TICKERS: dict[str, tuple[str, str]] = {
    "000985": ("sh000985", TENCENT_SOURCE), "000300": ("sh000300", SINA_SOURCE), "000852": ("sh000852", SINA_SOURCE),
    "000905": ("sh000905", SINA_SOURCE), "399006": ("sz399006", SINA_SOURCE), "000001": ("sh000001", SINA_SOURCE),
}
_A_SHARE = re.compile(r"(00|30|60|68|4|8|92)\d*")


def _op(source: str, target: str, op: str, **kw) -> dict:
    return {"source": source, "target": target, "op": op, **kw}


#: Same operations as the legacy ``bars`` profile (decimal_from_float64 at scale 12, shares as integral float) so a
#: native row and a legacy row of equal float values normalize to identical decimals.
ADJUSTED_PROFILE = {
    "profile_id": "sina_ref_adjusted_bars_20261119_v1", "source_id": SINA_SOURCE, "endpoint_id": OUT_ADJUSTED,
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "bar",
    "adjustment_mode": "forward_adjusted",
    "source_semantics": {"prices": f"Sina raw kline / Sina qfq factor (ratio), {ADJUSTMENT_VERSION}, float64",
                         "volume": "shares (Sina kline, float64 integral)", "amount": "not provided (NULL)", "date": "session date"},
    "operations": [
        _op("code", "instrument_id", "resolve_identifier_at_trade_date"),
        _op("date", "trade_date", "parse_date_iso"),
        _op("open", "open_price", "decimal_from_float64", round_scale=12), _op("high", "high_price", "decimal_from_float64", round_scale=12),
        _op("low", "low_price", "decimal_from_float64", round_scale=12), _op("close", "close_price", "decimal_from_float64", round_scale=12),
        _op("volume", "volume_shares", "float64_require_integer", raw_unit="shares"),
        _op("amount", "turnover_amount_cny", "decimal_from_float64", raw_unit="CNY", round_scale=2),
    ],
    "recognized_unmapped": [],
}


def bars_needed(start: date, today: date) -> int:
    """Trading bars from ``start`` to ``today`` with a 20-bar margin, capped at what one request returns."""
    days = (today - start).days
    return max(10, min(MAX_BARS, math.ceil(days * 250 / 365) + 20))


# ---------------------------------------------------------------------------------------------------------------
# parsing (pure)
# ---------------------------------------------------------------------------------------------------------------
def _finite_positive(*values: float) -> bool:
    return all(math.isfinite(v) and v > 0 for v in values)


def parse_kline(body: bytes) -> list[dict]:
    """Sina kline -> ``[{date, open, high, low, close, volume}]`` (float64, volume in shares), sorted, one row per date.

    Rows whose OHLC is not a positive finite number are dropped (never repaired); a body that is not a JSON array is
    ``NotData``; a row without the expected keys is ``SchemaChanged``."""
    text = body.decode("utf-8", errors="replace").strip()
    if text in ("", "null"):
        return []
    if text[0] != "[":
        raise NotData("sina_kline_body_not_json_array")
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("sina_kline_invalid_json") from exc
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict) or not {"day", "open", "high", "low", "close", "volume"} <= set(r):
            raise SchemaChanged("sina_kline_row_shape")
        try:
            o, h, lo, c, v = (float(r[k]) for k in ("open", "high", "low", "close", "volume"))
        except (TypeError, ValueError):
            continue
        if not _finite_positive(o, h, lo, c) or not math.isfinite(v) or v < 0:
            continue
        day = str(r["day"])[:10]
        out[day] = {"date": date.fromisoformat(day), "open": o, "high": h, "low": lo, "close": c, "volume": v}
    return [out[k] for k in sorted(out)]


def parse_factors(body: bytes) -> list[tuple[date, float]]:
    """Sina ``qfq.js`` -> ``[(d, f)]`` sorted by date; from ``d`` on, adjusted price = raw / f. No object -> no factors."""
    text = body.decode("utf-8", errors="replace")
    if "{" not in text:
        return []
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1]).get("data") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise NotData("sina_factor_invalid_json") from exc
    out = []
    for x in data:
        if not isinstance(x, dict) or "d" not in x or "f" not in x:
            raise SchemaChanged("sina_factor_row_shape")
        f = float(x["f"])
        if not _finite_positive(f):
            raise SchemaChanged("sina_factor_not_positive")
        out.append((date.fromisoformat(str(x["d"])[:10]), f))
    return sorted(out)


def adjust_rows(rows: list[dict], factors: list[tuple[date, float]]) -> list[dict]:
    """Divide OHLC by the factor in force on each day (the old backward as-of join); a date before the first factor
    uses the earliest factor; no factors means no adjustment (the caller counts that case)."""
    if not factors:
        return [dict(r) for r in rows]
    out, i, current = [], 0, factors[0][1]
    for r in sorted(rows, key=lambda x: x["date"]):
        while i < len(factors) and factors[i][0] <= r["date"]:
            current = factors[i][1]
            i += 1
        out.append({**r, **{k: r[k] / current for k in ("open", "high", "low", "close")}})
    return out


def parse_tencent_index(body: bytes, symbol: str) -> list[dict]:
    """Tencent fqkline of an index -> same row shape as :func:`parse_kline` (volume lots x 100 = shares)."""
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("x="):
        text = text[2:]
    if not text or text[0] != "{":
        raise NotData("tencent_index_body_not_json_object")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotData("tencent_index_invalid_json") from exc
    if doc.get("code") not in (0, "0", None):
        raise NotData(f"tencent_index_api_code_{doc.get('code')}")
    node = (doc.get("data") or {}).get(symbol)
    if not isinstance(node, dict):
        raise SchemaChanged("tencent_index_missing_symbol_node")
    out: dict[str, dict] = {}
    for r in node.get("qfqday") or node.get("day") or []:
        if not isinstance(r, list) or len(r) < 6:
            raise SchemaChanged("tencent_index_row_shape")
        try:
            o, c, h, lo, lots = (float(r[i]) for i in (1, 2, 3, 4, 5))
        except (TypeError, ValueError):
            continue
        if not _finite_positive(o, h, lo, c):
            continue
        out[str(r[0])[:10]] = {"date": date.fromisoformat(str(r[0])[:10]), "open": o, "high": h, "low": lo, "close": c,
                               "volume": lots * 100.0}
    return [out[k] for k in sorted(out)]


@dataclass(frozen=True)
class Board:
    kind: str
    board_id: str
    name: str


def _find(node: Any, name: str) -> list | None:
    if not isinstance(node, list):
        return None
    if len(node) >= 2 and node[0] == name and isinstance(node[1], list):
        return node
    for child in node:
        found = _find(child, name)
        if found is not None:
            return found
    return None


def parse_nodes(body: bytes) -> list[Board]:
    """Industry and concept boards from the nested ``getHQNodes`` answer; a repeated id is kept once."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotData("sina_nodes_invalid_json") from exc
    boards: dict[tuple[str, str], Board] = {}
    for title, kind in BOARD_KINDS.items():
        group = _find(payload, title)
        for leaf in group[1] if group else []:
            if isinstance(leaf, list) and len(leaf) >= 3 and isinstance(leaf[2], str) and leaf[2].strip():
                bid = leaf[2].strip()
                boards.setdefault((kind, bid), Board(kind, bid, str(leaf[0]).strip()))
    if not boards:
        raise SchemaChanged("sina_nodes_no_industry_or_concept_group")
    return list(boards.values())


def parse_members(body: bytes) -> tuple[list[str], int]:
    """(A-share codes of one member page in order without repeats, raw row count of the page)."""
    try:
        rows = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotData("sina_members_invalid_json") from exc
    if not isinstance(rows, list):
        raise SchemaChanged("sina_members_not_a_list")
    codes: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or str(row.get("symbol") or "")[-6:]).strip()
        if len(code) == 6 and code.isdigit() and _A_SHARE.fullmatch(code) and code not in codes:
            codes.append(code)
    return codes, len(rows)


# ---------------------------------------------------------------------------------------------------------------
# network adapters
# ---------------------------------------------------------------------------------------------------------------
def market_prefix(code: str) -> str:
    """Sina/Tencent market prefix of a stock code (exchange numbering ranges)."""
    if code[:1] == "6":
        return "sh"
    if code[:1] in "489":
        return "bj"
    return "sz"


def guard_not_blocked(client: RateLimitedClient, cap: Capture) -> Capture:
    """A redirect or an HTML page answering a data call is the block/captcha page: open the circuit and stop."""
    if 300 <= cap.status < 400 or cap.body.lstrip()[:1] == b"<":
        client.circuit_open, client.block_reason = True, "html_or_redirect_for_data_call"
        raise SourceBlocked("html_or_redirect_for_data_call", cap.status)
    return cap


class SinaRefAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def _get(self, endpoint: str, key: str, url: str) -> Capture:
        cap = self.client.request(source_id=SINA_SOURCE, endpoint_id=endpoint, request_key=key, url=url, headers=REFERER)
        return guard_not_blocked(self.client, cap)

    def kline(self, symbol: str, datalen: int) -> Capture:
        datalen = max(1, min(int(datalen), MAX_BARS))
        return self._get(EP_KLINE, f"kline:{symbol}:{datalen}", KLINE_URL.format(symbol=symbol, datalen=datalen))

    def factors(self, symbol: str) -> Capture:
        return self._get(EP_FACTOR, f"qfq:{symbol}", FACTOR_URL.format(symbol=symbol))

    def index_kline(self, symbol: str, datalen: int) -> Capture:
        datalen = max(1, min(int(datalen), MAX_BARS))
        return self._get(EP_INDEX, f"index:{symbol}:{datalen}", KLINE_URL.format(symbol=symbol, datalen=datalen))

    def board_nodes(self) -> Capture:
        return self._get(EP_NODES, "nodes", NODES_URL)

    def board_members(self, board_id: str, page: int) -> Capture:
        return self._get(EP_MEMBERS, f"members:{board_id}:{page}", MEMBERS_URL.format(page=page, num=PAGE_SIZE, node=board_id))


class TencentRefAdapter:
    def __init__(self, client: RateLimitedClient):
        self.client = client

    def index_kline(self, symbol: str, count: int) -> Capture:
        count = max(1, min(int(count), 640))
        cap = self.client.request(source_id=TENCENT_SOURCE, endpoint_id=EP_INDEX, request_key=f"index:{symbol}:{count}",
                                  url=TENCENT_INDEX_URL.format(symbol=symbol, count=count))
        return guard_not_blocked(self.client, cap)
