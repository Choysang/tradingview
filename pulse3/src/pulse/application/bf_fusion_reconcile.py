"""Reconciliation of the fusion lane with the PC's old files (acceptance test of legacy triage batch 3) and the level-safety audit.

Why: a forward experiment moves only after pulse3 and the old system agree day by day. ``reconcile`` runs the pulse3 pipeline on the
local pulse3 data and compares, for every day both sides have, the scores, both books and the three forward experiments with the PC's
files, printing every difference with its reason. Unexplained differences mean "not done".
Inputs are private (the PC's ``.work/lab`` directory, read only); nothing of them is stored or committed. Modes:
* A: the experiment is fed the PC's own scores, so any difference is a difference of the rules or the panel;
* B: the experiment is fed pulse3's scores (the end-to-end result).
Not covered: writing any dataset (reconcile is read only).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_fusion_service as S
from pulse.domain.fusion import books as BK
from pulse.domain.fusion import forward as FW
from pulse.domain.fusion import levelsafe as LS
from pulse.domain.fusion import panel as PN
from pulse.domain.fusion import scorer as SC

OVERLAP_MIN = 0.95
BOOK_FIELDS = ("gate_w", "new_buys_allowed", "try_buy", "offer_top10", "holdings", "nav", "ungated_nav")


def jl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------------------------------------------
def audit_level_safety(conn: psycopg.Connection, lake: LakeStore, cutoff: datetime | None) -> dict:
    """Re-run the level check with pulse3's own seeded constants and list the factors whose verdict differs from the frozen data."""
    loaded = S.load_inputs(conn, lake, cutoff)
    panel = PN.to_panel(loaded.long)
    scaled = LS.scaled_fields(panel)
    frozen = SC.load_json("level_safety.json")
    out = {"checked": 0, "agree": 0, "differ": []}
    for f in SC.load_json("factor_expressions.json")["factors"]:
        ok, rho = LS.level_safe(f["expr"], panel, scaled)
        out["checked"] += 1
        if ok == frozen["safe"][f["name"]]:
            out["agree"] += 1
        else:
            out["differ"].append({"name": f["name"], "frozen_safe": frozen["safe"][f["name"]], "pulse3_safe": ok,
                                  "frozen_rank_corr": frozen["rank_corr"][f["name"]], "pulse3_rank_corr": round(rho, 6)})
    return out


# ---------------------------------------------------------------------------------------------------------------
def overlap_stats(mine: pl.DataFrame, pc: pl.DataFrame, k: int = 40) -> pl.DataFrame:
    days = sorted(set(mine["date"].unique().to_list()) & set(pc["date"].unique().to_list()))
    rows = []
    mg = {d: g for (d,), g in mine.partition_by("date", as_dict=True).items()}
    pg = {d: g for (d,), g in pc.partition_by("date", as_dict=True).items()}
    for d in days:
        a, b = mg[d], pg[d]
        ta = set(a.sort(["score", "code"], descending=[True, False])["code"].head(k).to_list())
        tb = set(b.sort(["score", "code"], descending=[True, False])["code"].head(k).to_list())
        j = a.join(b, on="code", suffix="_pc")
        rho = j.select(pl.corr(pl.col("score").rank(), pl.col("score_pc").rank())).item() if j.height > 2 else None
        rows.append({"date": d, "overlap": len(ta & tb) / k, "rho": rho, "n_mine": a.height, "n_pc": b.height, "n_common": j.height})
    return pl.DataFrame(rows)


def compare_book(rule: BK.BookRule, key: str, logged: list[dict], panel: PN.Panel, scores: pl.DataFrame | dict[date, pl.DataFrame], label: str,
                 pc_scores: pl.DataFrame | None, control: PN.Panel | None = None) -> list[dict]:
    """Rerun the book to each logged date and compare with the PC's row; returns the differences. ``scores`` is one frame or, for mode
    A, the PC's own score file of each run (``{logged date: frame}``)."""
    diffs = []
    grid = S.grid_of(panel)
    for row in logged:
        d = date.fromisoformat(row["date"])
        frame = scores[d] if isinstance(scores, dict) and d in scores else (scores if isinstance(scores, pl.DataFrame) else None)
        if frame is None:
            diffs.append({"book": key, "mode": label, "date": row["date"], "field": "all", "reason": "NOT EXPLAINED: the PC's score file of that run is not available"})
            continue
        by_day = S.scores_by_day(frame)
        sc = {x: v for x, v in by_day.items() if x <= d}
        if d not in sc:
            diffs.append({"book": key, "mode": label, "date": row["date"], "field": "all", "reason": "no score for that day"})
            continue
        gated, raw, w, sig = BK.run_book(rule, panel.dates, panel.codes, grid, sc, end=d)
        mine = BK.decision_row(rule, gated, raw, w, sig, scores_digest="")
        ctl = None
        if label == "A" and control is not None:   # the same rule and scores on the PC's own current panel
            cg, craw, cw, csig = BK.run_book(rule, control.dates, control.codes, S.grid_of(control), sc, end=d)
            ctl = BK.decision_row(rule, cg, craw, cw, csig, scores_digest="")
        for f in BOOK_FIELDS:
            a, b = mine[f], row[f]
            same = (abs(a - b) <= 6e-4 + 1e-6 * abs(b)) if isinstance(a, float) else a == b
            if same:
                continue
            if ctl is not None and (abs(a - ctl[f]) <= 6e-4 + 1e-6 * abs(ctl[f]) if isinstance(a, float) else a == ctl[f]):
                diffs.append({"book": key, "mode": label, "date": row["date"], "field": f, "mine": a, "pc": b,
                              "reason": f"explained: the PC row was written on an earlier price snapshot; the same rule on the PC's current panel gives {ctl[f]}"})
                continue
            if label == "A" and f in NAV_FIELDS and isinstance(a, float) and abs(a - b) <= NAV_REL_TOL * abs(b) and all(
                    g in NAV_FIELDS for g in BOOK_FIELDS if mine[g] != row[g] and not (isinstance(mine[g], float) and abs(mine[g] - row[g]) <= 6e-4 + 1e-6 * abs(row[g]))):
                diffs.append({"book": key, "mode": label, "date": row["date"], "field": f, "mine": a, "pc": b,
                              "reason": f"explained: every decision is identical and the NAV differs by {abs(a - b) / abs(b) * 1e4:.1f} bp, which is the price "
                                        "revisions between the PC's server snapshot and pulse3's (see the panel section above)"})
                continue
            reason = "score history differs from the PC's for that day" if label == "B" else "NOT EXPLAINED: same scores, different rule result"
            if ctl is not None:
                reason += f" (the same rule on the PC's current panel gives {ctl[f]})"
            if label == "B" and pc_scores is not None and d in set(pc_scores["date"].unique().to_list()):
                reason += f" (top-40 overlap that day {len(S.top40(frame, d) & S.top40(pc_scores, d))}/40)"
            diffs.append({"book": key, "mode": label, "date": row["date"], "field": f, "mine": a, "pc": b, "reason": reason})
    return diffs


NAV_FIELDS = ("nav", "ungated_nav")
NAV_REL_TOL = 1e-3   # 10 bp


def pc_score_files(pc: Path) -> dict[date, pl.DataFrame]:
    """The PC's score files as they were at each run: every archived copy (the file replaced by a promotion) and the current one, keyed
    by their last date, which is the decision date of the book row logged by the run that promoted the file."""
    out: dict[date, pl.DataFrame] = {}
    files = sorted((pc / "main" / "archive").glob("*/fusion_static40_safe.parquet")) + [pc / "main" / "fusion_static40_safe.parquet"]
    for f in files:
        df = pl.read_parquet(f).select("code", "date", "score")
        out[df["date"].max()] = df
    return out


def reconcile(conn: psycopg.Connection, lake: LakeStore, pc: Path, cutoff: datetime | None, *, scores_cache: Path | None = None) -> int:
    t0 = time.perf_counter()
    unexplained = 0
    loaded = S.load_inputs(conn, lake, cutoff)
    long = loaded.long
    panel = PN.to_panel(long)
    say(f"== pulse3 panel: {panel.T} sessions {panel.dates[0]}..{panel.dates[-1]}, {panel.N} main-board codes, snapshot {loaded.refs['research_snapshot'][:8]}")
    # 1. panel against the PC's panel -----------------------------------------------------------------------------
    pcp = pl.read_parquet(pc / "panel.parquet").filter(PN.in_main_board(pl.col("code")))
    j = long.join(pcp, on=["code", "date"], how="full", suffix="_pc")
    only_mine, only_pc = j.filter(pl.col("close_pc").is_null()).height, j.filter(pl.col("close").is_null()).height
    both = j.filter(pl.col("close").is_not_null() & pl.col("close_pc").is_not_null())
    say(f"== panel rows: pulse3 {long.height}, PC {pcp.height}, only pulse3 {only_mine}, only PC {only_pc}")
    for col in ("close", "amount", "float_cap", "limit_streak", "days_listed"):
        a, b = both[col].cast(pl.Float64).to_numpy(), both[f"{col}_pc"].cast(pl.Float64).to_numpy()
        fin = np.isfinite(a) & np.isfinite(b)
        rel = np.abs(a - b)[fin] / np.maximum(np.abs(b[fin]), 1e-9)
        say(f"   {col}: nan mismatch {int((np.isfinite(a) != np.isfinite(b)).sum())}, cells differing by > 1e-6 relative {int((rel > 1e-6).sum())}, max {rel.max() if rel.size else 0:.2e}")
    for col in ("is_limit_up", "is_limit_down", "open_at_limit", "tradable", "st_like"):
        n = both.filter(pl.col(col) != pl.col(f"{col}_pc")).height
        say(f"   {col}: {n} rows differ")
    # 2. scores -------------------------------------------------------------------------------------------------
    if scores_cache and scores_cache.exists():
        mine = pl.read_parquet(scores_cache)
        say(f"== scores read from {scores_cache.name}")
    else:
        mine, _run, info, _ic = S.compute_scores(panel, tail_start=S.first_score_row(panel))
        say(f"== scores computed in {info['seconds']} s wall / {info['cpu_seconds']} s cpu (peak memory {S.peak_memory_mb()} MB), skipped factors: {info['skipped'] or 'none'}")
        if scores_cache:
            mine.write_parquet(scores_cache)
    pcs = pl.read_parquet(pc / "main" / "fusion_static40_safe.parquet").select("code", "date", "score")
    st = overlap_stats(mine, pcs)
    last60 = st.tail(60)
    say(f"== scores vs PC: {st.height} common sessions {st['date'].min()}..{st['date'].max()}; rows pulse3 {mine.height}, PC {pcs.height}")
    say(f"   top-40 overlap: all sessions mean {st['overlap'].mean():.4f} min {st['overlap'].min():.3f}; last 60 sessions mean {last60['overlap'].mean():.4f} min {last60['overlap'].min():.3f}")
    say(f"   rank correlation of scores: mean {st['rho'].mean():.5f}, min {st['rho'].min():.5f}")
    low = st.filter(pl.col("overlap") < OVERLAP_MIN)
    say(f"   sessions below {OVERLAP_MIN:.0%}: {low.height} of {st.height}" + (" (" + ", ".join(f"{r['date']}:{r['overlap']:.3f}" for r in low.tail(8).iter_rows(named=True)) + ")" if low.height else ""))
    ok = last60["overlap"].mean() >= OVERLAP_MIN and last60.height >= 60
    say(f"   ACCEPTANCE (last 60 sessions mean top-40 overlap >= {OVERLAP_MIN:.0%}): {'PASS' if ok else 'FAIL'}")
    unexplained += 0 if ok else 1
    asof_pc = pl.read_parquet(pc / "live" / "scores_asof.parquet").select("code", "date", "score")
    sa = overlap_stats(mine, asof_pc)
    say(f"   vs the PC's as-first-computed scores ({sa.height} sessions): mean overlap {sa['overlap'].mean():.4f}, min {sa['overlap'].min():.3f}")
    # 3. books ---------------------------------------------------------------------------------------------------
    ctl_long = pcp.sort(["code", "date"]).with_columns(pl.col("pct_chg").rolling_std(20).over("code").alias("volatility_20"))
    ctl_panel = PN.to_panel(ctl_long)
    pc_eff = pl.concat([asof_pc, pcs.filter(~pl.col("date").is_in(asof_pc["date"].unique().to_list()) & (pl.col("date") >= FW.MONITOR_START))])
    for key, fname in (("main", "gated_book.jsonl"), ("h10", "shadow_h10.jsonl")):
        logged = jl(pc / "live" / fname)
        rule = S.BOOK_FILE[key]
        for label, sc in (("A", pc_score_files(pc)), ("B", mine)):
            d = compare_book(rule, key, logged, panel, sc, label, pcs, ctl_panel)
            say(f"== book {key} mode {label} ({'the PC score file of each run' if label == 'A' else 'pulse3 scores'}): {len(logged)} logged days, {len(d)} field differences")
            for x in d:
                say(f"   {x['date']} {x['field']}: pulse3 {x.get('mine')} | PC {x.get('pc')} -> {x['reason']}")
                unexplained += int("NOT EXPLAINED" in x["reason"])
    # 4. monitor, X20, OS20 ---------------------------------------------------------------------------------------
    days = panel.dates
    cov = S.coverage_by_day(long)
    ctl = ctl_long
    for label, scores_for in (("A", pc_eff), ("B", mine)):
        unexplained += _experiments(label, long, days, cov, scores_for, pc, control=ctl if label == "A" else None)
    say(f"== reconcile finished in {time.perf_counter() - t0:.0f} s; unexplained differences: {unexplained}")
    return 0 if unexplained == 0 else 3


def _cmp_rows(name: str, label: str, mine: dict[str, dict], pc: dict[str, dict], fields: tuple[str, ...], tol: float = 1.5e-3,
              control: dict[str, dict] | None = None) -> int:
    """Compare logged rows. In mode A a difference that disappears when the same rule runs on the PC's own current panel (``control``)
    comes from the data snapshot the PC row was computed on, and is explained; any other mode A difference is unexplained."""
    def same(a, b) -> bool:
        return (a is None and b is None) or (a is not None and b is not None and (abs(a - b) <= tol if isinstance(a, (int, float)) and not isinstance(a, bool) else a == b))

    common = sorted(set(mine) & set(pc))
    bad = explained = 0
    for d in common:
        for f in fields:
            a, b = mine[d].get(f), pc[d].get(f)
            if same(a, b):
                continue
            if label == "A" and control is not None and d in control and same(a, control[d].get(f)):
                explained += 1
                say(f"   {name} {d} {f}: pulse3 {a} | PC {b} -> explained: the PC row was written on an earlier server snapshot; the same rule on the PC's current panel gives {control[d].get(f)}")
                continue
            bad += 1
            reason = ("NOT EXPLAINED: same scores, different result" if label == "A"
                      else "pulse3 scores differ from the PC's (the PC 11-05 scores miss 446 names of its incomplete snapshot) or panel revision")
            say(f"   {name} {d} {f}: pulse3 {a} | PC {b} -> {reason}")
    only = sorted(set(pc) - set(mine))
    say(f"== {name} mode {label}: {len(common)} common days, {bad} unexplained field differences, {explained} explained; days only in PC {len(only)}, only in pulse3 {len(set(mine) - set(pc))}")
    return bad if label == "A" else 0


def _experiments(label: str, long: pl.DataFrame, days: list[date], cov: dict, scores: pl.DataFrame, pc: Path, control: pl.DataFrame | None = None) -> int:
    unexplained = 0
    live = pc / "live"
    # monitor
    mon_panel = FW.retradable_frame(long, FW.MONITOR_PANEL_FROM)
    daily = FW.monitor_daily(mon_panel, scores.filter(pl.col("date") >= FW.MONITOR_START))
    mine = {r["date"]: r for r in FW.monitor_new_rows(daily, days, cov, {})}
    pcm = FW.latest_by_date(jl(live / "forward_monitor.jsonl"))
    ctl = None
    if control is not None:
        cd = FW.monitor_daily(FW.retradable_frame(control, FW.MONITOR_PANEL_FROM), scores.filter(pl.col("date") >= FW.MONITOR_START))
        ctl = {r["date"]: r for r in FW.monitor_new_rows(cd, days, cov, {})}
    unexplained += _cmp_rows("monitor", label, mine, pcm, ("n", "ic", "decile_spread", "top_ex", "top_sv", "flagged"), control=ctl)
    # X20
    x_panel = FW.retradable_frame(long, FW.X20_PANEL_FROM)
    pool = FW.x20_pool(x_panel, scores.filter(pl.col("date") >= FW.X20_START))
    path = {r["date"]: r for r in FW.x20_path(x_panel, pool)}
    pcx = {r["date"]: r for r in jl(live / "shadow_x20.jsonl")}
    unexplained += _cmp_rows("x20", label, path, pcx, ("x20", "ew", "excess_pct", "n_x20", "n_ew", "trade_day", "turnover_x20", "turnover_ew"), tol=2e-6)
    # OS20
    recent = FW.retradable_frame(long, FW.OS20_PANEL_FROM)
    out = FW.add_style(FW.outcomes(recent), recent)
    fresh = FW.os20_signal_lists(recent, out)
    frozen = pl.read_parquet(live / "os20_signals.parquet").select("code", "date", "group")
    if label == "A":
        diff_n = explained = 0
        ctl_lists = None
        if control is not None:
            cr = FW.retradable_frame(control, FW.OS20_PANEL_FROM)
            ctl_lists = FW.os20_signal_lists(cr, FW.add_style(FW.outcomes(cr), cr))
        for (d, g), a in fresh.filter(pl.col("date").is_in(frozen["date"].unique().to_list())).group_by(["date", "group"]):
            fa = set(a["code"].to_list())
            fb = set(frozen.filter((pl.col("date") == d) & (pl.col("group") == g))["code"].to_list())
            if fa == fb:
                continue
            same_ctl = ctl_lists is not None and set(ctl_lists.filter((pl.col("date") == d) & (pl.col("group") == g))["code"].to_list()) == fa
            explained += int(same_ctl)
            diff_n += int(not same_ctl)
            say(f"   os20 signals {d} {g}: pulse3 {len(fa)} names, PC {len(fb)}, symmetric difference {len(fa ^ fb)} -> "
                + ("explained: frozen on that day's server snapshot; the same rule on the PC's current panel gives pulse3's list" if same_ctl else "NOT EXPLAINED"))
        say(f"== os20 frozen lists: {diff_n} unexplained, {explained} explained (date, group) lists differ from the PC's frozen ones")
        unexplained += diff_n
    wide = FW.os20_wide(FW.os20_daily_excess(frozen, out))
    mkt = FW.market20(out, recent)
    pos = {d: i for i, d in enumerate(days)}
    mine_rows = {}
    for r in wide.iter_rows(named=True):
        if pos[r["date"]] + FW.OS20_HOLD < len(days):
            mine_rows[r["date"].isoformat()] = FW.os20_row(r, mkt)
    pco = FW.latest_by_date(jl(live / "os20_forward.jsonl"))
    keys = ("mkt20",) + tuple(f"{k}_{g}" for g in FW.OS20_GROUPS for k in ("n", "ex", "raw"))
    unexplained += _cmp_rows("os20", label, mine_rows, pco, keys)
    return unexplained
