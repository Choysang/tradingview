"""Stock-level offline RL by Fitted Q Iteration (Ernst et al. 2005) with LightGBM (lab venv).

MDP per stock-day: state x = per-day percentile features (zoo subset, the walk-forward fusion score
and its change) plus market-state columns; position p in {0, 1}; action a in {0 flat, 1 long}.
Reward for a decision at close t: a * r(t) - c * |a - p|, where r(t) is the open t+1 -> open t+2
return (raw or minus the market mean, env RL_REWARD=raw|excess) and c = 0.1% per side. Because the
cost only depends on (p, a), Q splits into W_a(x) = E[a*r + gamma * V(x', a)], with
  W1 <- r + gamma * max(W1(x'), W0(x') - c),   W0 <- gamma * max(W0(x'), W1(x') - c).
Trained on every tradable stock-day of the training window (about a million transitions), refit
for K sweeps. Policy: hold a name while W1 > W0 - c, enter while W1 - c > W0, best advantage first,
at most `slots` names. Evaluated on SELECT with book.py's rules (same as the fusion books).

Env: RL_REWARD (excess), RL_GAMMA (0.8), RL_SWEEPS (6), RL_TRAIN (2025-01-02..2026-06-22), RL_SCORE
(fusion grid used as a state column, default fusion_lgbm_hl120.pt).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date

import lightgbm as lgb
import numpy as np
import torch

import book as B
import fusion
import tengine as E
import zoo

REWARD = os.environ.get("RL_REWARD", "excess")
GAMMA, SWEEPS = float(os.environ.get("RL_GAMMA", "0.8")), int(os.environ.get("RL_SWEEPS", "6"))
TRAIN = tuple(date.fromisoformat(x) for x in os.environ.get("RL_TRAIN", "2025-01-02..2026-06-22").split(".."))
SCORE = os.environ.get("RL_SCORE", "fusion_lgbm_hl120.pt")
C = B.COST
SCORE_COL = 30  # index of "SCORE" in state(): len(STATE_ZOO)
STATE_ZOO = ("ROC5", "ROC20", "ROC60", "STD20", "VMA20", "VMA60", "VSUMP20", "CORR10", "CORR20", "KUP", "KLOW", "KMID",
             "SIZE", "TURN_MA20", "TURN_REL5", "AMIHUD20", "IVOL20", "GAP20", "INTRA20", "ZT20", "MAXRET20",
             "BETA_MKT60", "MA20", "MA60", "RSV20", "MIN20", "MAX20", "QTLD20", "CNTP20", "RET_STD20")
PARAMS = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 1000,
          "feature_fraction": 0.8, "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 10.0,
          "verbose": -1, "num_threads": 8, "seed": 5}


def state(d: E.Data) -> tuple[list[str], torch.Tensor]:
    ev = E.Evaluator(d, cache_size=16)
    z = zoo.zoo()
    cols, names = [], []
    for n in STATE_ZOO:
        cols.append(E.cs_rank(torch.where(d.tradable, ev(z[n]), torch.nan)))
        names.append(n)
    sc = torch.load(E.LAB / SCORE)
    rs = E.cs_rank(sc)
    cols += [rs, rs - E._shift(rs, 1)]
    names += ["SCORE", "SCORE_D1"]
    for n, x in fusion.market_state(d).items():
        cols.append(x)
        names.append(n)
    return names, torch.stack(cols, -1).to(torch.float32)  # [T, N, F]


def reward(d: E.Data) -> torch.Tensor:
    r = B._o2o(d)
    if REWARD == "excess":
        m = torch.where(d.tradable, r, torch.nan)
        r = r - E._nanmean_row(m)[:, None]
    return torch.where(d.tradable, r, 0.0)


def train(d: E.Data, X: torch.Tensor, r: torch.Tensor) -> tuple[lgb.Booster, lgb.Booster]:
    a, b = d.dates.index(min(x for x in d.dates if x >= TRAIN[0])), d.dates.index(max(x for x in d.dates if x <= TRAIN[1]))
    has_score = torch.isfinite(X[..., SCORE_COL])
    ok = d.tradable & has_score
    nxt_ok = torch.zeros_like(ok)
    nxt_ok[:-1] = ok[1:]
    idx = torch.nonzero(ok[a: b + 1])
    idx[:, 0] += a
    ti, ni = idx[:, 0], idx[:, 1]
    xs = X[ti, ni].numpy()
    xn = X[(ti + 1).clamp(max=d.T - 1), ni].numpy()
    alive = nxt_ok[ti, ni].numpy()
    rr = r[ti, ni].numpy().astype(np.float64)
    print(f"[fqi] {len(ti)} transitions {d.dates[a]}..{d.dates[b]}, reward {REWARD}, gamma {GAMMA}, mean r {rr.mean() * 100:.3f}%", flush=True)
    w1 = w0 = None
    for k in range(SWEEPS):
        if w1 is None:
            v1n = v0n = np.zeros(len(ti))
        else:
            q1, q0 = w1.predict(xn, num_threads=8), w0.predict(xn, num_threads=8)
            v1n = np.where(alive, np.maximum(q1, q0 - C), 0.0)
            v0n = np.where(alive, np.maximum(q0, q1 - C), 0.0)
        y1, y0 = rr + GAMMA * v1n, GAMMA * v0n
        t0 = time.perf_counter()
        w1 = lgb.train(PARAMS, lgb.Dataset(xs, y1), num_boost_round=200)
        w0 = lgb.train(PARAMS, lgb.Dataset(xs, y0), num_boost_round=200)
        adv = w1.predict(xs[:200_000], num_threads=8) - w0.predict(xs[:200_000], num_threads=8)
        print(f"  sweep {k}: target1 mean {y1.mean() * 100:.3f}% sd {y1.std() * 100:.3f}%, adv>c share {(adv > C).mean():.3f} {time.perf_counter() - t0:.0f}s", flush=True)
    return w1, w0


def policy_book(d: E.Data, X: torch.Tensor, w1, w0, rows: slice, slots: int = 20) -> dict:
    target = torch.zeros((d.T, d.N), dtype=torch.bool)
    held: set[int] = set()
    blocked = E._shift(d.f["open_at_limit"], -1) > 0
    exposure = []
    for t in range(rows.start, rows.stop):
        cols = torch.nonzero(d.tradable[t] & torch.isfinite(X[t, :, SCORE_COL])).flatten()
        q1 = w1.predict(X[t, cols].numpy(), num_threads=8)
        q0 = w0.predict(X[t, cols].numpy(), num_threads=8)
        pos = {int(j): i for i, j in enumerate(cols.tolist())}
        keep = {j for j in held if j in pos and q1[pos[j]] > q0[pos[j]] - C}
        adv = q1 - C - q0
        order = np.argsort(-adv)
        for i in order:
            if len(keep) >= slots or adv[i] <= 0:
                break
            j = int(cols[i])
            if j not in keep and not bool(blocked[t, j]):
                keep.add(j)
        held = keep
        exposure.append(len(held) / slots)
        for j in held:
            target[t, j] = True
    res = B.holdings(d, target, rows, slots)
    res["avg_exposure"] = round(float(np.mean(exposure)), 3)
    return res


def main() -> None:
    d = E.Data()
    names, X = state(d)
    r = reward(d)
    w1, w0 = train(d, X, r)
    imp = sorted(zip(w1.feature_importance("gain"), names), reverse=True)[:10]
    print("[fqi] W1 top features:", [n for _, n in imp], flush=True)
    out = {}
    for p, rows in fusion.periods(d).items():
        if p in ("2025H1", "2025H2", "2026H1"):
            continue  # in-sample for the RL fit
        res = policy_book(d, X, w1, w0, rows)
        res.pop("nav")
        out[p] = res
        print(f"[fqi] {p}: {res}", flush=True)
    tag = f"{REWARD}_g{GAMMA}"
    json.dump(out, open(E.LAB / f"rl_fqi_{tag}{'_verify' if 'VERIFY' in out else ''}.json", "w"), indent=1)
    w1.save_model(str(E.LAB / f"rl_fqi_{tag}_w1.txt"))
    w0.save_model(str(E.LAB / f"rl_fqi_{tag}_w0.txt"))


if __name__ == "__main__":
    sys.exit(main())
