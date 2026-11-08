"""R7-1: pick the sub-strategy that did best after past days like today, vs "best of the last 30 days" (backend venv).

The user's correction (11-01): a 5~15 day theme makes the trailing-window champion a late buyer. Instead,
find the past days whose market state was closest to today and take the book that did best in the 5 days
after them. Cash is a candidate.

Books (daily returns from the standard engine, hold 5 / 20 slots): main40 (fusion_static40_safe), main40 with
the equity-curve gate (20 d / 0.3), long10 (fusion_long10_exp_safe), main40 with the price < 100 and float
cap < 100 亿 funnel, the 上证 index, cash. Fusion scores start 2024-08-20, so the comparison starts 2024-09.

State on day D, known at its close (main board, claims_check.state): limit-ups, limit-downs, highest board,
promotion rate, yesterday's limit-ups' return today, and the 上证's 5 / 20-day return and distance to its
60-day mean; each z-scored with an expanding mean and std (no future data).
Selector, every 5 trading days at the close of D: neighbours = the K=20 past days s nearest in state with
s + 5 <= D (their outcome is known); take the book with the best mean 5-day return after them; hold it for
the next 5 days. A switch costs 0.3% (sell and buy fees plus slippage). The trailing rule takes the book
with the best last-30-day return on the same schedule. K, 5 and 30 were fixed before the run.
Usage: python analog_select.py   (writes .work/lab/main/analog_select.json)
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import cap_filter as CF
import claims_check as CC
import curve_gate as G
import harness as H
import style_test as S

K, EVERY, TRAIL, SWITCH = 20, 5, 30, 0.003
START = date(2024, 9, 2)


def book_returns() -> pl.DataFrame:
    s40 = pl.read_parquet(H.LAB / "fusion_static40_safe.parquet")
    l10 = pl.read_parquet(H.LAB / "fusion_long10_exp_safe.parquet")
    books = {"main40": (s40, False), "main40_gate": (s40, True), "long10": (l10, False), "funnel40": (CF.funnel(s40), False)}
    cols = []
    for name, (scores, gated) in books.items():
        parts = []
        for p in S.PERIODS:
            eq = H.equity(scores, p, hold=5, positions=20, offer=40)
            if gated:
                eq = H.equity(scores, p, hold=5, positions=20, offer=40, exposure=G.gate(eq))
            parts.append(eq.select("date", (pl.col("strategy") / pl.col("strategy").shift(1) - 1).fill_null(0.0).alias(name)))
        cols.append(pl.concat(parts))
    out = cols[0]
    for c in cols[1:]:
        out = out.join(c, on="date", how="inner")
    idx = pl.read_parquet(H.BASE / "benchmark_sh.parquet").sort("date").select(
        "date", (pl.col("close") / pl.col("close").shift(1) - 1).alias("index"))
    return out.join(idx, on="date", how="left").with_columns(pl.col("index").fill_null(0.0), pl.lit(0.0).alias("cash")).sort("date")


def states() -> pl.DataFrame:
    day, _ = CC.state()
    b = pl.read_parquet(H.BASE / "benchmark_sh.parquet").sort("date").with_columns(
        (pl.col("close") / pl.col("close").shift(5) - 1).alias("idx_r5"),
        (pl.col("close") / pl.col("close").shift(20) - 1).alias("idx_r20"),
        (pl.col("close") / pl.col("close").rolling_mean(60) - 1).alias("idx_ma60"))
    st = day.join(b.select("date", "idx_r5", "idx_r20", "idx_ma60"), on="date", how="left").sort("date")
    feats = ["zt", "dt", "max_streak", "promo", "premium", "idx_r5", "idx_r20", "idx_ma60"]
    z = []
    for f in feats:  # expanding z-score: day D is scaled with data up to D only
        x = pl.col(f).cast(pl.Float64)
        z.append(((x - x.cum_sum() / x.cum_count()) / (x.pow(2).cum_sum() / x.cum_count() - (x.cum_sum() / x.cum_count()).pow(2)).sqrt()).alias(f"z_{f}"))
    return st.with_columns(z).select("date", *[f"z_{f}" for f in feats])


def stats(r: np.ndarray) -> dict:
    nav = np.cumprod(1 + r)
    peak = np.maximum.accumulate(nav)
    sd = r.std(ddof=1)
    return {"total": round(float(nav[-1] - 1) * 100, 2), "mdd": round(float((nav / peak - 1).min()) * 100, 2),
            "sharpe": round(float(r.mean() / sd * np.sqrt(244)), 2) if sd > 0 else None}


def run_selector(R: np.ndarray, Z: np.ndarray, names: list[str], rule: str) -> tuple[np.ndarray, list[str]]:
    n, m = R.shape
    fwd = np.full((n, m), np.nan)  # fwd[s] = compounded return of days s+1..s+5
    for s in range(n - EVERY):
        fwd[s] = np.prod(1 + R[s + 1:s + 1 + EVERY], axis=0) - 1
    out, picks, choice = np.zeros(n), [], None
    for t in range(n):
        if t % EVERY == 0:  # decide at the close of day t-1, i.e. with rows < t
            d = t - 1
            new = None
            if rule == "analog" and d - EVERY >= 60 and np.isfinite(Z[d]).all():
                hist = np.arange(0, d - EVERY + 1)
                ok = hist[np.isfinite(Z[hist]).all(axis=1)]
                if len(ok) >= K:
                    near = ok[np.argsort(((Z[ok] - Z[d]) ** 2).sum(axis=1))[:K]]
                    new = int(np.nanargmax(np.nanmean(fwd[near], axis=0)))
            elif rule == "trail" and d >= TRAIL:
                new = int(np.argmax(np.prod(1 + R[d - TRAIL + 1:d + 1], axis=0)))
            new = names.index("main40") if new is None else new
            cost = SWITCH if choice is not None and new != choice else 0.0
            choice = new
            out[t] -= cost
        out[t] += R[t, choice]
        picks.append(names[choice])
    return out, picks


def main() -> None:
    br = book_returns()
    st = states()
    df = br.join(st, on="date", how="left").filter(pl.col("date") >= START).sort("date")
    names = ["main40", "main40_gate", "long10", "funnel40", "index", "cash"]
    R = df.select(names).to_numpy()
    Z = df.select([c for c in df.columns if c.startswith("z_")]).to_numpy()
    dates = df["date"].to_list()
    runs = {n: R[:, i] for i, n in enumerate(names) if n != "cash"}
    picks = {}
    for rule in ("analog", "trail"):
        runs[f"sel_{rule}"], picks[rule] = run_selector(R, Z, names, rule)
    runs["equal_books"] = R[:, :4].mean(axis=1)
    segs = {"2024-09..2026-06": (date(2024, 9, 1), date(2026, 6, 30)), "SELECT": H.period_bounds("SELECT")}
    if "VERIFY" in S.PERIODS:
        segs["VERIFY"] = H.period_bounds("VERIFY")
    out: dict = {"rows": len(dates), "segments": {}, "picks": {}}
    for seg, (a, b) in segs.items():
        mask = np.array([a <= d <= b for d in dates])
        out["segments"][seg] = {k: stats(v[mask]) for k, v in runs.items()}
        print(f"\n[{seg}] total % / max drawdown % / Sharpe", flush=True)
        for k, v in out["segments"][seg].items():
            print(f"  {k:12s} {v['total']:+7.2f} / {v['mdd']:7.2f} / {v['sharpe']}", flush=True)
        for rule in picks:
            seq = [p for p, m in zip(picks[rule], mask) if m]
            share = {n: round(seq.count(n) / max(1, len(seq)), 2) for n in names}
            out["picks"][f"{rule}|{seg}"] = share
            print(f"  picks {rule}: {share}", flush=True)
    (H.LAB / "analog_select.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
