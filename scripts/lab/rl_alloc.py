"""RL track: tabular Q-learning that allocates between stock books (and cash) from market state.

Books: the standard book run on each score file over TRAIN + VALID; their daily returns are the
environment. Every K days, after the close, the agent sees the market state and picks a weight
vector over books; it applies from the day after next (one day to trade) for K days. Changing
weights costs 0.2% per unit moved (sell one book, buy another). Bins come from TRAIN quantiles;
the Q table is learned on TRAIN only, frozen, then run on VALID.
State: 中证全指 20-day return, breadth (share of tradable stocks above their 20-day line), the
offensive book's own trailing 20-day return minus the defensive book's (which style is working),
3 bins each, plus the current action.
usage: python rl_alloc.py defensive.parquet offensive.parquet [K]
"""
import json
import sys
import time

import numpy as np
import polars as pl

import harness as H

DEF, OFF = sys.argv[1], sys.argv[2]
K = int(sys.argv[3]) if len(sys.argv) > 3 else 5
COST, ALPHA, GAMMA, EPOCHS, BINS = 0.002, 0.05, 0.8, 400, 3
# weights over (defensive, offensive); the rest is cash
ACTIONS = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [0.5, 0.0], [0.0, 0.0]])
c = pl.col
t0 = time.perf_counter()
k = H.ctx()


def book(name: str) -> pl.DataFrame:
    if name == "low_risk":
        F = H.features(20)
        s = F.select("code", "date", pl.mean_horizontal(*(c(n).rank().over("date") / c(n).count().over("date")
                                                          for n in ("low_turn_5", "max_20", "lowvol_60"))).alias("score"))
    else:
        s = pl.read_parquet(H.LAB / name)
    eq = H.equity(s, "ALL_OPEN").select("date", "strategy")
    return eq.with_columns((c("strategy") / c("strategy").shift(1) - 1).fill_null(0.0).alias(name)).select("date", name)


R = book(DEF).join(book(OFF), on="date")
days = R["date"].to_list()
ret = R.select(DEF, OFF).to_numpy()
idx = k.benchmark.select("date", (c("close") / c("close").shift(20) - 1).alias("i20"))
br = (k.panel.lazy().filter(H.tradable().fill_null(False)).group_by("date")
      .agg((c("close") > c("ma20")).mean().alias("breadth")).collect())
st = pl.DataFrame({"date": days}).join(idx, on="date", how="left").join(br, on="date", how="left").fill_null(strategy="forward")
spread = np.zeros(len(days))
logr = np.log1p(ret)
cum = np.vstack([np.zeros(2), np.cumsum(logr, axis=0)])
for t in range(len(days)):
    a = max(0, t - 19)
    spread[t] = (cum[t + 1, 1] - cum[a, 1]) - (cum[t + 1, 0] - cum[a, 0])
X = np.column_stack([st["i20"].to_numpy(), st["breadth"].to_numpy(), spread])
train = np.array([d <= H.TRAIN_END for d in days])
valid = np.array([H.TRAIN_END < d <= H.VALID_END for d in days])
edges = [np.nanquantile(X[train, j], [1 / 3, 2 / 3]) for j in range(X.shape[1])]
cell = np.zeros(len(days), dtype=int)
for j in range(X.shape[1]):
    cell = cell * BINS + np.digitize(np.nan_to_num(X[:, j], nan=np.nanmedian(X[train, j])), edges[j])


def reward(t: int, a_prev: int, a: int) -> float:
    w = ACTIONS[a]
    seg = ret[t + 2: t + 2 + K]
    gross = float(np.prod(1 + seg @ w) - 1) if len(seg) else 0.0
    return gross - COST * float(np.abs(w - ACTIONS[a_prev]).sum())


decisions = list(range(20, len(days) - K - 2, K))
tr = [t for t in decisions if train[t + K + 1]]
rng = np.random.default_rng(5)
nA = len(ACTIONS)
Q = np.zeros((BINS ** X.shape[1] * nA, nA))
for ep in range(EPOCHS):
    eps = max(0.05, 1 - ep / (EPOCHS * 0.7))
    a_prev = 0
    for i, t in enumerate(tr):
        s = cell[t] * nA + a_prev
        a = int(rng.integers(nA)) if rng.random() < eps else int(Q[s].argmax())
        r = reward(t, a_prev, a) * 100
        nxt = tr[i + 1] if i + 1 < len(tr) else None
        target = r + (GAMMA * Q[cell[nxt] * nA + a].max() if nxt is not None else 0.0)
        Q[s, a] += ALPHA * (target - Q[s, a])
        a_prev = a


def run(choose, mask) -> dict:
    a_prev, rs, acts = 0, [], []
    for t in decisions:
        if not mask[t + 2] or not mask[min(t + K + 1, len(days) - 1)]:
            continue
        a = choose(t, a_prev)
        rs.append(reward(t, a_prev, a))
        acts.append(a)
        a_prev = a
    r = np.array(rs)
    curve = np.cumprod(1 + r)
    per_year = 244 / K
    return {"total": round(float(curve[-1] - 1) * 100, 2), "sharpe": round(float(r.mean() / r.std() * np.sqrt(per_year)), 2),
            "mdd": round(float((curve / np.maximum.accumulate(curve) - 1).min()) * 100, 2),
            "mix": np.bincount(acts, minlength=nA).tolist()}


i20 = X[:, 0]
policies = {
    "defensive": lambda t, a: 0, "offensive": lambda t, a: 1, "blend": lambda t, a: 2,
    "rule_trend": lambda t, a: 1 if i20[t] > 0 else 0,
    "rule_style_momentum": lambda t, a: 1 if spread[t] > 0 else 0,
    "rl_q": lambda t, a: int(Q[cell[t] * nA + a].argmax()),
}
print(f"books: defensive={DEF} offensive={OFF}; decide every {K} days; actions {ACTIONS.tolist()}")
for name, pol in policies.items():
    a, b = run(pol, train), run(pol, valid)
    print(f"{name:20s} TRAIN {a['total']:+8.2f}% sh {a['sharpe']:+5.2f} mdd {a['mdd']:6.2f}% | "
          f"VALID {b['total']:+8.2f}% sh {b['sharpe']:+5.2f} mdd {b['mdd']:6.2f}% actions {b['mix']}")
with H.TRIALS.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"name": f"rl/alloc_{DEF}_{OFF}_K{K}", "note": "book allocation", "periods": {}}) + "\n")
print(f"states visited {(np.abs(Q).sum(axis=1) > 0).sum()}/{Q.shape[0]}; done {time.perf_counter() - t0:.0f}s")
