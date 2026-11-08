"""E066: two random controls for the frozen main book (pre-registered in docs/research/2026-11-07-batch2-preregistration.md).

A. Selection control: replay the frozen book (gated, as forward_book.py) on the pinned scores, keep every trade's
   entry and exit dates, and swap the stock for a random one that, on the signal day (the session before entry),
   was in the pool (tradable, buyable at the entry open) in the same size x volatility quintile cell; also a
   pool-wide draw. Returns open(entry) -> close(exit) minus a 0.25% round trip for both sides; 1,000 draws.
B. Full-strategy control: 100 random scorers (uniform random scores over the same pool rows) run through the same
   rule: top 40, 4 a day, hold 5, 20 slots, the equity-curve gate on their own ungated NAV, the same engine.
Segments: pre-DEV 2024-08-20 ~ 2026-06-30 and DEV 2026-07-01 ~ 2026-10-30.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe random_controls.py
"""
from __future__ import annotations

import json
import time
from datetime import date

import numpy as np
import polars as pl

import curve_gate as G
import forward_book as F
import harness as H
import sameday_base as B
from harness import CODE, c

PINNED = H.LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
OUT = H.LAB / "random_controls.json"
SPLIT = date(2026, 7, 1)
COST = 0.25 / 100
N_A, N_B = 1000, 100
SEED = 20261108


def book(scores: pl.DataFrame):
    """The frozen rule on a score frame: (trades, gated equity)."""
    raw = H.equity(scores, "FORWARD", hold=F.HOLD, positions=F.SLOTS, offer=F.OFFER)
    w = G.gate(raw, look=F.LOOK, low=F.LOW)
    per_day = max(1, round(F.SLOTS / F.HOLD))
    start, end = H.period_bounds("FORWARD")
    sig = H.gate_signals(H.to_signals(scores, F.OFFER), w, F.OFFER, per_day)
    exits = H.ExitRules(holding_days=F.HOLD, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None)
    sizing = H.SizingRule(method="equal", max_positions=F.SLOTS, max_new_per_day=per_day)
    return H.simulate(H.ctx().panel, sig, exits, sizing, H.ctx().benchmark, start=start, end=end)


def seg_stats(eq: pl.DataFrame) -> dict:
    out = {}
    for name, f in (("PRE_DEV", c("date") < SPLIT), ("DEV", c("date") >= pl.lit(SPLIT) - pl.duration(days=1))):
        e = eq.filter(f) if name == "PRE_DEV" else eq.filter(c("date") >= eq.filter(c("date") < SPLIT)["date"].max())
        nav = e["strategy"].to_numpy()
        out[name] = {"total": float(nav[-1] / nav[0] - 1) * 100, "mdd": float((1 - nav / np.maximum.accumulate(nav)).max()) * 100}
    return out


def part_a(trades, panel: pl.DataFrame) -> dict:
    dates = sorted(panel["date"].unique().to_list())
    prev = {d: dates[i - 1] for i, d in enumerate(dates) if i}
    out = B.add_style(B.outcomes(panel), panel)
    pool = out.filter(c("in_pool") & c("size5").is_not_null() & c("vol5").is_not_null()).select(CODE, "date", "size5", "vol5")
    px = panel.select(CODE, "date", "open", "close")
    o = {(a, b): v for a, b, v in px.select(CODE, "date", "open").iter_rows()}
    cl = {(a, b): v for a, b, v in px.select(CODE, "date", "close").iter_rows()}
    by_day = {d: g for (d,), g in pool.partition_by("date", as_dict=True).items()}
    rng = np.random.default_rng(SEED)
    rows = []
    for t in trades:
        if t.exit_reason == "end":
            continue
        e, x = date.fromisoformat(t.entry_date), date.fromisoformat(t.exit_date)
        s = prev.get(e)
        g = by_day.get(s)
        if g is None or (t.code, e) not in o or (t.code, x) not in cl:
            continue
        me = g.filter(c(CODE) == t.code)
        if me.is_empty():
            continue
        sz, vo = me["size5"][0], me["vol5"][0]
        def rets(cand: pl.DataFrame) -> np.ndarray:
            r = [cl[(k, x)] / o[(k, e)] - 1 - COST for k in cand[CODE].to_list() if (k, e) in o and (k, x) in cl and o[(k, e)] > 0]
            return np.array(r)
        cell, allp = rets(g.filter((c("size5") == sz) & (c("vol5") == vo))), rets(g)
        if cell.size < 5 or allp.size < 50:
            continue
        actual = cl[(t.code, x)] / o[(t.code, e)] - 1 - COST
        rows.append((e, actual, cell[rng.integers(0, cell.size, N_A)], allp[rng.integers(0, allp.size, N_A)], t.return_pct / 100))
    res = {"trades": len(rows)}
    for name, f in (("PRE_DEV", lambda d: d < SPLIT), ("DEV", lambda d: d >= SPLIT)):
        sel = [r for r in rows if f(r[0])]
        act = np.array([r[1] for r in sel])
        cell = np.stack([r[2] for r in sel]).mean(axis=0)
        allp = np.stack([r[3] for r in sel]).mean(axis=0)
        res[name] = {"n": len(sel), "actual_pct": round(act.mean() * 100, 3), "engine_net_pct": round(float(np.mean([r[4] for r in sel])) * 100, 3),
                     "matched_pct": round(cell.mean() * 100, 3), "excess_matched": round((act.mean() - cell.mean()) * 100, 3),
                     "p_matched": round(float(((cell >= act.mean()).sum() + 1) / (N_A + 1)), 4),
                     "pool_pct": round(allp.mean() * 100, 3), "excess_pool": round((act.mean() - allp.mean()) * 100, 3),
                     "p_pool": round(float(((allp >= act.mean()).sum() + 1) / (N_A + 1)), 4)}
    a, b = res["PRE_DEV"], res["DEV"]
    res["selection_contributes"] = bool(a["excess_matched"] > 0 and b["excess_matched"] > 0 and a["p_matched"] < 0.05)
    return res


def part_b(scores: pl.DataFrame, frozen: dict) -> dict:
    rng = np.random.default_rng(SEED + 1)
    rows = []
    t0 = time.perf_counter()
    base = scores.select(CODE, "date")
    for i in range(N_B):
        rnd = base.with_columns(pl.Series("score", rng.random(base.height)))
        _, eq = book(rnd)
        rows.append(seg_stats(eq))
        if i % 10 == 9:
            print(f"[E066B] {i + 1} random strategies {time.perf_counter() - t0:.0f}s", flush=True)
    res = {}
    for seg in ("PRE_DEV", "DEV"):
        tot = np.array([r[seg]["total"] for r in rows])
        mdd = np.array([r[seg]["mdd"] for r in rows])
        res[seg] = {"frozen_total": round(frozen[seg]["total"], 2), "random_median": round(float(np.median(tot)), 2),
                    "random_p05_p95": [round(float(np.percentile(tot, 5)), 2), round(float(np.percentile(tot, 95)), 2)],
                    "frozen_pctile": round(float((tot < frozen[seg]["total"]).mean()) * 100, 1),
                    "frozen_mdd": round(frozen[seg]["mdd"], 2), "random_mdd_median": round(float(np.median(mdd)), 2),
                    "mdd_pctile_lower_is_better": round(float((mdd < frozen[seg]["mdd"]).mean()) * 100, 1)}
    res["strategy_beats_random"] = all(res[s]["frozen_pctile"] >= 95 for s in ("PRE_DEV", "DEV"))
    return res


def main() -> None:
    scores = pl.read_parquet(PINNED)
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    trades, eq = book(scores)
    frozen = seg_stats(eq)
    print("[E066] frozen", frozen, flush=True)
    res = {"frozen": frozen, "A": part_a(trades, panel)}
    print("[E066A]", json.dumps(res["A"], ensure_ascii=False), flush=True)
    res["B"] = part_b(scores, frozen)
    print("[E066B]", json.dumps(res["B"], ensure_ascii=False), flush=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
