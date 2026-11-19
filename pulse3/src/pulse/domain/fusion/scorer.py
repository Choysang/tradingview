"""Static40 fusion scorer (``fusion_static40_safe`` of the old lab): expanding-window ICIR weights over per-day factor ranks.

Why: legacy triage batch 3 asks for the round-5 main score to run on the server with the frozen rules. The rules, unchanged:
* every factor expression (zoo + mined pool top 200 + AlphaGen, ``fusion/data``) becomes a per-day percentile rank over the
  tradable names, stored as float16 (the quantisation is part of the frozen score);
* the label is the 5-day relative return (``Panel.label(5)``); a factor's daily IC is the Spearman correlation of its rank with
  the label over the names where both exist (at least 100); a day without an IC counts as 0;
* on day ``t`` the ICs of days ``130 .. t-6`` (every label complete; earliest score on day 196) give each factor its mean and
  standard deviation, ``icir = mean / sd``; the 40 factors with the largest ``|icir|`` are kept;
* score = sum(icir_i x (rank_i - 0.5)) / sum |icir_i| over those factors (a missing rank adds nothing), NaN when the name is
  not tradable. Factors whose ranks depend on forward-adjusted price levels are excluded (``levelsafe``).
Invariants: the score of day ``t`` only reads ICs whose labels use opens up to day ``t`` and ranks of day ``t``.
Tie rule: equal values get consecutive ranks; a big tie group is ordered like the old engine's unstable sort (``ranktie``), the float16
quantisation pairs inside ``daily_ic`` by column.
Not covered: building the panel (``panel``), the books (``books``), storage (``application.bf_fusion_service``).
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

import numpy as np

from .expr import F32, Evaluator, cs_rank, cs_rank_stable
from .panel import Panel

HORIZON = 5
PURGE = HORIZON + 1
FIRST = 130                     # the zoo needs about 120 bars before its factors exist
K = 40
MIN_N = 100
FIRST_SCORE_ROW = 60 + PURGE + FIRST   # 196
# the same two cut-offs as dates (rows 130 and 196 of the old panel, which starts 2023-11-01); a daily run on a shorter window keys on them
FIRST_IC_DATE = date(2024, 5, 17)
FIRST_SCORE_DATE = date(2024, 8, 20)
DATA = resources.files("pulse.domain.fusion") / "data"


def load_json(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def frozen_factors(*, only_level_safe: bool = True) -> list[tuple[str, str]]:
    """``[(name, expression)]`` in the frozen order; the level-unsafe factors are dropped unless asked for."""
    facs = load_json("factor_expressions.json")["factors"]
    safe = load_json("level_safety.json")["safe"]
    return [(f["name"], f["expr"]) for f in facs if not only_level_safe or safe[f["name"]]]


def rank16(x: np.ndarray, tradable: np.ndarray) -> np.ndarray:
    """The stored per-day percentile rank of a factor over the tradable names (float16)."""
    return cs_rank(np.where(tradable, x, np.nan).astype(F32)).astype(np.float16)


def daily_ic(x: np.ndarray, y: np.ndarray, min_n: int = MIN_N) -> np.ndarray:
    """Spearman rank IC per row over the cells where both grids are finite (ranks recomputed on those cells), NaN below ``min_n``."""
    m = np.isfinite(x) & np.isfinite(y)
    rx = cs_rank_stable(np.where(m, x, np.nan).astype(F32))
    ry = cs_rank_stable(np.where(m, y, np.nan).astype(F32))
    rx = np.where(m, rx - F32(0.5), 0).astype(np.float64)
    ry = np.where(m, ry - F32(0.5), 0).astype(np.float64)
    n = m.sum(axis=1).astype(np.float64)
    nn = np.maximum(n, 1)
    mx, my = rx.sum(1) / nn, ry.sum(1) / nn
    cov = (rx * ry).sum(1) / nn - mx * my
    vx = (rx * rx).sum(1) / nn - mx * mx
    vy = (ry * ry).sum(1) / nn - my * my
    ic = cov / np.sqrt(np.maximum(vx * vy, 1e-18))
    return np.where(n >= min_n, ic, np.nan)


@dataclass
class FactorRun:
    """Result of one pass over the factors: the IC of every factor on the rows asked for, and the ranks of the tail rows."""

    names: list[str]
    ic: np.ndarray            # [F, T] float32, NaN on rows that were not asked for or have no IC
    rows: np.ndarray          # rows that were ranked
    tail_rows: np.ndarray     # rows whose ranks are kept (a subset of ``rows``)
    ranks: np.ndarray         # [F, len(tail_rows), N] float16
    skipped: dict[str, str]   # factors that could not be evaluated -> reason

    @property
    def tail_start(self) -> int:
        return int(self.tail_rows[0]) if len(self.tail_rows) else self.ic.shape[1]


def run_factors(panel: Panel, factors: Sequence[tuple[str, str]], *, tail_start: int, rows: Sequence[int] | None = None,
                cache_size: int = 32, progress=None) -> FactorRun:
    """Evaluate each factor once on the whole panel, then rank it and take its IC only on ``rows`` (default every row) and keep
    the ranks of the rows ``>= tail_start``. The ranking is the expensive part (tie order), so a daily run asks for the one or
    two new IC rows and the last 61 rows only; memory is ``F x tail x N x 2`` bytes."""
    ev = Evaluator(panel.f, cache_size=cache_size)
    T, N = panel.T, panel.N
    rows_arr = np.arange(T) if rows is None else np.array(sorted({int(r) for r in rows if 0 <= int(r) < T}), dtype=np.int64)
    keep = rows_arr >= tail_start
    tail_rows = rows_arr[keep]
    y = panel.label(HORIZON)[rows_arr]
    trad = panel.tradable[rows_arr]
    ic = np.full((len(factors), T), np.nan, dtype=F32)
    ranks = np.zeros((len(factors), len(tail_rows), N), dtype=np.float16)
    skipped: dict[str, str] = {}
    for i, (name, expr) in enumerate(factors):
        try:
            r = rank16(ev(expr)[rows_arr], trad)
        except Exception as exc:  # a factor that cannot be evaluated is reported, never silently scored as 0  # noqa: BLE001
            skipped[name] = f"{type(exc).__name__}: {exc}"[:200]
            r = np.full((len(rows_arr), N), np.nan, dtype=np.float16)
        ic[i, rows_arr] = daily_ic(r.astype(F32), y).astype(F32)
        ranks[i] = r[keep]
        if progress:
            progress(i + 1, len(factors), name)
    return FactorRun([n for n, _ in factors], ic, rows_arr, tail_rows, ranks, skipped)


def cumulative(ic: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Prefix sums of the IC and of its square (a missing IC counts as 0), float32 with a leading zero column."""
    z = np.nan_to_num(ic.astype(F32), nan=0.0)
    zero = np.zeros((z.shape[0], 1), dtype=np.float64)
    cs = np.concatenate([zero, np.cumsum(z.astype(np.float64), axis=1)], axis=1).astype(F32)
    cs2 = np.concatenate([zero, np.cumsum(z.astype(np.float64) ** 2, axis=1)], axis=1).astype(F32)
    return cs, cs2


def icir_at(cs: np.ndarray, cs2: np.ndarray, t: int, a: int = FIRST) -> np.ndarray:
    """ICIR of every factor on day ``t`` from the days ``a .. t - PURGE`` (expanding window; ``a`` = the first IC day, row 130)."""
    b = t - PURGE + 1
    n = F32(b - a)
    m = (cs[:, b] - cs[:, a]) / n
    var = (cs2[:, b] - cs2[:, a]) / n - m ** 2
    sd = np.sqrt(np.maximum(var, F32(1e-12)))
    return (m / sd).astype(F32)


def top_factors(icir: np.ndarray, k: int = K) -> np.ndarray:
    """Indices of the ``k`` factors with the largest ``|icir|`` (the order inside the set carries no meaning)."""
    return np.argpartition(-np.abs(icir), k - 1)[:k]


def score_day(icir: np.ndarray, day_ranks: np.ndarray, tradable_row: np.ndarray, k: int = K) -> np.ndarray:
    """Score of one day: ``day_ranks`` is the float16 [F, N] rank grid of that day."""
    top = top_factors(icir, k)
    z = day_ranks[top].astype(F32) - F32(0.5)
    w = icir[top][:, None]
    with np.errstate(all="ignore"):
        s = np.nansum(z * w, axis=0, dtype=F32) / np.abs(w).sum(dtype=F32)
    return np.where(tradable_row, s, np.nan).astype(F32)


def score_rows(run: FactorRun, panel: Panel, ic: np.ndarray | None = None, rows: Sequence[int] | None = None, k: int = K,
               ic_dates: Sequence[date] | None = None, first_ic_date: date | None = None, first_score_date: date | None = None) -> dict[int, np.ndarray]:
    """Scores for the tail rows that are scoring days. ``ic`` is the full IC matrix aligned to ``ic_dates`` (default: the panel's
    dates and the run's own ICs), which lets a short panel use a stored history. The first IC day and first scoring day are rows 130
    and 196 unless dates are given. ``{panel row: [N] float32}``."""
    dates = list(ic_dates) if ic_dates is not None else list(panel.dates)
    cs, cs2 = cumulative(run.ic if ic is None else ic)
    at = {d: i for i, d in enumerate(dates)}
    a = FIRST if first_ic_date is None else next(i for i, d in enumerate(dates) if d >= first_ic_date)
    pos = {int(t): j for j, t in enumerate(run.tail_rows)}

    def scoring_day(t: int) -> bool:
        return t >= FIRST_SCORE_ROW if first_score_date is None else panel.dates[t] >= first_score_date

    todo = sorted(t for t in (rows if rows is not None else pos) if t in pos and scoring_day(t))
    return {t: score_day(icir_at(cs, cs2, at[panel.dates[t]], a), run.ranks[:, pos[t], :], panel.tradable[t], k) for t in todo}


def selected_factors(run: FactorRun, t: int, ic: np.ndarray | None = None, k: int = K) -> list[tuple[str, float]]:
    """The factors and ICIR weights used on row ``t`` (for display and audit), strongest first."""
    cs, cs2 = cumulative(run.ic if ic is None else ic)
    icir = icir_at(cs, cs2, t)
    top = top_factors(icir, k)
    return sorted(((run.names[i], float(icir[i])) for i in top), key=lambda p: -abs(p[1]))


def score_frame_rows(panel: Panel, scores: dict[int, np.ndarray]) -> Iterator[tuple[str, object, float]]:
    """Long rows ``(code, date, score)`` of the finite scores of tradable names."""
    for t in sorted(scores):
        s = scores[t]
        for j in np.flatnonzero(np.isfinite(s) & panel.tradable[t]):
            yield panel.codes[j], panel.dates[t], float(s[j])


def data_digest() -> dict[str, str]:
    """sha256 of each frozen data file (checked against ``manifest.json`` by the tests)."""
    import hashlib

    base = Path(str(DATA))
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.glob("*.json")) if p.name != "manifest.json"}
