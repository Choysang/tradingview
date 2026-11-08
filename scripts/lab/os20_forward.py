"""E081: forward test of the 20-day shock-type oversold rebound (pre-registered in
docs/research/2026-11-08-os20-shock-rebound-preregistration.md).

Signal on D (E070's definition, cond_reversal.flags with n = 20): bottom 10% of the industry-adjusted 20-day return
inside the pool (sameday_base.in_pool), split by GAP = a session among the stock's last 20 rows that opened <= -5%
below the previous close or closed limit-down (pct_chg <= -9.5). OS20_GAP is the test group, OS20_NOGAP the
comparison; the 5-day OS_GAP is kept as a reference. Outcome: buy at the D+1 open, sell at the D+10 close (r10);
excess over the same day's size x volatility cell (E062 baseline, sameday_base cells).

Forward mode (default; PULSE_DATA_DIR = the server snapshot, run by daily_forward.py after forward_monitor.py):
1. signal lists for every decision date >= START whose D+1 bar exists are frozen once in
   .work/lab/live/os20_signals.parquet (dates already there are never recomputed);
2. every frozen date whose label is complete (D+1 .. D+10 in the panel) is evaluated on the current panel and
   appended to .work/lab/live/os20_forward.jsonl. A date whose window holds a session that failed the coverage gate
   is flagged and evaluated again later (forward_monitor.latest_by_date / needs_row semantics).
The formal check comes at CHECK_AT unflagged decision dates, an interim report (no decision) at INTERIM.
Each row also carries mkt20, the mean 20-day return of the pool on D (H4: down market < 0).
History mode (--history): the same daily series on the lab panel 2023-11 ~ 2026-10 (data E070 already looked at),
for the background section of the pre-registration only.
Run: cd scripts/lab && PULSE_DATA_DIR=<snapshot> LAB_UNIVERSE=main PYTHONPATH=../.. python os20_forward.py [--history]
"""
from __future__ import annotations

import json
import sys
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
import sameday_base as B
from cond_reversal import flags
from forward_monitor import coverage_by_day, latest_by_date, needs_row
from harness import CODE, c

START = H.OOS_START  # 2026-11-02, the first session after the main book was frozen
HOLD = 10
CHECK_AT, INTERIM, H4_AT = 120, 60, 60
BLOCK, N_BOOT, SEED = 10, 2000, 20261108
COST = 0.25  # round trip, percent
SIGNALS = H.BASE / "live" / "os20_signals.parquet"
LOG = H.BASE / "live" / "os20_forward.jsonl"
HIST = H.LAB / "os20_history.json"
GROUPS = ("OS20_GAP", "OS20_NOGAP", "OS5_GAP")


def signal_lists(panel: pl.DataFrame, out: pl.DataFrame, days: list[date]) -> pl.DataFrame:
    """(code, date, group) for the three groups; flags() ranks inside the pool, so D needs its D+1 bar."""
    f20 = flags(panel, out, 20, {}, days).filter(c("os"))
    f5 = flags(panel, out, 5, {}, days).filter(c("os") & c("gap"))
    return pl.concat([
        f20.filter(c("gap")).select(CODE, "date", pl.lit("OS20_GAP").alias("group")),
        f20.filter(~c("gap")).select(CODE, "date", pl.lit("OS20_NOGAP").alias("group")),
        f5.select(CODE, "date", pl.lit("OS5_GAP").alias("group")),
    ])


def cells(out: pl.DataFrame, h: int = HOLD) -> pl.DataFrame:
    """Pool rows with a finite r_h and the size x volatility cell mean (falls back to size when the cell is small)."""
    r = f"r{h}"
    p = out.filter(c("in_pool") & c(r).is_finite() & c("size5").is_not_null() & c("vol5").is_not_null())
    p = p.with_columns(c(r).mean().over("date", "size5").alias("m_s"), c(r).mean().over("date", "size5", "vol5").alias("m_sv0"),
                       pl.len().over("date", "size5", "vol5").alias("n_sv"))
    return p.with_columns(pl.when(c("n_sv") >= B.MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv")).select(CODE, "date", r, "m_sv")


def daily_excess(sig: pl.DataFrame, pool: pl.DataFrame, h: int = HOLD) -> pl.DataFrame:
    """Per (date, group): n, mean matched excess (ex) and mean raw return (raw), percent before costs."""
    r = f"r{h}"
    j = sig.join(pool, on=[CODE, "date"], how="inner")
    return j.group_by("date", "group").agg(pl.len().alias("n"), (c(r) - c("m_sv")).mean().alias("ex"), c(r).mean().alias("raw")).sort("date", "group")


def wide(d: pl.DataFrame) -> pl.DataFrame:
    return d.pivot(on="group", index="date", values=["n", "ex", "raw"]).sort("date")


def summarize(rows: list[dict]) -> dict:
    """Equal-weight mean over decision dates; moving-block bootstrap p (share of resampled means <= 0)."""
    out: dict = {"dates": len(rows)}
    if not rows:
        return out
    g = np.array([r.get("ex_OS20_GAP") if r.get("ex_OS20_GAP") is not None else np.nan for r in rows], dtype=float)
    ng = np.array([r.get("ex_OS20_NOGAP") if r.get("ex_OS20_NOGAP") is not None else np.nan for r in rows], dtype=float)
    raw = np.array([r.get("raw_OS20_GAP") if r.get("raw_OS20_GAP") is not None else np.nan for r in rows], dtype=float)
    out["ex_gap"] = round(float(np.nanmean(g)), 3) if np.isfinite(g).any() else None
    out["p_gap"] = L.block_boot_p(g, BLOCK, N_BOOT, SEED)
    out["diff_gap_nogap"] = round(float(np.nanmean(g - ng)), 3) if np.isfinite(g - ng).any() else None
    out["p_diff"] = L.block_boot_p(g - ng, BLOCK, N_BOOT, SEED)
    out["net_raw_gap"] = round(float(np.nanmean(raw)) - COST, 3) if np.isfinite(raw).any() else None
    out["mean_n_gap"] = round(float(np.mean([r.get("n_OS20_GAP") or 0 for r in rows])), 1)
    # H4: down market (mkt20 < 0) vs up market; checked once H4_AT unflagged down-market dates exist
    for name, keep in (("down", lambda m: m < 0), ("up", lambda m: m >= 0)):
        x = [r["ex_OS20_GAP"] for r in rows if r.get("mkt20") is not None and keep(r["mkt20"]) and r.get("ex_OS20_GAP") is not None]
        out[f"ex_gap_{name}"] = {"dates": len(x), "mean": round(float(np.mean(x)), 3) if x else None,
                                 "p": L.block_boot_p(np.array(x), BLOCK, N_BOOT, SEED) if x else None}
    return out


def market20(out: pl.DataFrame, panel: pl.DataFrame) -> dict[date, float]:
    """Mean 20-day return (percent) of the pool on each date (H4's market state)."""
    p = panel.sort(CODE, "date").select(CODE, "date", ((c("close") / c("close").shift(20).over(CODE) - 1) * 100).alias("m"))
    j = out.filter(c("in_pool")).select(CODE, "date").join(p, on=[CODE, "date"]).filter(c("m").is_finite())
    return dict(j.group_by("date").agg(c("m").mean()).iter_rows())


def row_of(r: dict, mkt: dict[date, float]) -> dict:
    m = mkt.get(r["date"])
    out = {"date": r["date"].isoformat(), "mkt20": round(float(m), 3) if m is not None else None}
    for g in GROUPS:
        out[f"n_{g}"] = int(r.get(f"n_{g}") or 0)
        for k in ("ex", "raw"):
            v = r.get(f"{k}_{g}")
            out[f"{k}_{g}"] = round(float(v), 3) if v is not None else None
    return out


def history() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    out = B.add_style(B.outcomes(panel), panel)
    w = wide(daily_excess(signal_lists(panel, out, days), cells(out)))
    mkt = market20(out, panel)
    res = {}
    for seg, f in (("PRE", c("date") <= H.PRE_END), ("DEV", (c("date") >= H.DEV_START) & (c("date") <= H.DEV_END))):
        rows = [row_of(r, mkt) for r in w.filter(f).iter_rows(named=True)]
        s = summarize(rows)
        g = np.array([r["ex_OS20_GAP"] for r in rows if r["ex_OS20_GAP"] is not None])
        s["sd_daily_ex_gap"] = round(float(g.std(ddof=1)), 3) if g.size > 1 else None
        # E070's day-level bootstrap ignores the overlap of 10-day holds; the block version is the one used forward
        s["p_gap_block1"] = L.block_boot_p(g, 1, N_BOOT, SEED)
        res[seg] = s
        # power on seen data: share of rolling windows (step 20 dates) of INTERIM / CHECK_AT dates with block p < 0.05
        for size in (INTERIM, CHECK_AT):
            if g.size >= size:
                ps = [L.block_boot_p(g[i:i + size], BLOCK, 500, SEED) for i in range(0, g.size - size + 1, 20)]
                s[f"windows_{size}"] = len(ps)
                s[f"share_p_lt_005_{size}"] = round(float(np.mean([p < 0.05 for p in ps])), 2)
    HIST.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))


def forward() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    recent = panel.filter(c("date") >= date(2026, 6, 1))  # 20-day returns and 20-day volatility need a lead-in
    out = B.add_style(B.outcomes(recent), recent)
    old = pl.read_parquet(SIGNALS) if SIGNALS.exists() else pl.DataFrame(schema={CODE: pl.Utf8, "date": pl.Date, "group": pl.Utf8})
    have = set(old["date"].unique().to_list())
    pos = {d: i for i, d in enumerate(days)}
    ready = [d for d in days if d >= START and d not in have and pos[d] + 1 < len(days)]
    if ready:
        new = signal_lists(recent, out, sorted(recent["date"].unique().to_list())).filter(c("date").is_in(ready))
        old = pl.concat([old, new.with_columns(c("date").cast(pl.Date))]).sort("date", "group", CODE)
        SIGNALS.parent.mkdir(parents=True, exist_ok=True)
        old.write_parquet(SIGNALS)
    w = wide(daily_excess(old, cells(out))) if old.height else pl.DataFrame()
    cov = coverage_by_day(panel)
    mkt = market20(out, recent)
    latest = latest_by_date(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else {}
    added = 0
    with LOG.open("a", encoding="utf-8") as fh:
        for r in w.iter_rows(named=True) if w.height else []:
            d = r["date"]
            if pos[d] + HOLD >= len(days):
                continue  # label not complete yet
            bad = [x.isoformat() for x in days[pos[d] + 1: pos[d] + HOLD + 1] if cov.get(x) == "refuse"]
            prev = latest.get(d.isoformat())
            if not needs_row(prev, bool(bad)):
                continue
            row = {**row_of(r, mkt), "flagged": bool(bad), "coverage_fail_days": bad}
            if prev is not None:
                row["replaces_flagged"] = True
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            added += 1
    rows = sorted(latest_by_date(LOG.read_text(encoding="utf-8").splitlines()).values(), key=lambda x: x["date"]) if LOG.exists() else []
    ok = [r for r in rows if not r["flagged"]]
    s = summarize(ok)
    s.update({"signal_dates": len(set(old["date"].to_list())), "new_signal_dates": len(ready), "new_rows": added,
              "interim_at": INTERIM, "check_at": CHECK_AT, "remaining": max(0, CHECK_AT - len(ok)), "h4_at_down_dates": H4_AT})
    print(json.dumps(s, ensure_ascii=False, default=str))


if __name__ == "__main__":
    history() if "--history" in sys.argv else forward()
