"""TriggerPolicy state machine: when a TRUE condition may actually fire.

Why: SPEC §7. Single vs repeated firing, cooldown, per-day count, bar-closed requirement
and the open-order block are all written into the contract ``TriggerPolicy``; this module
interprets them deterministically.
Semantics (documented choices where the contract is terse):
  * UNKNOWN never fires (``unknown_policy = do_not_fire``) and does not touch the state;
  * ``max_fires`` is the number of fires allowed per trading date (per-day count); with
    ``rearm.mode = never`` at most one fire ever happens regardless of the date;
  * ``on_transition_to_true`` fires only when the last *known* value was not TRUE
    (UNKNOWN in between does not create a transition); ``repeated_while_true`` fires on each
    TRUE evaluation subject to min_interval / rearm / max_fires;
  * rearm ``after_false`` needs a FALSE since the last fire; ``after_cooldown`` needs
    ``cooldown_ms`` elapsed; ``min_interval_ms`` applies in every mode;
  * ``require_final_bar``: a TRUE on a still-forming bar does not fire and is not remembered;
  * pending order: ``block_retrigger`` blocks; ``net_target_delta`` fires but flags that the
    order must net against the pending target.
Not covered: placing orders (execution package), evaluating the condition (conditions.py).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from pulse.domain.plans.conditions import Tri


@dataclass(frozen=True)
class TriggerState:
    fires_total: int = 0
    fires_today: int = 0
    today: date | None = None
    last_fire_at: datetime | None = None
    last_known: Tri | None = None
    false_seen_since_fire: bool = False


@dataclass(frozen=True)
class TriggerDecision:
    fire: bool
    reason: str
    net_target_delta: bool = False


def decide_trigger(
    policy: dict[str, Any],
    state: TriggerState,
    value: Tri,
    *,
    now: datetime,
    trade_date: date,
    bar_final: bool,
    pending_order: bool,
) -> tuple[TriggerDecision, TriggerState]:
    if policy.get("unknown_policy") != "do_not_fire":
        raise ValueError("unknown_policy must be do_not_fire")
    st = state
    if st.today != trade_date:  # new trading date resets the per-day count only
        st = replace(st, today=trade_date, fires_today=0)
    if value == Tri.UNKNOWN:
        return TriggerDecision(False, "unknown_do_not_fire"), st
    if value == Tri.FALSE:
        return TriggerDecision(False, "condition_false"), replace(st, last_known=Tri.FALSE, false_seen_since_fire=True)
    # value is TRUE
    if policy["require_final_bar"] and not bar_final:
        return TriggerDecision(False, "bar_not_final"), st
    if policy["fire_mode"] == "on_transition_to_true" and st.last_known == Tri.TRUE:
        return TriggerDecision(False, "not_a_transition"), st
    rearm = policy["rearm"]
    if st.fires_total > 0:
        if rearm["mode"] == "never":
            return TriggerDecision(False, "rearm_never"), st
        if rearm["mode"] == "after_false" and not st.false_seen_since_fire:
            return TriggerDecision(False, "rearm_needs_false"), st
        if rearm["mode"] == "after_cooldown" and st.last_fire_at is not None:
            if (now - st.last_fire_at).total_seconds() * 1000 < rearm["cooldown_ms"]:
                return TriggerDecision(False, "cooldown"), st
    if st.last_fire_at is not None and (now - st.last_fire_at).total_seconds() * 1000 < policy["min_interval_ms"]:
        return TriggerDecision(False, "min_interval"), st
    if st.fires_today >= policy["max_fires"]:
        return TriggerDecision(False, "max_fires_per_day"), st
    net = False
    if pending_order:
        if policy["pending_order_policy"] == "block_retrigger":
            return TriggerDecision(False, "pending_order_block"), st
        net = True
    new = replace(
        st,
        fires_total=st.fires_total + 1,
        fires_today=st.fires_today + 1,
        last_fire_at=now,
        last_known=Tri.TRUE,
        false_seen_since_fire=False,
    )
    return TriggerDecision(True, "fired", net_target_delta=net), new
