"""Three-valued condition tree evaluator (TRUE / FALSE / UNKNOWN).

Why: SPEC §7. The TradePlan condition tree is the ``Condition`` contract (all / any / not,
compare, crosses, within_session, event_state). Semantics are Kleene three-valued logic:
a missing input yields UNKNOWN - never 0 (a missing MACD does not compare as 0), and
UNKNOWN never fires a trigger (``unknown_policy = do_not_fire``).
Invariants:
  * all: any FALSE -> FALSE; all TRUE -> TRUE; otherwise UNKNOWN. any is the dual;
  * crosses needs TWO confirmed points (previous_valid within ``max_age_ms`` and current),
    otherwise UNKNOWN - a single current value never proves a cross;
  * a compare under a ``bar_close`` clock needs a *confirmed* (closed-bar) observation;
  * units must match between operands (static error, not a silent conversion).
Not covered: resolving FeatureKeys to values (the ``EvalContext`` port), executing orders.
The tree is the wire dict itself so the evaluated object is exactly the contract object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

Cond = dict[str, Any]


class Tri(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class ConditionError(ValueError):
    """The condition tree is malformed or mixes units (a compile-time defect)."""


@dataclass(frozen=True)
class Observation:
    value: Decimal
    observed_at: datetime
    confirmed: bool  # true = from a closed bar / final value


class EvalContext(Protocol):
    now: datetime

    def current(self, operand: dict[str, Any]) -> Observation | None: ...
    def previous(self, operand: dict[str, Any]) -> Observation | None: ...
    def session_kind(self) -> str | None: ...
    def event(self, event_id: str) -> tuple[int, str] | None: ...


@dataclass
class StaticContext:
    """Simple in-memory context keyed by ``(feature_id, instrument_id, field_path)``."""

    now: datetime
    values: dict[tuple[str, str, str], Observation] = field(default_factory=dict)
    previous_values: dict[tuple[str, str, str], Observation] = field(default_factory=dict)
    session: str | None = None
    events: dict[str, tuple[int, str]] = field(default_factory=dict)

    @staticmethod
    def key(operand: dict[str, Any]) -> tuple[str, str, str]:
        return (operand["feature_id"], str(operand["instrument_id"]), operand["field_path"])

    def current(self, operand):
        return self.values.get(self.key(operand))

    def previous(self, operand):
        return self.previous_values.get(self.key(operand))

    def session_kind(self):
        return self.session

    def event(self, event_id):
        return self.events.get(event_id)


@dataclass(frozen=True)
class Result:
    value: Tri
    unknown_reasons: tuple[str, ...] = ()


def _operand_unit(op: dict[str, Any]) -> str:
    return op["unit"]


def validate_condition(cond: Cond) -> None:
    """Static checks: node shapes, non-empty children, unit-consistent comparisons."""
    node = cond.get("node")
    if node in ("all", "any"):
        if not cond.get("children"):
            raise ConditionError(f"{node} needs at least one child")
        for c in cond["children"]:
            validate_condition(c)
    elif node == "not":
        validate_condition(cond["child"])
    elif node in ("compare", "crosses"):
        if _operand_unit(cond["left"]) != _operand_unit(cond["right"]):
            raise ConditionError(
                f"unit mismatch {cond['left']['unit']!r} vs {cond['right']['unit']!r}: no implicit conversion"
            )
    elif node in ("within_session", "event_state"):
        pass
    else:
        raise ConditionError(f"unknown condition node {node!r}")


def _cmp(op: str, a: Decimal, b: Decimal) -> bool:
    return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b, "eq": a == b}[op]


def _resolve(operand: dict[str, Any], ctx: EvalContext, *, previous: bool) -> Observation | None:
    if operand["kind"] == "constant":
        # A constant is trivially confirmed at every time point.
        return Observation(Decimal(operand["value"]), ctx.now, True)
    return ctx.previous(operand) if previous else ctx.current(operand)


def evaluate(cond: Cond, ctx: EvalContext) -> Result:
    node = cond["node"]
    if node in ("all", "any"):
        rs = [evaluate(c, ctx) for c in cond["children"]]
        unknowns = tuple(u for r in rs for u in r.unknown_reasons)
        vals = [r.value for r in rs]
        decisive, neutral = (Tri.FALSE, Tri.TRUE) if node == "all" else (Tri.TRUE, Tri.FALSE)
        if decisive in vals:
            return Result(decisive)  # decisive wins over unknowns (Kleene)
        if all(v == neutral for v in vals):
            return Result(neutral)
        return Result(Tri.UNKNOWN, unknowns)
    if node == "not":
        r = evaluate(cond["child"], ctx)
        flip = {Tri.TRUE: Tri.FALSE, Tri.FALSE: Tri.TRUE, Tri.UNKNOWN: Tri.UNKNOWN}[r.value]
        return Result(flip, r.unknown_reasons)
    if node == "compare":
        return _compare(cond, ctx)
    if node == "crosses":
        return _crosses(cond, ctx)
    if node == "within_session":
        kind = ctx.session_kind()
        if kind is None:
            return Result(Tri.UNKNOWN, ("session_unknown",))
        return Result(Tri.TRUE if kind in cond["session_kinds"] else Tri.FALSE)
    if node == "event_state":
        ev = ctx.event(str(cond["event_id"]))
        if ev is None:
            return Result(Tri.UNKNOWN, (f"event_missing:{cond['event_id']}",))
        revision, state = ev
        if revision < cond["minimum_revision"]:
            return Result(Tri.UNKNOWN, (f"event_revision_too_old:{cond['event_id']}",))
        return Result(Tri.TRUE if state in cond["states"] else Tri.FALSE)
    raise ConditionError(f"unknown condition node {node!r}")


def _needs_confirmed(cond: Cond) -> bool:
    return cond["clock"]["kind"] == "bar_close"


def _compare(cond: Cond, ctx: EvalContext) -> Result:
    if _operand_unit(cond["left"]) != _operand_unit(cond["right"]):
        raise ConditionError("unit mismatch in compare")
    left, right = _resolve(cond["left"], ctx, previous=False), _resolve(cond["right"], ctx, previous=False)
    unknown: list[str] = []
    for name, obs, op in (("left", left, cond["left"]), ("right", right, cond["right"])):
        if obs is None:
            unknown.append(f"missing:{op.get('feature_id', 'constant')}")
        elif _needs_confirmed(cond) and not obs.confirmed:
            unknown.append(f"unconfirmed_bar:{op.get('feature_id', 'constant')}")
    if unknown:
        return Result(Tri.UNKNOWN, tuple(unknown))
    return Result(Tri.TRUE if _cmp(cond["operator"], left.value, right.value) else Tri.FALSE)  # type: ignore[union-attr]


def _crosses(cond: Cond, ctx: EvalContext) -> Result:
    if _operand_unit(cond["left"]) != _operand_unit(cond["right"]):
        raise ConditionError("unit mismatch in crosses")
    max_age_ms = cond["previous_observation"]["max_age_ms"]
    cur_l, cur_r = _resolve(cond["left"], ctx, previous=False), _resolve(cond["right"], ctx, previous=False)
    prv_l, prv_r = _resolve(cond["left"], ctx, previous=True), _resolve(cond["right"], ctx, previous=True)
    unknown: list[str] = []
    for tag, obs in (("current_left", cur_l), ("current_right", cur_r), ("previous_left", prv_l), ("previous_right", prv_r)):
        if obs is None:
            unknown.append(f"missing:{tag}")
        elif not obs.confirmed:
            unknown.append(f"unconfirmed:{tag}")
    if unknown:
        return Result(Tri.UNKNOWN, tuple(unknown))
    for tag, obs in (("previous_left", prv_l), ("previous_right", prv_r)):
        if (ctx.now - obs.observed_at).total_seconds() * 1000 > max_age_ms:  # type: ignore[union-attr]
            unknown.append(f"stale:{tag}")
    if unknown:
        return Result(Tri.UNKNOWN, tuple(unknown))
    if cond["direction"] == "above":
        crossed = prv_l.value <= prv_r.value and cur_l.value > cur_r.value  # type: ignore[union-attr]
    else:
        crossed = prv_l.value >= prv_r.value and cur_l.value < cur_r.value  # type: ignore[union-attr]
    return Result(Tri.TRUE if crossed else Tri.FALSE)


# ---------------------------------------------------------------- tree builders (wire dicts)


def feature_operand(
    *, feature_id: str, instrument_id: Any, timeframe: str, field_path: str, unit: str,
    definition_version: str = "1", resolution_policy_version: str = "latest-completed-bar-v1",
    initial_feature_key_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "feature", "feature_id": feature_id, "definition_version": definition_version,
        "instrument_id": str(instrument_id), "timeframe": timeframe, "field_path": field_path,
        "unit": unit, "resolution_policy_version": resolution_policy_version,
        "initial_feature_key_hash": initial_feature_key_hash,
    }


def constant(value: Decimal | str, unit: str) -> dict[str, Any]:
    from pulse.contracts import decimal_str

    return {"kind": "constant", "value": decimal_str(Decimal(str(value))), "unit": unit}


def compare(operator: str, left: dict, right: dict, clock: dict) -> Cond:
    return {"node": "compare", "clock": clock, "operator": operator, "left": left, "right": right}


def crosses(direction: str, left: dict, right: dict, clock: dict, max_age_ms: int) -> Cond:
    return {
        "node": "crosses", "clock": clock, "direction": direction, "left": left, "right": right,
        "previous_observation": {"selection": "previous_valid", "max_age_ms": max_age_ms},
    }


def all_of(clock: dict, *children: Cond) -> Cond:
    return {"node": "all", "clock": clock, "children": list(children)}


def any_of(clock: dict, *children: Cond) -> Cond:
    return {"node": "any", "clock": clock, "children": list(children)}


def not_(clock: dict, child: Cond) -> Cond:
    return {"node": "not", "clock": clock, "child": child}


def within_session(clock: dict, *kinds: str) -> Cond:
    return {"node": "within_session", "clock": clock, "session_kinds": list(kinds)}


def event_state(clock: dict, event_id: Any, minimum_revision: int, *states: str) -> Cond:
    return {"node": "event_state", "clock": clock, "event_id": str(event_id),
            "minimum_revision": minimum_revision, "states": list(states)}


def bar_close_clock(timeframe: str, session_policy_version: str) -> dict[str, Any]:
    return {"kind": "bar_close", "timeframe": timeframe, "session_policy_version": session_policy_version}
