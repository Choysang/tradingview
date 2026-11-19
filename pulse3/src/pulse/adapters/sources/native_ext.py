"""Step 2 cutover readiness: endpoint registration of the native extended-intraday source (LEGACY_TRIAGE #104/#186).

Why: each native source registers its own endpoints here so the shared registry needs one line per lane. Three hosts, three sources: a block
on one host (health OPEN) must not mark the others unavailable, and the raw evidence of each answer names the host that produced it.
Invariants: `register` only declares specs; it never opens a connection. The endpoints feed the dataset ``ext_quote_snapshots`` (the one the
legacy import ``legacy_recordings/intraday_ext`` feeds today); nothing here is scheduled before the M6 cutover.
Not covered: the parsing and the request plan (``ext_quotes``), the recorder (``application.bf_ext_record``).
"""
from __future__ import annotations

from . import ext_quotes as X

_TITLES = {
    X.SRC_TENCENT: ("Tencent quote text (extended intraday: ETF, index, Hang Seng futures, gold, crude)", "https://qt.gtimg.cn"),
    X.SRC_CFFEX: ("CFFEX exchange quote files (IF/IH/IC/IM index futures)", "http://www.cffex.com.cn"),
    X.SRC_EASTMONEY: ("Eastmoney minute series (FTSE China A50 futures, offshore CNH)", "https://push2his.eastmoney.com"),
}
# stale_after: the recorder polls every 60 s in session (CFFEX may be slowed to 300 s), Eastmoney only at the close and at 16:00
_STALE = {X.EP_QUOTE: 300, X.EP_FILE: 900, X.EP_TRENDS: 6 * 3600}
_POLL = {X.EP_QUOTE: 60, X.EP_FILE: 60, X.EP_TRENDS: None}


def register(add, spec_cls, sources: dict) -> None:
    """Declare this lane's sources (`sources[id] = (title, base_url)`) and endpoints (`add(spec_cls(...))`)."""
    sources.update(_TITLES)
    for host in X.HOSTS:
        source_id, endpoint_id = X.HOST_ENDPOINT[host]
        add(spec_cls(source_id, endpoint_id, X.SCHEMA_VERSION, X.MAPPING_VERSION, "CanonicalQuote", "poll", _STALE[endpoint_id], _POLL[endpoint_id],
                     None, X.ENDPOINT_POLICIES[endpoint_id], True, None))
