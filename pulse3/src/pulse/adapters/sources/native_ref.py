"""Step 2 cutover readiness, lane D: endpoint registration of the native reference-data producers.

Why: until the M6 cutover the old system fetches ratio-adjusted daily bars, index bars, board membership and listing
dates on the server and pulse3 imports its parquet warehouse (``legacy_warehouse``). The native producers
(``workers/refdata.py``, ``application/refdata_service.py``) publish the SAME frame schemas under NEW dataset ids so the
legacy datasets stay untouched until the consumers are switched (a cutover decision, see the lane report):
``bars_native_fwd_adjusted_1d``, ``index_bars_native_1d``, ``sector_membership_native_boards_industry`` /
``..._concept``, ``instrument_listing_native``. Lineage shows the native source ids ``sina_ref`` / ``tencent_ref`` /
``exchange_lists``. Vendor responses are raw evidence under the ``adj_*`` / ``*_kline`` / ``board_*`` / ``*_stock_list``
endpoints; the producer endpoints (``adjusted_bars``, ``index_bars``, ``boards``, ``listings``) own the batches.
Invariants: ``register`` only declares specs (never opens a connection); every endpoint honours <= 1 request per second
and stops at the first block; the adjusted series is a ratio forward adjustment by Sina's factor file (NOT Tencent's
subtractive qfq), re-published whole as a new revision when a new ex-right event changes past values.
Not covered: scheduling (``deploy/windows/run_refdata.ps1`` is registered by the cutover script), STAR/BSE listing dates.
"""
from __future__ import annotations

from . import ref_listing as L
from . import ref_market as M

_BASE = {"timezone": "Asia/Shanghai", "mode": "native_producer_not_scheduled_before_m6_cutover",
         "entitlement_status": "public_unauthenticated_endpoint", "rate_limit": "<=1 request/second (all hosts of one client)",
         "on_block": "stop immediately, never retried; an HTML or 3xx answer to a data call counts as a block"}
_EVIDENCE = "EvidenceDocument"


def register(add, spec_cls, sources: dict) -> None:
    """Declare this lane's sources (``sources[id] = (title, base_url)``) and endpoints (``add(spec_cls(...))``)."""
    sources[M.SINA_SOURCE] = ("Sina Finance reference data (native producer): ratio-adjusted bars, index bars, board membership",
                              "https://finance.sina.com.cn")
    sources[M.TENCENT_SOURCE] = ("Tencent index kline (native producer, 000985 only)", "https://web.ifzq.gtimg.cn")
    sources[L.SOURCE_ID] = ("SSE / SZSE stock lists (native producer): listing dates", "https://query.sse.com.cn")
    sources[L.EM_SOURCE_ID] = ("Eastmoney stock list (native producer): listing dates when the exchange lists are unreachable", "https://82.push2.eastmoney.com")

    def raw(source: str, endpoint: str, schema: str, mapping: str, stale: int, extra: dict) -> None:
        add(spec_cls(source, endpoint, schema, mapping, _EVIDENCE, "poll", stale, None, None, {**_BASE, **extra, "endpoint": endpoint}, True, None))

    day = 86400
    raw(M.SINA_SOURCE, M.EP_KLINE, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "history": f"datalen up to {M.MAX_BARS} bars (about 4 years)", "prices": "raw (unadjusted)",
         "volume": "shares", "host": "quotes.sina.cn", "referer_required": True})
    raw(M.SINA_SOURCE, M.EP_FACTOR, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "semantics": "qfq.js: from date d on, adjusted price = raw / f", "host": "finance.sina.com.cn"})
    raw(M.SINA_SOURCE, M.EP_INDEX, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "prices": "index points, no adjustment", "volume": "as served; unit NOT verified"})
    raw(M.SINA_SOURCE, M.EP_NODES, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "content": "nested board list: 新浪行业 (industry) and 概念板块 (concept)"})
    raw(M.SINA_SOURCE, M.EP_MEMBERS, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "paging": f"{M.PAGE_SIZE} per page, a short page ends the board", "semantics": "CURRENT members only"})
    raw(M.TENCENT_SOURCE, M.EP_INDEX, M.SCHEMA_VERSION, M.MAPPING_VERSION, 3 * day,
        {"role": "raw evidence", "prices": "index points", "volume": "lots x 100 (as the legacy 000985 rows)"})
    raw(L.SOURCE_ID, L.EP_SSE, L.SCHEMA_VERSION, L.MAPPING_VERSION, 30 * day,
        {"role": "raw evidence", "content": "Shanghai main board A shares incl. LIST_DATE", "referer_required": True})
    raw(L.EM_SOURCE_ID, L.EP_EASTMONEY, L.SCHEMA_VERSION, L.MAPPING_VERSION, 30 * day,
        {"role": "raw evidence", "content": "push2 clist, Shenzhen main + ChiNext + Shanghai main, fields f12 code f14 name f26 listing date (yyyymmdd)",
         "paging": f"{L.EM_PAGE_SIZE} per page, about 55 pages", "use": "fallback only, for an exchange list that is unreachable (never after a block)"})
    raw(L.SOURCE_ID, L.EP_SZSE, L.SCHEMA_VERSION, L.MAPPING_VERSION, 30 * day,
        {"role": "raw evidence", "content": "Shenzhen A-share list (main + ChiNext) as .xlsx incl. A股上市日期"})

    add(spec_cls(M.SINA_SOURCE, M.OUT_ADJUSTED, M.SCHEMA_VERSION, M.MAPPING_VERSION, "AdjustedBar", "batch", 3 * day, None,
                 M.ADJUSTED_PROFILE,
                 {**_BASE, "endpoint": M.OUT_ADJUSTED, "dataset": "bars_native_fwd_adjusted_1d", "adjustment": M.ADJUSTMENT_VERSION,
                  "units": "prices forward-adjusted by ratio, float64 -> decimal(38,12); volume shares; turnover NULL",
                  "revisions": "every row of a code is re-published as a new revision when its values change (new ex-right event); "
                               "available_at is the fetch time; the frame keeps ALL revisions",
                  "schema": "identical to bars_legacy_fwd_adjusted_1d"}, True, "bars_native_fwd_adjusted_1d"))
    add(spec_cls(M.SINA_SOURCE, M.OUT_RAW, M.SCHEMA_VERSION, M.MAPPING_VERSION, "CanonicalBar", "batch", 3 * day, None, None,
                 {**_BASE, "endpoint": M.OUT_RAW, "datasets": ["bars_native_raw_1d", "adj_factor_native"],
                  "units": "raw prices CNY (decimal 38,12 from the vendor strings); volume shares; turnover NULL; factor rows as served",
                  "revisions": "a changed raw value / factor row is appended as revision n+1; available_at is the fetch time",
                  "modes": "published from the SAME responses as adjusted_bars (no extra request); `adjusted-bars --raw-only` fetches the kline alone "
                           "(1 request per code, own cursor raw:<scope>) and publishes the raw dataset only"}, True, None))
    add(spec_cls(M.SINA_SOURCE, M.OUT_INDEX, M.SCHEMA_VERSION, M.MAPPING_VERSION, "AdjustedBar", "batch", 3 * day, None, None,
                 {**_BASE, "endpoint": M.OUT_INDEX, "dataset": "index_bars_native_1d", "units": "index points; volume unit not verified",
                  "indices": sorted(M.INDEX_TICKERS), "schema": "identical to index_bars_legacy_1d",
                  "mapping": "columnar, no per-field profile"}, True, None))
    add(spec_cls(M.SINA_SOURCE, M.OUT_BOARDS, M.SCHEMA_VERSION, M.MAPPING_VERSION, _EVIDENCE, "batch", 3 * day, None, None,
                 {**_BASE, "endpoint": M.OUT_BOARDS, "datasets": ["sector_membership_native_boards_industry",
                                                                  "sector_membership_native_boards_concept"],
                  "semantics": "CURRENT membership snapshot per run; a run is published only when every board was fetched; "
                               "history is the sequence of READY manifests, never a back-fill",
                  "schema": "identical to sector_membership_legacy_boards_industry"}, True, None))
    add(spec_cls(L.SOURCE_ID, L.OUT_LISTINGS, L.SCHEMA_VERSION, L.MAPPING_VERSION, _EVIDENCE, "batch", 30 * day, None, None,
                 {**_BASE, "endpoint": L.OUT_LISTINGS, "dataset": "instrument_listing_native",
                  "semantics": "listing dates of Shanghai main + Shenzhen A shares; core.instrument.listed_on is NOT written"}, True, None))
