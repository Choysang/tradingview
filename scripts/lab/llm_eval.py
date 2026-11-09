"""E092 scoring rules, pre-registered in docs/research/2026-11-09-e092-llm-live-preregistration.md (section 3).

Pure, so backend/tests/test_lab_llm_ask.py can lock them without torch:
- `scorable(rows)`: the earliest row of each id, without the ones answered at or after their entry deadline;
- `evaluate(rows)`: the primary metric and the verdict on rows that carry `ex5` (the 5-day excess in percent).
"""
from __future__ import annotations

import math

import numpy as np
import polars as pl

from batch3_lib import block_boot_p

BLOCK, N_BOOT, SEED = 5, 2000, 20261109
MIN_DAYS, MIN_NAMES, MIN_NON_AVOID = 20, 20, 20
MAX_P, ROUND_TRIP_PCT = 0.05, 0.252
AVOID = "avoid"


def scorable(rows: list[dict]) -> list[dict]:
    """Rows of the E002 batch have no `not_after` and are kept; they carry no deadline to check."""
    kept: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("asked_at") or ""):
        if row.get("not_after") and row["asked_at"] >= row["not_after"]:
            continue
        kept.setdefault(row["id"], row)
    return list(kept.values())


def _finite(rows: list[dict], group: str) -> list[dict]:
    return [r for r in rows if r["group"] == group and r["p_outperform"] is not None and math.isfinite(r["ex5"])]


def daily_ic(rows: list[dict], min_names: int) -> tuple[list[float], int]:
    """Spearman of p_outperform and ex5 inside each decision day, in date order; also how many days had none (fewer
    than `min_names` names, so an interrupted day cannot weigh as much as a full one, or a constant p). NaN labels are
    gone before this: polars would rank one as a value."""
    if not rows:
        return [], 0
    frame = pl.DataFrame([{"d": r["decision_date"], "p": float(r["p_outperform"]), "y": float(r["ex5"])} for r in rows])
    day = frame.group_by("d").agg(pl.len().alias("n"), pl.corr("p", "y", method="spearman").alias("ic")).sort("d")
    ics = [ic for n, ic in day.select("n", "ic").iter_rows() if n >= min_names and ic is not None and math.isfinite(ic)]
    return ics, day.height - len(ics)


def _mean(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def evaluate(rows: list[dict]) -> dict:
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
    passed = ready and ic_pass and spread_pass is True
    verdict = "inconclusive" if not ready or (ic_pass and spread_pass is None) else "passed" if passed else "fails"
    return {"verdict": verdict, "days": len(ics), "days_without_ic": dropped, "ready": ready, "mean_ic": mean_ic, "p_boot": p_boot,
            "n_avoid": len(avoid), "n_other": len(other), "spread_pct": spread, "ic_pass": ic_pass,
            "spread_pass": spread_pass, "passed": passed,
            "control_mean_ic": _mean(controls)}
