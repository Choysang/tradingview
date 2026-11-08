"""E068: forward monitor of the frozen score (pre-registered in docs/research/2026-11-07-batch2-preregistration.md).

For every decision date from 2026-10-30 whose label is complete (D+1 open -> D+5 close), on the scores as first
computed for that date (.work/lab/live/scores_asof.parquet, written by daily_forward.py; the current
fusion_static40_safe fills dates not archived) and the server snapshot (PULSE_DATA_DIR): rank IC, top
minus bottom decile, top-decile excess over the pool and over the size x volatility cell (E062 baseline).
A decision date whose label window D+1..D+5 holds a session that failed the coverage gate (< 95% of the main-board
codes with a bar the session before) is flagged and kept out of the cumulative statistics.
Appends new dates to .work/lab/live/forward_monitor.jsonl and prints the cumulative mean IC with its Newey-West t
(lag 5). The log is append-only and the latest row per date wins: a flagged date is evaluated again on every run, and
once its window passes the gate (the server backfilled the missing session) a new row is appended with
"replaces_flagged": true; the flagged row stays in the file. The scores stay the as-of ones; only the labels change.
The formal check comes at 100 unflagged decision dates: mean IC > 0 with t >= 2.
Run: cd scripts/lab && PULSE_DATA_DIR=<snapshot> LAB_UNIVERSE=main PYTHONPATH=../.. python forward_monitor.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import harness as H
import sameday_base as B
from backend.app.domain.coverage_gate import decide
from harness import CODE, c

START = date(2026, 10, 30)
LOG = H.BASE / "live" / "forward_monitor.jsonl"
ASOF = H.BASE / "live" / "scores_asof.parquet"
CHECK_AT = 100


def latest_by_date(lines: list[str]) -> dict[str, dict]:
    """Latest logged row per decision date (later lines win)."""
    out: dict[str, dict] = {}
    for line in lines:
        if line.strip():
            row = json.loads(line)
            out[row["date"]] = row
    return out


def needs_row(prev: dict | None, flagged_now: bool) -> bool:
    """Write a row for a new date, or for a flagged date whose window now passes the gate."""
    return prev is None or (bool(prev["flagged"]) and not flagged_now)


def coverage_by_day(panel: pl.DataFrame) -> dict[date, str]:
    days = sorted(panel["date"].unique().to_list())
    by = {d: set(g[CODE].to_list()) for (d,), g in panel.select(CODE, "date").partition_by("date", as_dict=True).items()}
    return {d: decide(by[p], by[d]).status for p, d in zip(days, days[1:])}


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    cov = coverage_by_day(panel)
    out = B.add_style(B.outcomes(panel.filter(c("date") >= date(2026, 6, 1))), panel.filter(c("date") >= date(2026, 6, 1)))
    p = out.filter(c("in_pool") & c("r5").is_finite() & c("size5").is_not_null() & c("vol5").is_not_null() & (c("date") >= START))
    p = p.with_columns(c("r5").mean().over("date").alias("m_all"), c("r5").mean().over("date", "size5").alias("m_s"),
                       c("r5").mean().over("date", "size5", "vol5").alias("m_sv0"), pl.len().over("date", "size5", "vol5").alias("n_sv"))
    p = p.with_columns(pl.when(c("n_sv") >= B.MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv"))
    cur = pl.read_parquet(H.LAB / "fusion_static40_safe.parquet").filter(c("date") >= START).select(CODE, "date", "score")
    if ASOF.exists():  # the scores as first computed for each date (daily_forward.archive_asof); current scores fill the rest
        asof = pl.read_parquet(ASOF).select(CODE, "date", "score")
        s = pl.concat([asof, cur.filter(~c("date").is_in(asof["date"].unique().to_list()))])
    else:
        s = cur
    j = p.join(s, on=[CODE, "date"], how="inner").with_columns((c("score").rank() / c("score").count()).over("date").alias("q"))
    daily = (j.group_by("date").agg(pl.corr(c("score").rank(), c("r5").rank()).alias("ic"),
                                    (c("r5").filter(c("q") > 0.9).mean() - c("r5").filter(c("q") <= 0.1).mean()).alias("decile_spread"),
                                    (c("r5") - c("m_all")).filter(c("q") > 0.9).mean().alias("top_ex"),
                                    (c("r5") - c("m_sv")).filter(c("q") > 0.9).mean().alias("top_sv"), pl.len().alias("n"))
             .sort("date"))
    latest = latest_by_date(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else {}
    pos = {d: i for i, d in enumerate(days)}
    new = 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        for r in daily.iter_rows(named=True):
            d = r["date"]
            window = days[pos[d] + 1: pos[d] + 6]
            bad = [w.isoformat() for w in window if cov.get(w) == "refuse"]
            prev = latest.get(d.isoformat())
            if not needs_row(prev, bool(bad)):
                continue
            row = {"date": d.isoformat(), "n": r["n"], "ic": round(r["ic"], 4), "decile_spread": round(r["decile_spread"], 3),
                   "top_ex": round(r["top_ex"], 3), "top_sv": round(r["top_sv"], 3), "flagged": bool(bad), "coverage_fail_days": bad}
            if prev is not None:
                row["replaces_flagged"] = True
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            new += 1
    rows = sorted(latest_by_date(LOG.read_text(encoding="utf-8").splitlines()).values(), key=lambda x: x["date"])
    ok = [r for r in rows if not r["flagged"]]
    ic = np.array([r["ic"] for r in ok])
    summary = {"decision_dates": len(rows), "unflagged": len(ok), "new": new, "mean_ic": round(float(ic.mean()), 4) if ic.size else None,
               "nw_t": round(float(H.nw_t(ic, 5) or 0), 2) if ic.size > 8 else None,
               "mean_decile_spread": round(float(np.mean([r["decile_spread"] for r in ok])), 3) if ok else None,
               "mean_top_sv": round(float(np.mean([r["top_sv"] for r in ok])), 3) if ok else None,
               "formal_check_at": CHECK_AT, "remaining": max(0, CHECK_AT - len(ok))}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
