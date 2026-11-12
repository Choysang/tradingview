"""Per-endpoint / per-dataset data capability and requirement matching (M1-E05).

Why: a strategy or chart that needs true minute OHLC, raw prices or turnover
amounts must be refused (or explicitly downgraded) when the registered dataset
cannot supply them. Sampled snapshots never impersonate real OHLC; low
capability approximations are separate definitions with ``estimated=True``.

Invariants: a requirement is accepted only if every needed feature is declared
by the capability and no forbidden property (estimated, adjusted-only) is
present; the decision always lists the reasons; nothing is inferred from an
installed adapter's name. Persisted inside ``core.source_endpoint.policy_document``
and the manifest ``coverage_document``; rejections are recorded as
``ops.data_issue`` rows by the application layer.
Not covered: persistence and routing of strategies (W05/W06 consume this).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# capability feature vocabulary
DAILY_BARS = "daily_bars"
MINUTE_BARS_TRUE_OHLC = "minute_bars_true_ohlc"
MINUTE_CLOSE_SAMPLED = "minute_close_sampled"  # price per minute only (high/low unobservable)
L1_QUOTE = "l1_quote"
TURNOVER_AMOUNT = "turnover_amount"
RAW_PRICES = "raw_prices"
ADJUSTED_PRICES = "adjusted_prices"
INTERVAL_VOLUME = "interval_volume"
CUMULATIVE_ONLY = "cumulative_volume_only"
INDIVIDUAL_TRADES = "individual_trades"
FULL_ORDER_EVENTS = "full_order_events"
OFFICIAL_EVENTS = "official_events"

KNOWN = {DAILY_BARS, MINUTE_BARS_TRUE_OHLC, MINUTE_CLOSE_SAMPLED, L1_QUOTE, TURNOVER_AMOUNT, RAW_PRICES,
         ADJUSTED_PRICES, INTERVAL_VOLUME, CUMULATIVE_ONLY, INDIVIDUAL_TRADES, FULL_ORDER_EVENTS, OFFICIAL_EVENTS}


@dataclass(frozen=True)
class Capability:
    dataset_id: str
    source_id: str
    endpoint_id: str
    features: frozenset[str]
    estimated: bool = False
    sampled_interval_seconds: int | None = None
    history_depth: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = set(self.features) - KNOWN
        if unknown:
            raise ValueError(f"unknown capability features: {sorted(unknown)}")
        if self.estimated and MINUTE_BARS_TRUE_OHLC in self.features:
            raise ValueError("estimated data cannot declare true OHLC")

    def to_doc(self) -> dict:
        d = asdict(self)
        d["features"] = sorted(self.features)
        d["notes"] = list(self.notes)
        return d

    @staticmethod
    def from_doc(doc: dict) -> "Capability":
        return Capability(doc["dataset_id"], doc["source_id"], doc["endpoint_id"], frozenset(doc["features"]),
                          bool(doc.get("estimated", False)), doc.get("sampled_interval_seconds"),
                          doc.get("history_depth"), tuple(doc.get("notes", ())))


@dataclass(frozen=True)
class Requirement:
    name: str
    needs: frozenset[str]
    forbid_estimated: bool = True
    optional: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RoutingDecision:
    requirement: str
    dataset_id: str
    outcome: str  # accepted | degraded | rejected
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def data_ready(self) -> bool:
        return self.outcome == "accepted"


def evaluate(req: Requirement, cap: Capability) -> RoutingDecision:
    reasons: list[str] = []
    missing = sorted(req.needs - cap.features)
    if missing:
        reasons.append("missing_required:" + ",".join(missing))
    if cap.estimated and req.forbid_estimated:
        reasons.append("estimated_data_not_accepted")
    if missing or (cap.estimated and req.forbid_estimated):
        return RoutingDecision(req.name, cap.dataset_id, "rejected", tuple(reasons))
    lost = sorted(req.optional - cap.features)
    if lost or cap.estimated:
        if lost:
            reasons.append("optional_missing:" + ",".join(lost))
        if cap.estimated:
            reasons.append("estimated_inputs_flagged")
        return RoutingDecision(req.name, cap.dataset_id, "degraded", tuple(reasons))
    return RoutingDecision(req.name, cap.dataset_id, "accepted", ())


def route(req: Requirement, candidates: list[Capability]) -> RoutingDecision:
    """Pick the best candidate: accepted > degraded > rejected (first rejection reasons are kept)."""
    if not candidates:
        return RoutingDecision(req.name, "", "rejected", ("no_dataset_registered",))
    decisions = [evaluate(req, c) for c in candidates]
    for want in ("accepted", "degraded"):
        for d in decisions:
            if d.outcome == want:
                return d
    return decisions[0]


REGISTRY: dict[str, Capability] = {c.dataset_id: c for c in [
    Capability("bars_legacy_fwd_adjusted_1d", "legacy_warehouse", "bars",
               frozenset({DAILY_BARS, ADJUSTED_PRICES, INTERVAL_VOLUME}),
               history_depth="2023-11-01..2026-11-10", notes=("turnover amount missing in source", "adjustment factors unknown")),
    Capability("bars_raw_1d", "sina", "kline_daily",
               frozenset({DAILY_BARS, RAW_PRICES, INTERVAL_VOLUME}), history_depth="up to ~1000 sessions",
               notes=("no turnover amount in this endpoint",)),
    Capability("bars_raw_1d_tencent", "tencent", "kline_day",
               frozenset({DAILY_BARS, RAW_PRICES, INTERVAL_VOLUME}), history_depth="up to 640 sessions per request",
               notes=("volume in lots converted to shares", "no turnover amount")),
    Capability("bars_raw_1m", "tencent", "kline_minute",
               frozenset({MINUTE_BARS_TRUE_OHLC, RAW_PRICES, INTERVAL_VOLUME}), history_depth="recent sessions only",
               notes=("no turnover amount",)),
    Capability("bars_raw_5m", "sina", "kline_minute",
               frozenset({MINUTE_BARS_TRUE_OHLC, RAW_PRICES, INTERVAL_VOLUME}), history_depth="recent sessions only",
               notes=("scales 5/15/30/60 only", "no turnover amount")),
    Capability("minute_close_sampled", "tencent", "minute_query",
               frozenset({MINUTE_CLOSE_SAMPLED, CUMULATIVE_ONLY, TURNOVER_AMOUNT}), estimated=True,
               sampled_interval_seconds=60, notes=("one price per minute; true high/low unobservable",)),
    Capability("quotes_l1", "sina", "hq_quote",
               frozenset({L1_QUOTE, RAW_PRICES, TURNOVER_AMOUNT, CUMULATIVE_ONLY}), notes=("cumulative day volume/amount",)),
    Capability("announcements_official", "cninfo_announcements", "his_announcement_query",
               frozenset({OFFICIAL_EVENTS}), notes=("public query endpoint; entitlement unconfirmed",)),
]}
