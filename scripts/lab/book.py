"""Light daily book for the tensor lab: turns a score grid or a target-holding grid into a NAV.

Rules (kept identical for every method compared in round 5):
- Decisions use day t's close; trades happen at the next open (T+1); daily return is open to open.
- A name whose next open is a sealed limit-up cannot be bought; sells always fill (slightly optimistic,
  the backend engine is used for the final numbers).
- Cost: 0.1% on each buy and 0.1% on each sell (0.2% round trip, as in the backend engine).
- Fixed-hold mode: each day the best `slots // hold` new names by score enter, each held `hold` days,
  equal weight per slot; empty slots sit in cash at 0%.
"""
from __future__ import annotations

import numpy as np
import torch

import tengine as E

COST = 0.001


def _o2o(data: E.Data) -> torch.Tensor:
    o = data.f["open"]
    r = E._shift(o, -2) / E._shift(o, -1) - 1  # open t+1 -> open t+2, the return earned by a decision at t
    return torch.where(torch.isfinite(r), r, 0.0)


def fixed_hold(data: E.Data, score: torch.Tensor, rows: slice, hold: int = 5, slots: int = 20,
               exposure: np.ndarray | None = None) -> dict:
    """Score-ranked entries held `hold` days. `exposure[t]` in [0, 1] scales the new names bought on t."""
    ret = _o2o(data)
    blocked = E._shift(data.f["open_at_limit"], -1) > 0
    ok = data.tradable & ~blocked & torch.isfinite(score)
    per_day = max(1, slots // hold)
    t0, t1 = rows.start, rows.stop
    book: list[tuple[int, int]] = []  # (stock, day bought)
    nav, navs, turnover, n_trades = 1.0, [], 0.0, 0
    for t in range(t0, t1):
        book = [(j, d) for j, d in book if t - d < hold]  # held `hold` decision days
        k = per_day if exposure is None else int(round(per_day * float(exposure[t])))
        held = {j for j, _ in book}
        if k > 0:
            s = torch.where(ok[t], score[t], -torch.inf)
            for j in torch.topk(s, min(k + len(held) + 5, s.shape[0])).indices.tolist():
                if len([1 for _, d in book if d == t]) >= k or len(book) >= slots:
                    break
                if j not in held and torch.isfinite(s[j]):
                    book.append((j, t))
                    held.add(j)
                    n_trades += 1
        new = sum(1 for _, d in book if d == t)
        leaving = sum(1 for _, d in book if t - d == hold - 1)
        day = sum(float(ret[t, j]) for j, _ in book) / slots
        day -= COST * (new + leaving) / slots
        turnover += (new + leaving) / slots
        nav *= 1 + day
        navs.append(nav)
    return _summary(data, np.array(navs), rows, n_trades, turnover)


def holdings(data: E.Data, target: torch.Tensor, rows: slice, slots: int = 20) -> dict:
    """Daily target set (bool [T, N]): hold every targeted name, equal weight 1/slots each, at most `slots`."""
    ret = _o2o(data)
    blocked = E._shift(data.f["open_at_limit"], -1) > 0
    prev: set[int] = set()
    nav, navs, turnover, n_trades = 1.0, [], 0.0, 0
    for t in range(rows.start, rows.stop):
        want = set(torch.nonzero(target[t]).flatten().tolist())
        keep = prev & want
        buys = [j for j in want - prev if not bool(blocked[t, j])][: max(0, slots - len(keep))]
        cur = keep | set(buys)
        sells = len(prev - cur)
        n_trades += len(buys)
        day = sum(float(ret[t, j]) for j in cur) / slots - COST * (len(buys) + sells) / slots
        turnover += (len(buys) + sells) / slots
        nav *= 1 + day
        navs.append(nav)
        prev = cur
    return _summary(data, np.array(navs), rows, n_trades, turnover)


def _summary(data: E.Data, navs: np.ndarray, rows: slice, n_trades: int, turnover: float) -> dict:
    daily = np.diff(np.r_[1.0, navs])  / np.r_[1.0, navs[:-1]]
    idx = data.index_close.numpy()
    # the book's day t return runs open t+1 -> open t+2; compare with the index close t+1 -> close t+2 as a proxy
    i0, i1 = rows.start + 1, min(rows.stop + 1, len(idx) - 1)
    index_ret = (idx[i1] / idx[i0] - 1) * 100 if np.isfinite(idx[i0]) and np.isfinite(idx[i1]) else float("nan")
    sd = daily.std(ddof=1)
    peak = np.maximum.accumulate(navs)
    return {"total": round(float((navs[-1] - 1) * 100), 2), "index": round(float(index_ret), 2),
            "sharpe": round(float(daily.mean() / sd * np.sqrt(244)), 2) if sd > 0 else None,
            "mdd": round(float(((navs / peak) - 1).min() * 100), 2), "trades": n_trades,
            "turnover_per_day": round(turnover / max(1, len(navs)), 3), "days": len(navs), "nav": navs}
