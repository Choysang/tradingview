"""E092 judgement rule (pre-registration section 3), ported unchanged from ``scripts/lab/llm_eval.py``.

Why: the success rule is written once and kept pure so the tests can lock it. ``ex5`` is the 5-day excess in percent (buy at the D+1 open, sell at the D+6 open, minus the same day's
mean over the tradable main board).
Invariants: only rows with a finite ``ex5`` and a ``p_outperform`` count; the primary metric is the mean over decision days of the Spearman rank correlation of ``p_outperform`` and
``ex5`` inside the top-30 group, a day counting only when it has at least 20 names; pass needs mean IC > 0 with a one-sided 5-day block-bootstrap p < 0.05 AND a mean excess of the
non-"avoid" names over the "avoid" names above the round-trip cost 0.252 % (undecidable, never a pass, below 20 non-avoid names); fewer than 20 valid days is ``inconclusive``.
The 10 random controls are reported with the same metric and never judged.
Not covered: computing ``ex5`` (``application.bf_llm_live_inputs``), writing the verdict row (``bf_llm_live_service``).
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import polars as pl

from pulse.domain.fusion.forward import block_boot_p

BLOCK, N_BOOT, SEED = 5, 2000, 20261109
MIN_DAYS, MIN_NAMES, MIN_NON_AVOID = 20, 20, 20
MAX_P, ROUND_TRIP_PCT = 0.05, 0.252
AVOID = "avoid"


def _finite(rows: Sequence[dict], group: str) -> list[dict]:
    return [r for r in rows if r["group"] == group and r.get("p_outperform") is not None and r.get("ex5") is not None and math.isfinite(r["ex5"])]


def daily_ic(rows: Sequence[dict], min_names: int) -> tuple[list[float], int]:
    """Spearman of p_outperform and ex5 inside each decision day, in date order, and how many days had none (fewer than ``min_names`` names, or a constant p).
    Days with an incomplete set weigh nothing rather than a little. Non-finite labels are dropped before this (polars would rank a NaN as a value)."""
    if not rows:
        return [], 0
    frame = pl.DataFrame([{"d": r["decision_date"], "p": float(r["p_outperform"]), "y": float(r["ex5"])} for r in rows])
    day = frame.group_by("d").agg(pl.len().alias("n"), pl.corr("p", "y", method="spearman").alias("ic")).sort("d")
    ics = [ic for n, ic in day.select("n", "ic").iter_rows() if n >= min_names and ic is not None and math.isfinite(ic)]
    return ics, day.height - len(ics)


def _mean(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if len(values) else None


def evaluate(rows: Sequence[dict]) -> dict:
    """The verdict on scored rows (keys decision_date, group, action, p_outperform, ex5)."""
    top = _finite(rows, "top")
    ics, dropped = daily_ic(top, MIN_NAMES)
    p_boot = block_boot_p(np.array(ics), BLOCK, N_BOOT, SEED) if ics else None
    mean_ic = _mean(ics)
    avoid = [r["ex5"] for r in top if r.get("action") == AVOID]
    other = [r["ex5"] for r in top if r.get("action") != AVOID]
    spread = _mean(other) - _mean(avoid) if avoid and other else None
    ic_pass = mean_ic is not None and p_boot is not None and mean_ic > 0 and p_boot < MAX_P
    spread_pass = None if len(other) < MIN_NON_AVOID or spread is None else spread > ROUND_TRIP_PCT
    ready = len(ics) >= MIN_DAYS
    controls, _ = daily_ic(_finite(rows, "control"), 3)
    passed = bool(ready and ic_pass and spread_pass is True)
    verdict = "inconclusive" if not ready or (ic_pass and spread_pass is None) else "passed" if passed else "fails"
    return {"verdict": verdict, "days": len(ics), "days_without_ic": dropped, "ready": ready, "mean_ic": mean_ic, "p_boot": p_boot,
            "n_avoid": len(avoid), "n_other": len(other), "spread_pct": spread, "ic_pass": bool(ic_pass), "spread_pass": spread_pass, "passed": passed,
            "control_mean_ic": _mean(controls)}
