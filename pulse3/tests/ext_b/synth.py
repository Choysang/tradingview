"""Deterministic synthetic panel for ext-B tests (random walks with limit-up bursts). SYNTHETIC: proves mechanics only."""
from __future__ import annotations

import random
from datetime import date, timedelta

from pulse.domain.playbooks.builder import RawSeries, build_views


def sessions(n: int, start: date = date(2024, 1, 2)) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def series(seed: int, days: list[str], iid: str, board: str = "main", cap: float | None = None) -> RawSeries:
    rng = random.Random(seed)
    price, o, h, l, c, v = 10.0, [], [], [], [], []
    for k in range(len(days)):
        drift = rng.gauss(0.0004, 0.018)
        if rng.random() < 0.012:
            drift = 0.10                                     # limit-up burst
        if rng.random() < 0.006:
            drift = -0.10
        op = price * (1 + rng.gauss(0, 0.004))
        cl = max(0.5, price * (1 + drift))
        hi = max(op, cl) * (1 + abs(rng.gauss(0, 0.006)))
        lo = min(op, cl) * (1 - abs(rng.gauss(0, 0.006)))
        o.append(round(op, 4)); h.append(round(hi, 4)); l.append(round(lo, 4)); c.append(round(cl, 4))
        v.append(float(int(rng.uniform(2e6, 2e7))))
        price = cl
    return RawSeries(iid, board, tuple(days), tuple(o), tuple(h), tuple(l), tuple(c), tuple(v),
                     float_cap_latest_cny=cap)


def panel(n_stocks: int = 40, n_days: int = 420, caps: bool = True):
    days = sessions(n_days)
    raws = [series(1000 + k, days, f"i{k:03d}", cap=(1e10 * (1 + k % 7)) if caps else None) for k in range(n_stocks)]
    views = build_views(raws, days)
    return days, raws, views
