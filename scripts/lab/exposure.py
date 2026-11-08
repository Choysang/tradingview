"""Exposure overlays from the contest research, applied to a fusion score on book.py's fixed-hold book (lab venv).

Each rule maps day t's market state (known at the close) to the share of the day's new buys made:
  always            1
  index_ma{5,20,60} 1 if the index close is above its n-day mean, else 0 (ma60 = the product's bear mode)
  breadth           1 if the 5-day mean share of rising stocks is >= 45%, else 0.5
  limit_down_spike  0 if the 5-day mean limit-down count is above 30 (the community "risk line"), else 1
  sentiment         1 if the 5-day mean limit-up count is above its own 60-day median, else 0.5
Prints total return, Sharpe and drawdown per period; VERIFY only with env VERIFY=1.
Usage: python exposure.py fusion_lgbm_hl120.pt
"""
from __future__ import annotations

import json
import sys

import numpy as np
import torch

import book as B
import fusion
import tengine as E


def rules(d: E.Data) -> dict[str, np.ndarray]:
    m = {k: v[:, 0].numpy() for k, v in fusion.market_state(d).items()}
    idx = d.index_close.numpy()
    out = {"always": np.ones(d.T)}
    for n in (5, 20, 60):
        ma = np.array([np.nanmean(idx[max(0, t - n + 1): t + 1]) for t in range(d.T)])
        out[f"index_ma{n}"] = (idx > ma).astype(float)
    up5 = np.array([np.nanmean(m["M_UP"][max(0, t - 4): t + 1]) for t in range(d.T)])
    out["breadth"] = np.where(up5 >= 0.45, 1.0, 0.5)
    dt5 = np.array([np.nanmean(m["M_DT"][max(0, t - 4): t + 1]) for t in range(d.T)])
    out["limit_down_spike"] = np.where(dt5 > 30, 0.0, 1.0)
    zt5 = m["M_ZT_MA5"]
    med = np.array([np.nanmedian(zt5[max(0, t - 59): t + 1]) for t in range(d.T)])
    out["sentiment"] = np.where(zt5 > med, 1.0, 0.5)
    return out


def main(score_file: str) -> None:
    d = E.Data()
    score = torch.load(E.LAB / score_file)
    res = {}
    for name, ex in rules(d).items():
        res[name] = {}
        for p, rows in fusion.periods(d).items():
            r = B.fixed_hold(d, score, rows, hold=5, slots=20, exposure=ex)
            r.pop("nav")
            r["avg_exposure"] = round(float(ex[rows].mean()), 2)
            res[name][p] = r
        print(f"{name:17s} " + " | ".join(f"{p} {v['total']:+6.2f}% (idx {v['index']:+6.2f}) mdd {v['mdd']:6.2f} exp {v['avg_exposure']:.2f}"
                                          for p, v in res[name].items()), flush=True)
    json.dump(res, open(E.LAB / f"exposure_{score_file.replace('.pt', '')}.json", "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fusion_lgbm_hl120.pt")
