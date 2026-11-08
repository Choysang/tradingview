"""Can the herding/momentum vs reversal/defensive switch be timed in advance? (lab venv)

regime.py showed, after the fact: in the 2026 H1 herding rally the best books were trend leaders
(close far above its 250-day mean, top 1-year gainers, relative-strength new highs, Minervini template, flag
breakouts) while the main fusion (reversal, low attention, low volatility) lost; from July the reverse.
Pre-registered here before any run: TREND = mean cohort of {F:MA250:bot, F:ROC250:bot, T:rs_new_high,
T:minervini_template, T:flag_break} (chosen from 2026 H1, so only SELECT and VERIFY are clean for it);
MAIN = the main fusion's top decile (fusion_static40_safe.pt). Each day one book is held (its 5-day cohort
excess, buy next open), chosen by:
  R1 style momentum: the book with the better mean cohort over the last 20 complete cohorts (t-25..t-6);
  R2 index trend: index close above its 60-day mean -> TREND, else MAIN;
  R3 both: TREND only when R1 and R2 agree on it;
  R4 a fixed 50/50 blend (no timing).
Output .work/lab/style_switch.json and a table of mean 5-day cohort excess % (NW t, lag 5) per period.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import polars as pl
import torch

import fusion
import regime
import tengine as E

TREND = ("F:MA250:bot", "F:ROC250:bot", "T:rs_new_high", "T:minervini_template", "T:flag_break")


def main() -> None:
    d = E.Data()
    panel = pl.read_parquet(E.LAB / "strategy_panel.parquet")
    names, X = regime.strategy_month(panel, d)
    trend = np.nanmean(np.stack([X[names.index(n)] for n in TREND if n in names]), axis=0)
    score = torch.load(E.LAB / os.environ.get("SWITCH_MAIN", "fusion_static40_safe.pt"))
    y = d.label(5)
    ok = torch.isfinite(score) & torch.isfinite(y)
    rk = E.cs_rank(torch.where(ok, score, torch.nan))
    top = ok & (rk >= 0.9)
    main_x = (torch.where(top, y, 0).sum(1) / top.sum(1).clamp(min=1)).numpy() - 0.2  # label is % excess; panel X nets 0.2% cost
    main_x[top.sum(1).numpy() < 10] = np.nan
    idx = d.index_close.numpy()
    ma60 = np.array([np.nan if i < 60 else idx[i - 59:i + 1].mean() for i in range(d.T)])
    up = idx > ma60

    def trail(x: np.ndarray, t: int) -> float:
        w = x[max(0, t - 25): max(0, t - 5)]
        w = w[np.isfinite(w)]
        return w.mean() if len(w) >= 10 else np.nan

    books = {"MAIN": main_x, "TREND": trend, "R4_blend": 0.5 * main_x + 0.5 * trend}
    r1, r2, r3 = np.full(d.T, np.nan), np.full(d.T, np.nan), np.full(d.T, np.nan)
    pick1 = np.zeros(d.T, dtype=bool)
    for t in range(d.T):
        a, b = trail(main_x, t), trail(trend, t)
        p1 = bool(np.isfinite(a) and np.isfinite(b) and b > a)
        pick1[t] = p1
        r1[t] = trend[t] if p1 else main_x[t]
        r2[t] = trend[t] if up[t] else main_x[t]
        r3[t] = trend[t] if (p1 and up[t]) else main_x[t]
    books |= {"R1_style_mom": r1, "R2_index_ma60": r2, "R3_both": r3}
    out = {}
    print("mean 5-day cohort excess % (NW t); share of days in TREND for R1 / R2")
    for p, r in fusion.periods(d).items():
        row = {}
        for k, v in books.items():
            x = v[r][np.isfinite(v[r])]
            row[k] = (float(x.mean()), E.nw_t(x, 5)) if len(x) > 8 else (float("nan"), float("nan"))
        row["trend_share_R1"], row["trend_share_R2"] = float(pick1[r].mean()), float(up[r].mean())
        out[p] = row
        print(f"  {p:7s} " + " | ".join(f"{k} {m:+.2f} ({t:+.1f})" for k, (m, t) in ((k, row[k]) for k in books))
              + f" | TREND days R1 {row['trend_share_R1']:.0%} R2 {row['trend_share_R2']:.0%}")
    json.dump(out, open(E.LAB / "style_switch.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    sys.exit(main())
