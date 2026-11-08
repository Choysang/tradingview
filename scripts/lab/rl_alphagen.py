"""AlphaGen reproduction (Yu et al., KDD 2023, github.com/RL-MLDM/alphagen) on our tensor engine (lab venv).

As in the paper: a PPO agent (sb3-contrib MaskablePPO, invalid actions masked) writes a formula token
by token in reverse Polish notation; at SEP the formula joins a linear alpha pool (capacity 20), the
pool weights are refit by least squares on the factors' mean daily ICs and mutual correlations, the
weakest factor (smallest |weight|) is dropped when over capacity, and the episode reward is the
pool's combined IC (gamma = 1, entropy 0.01). Differences from the paper: an MLP over one-hot
tokens instead of the LSTM extractor, a smaller episode budget (CPU only), our operator set, and the
5-day relative-return label. Mining uses PRE only; SELECT is scored afterwards.

Env: AG_EPISODES (6000), AG_CAPACITY (20), AG_SEED (3). Output .work/lab/rl_alphagen.json.
"""
from __future__ import annotations

import json
import os
import sys
import time

import gymnasium as gym
import numpy as np
import torch
from sb3_contrib import MaskablePPO

import gp_mine as G
import tengine as E

EPISODES, CAPACITY, SEED = int(os.environ.get("AG_EPISODES", "6000")), int(os.environ.get("AG_CAPACITY", "20")), int(os.environ.get("AG_SEED", "3"))
MAX_LEN = 15
FEATS = ["open", "high", "low", "close", "volume", "amount", "vwap", "turn", "ret", "mkt", "cap", "is_limit_up"]
CONSTS = [-1.0, -0.5, 0.5, 1.0, 2.0, 5.0]
DTS = [5, 10, 20, 30, 40, 60]
UN = ["Abs", "Log", "Sign", "CSRank"]
BIN = ["Add", "Sub", "Mul", "Div", "Greater", "Less"]
ROLL = ["Ref", "Mean", "Sum", "Std", "Var", "Max", "Min", "Delta", "WMA", "Slope", "Rank"]
PAIR = ["Corr", "Cov"]
VOCAB = ([("feat", f) for f in FEATS] + [("const", c) for c in CONSTS] + [("dt", d) for d in DTS] + [("un", u) for u in UN]
         + [("bin", b) for b in BIN] + [("roll", r) for r in ROLL] + [("pair", p) for p in PAIR] + [("sep", None)])


class Pool:
    """Linear alpha pool: single ICs g, mutual correlations M (sampled PRE days), weights w = lstsq(M, g)."""

    def __init__(self, miner: G.Miner):
        self.m = miner
        self.exprs: list[str] = []
        self.units: list[torch.Tensor] = []
        self.g = np.zeros(0)
        self.M = np.zeros((0, 0))
        self.w = np.zeros(0)
        self.cache: dict[str, tuple[float, torch.Tensor] | None] = {}
        self.best = 0.0
        self.evals = 0

    def _score(self, expr: str):
        if expr in self.cache:
            return self.cache[expr]
        out = None
        try:
            x = self.m.ev(expr)
            if E.coverage(x, self.m.d) >= 0.6:
                ic = float(np.nanmean(self.m.label.ic(x).numpy()))
                if np.isfinite(ic) and abs(ic) > 0.005:
                    out = (ic, G._unit(x[self.m.sample]))
        except (E.ExprError, RuntimeError):
            out = None
        self.cache[expr] = out
        self.evals += 1
        return out

    def objective(self, g, M) -> tuple[float, np.ndarray]:
        w = np.linalg.lstsq(M, g, rcond=None)[0]
        var = float(w @ M @ w)
        return (float(w @ g / np.sqrt(var)) if var > 1e-12 else 0.0), w

    def try_new(self, expr: str) -> float:
        s = self._score(expr)
        if s is None or expr in self.exprs:
            return self.best if s is not None else 0.0
        ic, u = s
        if self.units:
            corr = torch.einsum("sn,hsn->hs", u, torch.stack(self.units)).mean(1).numpy()
            if np.max(np.abs(corr)) > 0.99:
                return self.best
        else:
            corr = np.zeros(0)
        g = np.r_[self.g, ic]
        M = np.block([[self.M, corr[:, None]], [corr[None, :], np.ones((1, 1))]]) if len(self.g) else np.ones((1, 1))
        obj, w = self.objective(g, M)
        exprs, units = self.exprs + [expr], self.units + [u]
        if len(g) > CAPACITY:
            drop = int(np.argmin(np.abs(w)))
            keep = [i for i in range(len(g)) if i != drop]
            g, M = g[keep], M[np.ix_(keep, keep)]
            exprs, units = [exprs[i] for i in keep], [units[i] for i in keep]
            obj, w = self.objective(g, M)
        self.exprs, self.units, self.g, self.M, self.w = exprs, units, g, M, w
        self.best = max(self.best, obj)
        return obj


class AlphaEnv(gym.Env):
    def __init__(self, pool: Pool):
        self.pool = pool
        self.action_space = gym.spaces.Discrete(len(VOCAB))
        self.observation_space = gym.spaces.Box(0, 1, shape=(MAX_LEN * len(VOCAB),), dtype=np.float32)
        self.episodes = 0
        self.log = []

    def _obs(self):
        o = np.zeros((MAX_LEN, len(VOCAB)), dtype=np.float32)
        for i, a in enumerate(self.tokens):
            o[i, a] = 1
        return o.ravel()

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.tokens, self.stack = [], []  # stack items: ("expr", text, featured) | ("const", v) | ("dt", d)
        return self._obs(), {}

    def action_masks(self) -> np.ndarray:
        st, left = self.stack, MAX_LEN - len(self.tokens)
        top = st[-1] if st else None
        e = lambda it: it is not None and it[0] == "expr"  # noqa: E731
        feat_top = e(top) and top[2]
        n_expr = sum(1 for it in st if it[0] == "expr")
        mask = []
        for kind, _ in VOCAB:
            if kind == "feat":
                ok = (top is None or e(top)) and left - 1 >= len(st)  # room to reduce the stack afterwards
            elif kind == "const":
                ok = feat_top and left - 1 >= len(st) + 1
            elif kind == "dt":
                ok = feat_top and left - 1 >= len(st)
            elif kind == "un":
                ok = feat_top
            elif kind == "bin":
                ok = len(st) >= 2 and e(st[-1]) and e(st[-2]) and (st[-1][2] or st[-2][2])
            elif kind == "roll":
                ok = len(st) >= 2 and top[0] == "dt" and e(st[-2]) and st[-2][2]
            elif kind == "pair":
                ok = len(st) >= 3 and top[0] == "dt" and e(st[-2]) and e(st[-3]) and st[-2][2] and st[-3][2]
            else:
                ok = len(st) == 1 and feat_top
            mask.append(bool(ok) and (left > 0 or kind == "sep"))
        if not any(mask):
            mask[-1] = True
        return np.array(mask)

    def step(self, action):
        kind, val = VOCAB[action]
        self.tokens.append(action)
        st = self.stack
        if kind == "feat":
            st.append(("expr", f"${val}", True))
        elif kind == "const":
            st.append(("expr", f"{val:g}", False))
        elif kind == "dt":
            st.append(("dt", val))
        elif kind == "un":
            a = st.pop()
            st.append(("expr", f"{val}({a[1]})", True))
        elif kind == "bin":
            b, a = st.pop(), st.pop()
            st.append(("expr", f"{val}({a[1]},{b[1]})", True))
        elif kind == "roll":
            dt, a = st.pop(), st.pop()
            st.append(("expr", f"{val}({a[1]},{dt[1]})", True))
        elif kind == "pair":
            dt, b, a = st.pop(), st.pop(), st.pop()
            st.append(("expr", f"{val}({a[1]},{b[1]},{dt[1]})", True))
        done = kind == "sep" or len(self.tokens) >= MAX_LEN
        reward = 0.0
        if done:
            if len(st) == 1 and st[0][0] == "expr" and st[0][2]:
                reward = self.pool.try_new(st[0][1])
            else:
                reward = -1.0
            self.episodes += 1
            if self.episodes % 200 == 0:
                print(f"[alphagen] episodes {self.episodes} evals {self.pool.evals} pool {len(self.pool.exprs)} "
                      f"pool IC {self.pool.best:.4f}", flush=True)
        return self._obs(), reward, done, False, {}


def main() -> None:
    d = E.Data()
    miner = G.Miner(d, d.dates[0], E.PRE_END, out=E.LAB / "rl_alphagen.scores.jsonl")
    pool = Pool(miner)
    env = AlphaEnv(pool)
    model = MaskablePPO("MlpPolicy", env, gamma=1.0, ent_coef=0.01, batch_size=128, n_steps=1024, learning_rate=3e-4,
                        policy_kwargs={"net_arch": [256, 256]}, seed=SEED, verbose=0, device="cpu")
    t0 = time.perf_counter()
    steps_per_episode = 9
    model.learn(total_timesteps=EPISODES * steps_per_episode)
    print(f"[alphagen] trained {env.episodes} episodes, {pool.evals} distinct formulas evaluated, {time.perf_counter() - t0:.0f}s")
    # score the final pool on SELECT (single factors and the weighted combination)
    y = d.label(5)
    sel = d.rows(E.SELECT_START, E.SELECT_END)
    combo = torch.zeros((d.T, d.N))
    rows = []
    for expr, w, g in zip(pool.exprs, pool.w, pool.g):
        x = miner.ev(expr)
        s = E.ic_stats(E.daily_ic(x, y), sel, 5)
        rows.append({"expr": expr, "weight": float(w), "pre_ic": float(g), "sel_ic": s["ic"], "sel_t": s["t"]})
        # the pool's ICs and correlations are rank based, so combine per-day ranks (z-scores of raw values
        # are dominated by heavy tails and flipped the combination's sign in the first run)
        combo += float(w) * torch.nan_to_num(E.cs_rank(torch.where(d.tradable, x, torch.nan)) - 0.5, nan=0.0)
    combo = torch.where(d.tradable, combo, torch.nan)
    s = E.ic_stats(E.daily_ic(combo, y), sel, 5)
    out = {"episodes": env.episodes, "evals": pool.evals, "pool_ic_pre": pool.best, "combo_sel": s, "pool": rows}
    print(json.dumps({k: v for k, v in out.items() if k != "pool"}, indent=1))
    for r in sorted(rows, key=lambda r: -abs(r["weight"])):
        print(f"  w {r['weight']:+.3f} pre {r['pre_ic']:+.4f} SELECT {r['sel_ic']:+.4f} (t {r['sel_t']:+.2f}) {r['expr']}")
    json.dump(out, open(E.LAB / "rl_alphagen.json", "w"), indent=1)
    torch.save(combo, E.LAB / "rl_alphagen_combo.pt")


if __name__ == "__main__":
    sys.exit(main())
