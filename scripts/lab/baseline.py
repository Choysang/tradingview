"""Baselines on the shared book: the low-risk composite, single factors, and a random score."""
import time

import numpy as np
import polars as pl

import harness as H

t0 = time.perf_counter()
F = H.features(20)
c = pl.col


def rank_mean(names):
    return F.select("code", "date", pl.mean_horizontal(*(c(n).rank().over("date") / c(n).count().over("date") for n in names)).alias("score"))


low_risk = rank_mean(["low_turn_5", "max_20", "lowvol_60"]).drop_nulls()
print(H.show(H.evaluate(low_risk, "base/low_risk_combo", note="same 3 factors as the playbook")), flush=True)
rng = np.random.default_rng(0)
rand = F.select("code", "date").with_columns(score=pl.Series(rng.random(F.height)))
print(H.show(H.evaluate(rand, "base/random", note="random score: the book's own drift")), flush=True)
print(f"done {time.perf_counter() - t0:.0f}s")
