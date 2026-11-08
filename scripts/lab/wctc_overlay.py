"""World Cup Trading Championship lesson, tested: champions repeat through a risk process, not one entry
signal. Risk overlays on the main book's daily equity (backend venv; harness.equity, hold 5, 20 slots):
  base       the book as is;
  dd_scaled  exposure 1 while the overlay's own drawdown is under 5%, 0.5 to 10%, 0.25 beyond (Unger-style
             cut after losses, back to full size on a new high);
  vol15      volatility target: exposure = min(1, 15% / annualised 20-day realised volatility of the book);
  curve_ma   equity-curve trading: full size when the book's NAV is above its 20-day mean, else 0.3;
and each again with a CSI 1000 (IM) hedge of the invested share (basis not in the data: gross).
Exposure is set from data up to the prior close and scales that day's book return; this is an overlay
approximation (trade-level sizing would differ slightly). Uninvested cash earns nothing.
Usage: python wctc_overlay.py fusion_static40_safe.parquet; periods PRE, SELECT, plus VERIFY with env VERIFY=1.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import polars as pl

import harness as H
import style_test as S


def stats(r: np.ndarray) -> dict:
    r = r[np.isfinite(r)]
    nav = np.cumprod(1 + r)
    return {"total": round(float((nav[-1] - 1) * 100), 2), "sharpe": round(float(r.mean() / r.std(ddof=1) * np.sqrt(244)), 2) if r.std() > 0 else None,
            "mdd": round(float((nav / np.maximum.accumulate(nav) - 1).min() * 100), 2), "days": len(r)}


def overlays(r: np.ndarray) -> dict[str, np.ndarray]:
    """Exposure multiplier per day for each overlay, decided from data up to the prior close."""
    n = len(r)
    out = {"base": np.ones(n)}
    w, nav, peak = np.ones(n), 1.0, 1.0
    for t in range(n):
        dd = nav / peak - 1
        w[t] = 1.0 if dd > -0.05 else (0.5 if dd > -0.10 else 0.25)
        nav *= 1 + w[t] * r[t]
        peak = max(peak, nav)
    out["dd_scaled"] = w
    vol = np.array([np.nan if t < 20 else r[t - 20:t].std(ddof=1) * np.sqrt(244) for t in range(n)])
    out["vol15"] = np.where(np.isfinite(vol), np.minimum(1.0, 0.15 / np.maximum(vol, 1e-6)), 1.0)
    base_nav = np.cumprod(1 + r)
    ma = np.array([np.nan if t < 20 else base_nav[t - 20:t].mean() for t in range(n)])
    prev = np.r_[np.nan, base_nav[:-1]]
    out["curve_ma"] = np.where(np.isfinite(ma) & (prev < ma), 0.3, 1.0)
    return out


def main() -> None:
    idx = S.index_returns()
    res = {}
    for name in sys.argv[1:]:
        s = pl.read_parquet(H.LAB / name)
        for p in S.PERIODS:
            eq = H.equity(s, p, hold=5, positions=20, offer=40).join(idx, on="date", how="left").sort("date")
            nav = eq["strategy"].to_numpy()
            r = np.r_[0.0, nav[1:] / nav[:-1] - 1]
            w_inv = np.r_[0.0, eq["exposure"].to_numpy()[:-1]]
            im = np.nan_to_num(eq["000852"].to_numpy(), nan=0.0)
            row = {}
            for k, w in overlays(r).items():
                row[k] = stats(w * r)
                row[k + "+IM"] = stats(w * (r - w_inv * im))
            res[f"{name}:{p}"] = row
            print(f"{name} {p}: " + " | ".join(f"{k} {v['total']:+.1f}% (S {v['sharpe']}, dd {v['mdd']:.1f})" for k, v in row.items()), flush=True)
    json.dump(res, open(H.LAB / "wctc_overlay.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
