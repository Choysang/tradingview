"""E063: turnover of the main-book score — fixed holds vs a rank-buffer exit (pre-registered in
docs/research/2026-11-07-batch1-preregistration.md).

Book: 20 slots, each funded with NAV / 20 at entry. Decisions at D's close from fusion_static40_safe, fills at
the D+1 open; a D+1 open sealed at the limit-up is not bought (the next name in the top 40 takes the slot), a
holding whose open is sealed at the limit-down is not sold that day (retried next open), a suspended holding is
kept at its last close. Costs: half the round trip on each side.
- R0: 4 new names a day from the top 40 not held, sold after 5 sessions (the frozen book without its gate).
- R1: 2 a day, sold after 10 sessions.
- R2: after 5 sessions keep while the day's rank <= 100, else sell; before 5 sessions sell when the rank falls into
  the bottom half of the scored pool (no score = bottom); empty slots refilled, at most 4 a day, from the top 40.
- R3: as R2 with keep rank <= 200.
Benchmark: equal-weight mean close-to-close return of the scored pool (the lab's tradable main board).
Main metric: annualized excess over the benchmark after a 0.30% round trip; 0.45% also reported.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe rank_buffer.py
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

import harness as H
from harness import CODE, c

OUT = H.LAB / "rank_buffer.json"
SLOTS, OFFER = 20, 40
SPLIT = date(2026, 7, 1)


@dataclass(frozen=True)
class Rule:
    name: str
    per_day: int
    hold: int
    keep_rank: int | None = None  # rank buffer: keep after `hold` sessions while rank <= keep_rank
    early_bottom: bool = False  # sell before `hold` sessions when the rank falls into the bottom half


RULES = (Rule("R0", 4, 5), Rule("R1", 2, 10), Rule("R2", 4, 5, 100, True), Rule("R3", 4, 5, 200, True))


def matrices():
    k = H.ctx()
    panel = k.panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    sc = pl.read_parquet(H.LAB / "fusion_static40_safe.parquet").filter(c("score").is_finite())
    dates = sorted(d for d in panel["date"].unique().to_list() if d >= sc["date"].min())
    codes = sorted(set(sc[CODE].unique().to_list()))
    ci = {x: i for i, x in enumerate(codes)}
    di = {d: i for i, d in enumerate(dates)}
    T, N = len(dates), len(codes)
    px = panel.filter(c(CODE).is_in(codes) & c("date").is_in(dates)).select(CODE, "date", "open", "close", "open_at_limit", "open_at_floor")
    ii = np.array([ci[x] for x in px[CODE].to_list()])
    tt = np.array([di[d] for d in px["date"].to_list()])
    O, C = np.full((T, N), np.nan), np.full((T, N), np.nan)
    UP, DN = np.zeros((T, N), bool), np.zeros((T, N), bool)
    O[tt, ii], C[tt, ii] = px["open"].to_numpy(), px["close"].to_numpy()
    UP[tt, ii], DN[tt, ii] = px["open_at_limit"].fill_null(False).to_numpy(), px["open_at_floor"].fill_null(False).to_numpy()
    sc = sc.filter(c("date").is_in(dates)).with_columns(c("score").rank("ordinal", descending=True).over("date").alias("rk"),
                                                         pl.len().over("date").alias("n"))
    RK = np.full((T, N), np.inf)
    NS = np.zeros(T)
    si = np.array([ci[x] for x in sc[CODE].to_list()])
    st = np.array([di[d] for d in sc["date"].to_list()])
    RK[st, si] = sc["rk"].to_numpy()
    for t, n in sc.group_by("date").agg(c("n").first()).iter_rows():
        NS[di[t]] = n
    # benchmark: mean close-to-close return over names scored the day before
    ret = C[1:] / C[:-1] - 1
    scored_prev = np.isfinite(RK[:-1])
    bench = np.r_[0.0, np.nanmean(np.where(scored_prev & np.isfinite(ret), ret, np.nan), axis=1)]
    return dates, codes, O, C, UP, DN, RK, NS, bench


def simulate(rule: Rule, cost: float, m) -> dict:
    dates, codes, O, C, UP, DN, RK, NS, bench = m
    T = len(dates)
    half = cost / 2 / 100
    cash, nav = 1.0, np.ones(T)
    pos: dict[int, list] = {}  # code idx -> [units, entry_t, last_px]
    sell_q: set[int] = set()
    buy_q: list[int] = []
    buy_cap = 0
    sells = 0
    held_days = []
    for t in range(T):
        # opens: sells first, then buys
        for j in list(sell_q):
            if np.isfinite(O[t, j]) and not DN[t, j]:
                u, e, _ = pos.pop(j)
                cash += u * O[t, j] * (1 - half)
                sells += 1
                held_days.append(t - e)
                sell_q.discard(j)
        prev_nav = nav[t - 1] if t else 1.0
        alloc = prev_nav / SLOTS
        fills = 0
        for j in buy_q:
            if fills >= buy_cap or len(pos) >= SLOTS or cash < alloc * 0.5:
                break
            if j in pos or not np.isfinite(O[t, j]) or UP[t, j]:
                continue  # blocked at the open: the next name in the top 40 takes the slot
            spend = min(alloc, cash)
            pos[j] = [spend * (1 - half) / O[t, j], t, O[t, j]]
            cash -= spend
            fills += 1
        buy_q = []
        # close: mark
        val = 0.0
        for j, p in pos.items():
            if np.isfinite(C[t, j]):
                p[2] = C[t, j]
            val += p[0] * p[2]
        nav[t] = cash + val
        # decisions at the close of t
        for j, (u, e, _) in pos.items():
            if j in sell_q:
                continue
            held = t - e + 1
            rk = RK[t, j]
            if held >= rule.hold and (rule.keep_rank is None or rk > rule.keep_rank):
                sell_q.add(j)
            elif rule.early_bottom and held < rule.hold and rk > NS[t] / 2:
                sell_q.add(j)
        free = SLOTS - (len(pos) - len(sell_q))
        buy_cap = max(0, min(rule.per_day, free))
        if buy_cap:
            order = np.argsort(RK[t])[:OFFER]
            buy_q = [int(j) for j in order if np.isfinite(RK[t, j]) and int(j) not in pos]
    daily = np.r_[0.0, nav[1:] / nav[:-1] - 1]
    return {"nav": nav, "daily": daily, "sells": sells, "held": float(np.mean(held_days)) if held_days else None}


def stats(daily: np.ndarray, bench: np.ndarray, mask: np.ndarray) -> dict:
    d, b = daily[mask], bench[mask]
    n = int(mask.sum())
    ann = float(np.exp(np.log1p(d).sum() * 252 / n) - 1)
    ex = float(np.exp((np.log1p(d) - np.log1p(b)).sum() * 252 / n) - 1)
    curve = np.cumprod(1 + d)
    mdd = float((1 - curve / np.maximum.accumulate(curve)).max())
    return {"days": n, "ann": round(ann * 100, 2), "excess_ann": round(ex * 100, 2), "mdd": round(mdd * 100, 2),
            "sharpe": round(float(d.mean() / d.std() * np.sqrt(252)), 2) if d.std() else None}


def main() -> None:
    m = matrices()
    dates, bench = m[0], m[-1]
    da = np.array(dates, dtype="datetime64[D]")
    masks = {"ALL": np.ones(len(dates), bool), "PRE_DEV": da < np.datetime64(SPLIT), "DEV": da >= np.datetime64(SPLIT)}
    masks["ALL"][0] = masks["PRE_DEV"][0] = False
    years = len(dates) / 252
    res = {}
    for cost in (0.30, 0.45, 0.0):
        for r in RULES:
            s = simulate(r, cost, m)
            res[f"{r.name}@{cost}"] = {"turnover_per_slot_yr": round(s["sells"] / SLOTS / years, 1), "avg_held": s["held"],
                                       **{k: stats(s["daily"], bench, mk) for k, mk in masks.items()}}
            print(r.name, cost, res[f"{r.name}@{cost}"], flush=True)
    # consistency: the lab engine's 5-day book (its own costs: 0.25% round trip) vs R0 at 0.25%
    eq = H.equity(pl.read_parquet(H.LAB / "fusion_static40_safe.parquet"), "FORWARD", hold=5, positions=20, offer=40)
    eng = eq["strategy"].to_numpy()
    r0 = simulate(RULES[0], 0.25, m)["nav"]
    res["check_engine_total_pct"] = round(float(eng[-1] / eng[0] - 1) * 100, 2)
    res["check_R0_0.25_total_pct"] = round(float(r0[-1] - 1) * 100, 2)
    print("engine", res["check_engine_total_pct"], "R0@0.25", res["check_R0_0.25_total_pct"])
    base = {p: res["R0@0.3"][p]["excess_ann"] for p in ("PRE_DEV", "DEV")}
    verdict = {}
    for r in ("R2", "R3"):
        v = res[f"{r}@0.3"]
        verdict[r] = {"beats_R0_both": all(v[p]["excess_ann"] > base[p] for p in base),
                      "turnover_ratio": round(v["turnover_per_slot_yr"] / res["R0@0.3"]["turnover_per_slot_yr"], 2)}
        verdict[r]["pass"] = verdict[r]["beats_R0_both"] and verdict[r]["turnover_ratio"] <= 0.6
    res["verdict"] = verdict
    print(json.dumps(verdict, ensure_ascii=False))
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
