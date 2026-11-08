"""Quant track: walk-forward LightGBM ranker on the 33 factors.

Every 20 trading days a model is trained on all earlier rows whose label window has closed
(purged: label end < first predicted day), then scores the next 20 days. Features and label
are per-date percentile ranks, so the model learns cross-sectional order, not market level.
Predictions start once 160 days of history exist. Both TRAIN and VALID scores are therefore
out of sample for the model; settings are chosen on TRAIN only.
usage: python ml_track.py <horizon> [tag]
env ML_WINDOW=<days> trains on the last <days> only (default: all history);
env ML_REGIME=1 adds market-state columns (index 20-day return, breadth, equal-weight 20-day
return, limit-up count share), the same for every stock on a day, so trees can make factor
effects depend on the market state.
"""
import os
import sys
import time

import lightgbm as lgb
import numpy as np
import polars as pl

import harness as H

H_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
TAG = sys.argv[2] if len(sys.argv) > 2 else "v1"
STEP, WARM, SAMPLE = 20, 160, 3
PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=63, min_data_in_leaf=2000,
              feature_fraction=0.7, bagging_fraction=0.7, bagging_freq=1, lambda_l2=10.0,
              num_threads=8, seed=7, verbose=-1)
ROUNDS = 200
c = pl.col

t0 = time.perf_counter()
F = H.features(H_DAYS)
names = [n for n in F.columns if n not in ("code", "date", "fwd_ex")]
R = F.with_columns(
    *((c(n).rank().over("date") / c(n).count().over("date")).cast(pl.Float32).alias(n) for n in names),
    (c("fwd_ex").rank().over("date") / c("fwd_ex").count().over("date")).cast(pl.Float32).alias("y"),
).select("code", "date", *names, "y")
WINDOW = int(os.environ.get("ML_WINDOW", "0"))
if os.environ.get("ML_REGIME") == "1":
    k = H.ctx()
    ix = k.benchmark.select("date", (c("close") / c("close").shift(20) - 1).cast(pl.Float32).alias("m_idx20"))
    mk = (k.panel.lazy().filter(H.tradable().fill_null(False)).group_by("date").agg(
        (c("close") > c("ma20")).mean().cast(pl.Float32).alias("m_breadth"),
        c("ret_20").mean().cast(pl.Float32).alias("m_ew20"),
        c("is_limit_up").mean().cast(pl.Float32).alias("m_zt")).collect())
    R = R.join(ix, on="date", how="left").join(mk, on="date", how="left")
    names = [*names, "m_idx20", "m_breadth", "m_ew20", "m_zt"]
dates = H.ctx().dates
idx = {d: i for i, d in enumerate(dates)}
R = R.with_columns(c("date").replace_strict(idx, return_dtype=pl.Int32).alias("di"))
print(f"ranked {R.height} rows {time.perf_counter() - t0:.0f}s", flush=True)

out, imp = [], np.zeros(len(names))
for start in range(WARM, len(dates), STEP):
    stop = min(start + STEP, len(dates))
    cut = start - H_DAYS - 2  # label of day i ends at open i+1+h: must be before the open of `start`
    lo = cut - WINDOW if WINDOW else -1
    tr = R.filter((c("di") <= cut) & (c("di") > lo) & (c("di") % SAMPLE == 0) & c("y").is_not_null())
    te = R.filter((c("di") >= start) & (c("di") < stop))
    if tr.height < 50_000 or te.is_empty():
        continue
    model = lgb.train(PARAMS, lgb.Dataset(tr.select(names).to_numpy(), tr["y"].to_numpy()), ROUNDS)
    imp += model.feature_importance("gain")
    out.append(te.select("code", "date").with_columns(score=pl.Series(model.predict(te.select(names).to_numpy()))))
    print(f"  fold {dates[start]}..{dates[stop - 1]} train {tr.height} rows {time.perf_counter() - t0:.0f}s", flush=True)

scores = pl.concat(out)
scores.write_parquet(H.LAB / f"ml_h{H_DAYS}_{TAG}.parquet")
top = sorted(zip(names, imp / imp.sum()), key=lambda x: -x[1])[:12]
print("importance:", ", ".join(f"{n} {v:.3f}" for n, v in top))
print(H.show(H.evaluate(scores, f"ml/lgb_h{H_DAYS}_{TAG}", hold=H_DAYS, note=f"walk-forward every {STEP}d")), flush=True)
print(f"done {time.perf_counter() - t0:.0f}s")
