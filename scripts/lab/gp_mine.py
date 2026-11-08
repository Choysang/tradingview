"""Genetic-programming factor mining on the tensor engine (lab venv).

Every candidate is an expression tree in tengine syntax. Fitness uses only the mining window
(default PRE: first bar .. 2026-06-30): |mean daily rank IC| against the 5-day forward excess
return, shrunk by coverage, a size penalty (AlphaAgent-style complexity control) and a diversity
penalty against the hall of fame (max |rank correlation| with members already kept). SELECT
(2026-07..08) is never used inside the search; it is scored afterwards for every candidate that
passed the mining stage, with Benjamini-Hochberg control across all of them.

Every evaluated candidate goes to .work/lab/gp_<tag>.jsonl so the counts behind any survivor are known.
Env: GP_TAG (default "pre"), GP_START/GP_END (mining window, ISO dates), GP_POP, GP_GENS, GP_SEED.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from datetime import date

import numpy as np
import torch

import tengine as E

TAG = os.environ.get("GP_TAG", "pre")
POP, GENS, SEED = int(os.environ.get("GP_POP", "300")), int(os.environ.get("GP_GENS", "15")), int(os.environ.get("GP_SEED", "7"))
H = 5
GP_FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "turn", "ret", "mkt", "cap", "is_limit_up")
GP_UNARY = ("Abs", "Log", "Sign", "Sqrt", "CSRank", "Neg")
GP_BINARY = ("Add", "Sub", "Mul", "Div", "Greater", "Less")
GP_ROLL = ("Ref", "Mean", "Sum", "Std", "Max", "Min", "Delta", "WMA", "Slope", "Rsquare", "Resi", "Rank", "IdxMax", "IdxMin")
GP_PAIR = ("Corr", "Cov")
WINDOWS = (2, 3, 5, 10, 20, 30, 60, 120)
MAX_SIZE, MAX_DEPTH = 15, 6
DIVERSITY, HOF = 0.7, 300
OUT = E.LAB / f"gp_{TAG}.jsonl"


def to_str(n: tuple) -> str:
    k = n[0]
    if k == "num":
        return f"{n[1]:g}"
    if k == "field":
        return "$" + n[1]
    if k in ("gt", "lt"):
        return f"({to_str(n[1])}{'>' if k == 'gt' else '<'}{to_str(n[2])})"
    args = [to_str(a) if isinstance(a, tuple) else f"{a:g}" if isinstance(a, float) else str(a) for a in n[1:]]
    return f"{k}({','.join(args)})"


class Generator:
    def __init__(self, rng: random.Random):
        self.r = rng

    def leaf(self) -> tuple:
        if self.r.random() < 0.08:
            return ("num", self.r.choice((0.5, 1.0, 2.0, 5.0, 0.01)))
        return ("field", self.r.choice(GP_FIELDS))

    def tree(self, depth: int, full: bool) -> tuple:
        if depth <= 1 or (not full and self.r.random() < 0.3):
            return self.leaf()
        kind = self.r.random()
        if kind < 0.15:
            return (self.r.choice(GP_UNARY), self.tree(depth - 1, full))
        if kind < 0.40:
            return (self.r.choice(GP_BINARY), self.tree(depth - 1, full), self.tree(depth - 1, full))
        if kind < 0.88:
            op = self.r.choice(GP_ROLL)
            w = self.r.choice(WINDOWS if op in ("Ref", "Delta", "Mean", "Sum", "Max", "Min", "WMA") else WINDOWS[1:])
            return (op, self.tree(depth - 1, full), w)
        return (self.r.choice(GP_PAIR), self.tree(depth - 1, full), self.tree(depth - 1, full), self.r.choice(WINDOWS[2:]))

    def random(self) -> tuple:
        while True:
            t = self.tree(self.r.randint(2, 5), self.r.random() < 0.5)
            if self.ok(t):
                return t

    @staticmethod
    def ok(t: tuple) -> bool:
        if t[0] in ("field", "num") or E.size(t) > MAX_SIZE or E.depth(t) > MAX_DEPTH:
            return False
        if not E.fields_of(t):
            return False
        try:
            E.parse(to_str(t))
        except E.ExprError:
            return False
        return True

    def subtrees(self, t: tuple, path=()) -> list:
        out = [path]
        for i, a in enumerate(t[1:], start=1):
            if isinstance(a, tuple):
                out += self.subtrees(a, path + (i,))
        return out

    @staticmethod
    def get(t, path):
        for i in path:
            t = t[i]
        return t

    def put(self, t, path, new):
        if not path:
            return new
        lst = list(t)
        lst[path[0]] = self.put(t[path[0]], path[1:], new)
        return tuple(lst)

    def crossover(self, a: tuple, b: tuple) -> tuple:
        pa, pb = self.r.choice(self.subtrees(a)), self.r.choice(self.subtrees(b))
        return self.put(a, pa, self.get(b, pb))

    def mutate(self, t: tuple) -> tuple:
        p = self.r.choice(self.subtrees(t))
        node = self.get(t, p)
        roll = self.r.random()
        if roll < 0.35 and node[0] in GP_ROLL:
            return self.put(t, p, (node[0], node[1], self.r.choice(WINDOWS[1:])))
        if roll < 0.55 and node[0] in GP_PAIR:
            return self.put(t, p, (node[0], node[1], node[2], self.r.choice(WINDOWS[2:])))
        if roll < 0.7 and node[0] == "field":
            return self.put(t, p, ("field", self.r.choice(GP_FIELDS)))
        if roll < 0.8 and node[0] in GP_ROLL:
            return self.put(t, p, (self.r.choice(GP_ROLL), node[1], node[2]))
        return self.put(t, p, self.tree(self.r.randint(1, 3), False))


def _unit(x: torch.Tensor) -> torch.Tensor:
    """Per-day ranks, missing filled with the middle rank, centred and scaled to unit norm, so the dot
    product of two such rows is their daily rank correlation (missing names count as neutral)."""
    r = torch.nan_to_num(E.cs_rank(x), nan=0.5) - 0.5
    return r / r.norm(dim=1, keepdim=True).clamp(min=1e-9)


class Miner:
    def __init__(self, data: E.Data, start: date, end: date, out=None):
        self.d, self.ev = data, E.Evaluator(data, cache_size=48)
        win = data.rows(start, end)
        self.label = E.RankedLabel(data.label(H), list(range(win.start, win.stop))[::2])  # every 2nd day
        self.seen: dict[str, dict] = {}
        self.hof: list[tuple[float, str, object]] = []  # (fitness, expr, sampled rank tensor)
        self.sample = list(range(win.start, win.stop))[::6]
        self.out = (out or OUT).open("a", encoding="utf-8")
        self._stack = None

    def _hof_stack(self) -> torch.Tensor:
        if self._stack is None:
            self._stack = torch.stack([h[2] for h in self.hof])
        return self._stack

    def fitness(self, t: tuple) -> float:
        expr = to_str(t)
        if expr in self.seen:
            return self.seen[expr]["fit"]
        t0 = time.perf_counter()
        rec = {"expr": expr, "size": E.size(t), "fit": 0.0}
        try:
            x = self.ev(expr)
            cov = E.coverage(x, self.d)
            rec["coverage"] = round(cov, 3)
            if cov >= 0.6:
                ic = self.label.ic(x)
                s = E.ic_stats(ic, slice(None), max(1, H // 2))  # days are every 2nd, so the overlap lag halves
                rec |= {"ic": s["ic"], "t": s["t"], "icir": s["icir"]}
                if math.isfinite(s["ic"]):
                    sign = 1.0 if s["ic"] >= 0 else -1.0
                    base = abs(s["ic"]) * min(1.0, cov / 0.9) * (1 - 0.01 * rec["size"])
                    mc = 0.0
                    if abs(s["t"]) >= 2 and self.hof:  # weak candidates do not need the diversity check
                        u = _unit(x[self.sample] * sign)
                        mc = float((torch.einsum("sn,hsn->hs", u, self._hof_stack()).mean(1)).abs().max())
                    rec["max_corr"] = round(mc, 3)
                    rec["fit"] = base * (1.0 if mc <= DIVERSITY else max(0.0, (1 - mc) / (1 - DIVERSITY)))
                    if abs(s["t"]) >= 3 and mc <= DIVERSITY:
                        self.hof.append((rec["fit"], expr, _unit(x[self.sample] * sign)))
                        self.hof.sort(key=lambda h: -h[0])
                        del self.hof[HOF:]
                        self._stack = None
        except (E.ExprError, RuntimeError) as exc:
            rec["error"] = str(exc)[:120]
        rec["sec"] = round(time.perf_counter() - t0, 2)
        self.seen[expr] = rec
        self.out.write(json.dumps(rec) + "\n")
        self.out.flush()
        return rec["fit"]

    def run(self, rng: random.Random) -> None:
        g = Generator(rng)
        pop = [g.random() for _ in range(POP)]
        for gen in range(GENS):
            t0 = time.perf_counter()
            scored = sorted(((self.fitness(t), t) for t in pop), key=lambda z: -z[0])
            fits = [f for f, _ in scored]
            print(f"[gp {TAG}] gen {gen} evaluated {len(self.seen)} hof {len(self.hof)} best {fits[0]:.4f} "
                  f"median {np.median(fits):.4f} {time.perf_counter() - t0:.0f}s | {to_str(scored[0][1])[:90]}", flush=True)
            elite = [t for _, t in scored[: POP // 10]]
            nxt = list(elite)
            while len(nxt) < POP:
                a = max(rng.sample(scored, 5), key=lambda z: z[0])[1]
                roll = rng.random()
                if roll < 0.6:
                    b = max(rng.sample(scored, 5), key=lambda z: z[0])[1]
                    child = g.crossover(a, b)
                elif roll < 0.9:
                    child = g.mutate(a)
                else:
                    child = g.random()
                if g.ok(child) and to_str(child) not in self.seen:
                    nxt.append(child)
                elif rng.random() < 0.2:
                    nxt.append(g.random())
            pop = nxt


if __name__ == "__main__":
    data = E.Data()
    start = date.fromisoformat(os.environ.get("GP_START", data.dates[0].isoformat()))
    end = date.fromisoformat(os.environ.get("GP_END", E.PRE_END.isoformat()))
    print(f"[gp {TAG}] mining window {start}..{end}, pop {POP}, gens {GENS}, seed {SEED}", flush=True)
    m = Miner(data, start, end)
    m.run(random.Random(SEED))
    print(f"[gp {TAG}] done: {len(m.seen)} candidates, hall of fame {len(m.hof)}", flush=True)
