"""Exact endpoint registry: source + endpoint + schema + mapping + contract + capability (M1-E01/E05).

Why: units and parsing are configured per exact endpoint identity, never per
vendor. Each ``EndpointSpec`` becomes one ``core.source_endpoint`` row (with the
mapping rule hash) and declares which capability dataset its output feeds.

Invariants: ``desired enabled`` is configuration, not health; credentials are
referenced, never stored; every spec lists the polling/rate policy it honours.
Not covered: activating paid sources (Tushare etc.) - they stay unconfigured.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pulse.domain.market.units import profile_hash

from . import cninfo, legacy, sina, tencent

CONTRACT_VERSION = "3.0.0"


@dataclass(frozen=True)
class EndpointSpec:
    source_id: str
    endpoint_id: str
    source_schema_version: str
    mapping_version: str
    contract_id: str
    transport_kind: str
    stale_after_seconds: int
    poll_interval_seconds: int | None
    profile: dict | None
    policy: dict = field(default_factory=dict)
    enabled: bool = True
    capability_dataset: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.source_id, self.endpoint_id, self.source_schema_version, self.mapping_version)

    @property
    def mapping_hash(self) -> str:
        return profile_hash(self.profile or {"endpoint": self.endpoint_id, "mapping": self.mapping_version})


SOURCES = {
    "legacy_warehouse": ("Legacy parquet warehouse (read-only history)", "file://warehouse/live"),
    "sina": ("Sina Finance quotes", "https://finance.sina.com.cn"),
    "tencent": ("Tencent Finance quotes", "https://gu.qq.com"),
    "cninfo_announcements": ("CNINFO listed-company announcements", "http://www.cninfo.com.cn"),
}

_QUOTE_POLICY = {"timezone": "Asia/Shanghai", "calendar_id": "xshg_xshe_sessions", "rate_limit": "<=1 request/second",
                 "on_block": "stop immediately, circuit OPEN", "entitlement_status": "public_unauthenticated_endpoint"}


def _p(ep: str, **extra) -> dict:
    return {**_QUOTE_POLICY, "endpoint": ep, **extra}


SPECS: dict[tuple[str, str], EndpointSpec] = {}


def _add(spec: EndpointSpec) -> None:
    SPECS[(spec.source_id, spec.endpoint_id)] = spec


_add(EndpointSpec("legacy_warehouse", "bars", legacy.SCHEMA_VERSION, legacy.MAPPING_VERSION, "AdjustedBar", "batch",
                  7 * 86400, None, legacy.BARS_PROFILE,
                  {"timezone": "Asia/Shanghai", "mode": "historical_import_replayable", "adjustment": legacy.ADJUSTMENT_VERSION,
                   "units": "prices forward-adjusted float64; volume shares; amount NULL"}, True, "bars_legacy_fwd_adjusted_1d"))
_add(EndpointSpec("legacy_warehouse", "index_bars", legacy.SCHEMA_VERSION, legacy.MAPPING_VERSION, "AdjustedBar", "batch",
                  7 * 86400, None, None, {"timezone": "Asia/Shanghai", "mode": "historical_import_replayable",
                                          "units": "index points; volume unit not verified", "mapping": "columnar, no per-field profile"}, True, None))
_add(EndpointSpec("legacy_warehouse", "evidence_files", legacy.SCHEMA_VERSION, legacy.MAPPING_VERSION, "EvidenceDocument", "batch",
                  30 * 86400, None, None, {"mode": "raw files kept for the sector / seat packages; not normalized here"}, True, None))
_add(EndpointSpec("sina", "hq_quote", sina.SCHEMA_VERSION, sina.MAPPING_VERSION, "CanonicalQuote", "poll", 120, 5,
                  sina.HQ_PROFILE, _p("hq", referer_required=True), True, "quotes_l1"))
_add(EndpointSpec("sina", "kline_daily", sina.SCHEMA_VERSION, sina.MAPPING_VERSION, "CanonicalBar", "poll", 86400, 3600,
                  sina.KLINE_DAILY_PROFILE, _p("kline scale=240", history="datalen up to ~1000"), True, "bars_raw_1d"))
_add(EndpointSpec("sina", "kline_minute", sina.SCHEMA_VERSION, sina.MAPPING_VERSION, "CanonicalBar", "poll", 600, 60,
                  sina.KLINE_MINUTE_PROFILE, _p("kline scale=5/15/30/60", label="bar close time"), True, "bars_raw_5m"))
_add(EndpointSpec("tencent", "qt_quote", tencent.SCHEMA_VERSION, tencent.MAPPING_VERSION, "CanonicalQuote", "poll", 120, 5,
                  tencent.QT_PROFILE, _p("qt"), True, "quotes_l1"))
_add(EndpointSpec("tencent", "kline_day", tencent.SCHEMA_VERSION, tencent.MAPPING_VERSION, "CanonicalBar", "poll", 86400, 3600,
                  tencent.DAY_PROFILE, _p("fqkline day raw", history="up to 640 sessions"), True, "bars_raw_1d_tencent"))
_add(EndpointSpec("tencent", "kline_minute", tencent.SCHEMA_VERSION, tencent.MAPPING_VERSION, "CanonicalBar", "poll", 600, 60,
                  tencent.MKLINE_PROFILE, _p("mkline m1/m5", label="bar close time"), True, "bars_raw_1m"))
_add(EndpointSpec("tencent", "minute_query", tencent.SCHEMA_VERSION, tencent.MAPPING_VERSION, "CanonicalQuote", "poll", 600, 60,
                  tencent.MINUTE_QUERY_PROFILE, _p("minute/query", estimated=True), True, "minute_close_sampled"))
_add(EndpointSpec("cninfo_announcements", cninfo.ENDPOINT_ID, cninfo.SCHEMA_VERSION, cninfo.MAPPING_VERSION,
                  "EvidenceDocument", "poll", 6 * 3600, 600, None, cninfo.ENDPOINT_POLICY, True, "announcements_official"))


def spec(source_id: str, endpoint_id: str) -> EndpointSpec:
    try:
        return SPECS[(source_id, endpoint_id)]
    except KeyError as exc:
        raise KeyError(f"endpoint {source_id}/{endpoint_id} is not registered") from exc
