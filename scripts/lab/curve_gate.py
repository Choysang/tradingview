"""Trade-level check of the equity-curve gate found by wctc_overlay.py (backend venv).

The overlay approximation scaled every day's book return. Here the backend engine trades it: each day the
book's own NAV (the ungated run, known at the prior close) is compared with its 20-day mean; below it, only
30% of the usual new buys are allowed (harness `exposure`), above it, all. Existing positions are not cut,
so this is milder than the overlay. Periods PRE, SELECT, plus VERIFY with env VERIFY=1.
Usage: python curve_gate.py fusion_static40_safe.parquet
"""
from __future__ import annotations

import sys

import numpy as np
import polars as pl

import harness as H
import style_test as S


def gate(eq: pl.DataFrame, look: int = 20, low: float = 0.3) -> pl.DataFrame:
    nav = eq["strategy"].to_numpy()
    ma = np.array([np.nan if t < look else nav[t - look:t].mean() for t in range(len(nav))])
    prev = np.r_[np.nan, nav[:-1]]
    w = np.where(np.isfinite(ma) & (prev < ma), low, 1.0)
    return pl.DataFrame({"date": eq["date"], "w": w})


def main() -> None:
    for name in sys.argv[1:]:
        s = pl.read_parquet(H.LAB / name)
        for p in S.PERIODS:
            eq = H.equity(s, p, hold=5, positions=20, offer=40)
            g = gate(eq)
            res = H.evaluate(s, f"gate:{name}:{p}", hold=5, positions=20, offer=40, periods=(p,), note="equity-curve gate 20d/0.3", exposure=g)
            print(H.show(res), f"| gated days {float((g['w'] < 1).mean()):.0%}", flush=True)


if __name__ == "__main__":
    main()
