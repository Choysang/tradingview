"""Final test on the lockbox (2026-08-01 .. last bar), run once. The candidate list is fixed here,
before this script ever ran (see FROZEN), after all choices were made on TRAIN and VALID:

1. low_risk          the low-risk composite (traditional/quant track, the current product playbook)
2. llm_composite     the 6 LLM factors picked on TRAIN (LLM track)
3. ml_h20_v1b        walk-forward LightGBM, 20-day horizon (quant track)
4. switch            every 5 days hold whichever of low_risk / llm_composite returned more over the
                     trailing 20 days (fusion rule); one day to trade, 0.2% per unit switched
RL (Q-learning allocation) is not a candidate: it lost to the plain rules on VALID.
"""
import json
import os
import time

import numpy as np
import polars as pl

os.environ["LOCKBOX"] = "1"
import harness as H  # noqa: E402

FROZEN = ["low_risk", "llm_composite", "ml_h20_v1b", "switch"]
K, COST = 5, 0.002
c = pl.col
t0 = time.perf_counter()
F = H.features(20)
low_risk = F.select("code", "date", pl.mean_horizontal(
    *(c(n).rank().over("date") / c(n).count().over("date") for n in ("low_turn_5", "max_20", "lowvol_60"))).alias("score"))
books = {"low_risk": low_risk, "llm_composite": pl.read_parquet(H.LAB / "llm_composite.parquet"),
         "ml_h20_v1b": pl.read_parquet(H.LAB / "ml_h20_v1b.parquet")}
start, end = H.period_bounds("LOCKBOX")
print(f"lockbox {start}..{end}: 中证全指 {H.index_return(start, end):+.2f}%, equal weight {H.ew_return(start, end):+.2f}%")
results = {}
for name in ("low_risk", "llm_composite", "ml_h20_v1b"):
    res = H.evaluate(books[name], f"lockbox/{name}", periods=("LOCKBOX",), note="final test")
    results[name] = res["periods"]["LOCKBOX"]
    print(H.show(res))

# The switch runs on each book's continuous equity (from the first bar), restricted to lockbox days.
full = {}
for name in ("low_risk", "llm_composite"):
    from backend.app.playbooks.backtest import simulate  # noqa: E402
    from contracts.schema import ExitRules, SizingRule  # noqa: E402
    k = H.ctx()
    _, eq = simulate(k.panel, H.to_signals(books[name], 40), ExitRules(holding_days=20, stop_loss_pct=None),
                     SizingRule(method="equal", max_positions=20, max_new_per_day=1), k.benchmark)
    full[name] = eq.select("date", (c("strategy") / c("strategy").shift(1) - 1).fill_null(0.0).alias(name))
R = full["low_risk"].join(full["llm_composite"], on="date")
days = R["date"].to_list()
ret = R.select("low_risk", "llm_composite").to_numpy()
logr = np.log1p(ret)
cum = np.vstack([np.zeros(2), np.cumsum(logr, axis=0)])
first = next(i for i, d in enumerate(days) if d >= start)
rs, picks, prev = [], [], 0
for t in range(first - 2, len(days) - 2, K):
    a = max(0, t - 19)
    choice = 1 if (cum[t + 1, 1] - cum[a, 1]) > (cum[t + 1, 0] - cum[a, 0]) else 0
    seg = ret[t + 2: t + 2 + K, choice]
    rs.append(float(np.prod(1 + seg) - 1) - COST * (choice != prev) * 2)
    picks.append(choice)
    prev = choice
curve = np.cumprod(1 + np.array(rs))
results["switch"] = {"total": round(float(curve[-1] - 1) * 100, 2),
                     "mdd": round(float((curve / np.maximum.accumulate(curve) - 1).min()) * 100, 2),
                     "weeks_offensive": int(sum(picks)), "weeks": len(picks)}
print(f"switch   LOCKBOX total {results['switch']['total']:+.2f}% mdd {results['switch']['mdd']:.2f}% "
      f"(offensive {sum(picks)}/{len(picks)} decisions)")
for name in ("low_risk", "llm_composite"):
    seg = R.filter(c("date") >= start)[name].to_numpy()
    print(f"  continuous {name:14s} lockbox total {(np.prod(1 + seg) - 1) * 100:+.2f}%")
(H.LAB / "lockbox_result.json").write_text(json.dumps({"frozen": FROZEN, "results": results}, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
n = sum(1 for _ in H.TRIALS.open(encoding="utf-8"))
print(f"trials logged before and during the lockbox: {n}; done {time.perf_counter() - t0:.0f}s")
