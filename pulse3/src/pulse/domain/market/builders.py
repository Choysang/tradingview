"""From normalized source records to canonical bars and observation payloads (W04, M1-E02).

Why: one place decides how a normalized record becomes a ``Bar`` (daily or minute),
what its finality is, and how it is rendered in the contract wire form
(``CanonicalBar`` / ``AdjustedBar``) so every payload that leaves the data plane can
be checked with ``contracts.check``.

Invariants: finality needs explicit evidence under a named policy - a daily bar is
final only when captured at/after 16:00 CST of its trade date (2026 after-hours
trading and vendor completion make 15:00 unsafe); a minute bar only when captured
>= 60 s after its close; ``available_at`` is the actual capture/normalization time;
minute bars outside the regular grid are rejected, not clipped; observation field
quality stores non-observed fields explicitly and ONE profile-level entry for the
plainly observed ones (bulk-size compromise, declared in ``FQ_SUMMARY_KEY``).
Not covered: persistence, source fetching.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from pulse.contracts import decimal_str, utc_z

from .bars import SESSION_SCOPE_REGULAR, Bar, minute_grid
from .calendar import CST, session_close_utc, session_open_utc

DAILY_FINAL_POLICY = "daily-final-after-1600cst-v1"
MINUTE_FINAL_POLICY = "minute-final-after-close-plus-60s-v1"
FQ_SUMMARY_KEY = "*"  # profile-level entry covering every field not listed explicitly


def daily_is_final(trade_date: date, available_at: datetime) -> bool:
    return available_at >= datetime.combine(trade_date, time(16, 0), tzinfo=CST)


def minute_is_final(bar_close: datetime, available_at: datetime) -> bool:
    return available_at >= bar_close + timedelta(seconds=60)


def _iso(dt: str | None) -> datetime | None:
    return None if dt is None else datetime.fromisoformat(dt.replace("Z", "+00:00"))


def daily_bar(n: dict, *, adjusted: bool = False, adjustment_version: str | None = None) -> Bar:
    d = date.fromisoformat(n["trade_date"])
    avail = _iso(n["available_at"])
    pfx = ("open_price", "high_price", "low_price", "close_price") if adjusted else (
        "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw")
    return Bar(
        instrument_id=n["instrument_id"], timeframe="1d", session_scope=SESSION_SCOPE_REGULAR, trade_date=d,
        bar_open_time=session_open_utc(d), bar_close_time=session_close_utc(d),
        open=n.get(pfx[0]), high=n.get(pfx[1]), low=n.get(pfx[2]), close=n.get(pfx[3]),
        volume_shares=n.get("volume_shares"), turnover_cny=n.get("turnover_amount_cny"), available_at=avail,
        is_final=daily_is_final(d, avail) and n.get("quality_status") in ("valid", "partial") and not n["issues"],
        quality_status=n["quality_status"], exchange_reference_price=n.get("exchange_reference_price_raw"),
        adjustment_mode="forward_adjusted" if adjusted else "raw", adjustment_version=adjustment_version,
        issues=tuple(n["issues"]))


class OutOfSession(ValueError):
    pass


def minute_bar(n: dict, minutes: int) -> Bar:
    close = _iso(n["event_time"])
    assert close is not None
    d = close.astimezone(CST).date()
    if close not in set(minute_grid(d)):
        raise OutOfSession(f"bar close {utc_z(close)} is outside the regular continuous grid")
    avail = _iso(n["available_at"])
    return Bar(
        instrument_id=n["instrument_id"], timeframe=f"{minutes}m", session_scope=SESSION_SCOPE_REGULAR, trade_date=d,
        bar_open_time=close - timedelta(minutes=minutes), bar_close_time=close,
        open=n.get("open_price_raw"), high=n.get("high_price_raw"), low=n.get("low_price_raw"), close=n.get("close_price_raw"),
        volume_shares=n.get("volume_shares"), turnover_cny=n.get("turnover_amount_cny"), available_at=avail,
        is_final=minute_is_final(close, avail) and not n["issues"], quality_status=n["quality_status"],
        issues=tuple(n["issues"]))


def compact_field_quality(n: dict, profile_id: str) -> dict:
    """Explicit entries for non-observed fields + one profile-level entry for the rest."""
    fq: dict[str, Any] = {}
    sample = next(iter(n["field_quality"].values()))
    for name, entry in n["field_quality"].items():
        if entry["status"] != "observed":
            fq[name] = entry
    fq[FQ_SUMMARY_KEY] = {
        "status": "observed", "reason": None, "raw_object_id": sample["raw_object_id"], "source_id": sample["source_id"],
        "endpoint_id": sample["endpoint_id"], "mapping_version": sample["mapping_version"], "raw_value": None,
        "raw_unit": None, "conversion": f"profile:{profile_id}", "value_origin": "primary_source",
        "precision_note": "all fields not listed explicitly were converted by the declared profile operations"}
    return fq


def wire_bar_payload(bar: Bar, *, snapshot_id: str, field_quality: dict) -> dict:
    """CanonicalBar / AdjustedBar wire payload (validated by callers with contracts.check)."""
    base = {
        "instrument_id": bar.instrument_id, "trade_date": bar.trade_date.isoformat(), "timeframe": bar.timeframe,
        "session_scope": bar.session_scope, "bar_open_time": utc_z(bar.bar_open_time), "bar_close_time": utc_z(bar.bar_close_time),
        "snapshot_id": snapshot_id, "currency": "CNY", "volume_shares": bar.volume_shares,
        "turnover_amount_cny": decimal_str(bar.turnover_cny), "is_final": bar.is_final, "available_at": utc_z(bar.available_at),
        "quality_status": bar.quality_status, "field_quality": field_quality,
    }
    if bar.adjustment_mode == "raw":
        return {**base, "adjustment_mode": "raw", "open_price_raw": decimal_str(bar.open), "high_price_raw": decimal_str(bar.high),
                "low_price_raw": decimal_str(bar.low), "close_price_raw": decimal_str(bar.close),
                "exchange_reference_price_raw": decimal_str(bar.exchange_reference_price)}
    return {**base, "adjustment_mode": bar.adjustment_mode, "adjustment_version": bar.adjustment_version,
            "open_price": decimal_str(bar.open), "high_price": decimal_str(bar.high), "low_price": decimal_str(bar.low),
            "close_price": decimal_str(bar.close), "raw_snapshot_id": snapshot_id}


def observation_payload(bar: Bar) -> dict:
    """Compact canonical payload stored in ingest.observation (snapshot-independent)."""
    p: dict[str, Any] = {
        "trade_date": bar.trade_date.isoformat(), "timeframe": bar.timeframe, "session_scope": bar.session_scope,
        "bar_open_time": utc_z(bar.bar_open_time), "bar_close_time": utc_z(bar.bar_close_time),
        "volume_shares": bar.volume_shares, "turnover_amount_cny": decimal_str(bar.turnover_cny),
        "adjustment_mode": bar.adjustment_mode, "is_final": bar.is_final}
    keys = ("open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw") if bar.adjustment_mode == "raw" else (
        "open_price", "high_price", "low_price", "close_price")
    for k, v in zip(keys, (bar.open, bar.high, bar.low, bar.close)):
        p[k] = decimal_str(v)
    if bar.adjustment_version:
        p["adjustment_version"] = bar.adjustment_version
    if bar.exchange_reference_price is not None:
        p["exchange_reference_price_raw"] = decimal_str(bar.exchange_reference_price)
    return p


def bar_from_payload(instrument_id: str, p: dict, available_at: datetime, quality: str) -> Bar:
    """Rebuild a Bar from an observation payload (used for reconciliation and replays)."""
    raw = p["adjustment_mode"] == "raw"
    keys = ("open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw") if raw else (
        "open_price", "high_price", "low_price", "close_price")

    def d(k: str) -> Decimal | None:
        return None if p.get(k) is None else Decimal(p[k])

    return Bar(instrument_id, p["timeframe"], p["session_scope"], date.fromisoformat(p["trade_date"]),
               _iso(p["bar_open_time"]), _iso(p["bar_close_time"]), d(keys[0]), d(keys[1]), d(keys[2]), d(keys[3]),
               p["volume_shares"], d("turnover_amount_cny"), available_at, p["is_final"], quality,
               d("exchange_reference_price_raw"), p["adjustment_mode"], p.get("adjustment_version"))
