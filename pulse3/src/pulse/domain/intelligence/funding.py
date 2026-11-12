"""Fund-flow observations with method dependency (spec FINAL §5 / sources_sectors §7, SS-A12, SS-A27).

Why: a platform-computed "main-force net inflow" is a vendor METHOD output. Two vendors, or two methodology
versions of one vendor, are different series: they are never summed, averaged or spliced. Gross turnover
carries no direction and cannot be turned into a net flow.

Invariants: every observation carries (provider, metric namespace, metric name, methodology id/version,
measurement class, side convention, window); ``series_key`` is the only grouping allowed; ``combine`` /
``net_from_gross`` refuse mixed methods or missing direction; unknown is None, never 0; a bar fetched before
the vendor completes the day is ``partial``. Amounts are CNY ``Decimal``.
Not covered: exchange-disclosed margin/northbound data, ETF share changes, re-deriving large-order thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .feeds import FFLOW_COLUMNS, FundFlowDay

CST = ZoneInfo("Asia/Shanghai")
NAMESPACE = "eastmoney.fund_flow"
METHODOLOGY_ID = "eastmoney-software-order-size-class"
METHODOLOGY_VERSION = "observed-20261111"  # the vendor publishes no version; this is the observation epoch of the mapping
FINAL_AFTER = time(16, 0)

AMOUNT_METRICS = {"main_net": "main_net_inflow", "small_net": "small_net_inflow", "medium_net": "medium_net_inflow",
                  "large_net": "large_net_inflow", "super_large_net": "super_large_net_inflow"}
RATIO_METRICS = {"main_ratio": "main_net_inflow_ratio", "small_ratio": "small_net_inflow_ratio", "medium_ratio": "medium_net_inflow_ratio",
                 "large_ratio": "large_net_inflow_ratio", "super_large_ratio": "super_large_net_inflow_ratio"}
assert set(AMOUNT_METRICS) | set(RATIO_METRICS) == set(FFLOW_COLUMNS[1:11])


class MethodMismatch(ValueError):
    """Observations from different providers / methodologies cannot be combined (SS-A12)."""


class NoDirection(ValueError):
    """A gross turnover figure has no buy/sell direction, so no net flow can be derived (SS-A27)."""


@dataclass(frozen=True)
class FundingObs:
    provider_id: str
    metric_namespace: str
    metric_name: str
    methodology_id: str
    methodology_version: str
    measurement_class: str  # disclosed_observation | provider_derived | research_inference
    side_convention: str
    value: Decimal | None
    unit: str  # CNY | percent
    window_start: datetime
    window_end: datetime
    code: str | None = None
    market_direction: str = "not_applicable"
    trade_or_order_basis: str = "unknown"

    @property
    def series_key(self) -> tuple:
        return (self.provider_id, self.metric_namespace, self.metric_name, self.methodology_id, self.methodology_version, self.measurement_class,
                self.side_convention, self.unit)


def combine(obs: list[FundingObs]) -> Decimal:
    """Sum of amounts of ONE series. Mixed methods raise; missing values make the sum unknown (raise), not zero."""
    if not obs:
        raise ValueError("nothing to combine")
    keys = {o.series_key for o in obs}
    if len(keys) != 1:
        raise MethodMismatch(f"{len(keys)} distinct series; cross-method sums are not facts")
    if any(o.value is None for o in obs):
        raise ValueError("missing value inside series: sum is unknown, not zero")
    return sum((o.value for o in obs if o.value is not None), Decimal(0))


def net_from_gross(o: FundingObs) -> Decimal:
    """Always refuses: a net flow must be a published metric of its own, never derived from turnover/volume."""
    raise NoDirection(f"{o.metric_name} ({o.side_convention}) has no recoverable buy/sell difference")


def split_series(obs: list[FundingObs]) -> dict[tuple, list[FundingObs]]:
    out: dict[tuple, list[FundingObs]] = {}
    for o in obs:
        out.setdefault(o.series_key, []).append(o)
    return out


def day_window(d: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(d)
    start = datetime.combine(day, time(0, 0), tzinfo=CST)
    return start, start + timedelta(days=1)


def is_final(trade_date: str, fetched_at: datetime) -> bool:
    """A vendor day bar is final only after the evening completion time; before that the day is a partial accumulation."""
    day = date.fromisoformat(trade_date)
    return fetched_at.astimezone(CST) >= datetime.combine(day, FINAL_AFTER, tzinfo=CST)


def observations_from_days(days: tuple[FundFlowDay, ...], code: str) -> list[FundingObs]:
    out = []
    for d in days:
        ws, we = day_window(d.trade_date)
        for col, name in {**AMOUNT_METRICS, **RATIO_METRICS}.items():
            unit = "CNY" if col in AMOUNT_METRICS else "percent"
            out.append(FundingObs("eastmoney", NAMESPACE, name, METHODOLOGY_ID, METHODOLOGY_VERSION, "provider_derived",
                                  "platform_order_size_class_net", d.values[col], unit, ws, we, code))
    return out
