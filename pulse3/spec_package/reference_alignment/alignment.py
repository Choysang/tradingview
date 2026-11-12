"""Bounded observed-mode reference for Pulse input alignment.

This is neither a provider adapter nor a production PIT database query. It receives
already normalized immutable observations. It does not fill bars, alter source
timestamps, call models, or implement reconstructed availability policies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from typing import Iterable


FIELD_STATUS = {"observed", "derived", "missing", "invalid", "not_applicable", "disputed"}
# This deliberately small registry is not a claim that all production fields exist.
# shares.float is the existing catalogue dependency name, bound here to circulating
# shares. It is not a new public HTTP property or a free-float share count.
STATE_FIELDS = {"last_price_raw", "shares.float"}
SCALE = Decimal("0.000000000001")


def _numeric_context() -> Context:
    # Context(...) defaults may be changed via decimal.DefaultContext by another
    # library. Every field, flag and trap is explicit here, not just precision.
    return Context(prec=50, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                   capitals=1, clamp=0, flags=[],
                   traps=[InvalidOperation, DivisionByZero, Overflow])


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMESTAMP_REQUIRES_TIMEZONE")
    return value.astimezone(timezone.utc)


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"EMPTY_{field.upper()}")


def _decimal(value: Decimal | None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("VALUE_REQUIRES_FINITE_DECIMAL")
    integer_digits = max(value.adjusted() + 1, 0) if value else 0
    fraction_digits = max(-value.as_tuple().exponent, 0)
    if integer_digits > 26 or fraction_digits > 12:
        raise ValueError("VALUE_EXCEEDS_NUMERIC_38_12")


@dataclass(frozen=True)
class Scope:
    instrument_id: str
    source_id: str
    endpoint_id: str
    source_schema_version: str
    mapping_version: str
    field_path: str
    canonical_unit: str
    currency: str | None
    timeframe: str
    session_scope: str
    trade_date: date | None
    adjustment_mode: str
    methodology_id: str
    methodology_version: str
    snapshot_id: str

    def __post_init__(self) -> None:
        for name in (
            "instrument_id", "source_id", "endpoint_id", "source_schema_version",
            "mapping_version", "field_path", "canonical_unit", "timeframe",
            "session_scope", "adjustment_mode", "methodology_id", "methodology_version", "snapshot_id",
        ):
            _text(getattr(self, name), name)
        if self.currency is not None:
            _text(self.currency, "currency")
        if self.trade_date is not None and type(self.trade_date) is not date:
            raise ValueError("TRADE_DATE_REQUIRES_DATE")


@dataclass(frozen=True)
class Observation:
    observation_id: str
    natural_key: str
    observation_revision: int
    scope: Scope
    event_time: datetime
    available_at: datetime
    value: Decimal | None
    field_status: str = "observed"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    observation_window_start: datetime | None = None
    observation_window_end: datetime | None = None
    is_final: bool | None = None

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation_id")
        _text(self.natural_key, "natural_key")
        if type(self.observation_revision) is not int or self.observation_revision < 1:
            raise ValueError("OBSERVATION_REVISION_MUST_BE_POSITIVE_INTEGER")
        _utc(self.event_time)
        _utc(self.available_at)
        _decimal(self.value)
        if self.field_status not in FIELD_STATUS:
            raise ValueError("UNRECOGNIZED_FIELD_STATUS")
        usable = self.field_status in {"observed", "derived"}
        if usable != (self.value is not None):
            raise ValueError("FIELD_STATUS_VALUE_MISMATCH")
        begin = _utc(self.effective_from) if self.effective_from else _utc(self.event_time)
        if self.effective_to and _utc(self.effective_to) <= begin:
            raise ValueError("INVALID_EFFECTIVE_INTERVAL")
        if (self.observation_window_start is None) != (self.observation_window_end is None):
            raise ValueError("INCOMPLETE_OBSERVATION_WINDOW")
        if self.observation_window_start is not None:
            if _utc(self.observation_window_end) < _utc(self.observation_window_start):
                raise ValueError("INVALID_OBSERVATION_WINDOW")


@dataclass(frozen=True)
class AlignmentPolicy:
    alignment_policy_version: str
    selection_mode: str  # state_hold or exact_event
    max_age_seconds: int
    strategy: str = "backward"
    latest_invalid_policy: str = "surface_latest"

    def __post_init__(self) -> None:
        _text(self.alignment_policy_version, "alignment_policy_version")
        if self.selection_mode not in {"state_hold", "exact_event"}:
            raise ValueError("UNKNOWN_SELECTION_MODE")
        if self.strategy != "backward":
            raise ValueError("ONLY_BACKWARD_ALIGNMENT_IS_SUPPORTED")
        if self.latest_invalid_policy != "surface_latest":
            raise ValueError("REFERENCE_NEVER_SILENTLY_FALLS_BACK")
        if type(self.max_age_seconds) is not int or not 0 <= self.max_age_seconds <= 9007199254740991:
            raise ValueError("INVALID_MAX_AGE")


@dataclass(frozen=True)
class AlignmentResult:
    value: Decimal | None
    field_status: str
    freshness_state: str
    reason: str | None
    observation: Observation | None
    target_event_time: datetime
    decision_as_of: datetime
    alignment_policy_version: str
    age_seconds: Decimal | None


def select_asof(
    observations: Iterable[Observation], *, scope: Scope,
    target_event_time: datetime, decision_as_of: datetime,
    policy: AlignmentPolicy,
) -> AlignmentResult:
    """Select a past, then-known version. Never manufacture a new observation.

    Revisions are resolved before event-time selection, so a new invalid revision
    cannot cause the old valid revision to reappear. Different natural keys at the
    same most-recent effective/event time are ambiguous, never arbitrarily sorted.
    """
    target, cutoff = _utc(target_event_time), _utc(decision_as_of)
    if target > cutoff:
        raise ValueError("TARGET_AFTER_DECISION_CUTOFF")
    if policy.selection_mode == "state_hold" and scope.field_path not in STATE_FIELDS:
        raise ValueError("FIELD_NOT_REGISTERED_FOR_STATE_HOLD")

    def result(
        reason: str | None, obs: Observation | None = None,
        status: str = "missing", freshness: str = "unknown", value: Decimal | None = None,
    ) -> AlignmentResult:
        age = None
        if obs is not None:
            delta = target - _utc(obs.event_time)
            # Avoid float total_seconds; this remains independent of Decimal context.
            age = Decimal(delta.days * 86400 + delta.seconds)
            if delta.microseconds:
                with localcontext(_numeric_context()):
                    age += Decimal(delta.microseconds) / Decimal(1_000_000)
        return AlignmentResult(
            value, status, freshness, reason, obs, target_event_time, decision_as_of,
            policy.alignment_policy_version, age,
        )

    by_id: dict[str, Observation] = {}
    for obs in observations:
        if obs.scope != scope:
            continue
        if obs.observation_id in by_id and by_id[obs.observation_id] != obs:
            raise ValueError("OBSERVATION_ID_PAYLOAD_CONFLICT")
        by_id[obs.observation_id] = obs
    if not by_id:
        return result("NO_SCOPE_MATCH")
    eligible = [o for o in by_id.values() if _utc(o.available_at) <= cutoff]
    if not eligible:
        return result("NO_AVAILABLE_OBSERVATION")

    groups: dict[str, list[Observation]] = {}
    for obs in eligible:
        groups.setdefault(obs.natural_key, []).append(obs)
    current: list[Observation] = []
    conflicted_keys: set[str] = set()
    for group in groups.values():
        top_revision = max(o.observation_revision for o in group)
        top = [o for o in group if o.observation_revision == top_revision]
        if len(top) != 1:
            conflicted_keys.add(group[0].natural_key)
        current.extend(top)

    past = [o for o in current if _utc(o.event_time) <= target]
    if not past:
        return result("NO_PAST_EVENT")
    # Future-effective state announcements do not replace a still-effective state.
    effective = [
        o for o in past
        if _utc(o.effective_from or o.event_time) <= target
    ]
    if not effective:
        return result("NO_EFFECTIVE_OBSERVATION")
    def selection_time(o: Observation) -> datetime:
        return _utc(o.event_time if policy.selection_mode == "exact_event" else (o.effective_from or o.event_time))

    newest = max(selection_time(o) for o in effective)
    candidates = [o for o in effective if selection_time(o) == newest]
    if any(o.natural_key in conflicted_keys for o in candidates):
        return result("AMBIGUOUS_REVISION", status="disputed")
    if len(candidates) != 1:
        return result("AMBIGUOUS_OBSERVATION", status="disputed")
    chosen = candidates[0]
    if chosen.effective_to is not None and target >= _utc(chosen.effective_to):
        return result("LATEST_STATE_EXPIRED", chosen)
    if policy.selection_mode == "exact_event" and _utc(chosen.event_time) != target:
        return result("EXACT_EVENT_REQUIRED", chosen)
    age = target - _utc(chosen.event_time)
    whole_seconds = age.days * 86400 + age.seconds
    if whole_seconds > policy.max_age_seconds or (whole_seconds == policy.max_age_seconds and age.microseconds):
        return result("MAX_AGE_EXCEEDED", chosen, freshness="stale")
    if chosen.field_status not in {"observed", "derived"}:
        return result("LATEST_VALUE_UNUSABLE", chosen, status=chosen.field_status, freshness="current")
    return result(None, chosen, chosen.field_status, "current", chosen.value)


@dataclass(frozen=True)
class DerivedValue:
    feature_id: str
    definition_version: str
    instrument_id: str
    value: Decimal | None
    canonical_unit: str
    field_status: str
    quality_status: str
    reason: str | None
    observation_window_start: datetime
    observation_window_end: datetime
    decision_as_of: datetime
    computed_at: datetime
    available_at: datetime
    input_observation_ids: tuple[str, ...]
    input_snapshot_ids: tuple[str, ...]
    numeric_policy_version: str = "decimal50_scale12_half_even_v1"


def _input_times(observations: Iterable[Observation], cutoff: datetime, computed_at: datetime) -> None:
    decision = _utc(cutoff)
    computed = _utc(computed_at)
    if computed < decision:
        raise ValueError("COMPUTED_BEFORE_DECISION")
    for obs in observations:
        if _utc(obs.available_at) > decision:
            raise ValueError("INPUT_NOT_AVAILABLE_AT_DECISION")
        if _utc(obs.event_time) > decision:
            raise ValueError("INPUT_EVENT_AFTER_DECISION")


def _bar_input(obs: Observation) -> None:
    if obs.observation_window_start is None or obs.observation_window_end is None:
        raise ValueError("INTERVAL_REQUIRED")
    if _utc(obs.observation_window_start) >= _utc(obs.observation_window_end):
        raise ValueError("POSITIVE_INTERVAL_REQUIRED")
    if _utc(obs.event_time) != _utc(obs.observation_window_end):
        raise ValueError("BAR_EVENT_MUST_EQUAL_WINDOW_END")
    if obs.is_final is not True:
        raise ValueError("FINAL_BAR_REQUIRED")
    if obs.scope.adjustment_mode != "raw":
        raise ValueError("RAW_INTERVAL_REQUIRED")


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(_numeric_context()):
        value = (numerator / denominator).quantize(SCALE, rounding=ROUND_HALF_EVEN)
    if numerator > 0 and denominator > 0 and value == 0:
        raise ValueError("NUMERIC_RESOLUTION_UNDERFLOW")
    _decimal(value)
    return value


def _derived(
    feature_id: str, definition_version: str, bar: Observation,
    dependencies: tuple[Observation, ...], decision_as_of: datetime,
    computed_at: datetime, unit: str, value: Decimal | None,
    reason: str | None = None, invalid: bool = False,
) -> DerivedValue:
    _text(definition_version, "definition_version")
    _input_times(dependencies, decision_as_of, computed_at)
    ready = max([_utc(computed_at), *[_utc(o.available_at) for o in dependencies]])
    return DerivedValue(
        feature_id, definition_version, bar.scope.instrument_id, value, unit,
        "derived" if value is not None else ("invalid" if invalid else "missing"),
        "valid" if value is not None else ("invalid" if invalid else "partial"),
        reason, bar.observation_window_start, bar.observation_window_end,
        decision_as_of, computed_at, ready, tuple(o.observation_id for o in dependencies),
        tuple(o.scope.snapshot_id for o in dependencies),
    )


def derive_vwap(
    turnover: Observation, volume: Observation, *, decision_as_of: datetime,
    computed_at: datetime, definition_version: str,
) -> DerivedValue:
    """Compute a raw interval VWAP in CNY/share; never use a stale prior volume."""
    _bar_input(turnover)
    _bar_input(volume)
    _input_times((turnover, volume), decision_as_of, computed_at)
    if turnover.scope.field_path != "turnover_amount_cny" or turnover.scope.canonical_unit != "CNY":
        raise ValueError("TURNOVER_UNIT_OR_FIELD_MISMATCH")
    if volume.scope.field_path != "volume_shares" or volume.scope.canonical_unit != "shares":
        raise ValueError("VOLUME_UNIT_OR_FIELD_MISMATCH")
    if turnover.scope.currency != "CNY" or volume.scope.currency not in {"CNY", None}:
        raise ValueError("CURRENCY_MISMATCH")
    common = (
        "instrument_id", "source_id", "endpoint_id", "source_schema_version", "mapping_version",
        "timeframe", "session_scope", "trade_date", "adjustment_mode", "methodology_id", "methodology_version", "snapshot_id",
    )
    if any(getattr(turnover.scope, key) != getattr(volume.scope, key) for key in common):
        raise ValueError("INTERVAL_SCOPE_MISMATCH")
    if (
        _utc(turnover.observation_window_start), _utc(turnover.observation_window_end)
    ) != (_utc(volume.observation_window_start), _utc(volume.observation_window_end)):
        raise ValueError("INTERVAL_WINDOW_MISMATCH")
    deps = (turnover, volume)
    if turnover.value is None or volume.value is None:
        bad = any(o.field_status in {"invalid", "disputed"} for o in deps)
        return _derived("price.vwap", definition_version, turnover, deps, decision_as_of,
                        computed_at, "CNY/share", None, "UNUSABLE_INPUT", bad)
    if volume.value < 0 or volume.value != volume.value.to_integral_value() or turnover.value < 0:
        raise ValueError("NONNEGATIVE_INTEGER_VOLUME_AND_AMOUNT_REQUIRED")
    if volume.value == 0:
        return _derived("price.vwap", definition_version, turnover, deps, decision_as_of,
                        computed_at, "CNY/share", None,
                        "ZERO_VOLUME" if turnover.value == 0 else "AMOUNT_WITHOUT_VOLUME",
                        invalid=turnover.value != 0)
    if turnover.value == 0:
        return _derived("price.vwap", definition_version, turnover, deps, decision_as_of,
                        computed_at, "CNY/share", None, "VOLUME_WITHOUT_AMOUNT", invalid=True)
    return _derived("price.vwap", definition_version, turnover, deps, decision_as_of,
                    computed_at, "CNY/share", _divide(turnover.value, volume.value))


def derive_turnover_fraction(
    volume: Observation, denominator_observations: Iterable[Observation], *,
    denominator_scope: Scope, denominator_policy: AlignmentPolicy,
    decision_as_of: datetime, computed_at: datetime, definition_version: str,
) -> DerivedValue:
    """Interval volume / then-known circulating shares, returned as a fraction.

    The denominator may come from another documented source; its exact scope is
    supplied separately. This does not infer circulating shares from price/cap.
    """
    _bar_input(volume)
    _input_times((volume,), decision_as_of, computed_at)
    if volume.scope.field_path != "volume_shares" or volume.scope.canonical_unit != "shares":
        raise ValueError("VOLUME_UNIT_OR_FIELD_MISMATCH")
    if (
        denominator_scope.field_path != "shares.float"
        or denominator_scope.canonical_unit != "shares"
        or denominator_scope.methodology_id != "circulating_shares"
        or denominator_scope.instrument_id != volume.scope.instrument_id
    ):
        raise ValueError("CIRCULATING_SHARE_BASIS_REQUIRED")
    denominator = select_asof(
        denominator_observations, scope=denominator_scope,
        target_event_time=volume.observation_window_end, decision_as_of=decision_as_of,
        policy=denominator_policy,
    )
    deps = (volume,) + ((denominator.observation,) if denominator.observation else ())
    if volume.value is None or denominator.value is None:
        invalid = volume.field_status in {"invalid", "disputed"} or denominator.field_status in {"invalid", "disputed"}
        return _derived("turnover.fraction", definition_version, volume, deps, decision_as_of,
                        computed_at, "ratio", None, denominator.reason or "UNUSABLE_VOLUME", invalid)
    if _utc(denominator.observation.effective_from or denominator.observation.event_time) > _utc(volume.observation_window_start):
        return _derived("turnover.fraction", definition_version, volume, deps, decision_as_of,
                        computed_at, "ratio", None, "DENOMINATOR_CHANGED_WITHIN_WINDOW")
    if volume.value < 0 or volume.value != volume.value.to_integral_value():
        raise ValueError("NONNEGATIVE_INTEGER_VOLUME_REQUIRED")
    if denominator.value <= 0 or denominator.value != denominator.value.to_integral_value():
        raise ValueError("POSITIVE_INTEGER_CIRCULATING_SHARES_REQUIRED")
    return _derived("turnover.fraction", definition_version, volume, deps, decision_as_of,
                    computed_at, "ratio", _divide(volume.value, denominator.value))
