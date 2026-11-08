"""E079: overbought veto on the frozen main book (pre-registered in docs/research/2026-11-08-overbought-veto-preregistration.md).

V0 = the frozen rule (forward_book.py: hold 5, 20 slots, 4 new names a day from the top 40, the engine's fills and
costs) on the pinned fusion scores. V1 = the same after deleting, before the top-40 cut, every (code, D) whose
industry-adjusted 5-day return is in the top 10% of D's tradable main board (batch3_lib.adj_quantile; population =
harness.tradable on D, no D+1 information). Both run without a break from 2024-08-20 to the panel's last day,
ungated (primary) and gated by their own ungated NAV (secondary, the frozen gate rule).
H1: daily return difference V1 - V0 (ungated): PRE mean > 0 with moving-block bootstrap p < 0.05, DEV mean >= 0.
H2: the frozen book's buys whose decision day D is vetoed have a lower matched 5-day excess (ex_sv) than the
    kept buys, in PRE and in DEV.
H3 (report only): gated V1 vs gated V0 per segment: return difference >= 0 and max drawdown not worse by 1 pp.
Sensitivity (report only): top 5% / 20%, raw 5-day return, 20-day adjusted return, the 10-day-hold shadow rule.
The frozen book and its logs are not touched.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe overbought_veto.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import curve_gate as G
import forward_book as F
import harness as H
import sameday_base as B
from harness import CODE, c

OUT = H.LAB / "overbought_veto.json"
PINNED = H.LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
SEED, BLOCK, N_BOOT = 20261108, 20, 2000
SEGMENTS = (("PRE", date(2024, 8, 20), H.PRE_END), ("DEV", H.DEV_START, H.DEV_END))
MDD_SLACK = 1.0  # H3: percentage points
VARIANTS: tuple[tuple[str, int, float, bool], ...] = (  # (name, n-day return, top share vetoed, industry-adjusted)
    ("V1", 5, 0.10, True),
    ("S_top05", 5, 0.05, True),
    ("S_top20", 5, 0.20, True),
    ("S_raw10", 5, 0.10, False),
    ("S_20d10", 20, 0.10, True),
)


def veto_set(panel: pl.DataFrame, n: int, top: float, adjust: bool) -> pl.DataFrame:
    pop = panel.filter(H.tradable().fill_null(False)).select(CODE, "date", c("industry").fill_null("NA"))
    return L.adj_quantile(panel, pop, n, adjust=adjust).filter(c("q") > 1 - top).select(CODE, "date")


def run_book(scores: pl.DataFrame, hold: int, w: pl.DataFrame | None = None) -> tuple[list, pl.DataFrame]:
    per_day = max(1, round(F.SLOTS / hold))
    start, end = H.period_bounds("FORWARD")
    exits = H.ExitRules(holding_days=hold, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None)
    sizing = H.SizingRule(method="equal", max_positions=F.SLOTS, max_new_per_day=per_day)
    sig = H.gate_signals(H.to_signals(scores, F.OFFER), w, F.OFFER, per_day)
    return H.simulate(H.ctx().panel, sig, exits, sizing, H.ctx().benchmark, start=start, end=end)


def daily(eq: pl.DataFrame) -> pl.DataFrame:
    return eq.sort("date").select("date", (c("strategy") / c("strategy").shift(1) - 1).alias("r")).drop_nulls()


def in_seg(a: date, b: date) -> pl.Expr:
    return (c("date") >= a) & (c("date") <= b)


def seg_stats(eq: pl.DataFrame) -> dict:
    d, out = daily(eq), {}
    for name, a, b in SEGMENTS:
        r = d.filter(in_seg(a, b))["r"].to_numpy()
        nav = np.r_[1.0, np.cumprod(1 + r)]
        out[name] = {"days": int(r.size), "ret_pct": round(float(nav[-1] - 1) * 100, 2),
                     "mdd_pct": round(float((1 - nav / np.maximum.accumulate(nav)).max()) * 100, 2)}
    return out


def diff(eq1: pl.DataFrame, eq0: pl.DataFrame) -> dict:
    j = daily(eq1).join(daily(eq0), on="date", suffix="_0").sort("date")
    out = {}
    for name, a, b in SEGMENTS:
        x = j.filter(in_seg(a, b)).select(c("r") - c("r_0")).to_series().to_numpy()
        out[name] = {"days": int(x.size), "mean_bp": round(float(x.mean()) * 1e4, 3), "ann_pct": round(float(x.mean()) * 242 * 100, 2),
                     "p": L.block_boot_p(x, BLOCK, N_BOOT, SEED)}
    return out


def buys(trades: list) -> pl.DataFrame:
    """(code, D, entry, ret): every buy of a book with its decision day D = the session before the entry."""
    days = list(H.ctx().dates)
    prev = {d: days[i - 1] for i, d in enumerate(days) if i}
    rows = [{CODE: t.code, "date": prev[date.fromisoformat(t.entry_date)], "entry": t.entry_date, "ret": t.return_pct} for t in trades]
    return pl.DataFrame(rows)


def h2(trades: list, veto: pl.DataFrame, out: pl.DataFrame) -> dict:
    b = buys(trades).join(veto.with_columns(pl.lit(True).alias("vet")), on=[CODE, "date"], how="left").with_columns(c("vet").fill_null(False))
    res: dict = {"bite": {}}
    for name, a, e in SEGMENTS:
        s = b.filter(in_seg(a, e))
        v, k = s.filter(c("vet")), s.filter(~c("vet"))
        res["bite"][name] = {"buys": s.height, "vetoed": v.height, "share": round(v.height / s.height, 4) if s.height else None,
                             "trade_ret_vetoed": round(float(v["ret"].mean()), 3) if v.height else None,
                             "trade_ret_kept": round(float(k["ret"].mean()), 3) if k.height else None}
    res["vetoed"] = B.evaluate_matched(b.filter(c("vet")), out, "vetoed", holds=(5,))
    res["kept"] = B.evaluate_matched(b.filter(~c("vet")), out, "kept", holds=(5,))
    return res


def new_share(t1: list, t0: list) -> float:
    a = {(t.code, t.entry_date) for t in t1}
    z = {(t.code, t.entry_date) for t in t0}
    return round(len(a - z) / len(a), 4) if a else 0.0


def main() -> None:
    panel = H.ctx().panel
    pm = panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    scores = pl.read_parquet(PINNED).select(CODE, "date", "score")
    res: dict = {"scores": PINNED.name, "costs": "engine: commission 2.5 bp per side, stamp 5 bp on sells, slippage 5 bp per side",
                 "bootstrap": {"block": BLOCK, "n": N_BOOT, "seed": SEED}}
    t0, e0 = run_book(scores, 5)
    _, e0g = run_book(scores, 5, G.gate(e0, look=F.LOOK, low=F.LOW))
    res["V0"] = {"trades": len(t0), "ungated": seg_stats(e0), "gated": seg_stats(e0g)}
    print("V0", res["V0"], flush=True)
    out = B.add_style(B.outcomes(pm), pm)
    v1_scores = None
    for name, n, top, adjust in VARIANTS:
        v = veto_set(panel, n, top, adjust)
        s1 = scores.join(v, on=[CODE, "date"], how="anti")
        t1, e1 = run_book(s1, 5)
        row = {"n": n, "top": top, "adjust": adjust, "veto_rows": v.height, "scores_removed": scores.height - s1.height,
               "trades": len(t1), "new_buys_share": new_share(t1, t0), "ungated": seg_stats(e1), "diff": diff(e1, e0)}
        if name == "V1":
            v1_scores = s1
            _, e1g = run_book(s1, 5, G.gate(e1, look=F.LOOK, low=F.LOW))
            row["gated"] = seg_stats(e1g)
            row["gated_diff"] = diff(e1g, e0g)
            row["H2"] = h2(t0, v, out)
        res[name] = row
        print(name, {k: row[k] for k in ("veto_rows", "scores_removed", "new_buys_share", "diff")}, flush=True)
    _, e10 = run_book(scores, 10)
    _, e10v = run_book(v1_scores, 10)
    res["S_h10"] = {"base": seg_stats(e10), "veto": seg_stats(e10v), "diff": diff(e10v, e10)}
    print("S_h10", res["S_h10"], flush=True)

    d = res["V1"]["diff"]
    res["H1"] = bool(d["PRE"]["mean_bp"] > 0 and d["PRE"]["p"] < 0.05 and d["DEV"]["mean_bp"] >= 0)
    m = {k: {r["period"]: r.get("ex_sv") for r in res["V1"]["H2"][k]} for k in ("vetoed", "kept")}
    res["H2"] = all(m["vetoed"].get(p) is not None and m["kept"].get(p) is not None and m["vetoed"][p] < m["kept"][p] for p in ("PRE", "DEV"))
    g1, g0 = res["V1"]["gated"], res["V0"]["gated"]
    res["H3_not_worse"] = all(g1[p]["ret_pct"] >= g0[p]["ret_pct"] and g1[p]["mdd_pct"] - g0[p]["mdd_pct"] < MDD_SLACK for p in ("PRE", "DEV"))
    res["verdict"] = "works" if res["H1"] and res["H2"] else "mixed" if res["H1"] or res["H2"] else "fails"
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print({k: res[k] for k in ("H1", "H2", "H3_not_worse", "verdict")}, m)


if __name__ == "__main__":
    main()
