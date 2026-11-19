"""Decision statistics of the forward logs: E077 day-block bootstrap, E078 slope with Newey-West t, E089 counts (pre-registered tests).

Why: each forward experiment has a decision day written before the first result (E077: the 40th session with at least 25 signal days; E078:
the 60th session; E089: 10 independent panic episodes; the paper replay: 20 recorded sessions). Until then the reads say "pending" with the
progress, never a verdict. The numbers below are the old scripts' own (same seed, same resampling), so the old and the new log agree.
Invariants: a resampling unit is a session (the holding period overnight does not overlap); a p-value needs at least two sessions; nothing
here reads a number that is not in the stored rows.
Not covered: choosing a verdict beyond the pre-registered rule (works / mixed / fails is reported with its conditions, never promoted).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

import numpy as np

from .common import D, mean, q
from .tail import E077_CHECK_AT, E077_MIN_SIGNAL_DAYS, E078_CHECK_AT

E077_SEED = 20261108
E077_RESAMPLES = 10_000
ALPHA = 0.05
PAPER_CHECK_AT = 20


def bootstrap_p_le_zero(values: Sequence[float], seed: int = E077_SEED, resamples: int = E077_RESAMPLES) -> float | None:
    """Share of day-block resamples (with replacement) whose mean is <= 0; None with fewer than two days. Same draw as the old log."""
    ex = np.array(values, dtype=float)
    if ex.size <= 1:
        return None
    rng = np.random.default_rng(seed)
    return float((ex[rng.integers(0, ex.size, (resamples, ex.size))].mean(axis=1) <= 0).mean())


def e077_summary(rows: Sequence[dict]) -> dict:
    """Progress and the pre-registered tests. H1: mean daily excess > 0 with p < 0.05; H2: mean daily net (after 0.252 percent) > 0."""
    ex = [r["ex"] for r in rows if r.get("ex") is not None]
    net = [r["net"] for r in rows if r.get("net") is not None]
    p = bootstrap_p_le_zero([float(x) for x in ex])
    mean_ex, mean_net = (q(mean(ex), 3) if ex else None), (q(mean(net), 3) if net else None)
    decided = len(rows) >= E077_CHECK_AT and len(ex) >= E077_MIN_SIGNAL_DAYS
    h1 = h2 = verdict = None
    if decided and p is not None:
        h1, h2 = bool(mean_ex > 0 and p < ALPHA), bool(mean_net > 0)
        verdict = "works" if h1 and h2 else "mixed" if h1 or h2 else "fails"
    return {"decision_days": len(rows), "signal_days": len(ex), "mean_ex": mean_ex, "p_boot": p, "mean_net": mean_net,
            "check_at": E077_CHECK_AT, "min_signal_days": E077_MIN_SIGNAL_DAYS, "decided": decided, "h1": h1, "h2": h2, "verdict": verdict,
            "pending": None if decided else f"pending until {E077_CHECK_AT} decision days with at least {E077_MIN_SIGNAL_DAYS} signal days"}


def nw_slope(x: Sequence[float], y: Sequence[float], lag: int = 1) -> tuple[float, float] | None:
    """OLS slope of y on x and its Newey-West t (Bartlett weights, ``lag`` lags, no small-sample correction); None without variation or with < 3 points."""
    n = len(x)
    if n < 3 or n != len(y):
        return None
    X = np.column_stack([np.ones(n), np.asarray(x, dtype=float)])
    Y = np.asarray(y, dtype=float)
    xtx = X.T @ X
    if abs(np.linalg.det(xtx)) < 1e-18:
        return None
    inv = np.linalg.inv(xtx)
    beta = inv @ (X.T @ Y)
    e = Y - X @ beta
    Z = X * e[:, None]
    s = Z.T @ Z
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1.0)
        g = Z[k:].T @ Z[:-k]
        s = s + w * (g + g.T)
    var = inv @ s @ inv
    se = float(np.sqrt(var[1, 1])) if var[1, 1] > 0 else 0.0
    return float(beta[1]), (float(beta[1]) / se if se > 1e-9 else float("nan"))      # a perfect fit has no standard error: no t, not a huge one


def _series(rows: Sequence[dict], key: str) -> list[tuple[float, float]]:
    out = []
    for r in rows:
        s = r.get(key)
        if s and s.get("x") is not None and s.get("y") is not None:
            out.append((float(s["x"]), float(s["y"])))
    return out


def e078_summary(rows: Sequence[dict]) -> dict:
    """H1 at the 60th session: the index series slope > 0 with Newey-West t >= 2 AND the equal-weight series slope has the same sign.
    The slopes below are PROVISIONAL until then (reported so the progress is visible; they decide nothing)."""
    out: dict = {"days": len(rows), "index_days": sum(1 for r in rows if r.get("index")), "check_at": E078_CHECK_AT}
    for key in ("index", "ew"):
        pts = _series(rows, key)
        fit = nw_slope([p[0] for p in pts], [p[1] for p in pts])
        out[f"{key}_points"] = len(pts)
        out[f"{key}_slope"] = None if fit is None else round(fit[0], 4)
        out[f"{key}_nw_t"] = None if fit is None or fit[1] != fit[1] else round(fit[1], 3)
    ix, ew = out["index_points"], out["ew_points"]
    decided = len(rows) >= E078_CHECK_AT and ix >= 3
    out["decided"] = decided
    out["h1"] = None
    if decided and out["index_slope"] is not None and out["index_nw_t"] is not None and out["ew_slope"] is not None:
        out["h1"] = bool(out["index_slope"] > 0 and out["index_nw_t"] >= 2 and out["ew_slope"] > 0)
    out["pending"] = None if decided else f"pending until {E078_CHECK_AT} sessions; slopes are provisional"
    return out
