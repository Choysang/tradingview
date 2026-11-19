"""Bootstrap p-values for multi-day research tests (ledger E082, E062; the old ``sameday_base._boot`` / ``_boot_block`` as SPECIFICATION).

Why: E082 ("works") found that resampling decision days one by one ignores the overlap of holding windows, so a multi-day test's p-value is too
small. From then on every multi-day test uses a moving-block bootstrap with block length = holding days, by trading day. This module is that
rule as a pure utility; it needs no database, no clock and no global random state.
Rules (``BOOTSTRAP_VERSION``):
  * Series: every trading day of the tested segment is one slot (``all_days``). A day with a signal carries its mean excess ``e`` and signal
    count ``k``; a day without a signal carries ``k = 0`` (it still occupies a slot, so a block covers calendar sessions, not only signal days).
    One draw's statistic is ``sum(e * k) / sum(k)`` over the resampled slots; a draw whose ``k`` sum is 0 is dropped (``draws`` vs ``valid_draws``).
  * Moving blocks: ``ceil(n / block)`` start positions are drawn uniformly from ``[0, n - block]`` (block is capped at ``n``), each start opens a
    run of ``block`` consecutive slots, the runs are joined and cut to ``n`` slots.
  * ``p_value`` = share of valid draws whose statistic is <= 0 (one-sided: "the mean excess is not positive"). ``None`` when there is no
    slot, no signal at all or no valid draw (never 0, never 1 by default).
  * Reliability (E082): fewer than ``MIN_BLOCKS`` (10) blocks (``ceil(n / block)``) is REPORTED ONLY: ``reliable`` is False and a caller must not
    adopt the number (the ledger found 3 to 4 blocks produce spurious significance through edge effects).
  * Determinism: ``numpy.random.default_rng(seed)``; the same inputs and seed give the same answer. ``seed_for(hold)`` = ``DEFAULT_SEED + hold``
    as in the old script. Unlike the old script no random stream is shared between calls, so a call does not depend on the calls before it.
  * ``day_bootstrap_p`` is the older day-level resampling (kept because some tests are single-day horizons, block = 1).
Floats are used inside the resampling (ratios, 2,000 draws); the inputs may be ``Decimal`` and are converted once.
Not covered: stationary bootstrap, confidence intervals, multiple-testing correction.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

BOOTSTRAP_VERSION = "block-bootstrap-v1"
DEFAULT_SEED = 20261107
N_BOOT = 2000
MIN_BLOCKS = 10


def seed_for(hold: int) -> int:
    """The seed convention of the old lab: base seed plus the holding days."""
    return DEFAULT_SEED + hold


@dataclass(frozen=True)
class BootstrapResult:
    p_value: float | None
    draws: int
    valid_draws: int
    slots: int
    block: int
    blocks: int
    reliable: bool
    reason: str | None


def moving_block_indices(starts: np.ndarray, block: int, n: int) -> np.ndarray:
    """Join runs of ``block`` consecutive slots opened at ``starts`` (shape ``(draws, runs)``) and cut each row to ``n`` slots."""
    if block < 1:
        raise ValueError("block must be at least 1")
    idx = (starts[:, :, None] + np.arange(block)).reshape(starts.shape[0], -1)
    return idx[:, :n]


def block_bootstrap_p(all_days: Sequence[str], signal_days: Sequence[str], excess: Sequence[float | Decimal], count: Sequence[int], *,
                      block: int, seed: int, n_boot: int = N_BOOT) -> BootstrapResult:
    """One-sided moving-block bootstrap p-value of "the signal-weighted mean excess is <= 0". Days not in ``all_days`` are ignored."""
    if block < 1:
        raise ValueError("block must be at least 1")
    if not (len(signal_days) == len(excess) == len(count)):
        raise ValueError("signal_days, excess and count must have the same length")
    n = len(all_days)
    pos = {d: i for i, d in enumerate(all_days)}
    ek, kk = np.zeros(n), np.zeros(n)
    for d, e, k in zip(signal_days, excess, count):
        i = pos.get(d)
        if i is not None:
            ek[i] += float(e) * k
            kk[i] += k
    b = min(block, n) if n else block
    blocks = math.ceil(n / b) if n else 0
    reliable = blocks >= MIN_BLOCKS
    if n == 0:
        return BootstrapResult(None, 0, 0, 0, block, 0, False, "no_slots")
    if kk.sum() == 0:
        return BootstrapResult(None, 0, 0, n, b, blocks, False, "no_signals")
    starts = np.random.default_rng(seed).integers(0, n - b + 1, size=(n_boot, blocks))
    idx = moving_block_indices(starts, b, n)
    num, den = ek[idx].sum(axis=1), kk[idx].sum(axis=1)
    ok = den > 0
    if not ok.any():
        return BootstrapResult(None, n_boot, 0, n, b, blocks, reliable, "no_valid_draw")
    p = float((num[ok] / den[ok] <= 0).mean())
    return BootstrapResult(p, n_boot, int(ok.sum()), n, b, blocks, reliable, None if reliable else "fewer_than_10_blocks_report_only")


def day_bootstrap_p(excess: Sequence[float | Decimal], count: Sequence[int], *, seed: int, n_boot: int = N_BOOT) -> float | None:
    """Day-level resampling (the old ``_boot``): resample the signal days with replacement, weight by the signal count, share of means <= 0."""
    if len(excess) != len(count):
        raise ValueError("excess and count must have the same length")
    if len(excess) == 0 or sum(count) == 0:
        return None
    e, k = np.array([float(x) for x in excess]), np.array(count, dtype=float)
    bi = np.random.default_rng(seed).integers(0, e.size, size=(n_boot, e.size))
    boot = (e[bi] * k[bi]).sum(axis=1) / k[bi].sum(axis=1)
    return float((boot <= 0).mean())
