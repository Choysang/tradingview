"""Cross-source field reconciliation (M1-E01/E02, RECON §4, reference_alignment).

Why: two sources may only be compared when they describe the same economic
observation (instrument, date, timeframe, session, finality, adjustment basis).
Differences are classified, never averaged: ``matched`` (inside the declared
field tolerance), ``disputed`` (comparable but different), ``unverifiable``
(different basis/scope or missing value). Results map onto
``ops.reconciliation_result.reconciliation_status``.

Invariants: tolerances are per field and versioned (``tolerance_policy_version``),
not one percentage; a legacy forward-adjusted close versus a raw close is
``unverifiable`` with the implied factor recorded, not a dispute; volume is never
adjusted so it is comparable across adjustment bases.
Not covered: choosing a winner / writing the canonical value (selection policy).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

TOLERANCE_POLICY_VERSION = "daily-xsrc-v1"
ALIGNMENT_POLICY_VERSION = "daily-same-session-v1"
PRICE_ABS_TOL = Decimal("0.0051")        # one price tick plus provider rounding
VOLUME_ABS_TOL = 100                      # one board lot (providers round to lots)
VOLUME_REL_TOL = Decimal("0.001")


@dataclass(frozen=True)
class FieldObs:
    source: str
    value: Decimal | int | None
    adjustment_mode: str = "raw"
    is_final: bool = True
    session_scope: str = "regular_continuous_v1"
    unit: str = ""


@dataclass(frozen=True)
class FieldResult:
    field_path: str
    status: str            # matched | disputed | unverifiable
    reasons: tuple[str, ...]
    trace: dict
    independent_origin_count: int = 2


def compare_close(left: FieldObs, right: FieldObs, *, field_path: str = "close_price") -> FieldResult:
    trace = {"left": {"source": left.source, "value": str(left.value), "mode": left.adjustment_mode},
             "right": {"source": right.source, "value": str(right.value), "mode": right.adjustment_mode}}
    if left.value is None or right.value is None:
        return FieldResult(field_path, "unverifiable", ("missing_value",), trace)
    if left.session_scope != right.session_scope:
        return FieldResult(field_path, "unverifiable", ("session_scope_differs",), trace)
    if not (left.is_final and right.is_final):
        return FieldResult(field_path, "unverifiable", ("not_final",), trace)
    a, b = Decimal(left.value), Decimal(right.value)
    diff = abs(a - b)
    if diff <= PRICE_ABS_TOL:
        return FieldResult(field_path, "matched", (), {**trace, "abs_diff": str(diff)})
    if left.adjustment_mode != right.adjustment_mode:
        trace["implied_factor"] = str((a / b).quantize(Decimal("0.000001"))) if b else None
        return FieldResult(field_path, "unverifiable", ("adjustment_basis_differs",), trace)
    return FieldResult(field_path, "disputed", ("price_difference_exceeds_tolerance",), {**trace, "abs_diff": str(diff)})


def compare_volume(left: FieldObs, right: FieldObs, *, field_path: str = "volume_shares") -> FieldResult:
    trace = {"left": {"source": left.source, "value": str(left.value)}, "right": {"source": right.source, "value": str(right.value)}}
    if left.value is None or right.value is None:
        return FieldResult(field_path, "unverifiable", ("missing_value",), trace)
    if left.session_scope != right.session_scope:
        return FieldResult(field_path, "unverifiable", ("session_scope_differs",), trace)
    if not (left.is_final and right.is_final):
        return FieldResult(field_path, "unverifiable", ("not_final",), trace)
    a, b = int(left.value), int(right.value)
    diff = abs(a - b)
    tol = max(VOLUME_ABS_TOL, int(Decimal(max(a, b)) * VOLUME_REL_TOL))
    trace.update({"abs_diff": diff, "tolerance": tol})
    if diff <= tol:
        return FieldResult(field_path, "matched", (), trace)
    for label, scale in (("lots_vs_shares", 100),):
        if a and b and (a == b * scale or b == a * scale or abs(a - b * scale) <= tol * scale or abs(b - a * scale) <= tol * scale):
            return FieldResult(field_path, "disputed", (f"unit_change_suspected:{label}",), trace)
    return FieldResult(field_path, "disputed", ("volume_difference_exceeds_tolerance",), trace)
