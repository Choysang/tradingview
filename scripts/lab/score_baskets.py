"""E071: low-turnover baskets on the frozen score (pre-registered in docs/research/2026-11-08-batch3-preregistration.md).

Every R (10 / 20) sessions from 2024-08-20, decide on day D's close, trade at the D+1 open. Baskets over the pool
names with a score on D: T30 (top 30%), T30b (held names stay while in the top 50%, refill from the top 30%), X20
(all but the bottom 20%), X20b (held names leave only in the bottom 10%; new names only from the top 80%), EW (all).
Equal weight at each rebalance, buy and hold to the next trade day; stock return = next trade-day open / this one,
the last close standing in for a missing open. Cost per rebalance = (weight sold + weight bought) x 0.15%.
Control: 100 random scorers redrawn at every rebalance under the T30 / X20 rules, gross only.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe score_baskets.py
"""
from __future__ import annotations

import json
import math
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
import sameday_base as B
from harness import CODE, c

PINNED = H.LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
OUT = H.LAB / "score_baskets.json"
START, PRE_END, DEV_START = date(2024, 8, 20), date(2026, 6, 30), date(2026, 7, 1)
SIDE = 0.0015
N_RANDOM, SEED = 100, 20261108
RULES = ("T30", "T30b", "X20", "X20b", "EW")


def pick(rule: str, ranks: dict[str, float], held: set[str]) -> set[str]:
    """ranks: code -> score percentile in (0, 1], higher is better."""
    if rule == "EW":
        return set(ranks)
    if rule == "T30":
        return {k for k, q in ranks.items() if q > 0.7}
    if rule == "X20":
        return {k for k, q in ranks.items() if q > 0.2}
    if rule == "T30b":
        target = math.ceil(0.3 * len(ranks))
        keep = {k for k in held if ranks.get(k, 0) > 0.5}
        adds = sorted((k for k, q in ranks.items() if q > 0.7 and k not in keep), key=lambda k: -ranks[k])
        return keep | set(adds[:max(0, target - len(keep))])
    if rule == "X20b":
        return {k for k in held if ranks.get(k, 0) > 0.1} | {k for k, q in ranks.items() if q > 0.2}
    raise ValueError(rule)


def prices(panel: pl.DataFrame, days: list[date]) -> dict[date, dict[str, float]]:
    """Per trade day: code -> open, or the last close before when the stock has no bar that day."""
    want = set(days)
    p = panel.select(CODE, "date", "open", "close").sort(CODE, "date")
    out: dict[date, dict[str, float]] = {d: {} for d in days}
    for (code,), g in p.partition_by(CODE, as_dict=True).items():
        dts, op, cl = g["date"].to_list(), g["open"].to_list(), g["close"].to_list()
        j, last = 0, None
        for d in days:
            while j < len(dts) and dts[j] < d:
                last = cl[j]
                j += 1
            if j < len(dts) and dts[j] == d and op[j]:
                out[d][code] = op[j]
            elif last:
                out[d][code] = last
    return out


def segment(t: date) -> str:
    return "pre" if t <= PRE_END else "dev"


def run(R: int, panel: pl.DataFrame, pool: pl.DataFrame, days: list[date], rng: np.random.Generator) -> dict:
    nxt = {a: b for a, b in zip(days, days[1:])}
    sdays = sorted(pool["date"].unique().to_list())
    # restart the schedule at the DEV boundary (decision on the last day <= PRE_END) so both R share one split
    cut = max(i for i, d in enumerate(sdays) if d <= PRE_END)
    decide = sdays[:cut][::R] + sdays[cut:][::R]
    trade = [nxt[d] for d in decide if d in nxt]
    decide = decide[:len(trade)]
    px = prices(panel, trade + [days[-1]])
    by = {d: dict(zip(g[CODE].to_list(), g["q"].to_list())) for (d,), g in pool.select("date", CODE, "q").partition_by("date", as_dict=True).items()}
    held = {r: set() for r in RULES}
    nav = {r: {"pre": [1.0, 1.0], "dev": [1.0, 1.0]} for r in RULES}  # [net, gross]
    curve = {r: [1.0] for r in RULES}
    turn = {r: [] for r in RULES}
    rand = {"T30": {"pre": np.ones(N_RANDOM), "dev": np.ones(N_RANDOM)}, "X20": {"pre": np.ones(N_RANDOM), "dev": np.ones(N_RANDOM)}}
    for k, (d, t) in enumerate(zip(decide, trade)):
        end = trade[k + 1] if k + 1 < len(trade) else days[-1]
        if end == t:
            continue
        p0, p1 = px[t], px[end]
        ranks = by[d]
        seg = segment(t)
        ret = {code: p1[code] / p0[code] - 1 for code in ranks if code in p0 and code in p1 and p0[code] > 0}
        for r in RULES:
            new = {x for x in pick(r, ranks, held[r]) if x in ret}
            cost = L.rebalance_cost(held[r], new, SIDE)
            g = float(np.mean([ret[x] for x in new])) if new else 0.0
            turn[r].append(cost / SIDE / 2)
            nav[r][seg][0] *= 1 + g - cost
            nav[r][seg][1] *= 1 + g
            curve[r].append(curve[r][-1] * (1 + g - cost))
            held[r] = new
        arr = np.array(list(ret.values()))
        for r, share in (("T30", 0.3), ("X20", 0.8)):
            m = max(1, round(share * arr.size))
            idx = np.argsort(rng.random((N_RANDOM, arr.size)), axis=1)[:, :m]
            rand[r][seg] *= 1 + arr[idx].mean(axis=1)
    res = {"R": R, "rebalances": len(trade)}
    for r in RULES:
        cv = np.array(curve[r])
        mdd = float((1 - cv / np.maximum.accumulate(cv)).max())
        res[r] = {"pre_net": round(nav[r]["pre"][0] - 1, 4), "dev_net": round(nav[r]["dev"][0] - 1, 4),
                  "pre_gross": round(nav[r]["pre"][1] - 1, 4), "dev_gross": round(nav[r]["dev"][1] - 1, 4),
                  "mdd_net_rebalance_grid": round(mdd, 4), "mean_one_side_turnover": round(float(np.mean(turn[r])), 3)}
    for r in ("T30", "X20"):
        res[f"random_{r}"] = {s: {"p50": round(float(np.percentile(rand[r][s], 50)) - 1, 4), "p95": round(float(np.percentile(rand[r][s], 95)) - 1, 4)}
                              for s in ("pre", "dev")}
    for r in ("T30", "T30b", "X20", "X20b"):
        base = "T30" if r.startswith("T30") else "X20"
        beat_ew = all(res[r][f"{s}_net"] > res["EW"][f"{s}_net"] for s in ("pre", "dev"))
        beat_rand = all(res[r][f"{s}_gross"] > res[f"random_{base}"][s]["p95"] for s in ("pre", "dev"))
        res[r]["works"] = bool(beat_ew and beat_rand)
        res[r]["beat_ew_net"], res[r]["beat_random_p95_gross"] = beat_ew, beat_rand
    return res


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    out = B.outcomes(panel).filter(c("in_pool") & (c("date") >= START))
    s = pl.read_parquet(PINNED).select(CODE, "date", "score")
    pool = out.join(s, on=[CODE, "date"], how="inner").with_columns((c("score").rank() / c("score").count()).over("date").alias("q"))
    rng = np.random.default_rng(SEED)
    res = {f"R{R}": run(R, panel, pool, days, rng) for R in (10, 20)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in res.items():
        print(k, json.dumps(v, ensure_ascii=False))


if __name__ == "__main__":
    main()
