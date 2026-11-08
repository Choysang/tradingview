"""Which strategy wins in which period, and can the winner be known in advance? (lab venv)

Strategies: the 75 traditional rules (trad_signals.parquet) plus a long-only top-decile and a
bottom-decile book for every zoo factor (Alpha158, A-share extras, behavioural). For strategy s the
cohort of day t is every eligible name it signals (tradable, next open not a sealed limit-up), bought
at the next open and sold at the open 5 days later; R_s(t) is the cohort's mean return minus 0.2%
cost, and X_s(t) the same minus the market mean (percent). Cohorts with fewer than 5 names are empty.

Questions answered on real data:
1. Hindsight: each month's best strategy and what it earned (an upper bound nobody can trade).
2. Persistence: Spearman correlation of strategy performance between consecutive months.
3. Selection rules decided at day t with only complete past cohorts (6-day purge):
   recent winner over L days (L = 5, 20, 60, 120, all history), top 1 and top 5;
   a state model (LightGBM over market/sentiment state x strategy history, refit every 20 days).
Outputs .work/lab/strategy_panel.parquet (R and X per strategy and day) and prints the tables.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date

import lightgbm as lgb
import numpy as np
import polars as pl
import torch

import fusion
import tengine as E
import zoo

H, PURGE, MIN_COHORT = 5, 6, 5


def raw_fwd(d: E.Data) -> torch.Tensor:
    o = d.f["open"]
    f = (E._shift(o, -1 - H) / E._shift(o, -1) - 1) * 100
    blocked = E._shift(d.f["open_at_limit"], -1) > 0
    return torch.where(d.tradable & ~blocked & torch.isfinite(f), f, torch.nan)


def cohort_returns(sig: torch.Tensor, f: torch.Tensor, mkt: torch.Tensor) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    m = sig & torch.isfinite(f)
    n = m.sum(1)
    r = torch.where(m, f, 0).sum(1) / n.clamp(min=1) - 0.2
    ok = n >= MIN_COHORT
    r = torch.where(ok, r, torch.nan)
    return r.numpy(), (r - mkt).numpy(), n.numpy()


def build(d: E.Data) -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
    f = raw_fwd(d)
    mkt = E._nanmean_row(f)
    names, groups, R, X = [], [], [], []
    sig = pl.read_parquet(E.LAB / "trad_signals.parquet").filter(pl.col("code").is_in(d.codes))  # this universe only
    di = {x: i for i, x in enumerate(d.dates)}
    ci = {x: i for i, x in enumerate(d.codes)}
    for (rule,), g in sig.group_by("rule", maintain_order=True):
        grid = torch.zeros((d.T, d.N), dtype=torch.bool)
        grid[[di[x] for x in g["date"].to_list()], [ci[x] for x in g["code"].to_list()]] = True
        r, x, _ = cohort_returns(grid, f, mkt)
        names.append(f"T:{rule}")
        groups.append("traditional")
        R.append(r)
        X.append(x)
    ev = E.Evaluator(d, cache_size=16)
    for name, expr in zoo.zoo().items():
        rank = E.cs_rank(torch.where(d.tradable, ev(expr), torch.nan))
        for side, mask in (("top", rank >= 0.9), ("bot", rank <= 0.1)):
            r, x, _ = cohort_returns(mask & torch.isfinite(rank), f, mkt)
            names.append(f"F:{name}:{side}")
            groups.append("behavioral" if name.startswith("BEH_") else "factor")
            R.append(r)
            X.append(x)
    return names, groups, np.array(R), np.array(X)


def months(d: E.Data) -> list[tuple[str, list[int]]]:
    out: dict[str, list[int]] = {}
    for i, x in enumerate(d.dates):
        out.setdefault(x.strftime("%Y-%m"), []).append(i)
    return list(out.items())


def trailing_mean(X: np.ndarray, t: int, look: int | None, first: int = 130) -> np.ndarray:
    b = t - PURGE + 1
    a = max(first, b - look) if look else first
    with np.errstate(all="ignore"):
        return np.nanmean(X[:, a:b], axis=1) if b > a else np.full(X.shape[0], np.nan)


def select_rules(d: E.Data, X: np.ndarray, start: int) -> dict[str, np.ndarray]:
    """Daily pick of strategies from past cohorts; returns each rule's realised X per day (top1, top5)."""
    rules = {}
    for look in (5, 20, 60, 120, None):
        top1, top5 = np.full(d.T, np.nan), np.full(d.T, np.nan)
        for t in range(start, d.T):
            m = trailing_mean(X, t, look)
            avail = np.isfinite(m) & np.isfinite(X[:, t])
            if avail.sum() < 10:
                continue
            order = np.argsort(-np.where(avail, m, -np.inf))
            top1[t] = X[order[0], t]
            top5[t] = np.nanmean(X[order[:5], t])
        tag = f"recent{look}" if look else "alltime"
        rules[f"{tag}_top1"], rules[f"{tag}_top5"] = top1, top5
    rules["all_strategies_mean"] = np.nanmean(X, axis=0)
    return rules


def state_model(d: E.Data, X: np.ndarray, groups: list[str], start: int, refit: int = 20) -> dict[str, np.ndarray]:
    """LightGBM: X_s(t) ~ market/sentiment state at t + the strategy's own past performance + group."""
    ms = {k: v[:, 0].numpy() for k, v in fusion.market_state(d).items()}
    S = X.shape[0]
    gid = np.array([["traditional", "factor", "behavioral"].index(g) for g in groups])
    feats = {}
    for look in (5, 20, 60, None):
        tm = np.full((S, d.T), np.nan)
        for t in range(130 + PURGE, d.T):
            tm[:, t] = trailing_mean(X, t, look)
        feats[f"past{look or 'all'}"] = tm
    names = list(ms) + list(feats) + ["group", "sid"]

    def rows(t):
        return np.column_stack([np.full(S, ms[k][t]) for k in ms] + [feats[k][:, t] for k in feats] + [gid, np.arange(S)])

    top1, top5 = np.full(d.T, np.nan), np.full(d.T, np.nan)
    params = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 200,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1, "num_threads": 4, "seed": 1}
    model = None
    for t in range(start, d.T):
        if (t - start) % refit == 0:
            tr = [u for u in range(200, t - PURGE + 1)]
            Xtr = np.vstack([rows(u) for u in tr])
            ytr = np.concatenate([X[:, u] for u in tr])
            ok = np.isfinite(ytr)
            model = lgb.train(params, lgb.Dataset(Xtr[ok], np.clip(ytr[ok], -15, 15), feature_name=names,
                                                  categorical_feature=["group", "sid"]), num_boost_round=200)
        p = model.predict(rows(t))
        avail = np.isfinite(X[:, t])
        if avail.sum() < 10:
            continue
        order = np.argsort(-np.where(avail, p, -np.inf))
        top1[t] = X[order[0], t]
        top5[t] = np.nanmean(X[order[:5], t])
    return {"state_model_top1": top1, "state_model_top5": top5}


def main() -> None:
    t0 = time.perf_counter()
    d = E.Data()
    names, groups, R, X = build(d)
    print(f"[panel] {len(names)} strategies built in {time.perf_counter() - t0:.0f}s", flush=True)
    pl.DataFrame({"strategy": np.repeat(names, d.T), "date": d.dates * len(names),
                  "R": R.ravel(), "X": X.ravel()}).write_parquet(E.LAB / "strategy_panel.parquet")
    verify = os.environ.get("VERIFY") == "1"
    last_ok = d.dates.index(E.SELECT_END) if not verify else d.T - 1
    # 1. hindsight best per month (excess over the market, percent per 5-day cohort)
    mon = [(m, idx) for m, idx in months(d) if idx[0] >= 140 and idx[-1] <= last_ok]
    perf = np.array([[np.nanmean(X[s, idx]) if np.isfinite(X[s, idx]).sum() >= 5 else np.nan for _, idx in mon] for s in range(len(names))])
    print("\n[1] best strategy each month (hindsight), mean 5-day excess per cohort %, and the median strategy")
    best_rows = []
    for j, (m, _) in enumerate(mon):
        col = perf[:, j]
        b = int(np.nanargmax(col))
        trad = [i for i, g in enumerate(groups) if g == "traditional"]
        bt = trad[int(np.nanargmax(col[trad]))]
        best_rows.append({"month": m, "best": names[b], "best_x": round(float(col[b]), 2), "best_trad": names[bt],
                          "best_trad_x": round(float(col[bt]), 2), "median": round(float(np.nanmedian(col)), 2)})
        print(f"  {m}: best {names[b]:28s} {col[b]:+6.2f} | best traditional {names[bt]:24s} {col[bt]:+6.2f} | median {np.nanmedian(col):+5.2f}")
    # 2. persistence of monthly ranks
    rho = []
    for j in range(len(mon) - 1):
        a, b = perf[:, j], perf[:, j + 1]
        ok = np.isfinite(a) & np.isfinite(b)
        rho.append(float(pl.DataFrame({"a": a[ok], "b": b[ok]}).select(pl.corr("a", "b", method="spearman")).item()))
    print(f"\n[2] month-to-month rank persistence (Spearman across {len(names)} strategies): mean {np.mean(rho):+.3f}; "
          + " ".join(f"{mon[j + 1][0]}:{r:+.2f}" for j, r in enumerate(rho)))
    winners_next = [float(np.nanpercentile(perf[:, j + 1], 50)) for j in range(len(mon) - 1)]
    pct = []
    for j in range(len(mon) - 1):
        b = int(np.nanargmax(perf[:, j]))
        col = perf[:, j + 1]
        pct.append(float(np.nanmean(col <= col[b])) if np.isfinite(col[b]) else np.nan)
    print(f"    last month's winner lands at percentile {np.nanmean(pct) * 100:.0f} next month on average")
    # 3. selection rules
    start = d.dates.index(min(x for x in d.dates if x >= date(2025, 1, 2)))
    picks = select_rules(d, X, start) | state_model(d, X, groups, start)
    print("\n[3] daily selection rules: mean 5-day excess of the picked cohort, percent (6-day purge, no hindsight)")
    per = fusion.periods(d)
    table = {}
    for rule, v in picks.items():
        table[rule] = {p: round(float(np.nanmean(v[r])), 3) for p, r in per.items()}
        print(f"  {rule:22s} " + " | ".join(f"{p} {x:+6.3f}" for p, x in table[rule].items()))
    json.dump({"months": best_rows, "persistence": rho, "winner_next_pct": pct, "selection": table},
              open(E.LAB / f"strategy_panel{'_verify' if verify else ''}.json", "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
