"""Checks three numeric claims from the user's 11-01 correction note against the main-board data (lab venv).

1. Regime thresholds: 主升 = highest board >= 6, limit-ups > 60, promotion rate > 30%, limit-downs < 5;
   退潮 = yesterday's limit-ups average a negative return today. Share of days per month in each state.
2. Do the replay's short-line strategies (replay_trades.parquet, concepts, P_SEAL 0.3) make money when the
   signal day is in 主升? (the note says the sentiment-leader book should own the bull phase)
3. The hot-money funnel: price < 100, float cap < 100 亿, turnover 15%~35% on the signal day. Filtered
   trades vs the rest, per strategy.
All state is computed from day D and earlier; trades are entered on D+1, as in replay.py.
Usage: python claims_check.py   (writes .work/lab/claims_check.json)
"""
from __future__ import annotations

import json
import os

import numpy as np
import polars as pl

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LAB = os.path.join(ROOT, ".work", "lab")
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")


def state() -> pl.DataFrame:
    p = pl.read_parquet(os.path.join(LAB, "panel.parquet"),
                        columns=["code", "date", "close", "amount", "float_cap", "pct_chg", "is_limit_up", "is_limit_down",
                                 "limit_streak", "days_listed", "st_like"])
    p = p.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX) & ~pl.col("st_like").fill_null(False) & (pl.col("days_listed") >= 60))
    p = p.sort("code", "date").with_columns(
        pl.col("is_limit_up").shift(1).over("code").alias("zt_prev"),
        pl.col("limit_streak").shift(1).over("code").alias("streak_prev"))
    day = p.group_by("date").agg(
        zt=pl.col("is_limit_up").sum(),
        dt=pl.col("is_limit_down").sum(),
        max_streak=pl.col("limit_streak").max(),
        promo=pl.col("is_limit_up").filter(pl.col("zt_prev")).mean() * 100,
        premium=pl.col("pct_chg").filter(pl.col("zt_prev")).mean(),
    ).sort("date")
    bull = (pl.col("max_streak") >= 6) & (pl.col("zt") > 60) & (pl.col("promo") > 30) & (pl.col("dt") < 5)
    day = day.with_columns(pl.when(bull).then(pl.lit("主升"))
                           .when(pl.col("premium") < 0).then(pl.lit("退潮"))
                           .when(pl.col("max_streak").is_between(3, 5)).then(pl.lit("轮动"))
                           .otherwise(pl.lit("其他")).alias("regime"))
    return day, p


def clustered(f: pl.DataFrame) -> tuple[float, float, int]:
    if f.height < 3:
        return float("nan"), float("nan"), f.height
    dm = f.group_by("date").agg(pl.col("ret").mean())["ret"].to_numpy()
    t = float(dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))) if len(dm) > 2 and dm.std() > 0 else float("nan")
    return float(f["ret"].mean()), t, f.height


def main() -> None:
    day, p = state()
    out: dict = {}
    month = (day.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
             .group_by("m").agg([(pl.col("regime") == r).mean().round(2).alias(r) for r in ("主升", "轮动", "退潮", "其他")]
                                + [pl.col("zt").mean().round(0).alias("zt"), pl.col("max_streak").max().alias("max_h")])
             .sort("m"))
    print("[1] share of days per regime, mean limit-ups, highest board")
    for r in month.iter_rows(named=True):
        print(f"  {r['m']}  主升 {r['主升']:.0%} 轮动 {r['轮动']:.0%} 退潮 {r['退潮']:.0%} 其他 {r['其他']:.0%} | zt {r['zt']:.0f} max {r['max_h']}")
    out["regime_month"] = month.to_dicts()

    tr = pl.read_parquet(os.path.join(LAB, "replay_trades.parquet")).filter(pl.col("filled") & pl.col("ret").is_not_null())
    tr = tr.join(day.select("date", "regime"), on="date", how="left")
    print("\n[2] per trade net % by the signal day's regime (mean, day-clustered t, n), 2023-11..2026-10")
    out["by_regime"] = {}
    for k in sorted(tr["kind"].unique().to_list()):
        cells = []
        for r in ("主升", "轮动", "退潮", "其他"):
            m, t, n = clustered(tr.filter((pl.col("kind") == k) & (pl.col("regime") == r)))
            out["by_regime"][f"{k}|{r}"] = {"mean": m, "t": t, "n": n}
            cells.append(f"{r} {m:+.2f} (t {t:+.1f}, {n})")
        print(f"  {k:10s} " + " | ".join(cells))

    feat = p.select("code", "date", "close", "float_cap", (pl.col("amount") / pl.col("float_cap") * 100).alias("turn"))
    tr = tr.join(feat, on=["code", "date"], how="left")
    cap_unit = float(tr["float_cap"].median())
    yi = 1e8 if cap_unit > 1e6 else 1.0  # float_cap in yuan in the warehouse; guard against 亿 units
    funnel = (pl.col("close") < 100) & (pl.col("float_cap") / yi < 100) & pl.col("turn").is_between(15, 35)
    print(f"\n[3] hot-money funnel (price<100, float cap<100亿, turnover 15~35% on D); float_cap median {cap_unit:.3g}")
    out["funnel"] = {}
    for k in sorted(tr["kind"].unique().to_list()):
        x = tr.filter(pl.col("kind") == k)
        a, b = clustered(x.filter(funnel)), clustered(x.filter(~funnel.fill_null(False)))
        out["funnel"][k] = {"in": a, "out": b}
        print(f"  {k:10s} in {a[0]:+.2f} (t {a[1]:+.1f}, {a[2]}) | out {b[0]:+.2f} (t {b[1]:+.1f}, {b[2]})")
    with open(os.path.join(LAB, "claims_check.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
