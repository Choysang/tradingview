"""Native intraday recorder source: endpoint registration (step 2 cutover readiness, LEGACY_TRIAGE #103/#185/#119).

Why: until the M6 cutover the old system records the main-board snapshots and pulse3 imports its files (``legacy_recordings``). At the
cutover pulse3's own recorder (``application.intraday_recorder``) takes over and publishes the SAME dataset (``quote_snapshots_60s``) in the
SAME frame schema, so its lineage names this source instead of ``legacy_recordings`` and nothing downstream changes. The vendor responses
themselves stay raw evidence under their own ids (``sina/hq_quote``, ``tencent/qt_quote``); this source is the PRODUCER of the dataset.
Invariants: ``register`` only declares specs (it never opens a connection); both endpoints produce ESTIMATED sampled snapshots, never ticks;
the producer honours the quote endpoints' own limit of 1 request per second per host and stops on the first block.
Not covered: scheduling (a cutover script registers ``deploy/windows/run_intraday_record.ps1``), Eastmoney limit pools, minute bars,
convertible bonds and news-linked names of the old recorder (other lanes / not ported).
"""
from __future__ import annotations

SOURCE_ID = "pulse_intraday_recorder"
EP_QUOTES, EP_HOT = "intraday_quotes", "intraday_hot"
SCHEMA_VERSION = "native-intraday-20261119"
MAPPING_VERSION = "1.0.0"

_COMMON = {
    "timezone": "Asia/Shanghai", "calendar_id": "xshg_xshe_sessions", "mode": "native_recorder_not_scheduled_before_m6_cutover",
    "entitlement_status": "public_unauthenticated_endpoint", "rate_limit": "<=1 request/second per host",
    "on_block": "stop immediately, never retried; with the opt-in fallback (--fallback sina; off by default) the primary host's block switches "
                "to the fallback host for the rest of the session and both blocked stops the run; without it the block stops the run",
    "capability_class": "sampled_snapshots_estimated", "sampled_interval_seconds_nominal": 60, "not_tick_data": True, "estimated": True,
    "vendors": {"tencent": "primary (the old recorder's vendor): qt.gtimg.cn, 80 symbols per request, volumes in lots, amount in yuan",
                "sina": "optional fallback (opt-in): hq.sinajs.cn, 200 symbols per request, volumes in shares (converted to lots), amount in yuan"},
    "column_availability": {"sina": "no outer/inner lots, turnover, caps, volume ratio, vwap; limit prices come from the versioned price-limit "
                                    "rules (NULL when a band is unverified, e.g. risk-warning names)",
                            "tencent": "all columns; limit prices are the vendor's"},
    "units": "same as the legacy snapshot files: prices yuan, volumes/book sizes lots (100 shares), amount yuan, caps in 100 million yuan, "
             "after-hours amount in 10 thousand yuan",
    "retire": "none: this replaces the legacy_recordings import after the M6 cutover",
}


def register(add, spec_cls, sources: dict) -> None:
    """Declare this lane's sources (`sources[id] = (title, base_url)`) and endpoints (`add(spec_cls(...))`)."""
    sources[SOURCE_ID] = ("Pulse native intraday recorder (Tencent primary, optional Sina fallback)", "pulse3://intraday-recorder")
    add(spec_cls(SOURCE_ID, EP_QUOTES, SCHEMA_VERSION, MAPPING_VERSION, "CanonicalQuote", "poll", 120, 60, None,
                 {**_COMMON, "endpoint": "full snapshots", "kinds": "auction (09:15-09:25 every 60 s), auction_final (09:25:10), full (60 s in "
                  "the sessions), close (15:01:30), after (15:05:05, 15:15, 15:30:05)", "universe": "main board, every code of the run"},
                 True, None))
    add(spec_cls(SOURCE_ID, EP_HOT, SCHEMA_VERSION, MAPPING_VERSION, "CanonicalQuote", "poll", 120, 10, None,
                 {**_COMMON, "endpoint": "hot subset", "kinds": "auction_hot (5 s, 09:19:30-09:25:10, static pool), hot (10 s in the sessions), "
                  "after_hot (60 s, 15:05-15:30)", "universe": "static pool (names that touched a limit in the last 3 recorded sessions) "
                  "plus names within 2% of a limit or that touched one today, at most 800"},
                 True, None))
