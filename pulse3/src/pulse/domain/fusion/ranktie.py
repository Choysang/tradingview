"""Tie order of the old engine's cross-section rank (a port of the Microsoft STL ``std::sort``), for the fusion score.

Why: the frozen score ranks every factor per day with ``argsort`` and gives equal values consecutive ranks in the order the sort
leaves them. The old engine's torch build used an unstable introsort (the Microsoft STL one), so for a factor with big tie groups
(limit-up counts, 5-day counts, window positions ...) the stocks inside a tie group are ranked in a pseudo-random but fully
deterministic order. A stable sort would instead rank by stock code inside every tie group, a systematic bias, and the top 40
of the score would differ from the frozen one by about a quarter (measured on the PC data). This module reproduces that order.
What it is: ``std::sort`` of (value, index) pairs by ``value < value``: insertion sort up to 32 elements, otherwise a quicksort
with median of three (median of nine from 42 elements) and an equal-range partition; depth limit ``n``, shrinking by 3/4 per level.
Invariants: for distinct values the result is the unique sorted order (so the fast numpy path is exact); for ties the result equals
``torch.argsort`` of the old engine on every fixture of ``tests/bf_fusion/fixtures`` (generated with that engine).
Not covered: the heap-sort fallback of the STL (a ``stable`` sort stands in; the depth limit is never reached on 3,000 values).
"""
from __future__ import annotations

import numpy as np

ISORT_MAX = 32
BIG_TIE = 8            # a tie group of fewer values than this is ordered by position: it moves a rank by < 8 places


def _swap(k: list, ix: list, a: int, b: int) -> None:
    k[a], k[b] = k[b], k[a]
    ix[a], ix[b] = ix[b], ix[a]


def _med3(k: list, ix: list, a: int, b: int, c: int) -> None:
    if k[b] < k[a]:
        _swap(k, ix, b, a)
    if k[c] < k[b]:
        _swap(k, ix, c, b)
        if k[b] < k[a]:
            _swap(k, ix, b, a)


def _insertion(k: list, ix: list, first: int, last: int) -> None:
    for nxt in range(first + 1, last):
        vk, vi = k[nxt], ix[nxt]
        if vk < k[first]:
            for j in range(nxt, first, -1):
                k[j] = k[j - 1]
                ix[j] = ix[j - 1]
            k[first], ix[first] = vk, vi
        else:
            j = nxt
            while vk < k[j - 1]:
                k[j] = k[j - 1]
                ix[j] = ix[j - 1]
                j -= 1
            k[j], ix[j] = vk, vi


def _partition(k: list, ix: list, first: int, last: int) -> tuple[int, int]:
    """Partition ``[first, last)`` around a median guess; returns the range of elements equal to the pivot."""
    mid = first + ((last - first) >> 1)
    if last - first - 1 > 40:                    # median of nine (the STL compares an inclusive range with 40)
        step = (last - first) >> 3
        two = step << 1
        _med3(k, ix, first, first + step, first + two)
        _med3(k, ix, mid - step, mid, mid + step)
        _med3(k, ix, last - 1 - two, last - 1 - step, last - 1)
        _med3(k, ix, first + step, mid, last - 1 - step)
    else:
        _med3(k, ix, first, mid, last - 1)
    pfirst, plast = mid, mid + 1
    while first < pfirst and not (k[pfirst - 1] < k[pfirst]) and not (k[pfirst] < k[pfirst - 1]):
        pfirst -= 1
    while plast < last and not (k[plast] < k[pfirst]) and not (k[pfirst] < k[plast]):
        plast += 1
    gfirst, glast = plast, pfirst
    while True:
        while gfirst < last:
            if k[pfirst] < k[gfirst]:
                gfirst += 1
            elif k[gfirst] < k[pfirst]:
                break
            elif plast != gfirst:
                _swap(k, ix, plast, gfirst)
                plast += 1
                gfirst += 1
            else:
                plast += 1
                gfirst += 1
        while first < glast:
            if k[glast - 1] < k[pfirst]:
                glast -= 1
            elif k[pfirst] < k[glast - 1]:
                break
            else:
                pfirst -= 1
                if pfirst != glast - 1:
                    _swap(k, ix, pfirst, glast - 1)
                glast -= 1
        if glast == first and gfirst == last:
            return pfirst, plast
        if glast == first:                       # no room at the bottom: rotate the pivot range upward
            if plast != gfirst:
                _swap(k, ix, pfirst, plast)
            plast += 1
            _swap(k, ix, pfirst, gfirst)
            pfirst += 1
            gfirst += 1
        elif gfirst == last:                     # no room at the top: rotate the pivot range downward
            glast -= 1
            pfirst -= 1
            if glast != pfirst:
                _swap(k, ix, glast, pfirst)
            plast -= 1
            _swap(k, ix, pfirst, plast)
        else:
            glast -= 1
            _swap(k, ix, gfirst, glast)
            gfirst += 1


def _sort(k: list, ix: list, first: int, last: int, ideal: int) -> None:
    while True:
        if last - first <= ISORT_MAX:
            _insertion(k, ix, first, last)
            return
        if ideal <= 0:                           # the STL switches to heap sort; never reached on cross-sections of this size
            order = sorted(range(first, last), key=lambda i: k[i])
            k[first:last], ix[first:last] = [k[i] for i in order], [ix[i] for i in order]
            return
        mf, ml = _partition(k, ix, first, last)
        ideal = (ideal >> 1) + (ideal >> 2)
        if mf - first < last - ml:
            _sort(k, ix, first, mf, ideal)
            first = ml
        else:
            _sort(k, ix, ml, last, ideal)
            last = mf


def argsort_stl(row: np.ndarray) -> np.ndarray:
    """Indices that sort a 1-D array the way ``std::sort`` leaves equal values (the old engine's ``torch.argsort``)."""
    k = row.tolist()
    ix = list(range(len(k)))
    _sort(k, ix, 0, len(k), len(k))
    return np.array(ix, dtype=np.int64)


def _has_big_tie(sorted_row: np.ndarray, finite_count: int) -> bool:
    """True when some value repeats ``BIG_TIE`` or more times among the finite entries (the sorted row puts them first)."""
    s = sorted_row[:finite_count]
    if s.size < BIG_TIE:
        return False
    return bool((s[BIG_TIE - 1:] == s[: s.size - BIG_TIE + 1]).any())


def rank_rows(x: np.ndarray) -> np.ndarray:
    """Per-row ordinal rank ``(position + 0.5) / count`` of the finite values, float32; non-finite stay NaN. Equal values are
    ordered like the old engine's unstable sort when a tie group has ``BIG_TIE`` or more members, by column otherwise."""
    m = np.isfinite(x)
    filled = np.where(m, x, np.inf).astype(np.float32)
    idx = np.argsort(filled, axis=1, kind="stable")
    n = x.shape[1]
    counts = m.sum(axis=1)
    sorted_vals = np.take_along_axis(filled, idx, axis=1)
    for t in range(x.shape[0]):
        c = int(counts[t])
        if c >= BIG_TIE and _has_big_tie(sorted_vals[t], c):
            idx[t] = argsort_stl(filled[t])
    r = np.empty(x.shape, dtype=np.float32)
    np.put_along_axis(r, idx, np.broadcast_to(np.arange(n, dtype=np.float32), x.shape), axis=1)
    cnt = counts[:, None].astype(np.float32)
    return np.where(m, (r + np.float32(0.5)) / np.maximum(cnt, np.float32(1)), np.nan).astype(np.float32)
