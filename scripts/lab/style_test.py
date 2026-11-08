"""Holding style on real fills (backend engine via harness.evaluate, backend venv).

The user's question: hold a liked stock for long and trade around it (做T), go ultra-short, or combine?
The main fusion score is held 2, 5, 20 and 60 days, each with:
  none  - buy and hold to the exit day;
  正T2  - each holding day a resting buy 2% below the previous close; if filled, the same number of
          old shares is sold at that day's close (a real daily-bar trade: the order rests before the open);
  倒T3  - a resting sell 3% above the previous close; if filled, the shares are bought back at the close;
  双T3  - both at 3%.
t_share 0.5 (half the base position), each leg pays its own costs (backend engine rules).
Hedging of the plain books: daily book return minus the prior day's invested share times the daily return
of CSI 1000 (IM), CSI 500 (IC) or CSI 300 (IF). Futures basis is not in the data, so the hedged total is
shown gross and with 6% and 10% a year of basis drag on the hedged notional (sensitivity, not a measurement).
Usage: python style_test.py fusion_static40_mined.parquet [...]; periods PRE, SELECT, plus VERIFY with env VERIFY=1.
Output .work/lab/style_test.json.
"""
from __future__ import annotations

import glob
import json
import os
import sys

import numpy as np
import polars as pl

import harness as H

HOLDS = (2, 5, 20, 60)
T_RULES = {"none": {}, "正T2": {"t_low_pct": 2.0}, "倒T3": {"t_high_pct": 3.0}, "双T3": {"t_high_pct": 3.0, "t_low_pct": 3.0}}
HEDGES = {"IM": "000852", "IC": "000905", "IF": "000300"}
BASIS = (0.0, 6.0, 10.0)
PERIODS = ("PRE", "SELECT") + (("VERIFY",) if os.environ.get("VERIFY") == "1" else ())


def index_returns() -> pl.DataFrame:
    root = os.environ["PULSE_DATA_DIR"]
    files = glob.glob(os.path.join(root, "warehouse", "live", "index_bars", "**", "*.parquet"), recursive=True)
    df = pl.concat([pl.read_parquet(f).select("symbol", "date", "close") for f in files], how="vertical_relaxed")
    df = df.unique(["symbol", "date"], keep="last").sort("symbol", "date")
    df = df.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r"))
    return df.pivot(on="symbol", index="date", values="r").sort("date")


def hedge(eq: pl.DataFrame, idx: pl.DataFrame) -> dict:
    x = eq.select("date", "strategy", "exposure").join(idx, on="date", how="left").sort("date")
    nav = x["strategy"].to_numpy()
    r = np.r_[np.nan, nav[1:] / nav[:-1] - 1]
    w = np.r_[np.nan, x["exposure"].to_numpy()[:-1]]  # invested share at the prior close
    out = {"plain": round(float((nav[-1] / nav[0] - 1) * 100), 2)}
    for name, sym in HEDGES.items():
        ri = x[sym].to_numpy()
        for b in BASIS:
            h = r - w * ri - w * b / 100 / 244
            h = h[np.isfinite(h)]
            out[f"{name}_basis{b:g}"] = round(float((np.prod(1 + h) - 1) * 100), 2)
            if b == 0.0:
                out[f"{name}_sharpe"] = round(float(h.mean() / h.std(ddof=1) * np.sqrt(244)), 2) if h.std() > 0 else None
                out[f"{name}_mdd"] = round(float(((np.cumprod(1 + h) / np.maximum.accumulate(np.cumprod(1 + h))) - 1).min() * 100), 2)
    return out


def main() -> None:
    idx = index_returns()
    result = {}
    for name in sys.argv[1:]:
        s = pl.read_parquet(H.LAB / name)
        rows = []
        for hold in HOLDS:
            for tk, kw in T_RULES.items():
                res = H.evaluate(s, f"style:{name}:h{hold}:{tk}", hold=hold, positions=20, offer=40, periods=PERIODS,
                                 note=f"round 6 holding style {tk}", exit_kw=kw)
                print(H.show(res), flush=True)
                rows.append({"hold": hold, "t": tk, "periods": res["periods"]})
        hedges = {}
        for hold in HOLDS:
            for p in PERIODS:
                hedges[f"h{hold}:{p}"] = hedge(H.equity(s, p, hold=hold, positions=20, offer=40), idx)
                print(f"[hedge] {name} hold {hold} {p}: {hedges[f'h{hold}:{p}']}", flush=True)
        result[name] = {"styles": rows, "hedges": hedges}
    json.dump(result, open(H.LAB / "style_test.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    main()
