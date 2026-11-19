"""Level-safety check of the fusion lane: does a factor's daily ranking survive a per-stock rescaling of price levels?

Why: the legacy bars are forward-adjusted; a dividend later rewrites past price levels, so a factor whose ranks depend on the
level (``$close``, ``$amount``, ``$cap`` ...) reads the future. The old pipeline rescaled each stock's price fields by a constant
in [0.5, 1] (float cap by a constant in [0.9, 1]) and kept a factor only when the mean Spearman correlation between the
original and the rescaled values over rows 150, 160, ... was at least 0.98.
Use here: the decision of the frozen score is DATA (``fusion/data/level_safety.json``, 392 of 428 factors safe); the old random
constants came from another generator and cannot be reproduced, so this module re-runs the test with its own seeded constants as an
audit (``fusion audit-level-safety``) and for any factor that is added later. Invariants: the data fields are never modified; the
scaled copy only changes the level fields.
Not covered: choosing a different cut, or deciding for the frozen set (the data file decides).
"""
from __future__ import annotations

import numpy as np

from .expr import F32, Evaluator
from .panel import Panel
from .scorer import daily_ic

LEVEL_FIELDS = ("open", "high", "low", "close", "vwap", "prev_close", "amount")
CUT = 0.98
SEED = 7
ROW_START, ROW_STEP = 150, 10


def scaled_fields(panel: Panel, seed: int = SEED) -> dict[str, np.ndarray]:
    """The panel's fields with the level fields multiplied by a per-stock k in [0.5, 1] and the cap by k_cap in [0.9, 1]."""
    rng = np.random.default_rng(seed)
    k = (0.5 + 0.5 * rng.random(panel.N)).astype(F32)
    k_cap = (0.9 + 0.1 * rng.random(panel.N)).astype(F32)
    f = dict(panel.f)
    for name in LEVEL_FIELDS:
        f[name] = (panel.f[name] * k[None, :]).astype(F32)
    f["cap"] = (panel.f["cap"] * k_cap[None, :]).astype(F32)
    return f


def level_safe(expr: str, panel: Panel, scaled: dict[str, np.ndarray], rows: list[int] | None = None, cut: float = CUT) -> tuple[bool, float]:
    """(safe, mean rank correlation) of a factor on the original and the rescaled panel; an expression that gives no correlation is unsafe."""
    rows = rows if rows is not None else list(range(ROW_START, panel.T, ROW_STEP))
    a = Evaluator(panel.f, cache_size=4)(expr)[rows]
    b = Evaluator(scaled, cache_size=4)(expr)[rows]
    rho = daily_ic(a, b, min_n=50)
    mean = float(np.nanmean(rho)) if np.isfinite(rho).any() else float("nan")
    return bool(mean >= cut), mean
