"""Forward experiments on the frozen score: fusion monitor (E068), X20 basket (E085) and OS20 shock rebound (E081).

Why: these three are pre-registered forward records of the old PC pipeline (docs/research 2026-11-07-batch2, 2026-11-08-batch4 and
2026-11-08-os20 pre-registrations). Their rules are reproduced here unchanged; a change of a rule is a new experiment.
Common definitions (``outcomes``): the pool of a decision day ``D`` is the tradable main board whose D+1 bar exists and does not
open sealed at the limit-up; a return buys at the D+1 open and sells at the close ``h`` sessions after ``D`` (the stock's own rows),
percent, before costs. ``add_style`` puts every pool name into a size quintile (float cap, point in time) and a volatility quintile
(20-day std of the daily change); the matched baseline of a name is the mean return of its day's size x volatility cell (the
size cell alone when the cell has fewer than 5 names).
* monitor: rank IC, top minus bottom decile and the top decile's matched excess of the frozen score for each decision date whose 5-day
  label is complete; a date whose label window holds a session that failed the coverage gate (< 95% of yesterday's main-board names
  have a bar) is flagged and kept out of the statistics until the gap is filled.
* X20: every 10 decision days hold every pool name with a score except the lowest 20% (score percentile > 0.2), equal weight,
  against the equal-weight pool; the rebalance costs (sold + bought weight) x 0.126%.
* OS20: the bottom 10% of the industry-adjusted 20-day return of the pool, split by a shock (an open <= -5% below the previous close
  or a limit-down close in the last 20 rows); outcome buy at D+1 open, sell at D+10 close, excess over the matched cell.
Invariants: a decision date's inputs are the frozen scores (as first computed when archived); labels only use bars after the decision.
Not covered: loading data and writing logs (``application.bf_fusion_service``).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np
import polars as pl

from .panel import retradable

MIN_CELL = 5
HOLDS = (5, 10, 20)
COVERAGE_REFUSE = 0.95
MONITOR_START = date(2026, 10, 30)
MONITOR_PANEL_FROM = date(2026, 6, 1)
X20_START, X20_PANEL_FROM, X20_EVERY, X20_SIDE = date(2026, 11, 2), date(2026, 9, 1), 10, 0.00126
OS20_START, OS20_PANEL_FROM, OS20_HOLD = date(2026, 11, 2), date(2026, 6, 1), 10
OS20_GROUPS = ("OS20_GAP", "OS20_NOGAP", "OS5_GAP")
MIN_IND = 5
BLOCK, N_BOOT, OS20_SEED = 10, 2000, 20261108
OS20_COST = 0.25

c = pl.col


def retradable_frame(long: pl.DataFrame, since: date) -> pl.DataFrame:
    """The panel cut at ``since`` with ``st_like`` / ``tradable`` recomputed inside the cut, as the forward logs were frozen."""
    return retradable(long.filter(c("date") >= since))


def outcomes(panel: pl.DataFrame) -> pl.DataFrame:
    """(code, date, in_pool, r5, r10, r20) of a panel frame (its rows define the stock's own shifts and rolling windows)."""
    o1 = c("open").shift(-1).over("code")
    cols = [
        c("tradable").fill_null(False).alias("_ok"),
        o1.alias("o1"),
        c("open_at_limit").shift(-1).over("code").fill_null(True).alias("_blocked"),
    ] + [((c("close").shift(-h).over("code") / o1 - 1) * 100).alias(f"r{h}") for h in HOLDS]
    out = panel.lazy().sort("code", "date").with_columns(cols).with_columns(
        (c("_ok") & ~c("_blocked") & c("o1").is_not_null() & (c("o1") > 0)).alias("in_pool"))
    return out.select("code", "date", "in_pool", *[f"r{h}" for h in HOLDS]).collect()


def add_style(out: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """Size and volatility quintiles (1..5) of the pool names per day, and the industry (NA when unknown)."""
    st = panel.select("code", "date", "industry", c("float_cap").alias("_cap"), c("volatility_20").alias("_vol"))
    j = out.join(st, on=["code", "date"], how="left")

    def q(x: str) -> pl.Expr:
        return pl.when(c("in_pool") & c(x).is_not_null()).then(
            ((c(x).rank() / c(x).count()).over("date", "in_pool") * 5).ceil().clip(1, 5)).otherwise(None)

    return j.with_columns(q("_cap").alias("size5"), q("_vol").alias("vol5"), c("industry").fill_null("NA")).drop("_cap", "_vol")


def matched_cells(out: pl.DataFrame, h: int) -> pl.DataFrame:
    """Pool rows with a finite ``r{h}`` and their baseline ``m_sv`` (cell mean; size mean when the cell is small), ``m_all``."""
    r = f"r{h}"
    p = out.filter(c("in_pool") & c(r).is_finite() & c("size5").is_not_null() & c("vol5").is_not_null())
    p = p.with_columns(c(r).mean().over("date").alias("m_all"), c(r).mean().over("date", "size5").alias("m_s"),
                       c(r).mean().over("date", "size5", "vol5").alias("m_sv0"), pl.len().over("date", "size5", "vol5").alias("n_sv"))
    return p.with_columns(pl.when(c("n_sv") >= MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv"))


# ---------------------------------------------------------------------------------------------------------------
# coverage gate and log semantics
# ---------------------------------------------------------------------------------------------------------------
def coverage_status(by_day: Mapping[date, set[str]]) -> dict[date, tuple[float, str]]:
    """Per session after the first: ``(coverage, status)`` where coverage = share of the previous session's names that have a
    bar and status is ``ok`` (>= 0.98), ``degraded`` (>= 0.95) or ``refuse``."""
    days = sorted(by_day)
    out: dict[date, tuple[float, str]] = {}
    for prev, d in zip(days, days[1:]):
        exp = by_day[prev]
        cov = 1.0 - len(exp - by_day[d]) / len(exp) if exp else 0.0
        cov = round(cov, 6)
        out[d] = (cov, "ok" if cov >= 0.98 else "degraded" if cov >= COVERAGE_REFUSE else "refuse")
    return out


def latest_by_date(rows: Sequence[Mapping]) -> dict[str, Mapping]:
    """Latest row per decision date (later rows win)."""
    return {r["date"]: r for r in rows}


def needs_row(prev: Mapping | None, flagged_now: bool) -> bool:
    """A row for a new date, or for a flagged date whose window now passes the gate."""
    return prev is None or (bool(prev["flagged"]) and not flagged_now)


def nw_t(x: np.ndarray, lag: int) -> float | None:
    """Newey-West t of the mean (Bartlett weights); None below 10 observations or without variance."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return None
    d = x - x.mean()
    var = float(d @ d / n)
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * float(d[k:] @ d[:-k]) / n
    return float(x.mean() / math.sqrt(var / n)) if var > 0 else None


def block_boot_p(x: np.ndarray, block: int, n_boot: int, seed: int) -> float | None:
    """Moving-block bootstrap of the mean of a daily series: share of resampled means <= 0 (NaNs dropped first)."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return None
    b = min(block, x.size)
    k = math.ceil(x.size / b)
    starts = np.random.default_rng(seed).integers(0, x.size - b + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)).reshape(n_boot, -1)[:, :x.size]
    return float((x[idx].mean(axis=1) <= 0).mean())


# ---------------------------------------------------------------------------------------------------------------
# E068 monitor
# ---------------------------------------------------------------------------------------------------------------
def monitor_daily(panel: pl.DataFrame, scores: pl.DataFrame, *, start: date = MONITOR_START) -> pl.DataFrame:
    """Per decision date >= ``start`` with a complete label: n, ic, decile_spread, top_ex, top_sv. ``panel`` is the main-board frame
    cut at ``MONITOR_PANEL_FROM`` (the windows start there); ``scores`` has (code, date, score)."""
    out = add_style(outcomes(panel), panel)
    p = matched_cells(out, 5).filter(c("date") >= start)
    j = p.join(scores.select("code", "date", "score"), on=["code", "date"], how="inner").with_columns(
        (c("score").rank() / c("score").count()).over("date").alias("q"))
    return (j.group_by("date").agg(
        pl.corr(c("score").rank(), c("r5").rank()).alias("ic"),
        (c("r5").filter(c("q") > 0.9).mean() - c("r5").filter(c("q") <= 0.1).mean()).alias("decile_spread"),
        (c("r5") - c("m_all")).filter(c("q") > 0.9).mean().alias("top_ex"),
        (c("r5") - c("m_sv")).filter(c("q") > 0.9).mean().alias("top_sv"), pl.len().alias("n")).sort("date"))


def monitor_new_rows(daily: pl.DataFrame, days: Sequence[date], cov: Mapping[date, tuple[float, str]],
                     latest: Mapping[str, Mapping]) -> list[dict]:
    """Rows to append: new dates, and flagged dates whose label window now passes the coverage gate."""
    pos = {d: i for i, d in enumerate(days)}
    rows = []
    for r in daily.iter_rows(named=True):
        d = r["date"]
        window = days[pos[d] + 1: pos[d] + 6]
        bad = [w.isoformat() for w in window if cov.get(w, (1.0, "ok"))[1] == "refuse"]
        prev = latest.get(d.isoformat())
        if not needs_row(prev, bool(bad)):
            continue
        row = {"date": d.isoformat(), "n": r["n"], "ic": round(r["ic"], 4), "decile_spread": round(r["decile_spread"], 3),
               "top_ex": round(r["top_ex"], 3), "top_sv": round(r["top_sv"], 3), "flagged": bool(bad), "coverage_fail_days": bad}
        if prev is not None:
            row["replaces_flagged"] = True
        rows.append(row)
    return rows


def monitor_summary(rows: Sequence[Mapping], check_at: int = 100) -> dict:
    ok = [r for r in rows if not r["flagged"]]
    ic = np.array([r["ic"] for r in ok])
    return {"decision_dates": len(rows), "unflagged": len(ok), "mean_ic": round(float(ic.mean()), 4) if ic.size else None,
            "nw_t": round(float(nw_t(ic, 5) or 0), 2) if ic.size > 8 else None,
            "mean_decile_spread": round(float(np.mean([r["decile_spread"] for r in ok])), 3) if ok else None,
            "mean_top_sv": round(float(np.mean([r["top_sv"] for r in ok])), 3) if ok else None,
            "formal_check_at": check_at, "remaining": max(0, check_at - len(ok))}


# ---------------------------------------------------------------------------------------------------------------
# E085 X20 basket
# ---------------------------------------------------------------------------------------------------------------
def x20_path(panel: pl.DataFrame, pool: pl.DataFrame) -> list[dict]:
    """Daily values of the X20 basket and the equal-weight pool from the first trade day. ``pool`` has (code, date, q): q = score
    percentile of the pool names with a score; the days are the ones of ``panel``."""
    days = sorted(panel["date"].unique().to_list())
    nxt = dict(zip(days, days[1:]))
    decide = sorted(pool["date"].unique().to_list())[::X20_EVERY]
    trade = [nxt[d] for d in decide if d in nxt]
    if not trade:
        return []
    px = panel.select("code", "date", "open", "close")
    opens = {d: dict(zip(g["code"].to_list(), g["open"].to_list()))
             for (d,), g in px.filter(c("date").is_in(trade)).partition_by("date", as_dict=True).items()}
    closes = {d: dict(zip(g["code"].to_list(), g["close"].to_list())) for (d,), g in px.partition_by("date", as_dict=True).items()}
    ranks = {d: dict(zip(g["code"].to_list(), g["q"].to_list())) for (d,), g in pool.partition_by("date", as_dict=True).items()}
    held: dict[str, set[str]] = {"X20": set(), "EW": set()}
    base = {"X20": 1.0, "EW": 1.0}
    held_px: dict[str, dict[str, float]] = {"X20": {}, "EW": {}}
    last_close: dict[str, float] = {}
    rows, k = [], 0
    for d in [x for x in days if x >= trade[0]]:
        turn: dict[str, float] = {}
        if k < len(trade) and d == trade[k]:
            q = ranks[decide[k]]
            o = opens.get(d, {})
            for r in held:
                if held[r]:   # close the old period at today's open (the last close when there is no open)
                    base[r] *= float(np.mean([(o.get(x) or last_close.get(x, 0)) / held_px[r][x] for x in held[r] if held_px[r].get(x)]))
                new = {x for x, v in q.items() if (v > 0.2 if r == "X20" else True) and o.get(x)}
                cost = rebalance_cost(held[r], new, X20_SIDE)
                base[r] *= 1 - cost
                turn[r] = round(cost / X20_SIDE / 2, 4)
                held[r] = new
            held_px = {r: {x: opens[d][x] for x in held[r]} for r in held}
            k += 1
        last_close.update(closes.get(d, {}))
        val = {r: base[r] * float(np.mean([last_close.get(x, held_px[r][x]) / held_px[r][x] for x in held[r]])) if held[r] else base[r]
               for r in held}
        rows.append({"date": d.isoformat(), "x20": round(val["X20"], 6), "ew": round(val["EW"], 6),
                     "excess_pct": round((val["X20"] / val["EW"] - 1) * 100, 3), "n_x20": len(held["X20"]), "n_ew": len(held["EW"]),
                     **({"trade_day": True, "turnover_x20": turn.get("X20"), "turnover_ew": turn.get("EW")} if turn else {})})
    return rows


def rebalance_cost(old: set[str], new: set[str], side_cost: float) -> float:
    """Equal-weight sets, drift ignored: (weight sold + weight bought) x side cost, as a fraction."""
    if not new:
        return 0.0
    sold = len(old - new) / len(old) if old else 0.0
    bought = len(new - old) / len(new)
    return (sold + bought) * side_cost


def x20_pool(panel: pl.DataFrame, scores: pl.DataFrame) -> pl.DataFrame:
    """(code, date, q) for the pool names (decision day >= start) that have a score; ``panel`` is cut at ``X20_PANEL_FROM``."""
    out = outcomes(panel).filter(c("in_pool") & (c("date") >= X20_START))
    pool = out.join(scores.select("code", "date", "score"), on=["code", "date"], how="inner").with_columns(
        (c("score").rank() / c("score").count()).over("date").alias("q"))
    return pool.select("code", "date", "q").sort("date", "code")


# ---------------------------------------------------------------------------------------------------------------
# E081 OS20
# ---------------------------------------------------------------------------------------------------------------
def adj_quantile(panel: pl.DataFrame, pop: pl.DataFrame, n: int) -> pl.DataFrame:
    """(code, date, q): q = rank / count of the industry-adjusted ``n``-row return of the pool inside the day (the industry mean
    when the industry has >= 5 pool names that day, else the pool mean, is subtracted). q <= 0.1 is the bottom decile."""
    p = panel.sort("code", "date").with_columns((c("close") / c("close").shift(n).over("code") - 1).alias("ret"))
    j = pop.select("code", "date", "industry").join(p.select("code", "date", "ret"), on=["code", "date"])
    j = j.filter(c("ret").is_finite())
    j = j.with_columns(c("ret").mean().over("date", "industry").alias("_mi"), pl.len().over("date", "industry").alias("_ni"),
                       c("ret").mean().over("date").alias("_mp"))
    j = j.with_columns((c("ret") - pl.when(c("_ni") >= MIN_IND).then(c("_mi")).otherwise(c("_mp"))).alias("adj"))
    return j.with_columns((c("adj").rank() / c("adj").count()).over("date").alias("q")).select("code", "date", "q")


def os_flags(panel: pl.DataFrame, out: pl.DataFrame, n: int) -> pl.DataFrame:
    """(code, date, os, gap) of the pool names in the bottom or top decile of the adjusted ``n``-row return."""
    p = panel.sort("code", "date").with_columns(
        ((c("open") / c("close").shift(1).over("code") - 1 <= -0.05) | (c("pct_chg") <= -9.5)).fill_null(False).alias("_g"))
    p = p.with_columns(c("_g").cast(pl.Int8).rolling_max(n).over("code").fill_null(0).cast(pl.Boolean).alias("gap"))
    q = adj_quantile(panel, out.filter(c("in_pool")).select("code", "date", "industry"), n)
    j = q.join(p.select("code", "date", "gap"), on=["code", "date"]).filter((c("q") <= 0.1) | (c("q") > 0.9))
    return j.with_columns((c("q") <= 0.1).alias("os")).select("code", "date", "os", "gap")


def os20_signal_lists(panel: pl.DataFrame, out: pl.DataFrame) -> pl.DataFrame:
    """(code, date, group) for OS20_GAP, OS20_NOGAP and OS5_GAP."""
    f20 = os_flags(panel, out, 20).filter(c("os"))
    f5 = os_flags(panel, out, 5).filter(c("os") & c("gap"))
    return pl.concat([
        f20.filter(c("gap")).select("code", "date", pl.lit("OS20_GAP").alias("group")),
        f20.filter(~c("gap")).select("code", "date", pl.lit("OS20_NOGAP").alias("group")),
        f5.select("code", "date", pl.lit("OS5_GAP").alias("group"))])


def os20_daily_excess(sig: pl.DataFrame, out: pl.DataFrame) -> pl.DataFrame:
    """Per (date, group): n, mean matched excess ``ex`` and mean raw return ``raw`` (percent, before costs)."""
    cells = matched_cells(out, OS20_HOLD).select("code", "date", f"r{OS20_HOLD}", "m_sv")
    r = f"r{OS20_HOLD}"
    j = sig.join(cells, on=["code", "date"], how="inner")
    return j.group_by("date", "group").agg(pl.len().alias("n"), (c(r) - c("m_sv")).mean().alias("ex"), c(r).mean().alias("raw")).sort("date", "group")


def os20_wide(d: pl.DataFrame) -> pl.DataFrame:
    return d.pivot(on="group", index="date", values=["n", "ex", "raw"]).sort("date")


def market20(out: pl.DataFrame, panel: pl.DataFrame) -> dict[date, float]:
    """Mean 20-row return (percent) of the pool on each date: the market state of hypothesis H4."""
    p = panel.sort("code", "date").select("code", "date", ((c("close") / c("close").shift(20).over("code") - 1) * 100).alias("m"))
    j = out.filter(c("in_pool")).select("code", "date").join(p, on=["code", "date"]).filter(c("m").is_finite())
    return dict(j.group_by("date").agg(c("m").mean()).iter_rows())


def os20_row(r: Mapping, mkt: Mapping[date, float]) -> dict:
    m = mkt.get(r["date"])
    row = {"date": r["date"].isoformat(), "mkt20": round(float(m), 3) if m is not None else None}
    for g in OS20_GROUPS:
        row[f"n_{g}"] = int(r.get(f"n_{g}") or 0)
        for k in ("ex", "raw"):
            v = r.get(f"{k}_{g}")
            row[f"{k}_{g}"] = round(float(v), 3) if v is not None else None
    return row


def os20_summary(rows: Sequence[Mapping]) -> dict:
    """Equal-weight mean over decision dates with a moving-block bootstrap p (share of resampled means <= 0)."""
    out: dict = {"dates": len(rows)}
    if not rows:
        return out

    def col(key: str) -> np.ndarray:
        return np.array([r.get(key) if r.get(key) is not None else np.nan for r in rows], dtype=float)

    g, ng, raw = col("ex_OS20_GAP"), col("ex_OS20_NOGAP"), col("raw_OS20_GAP")
    out["ex_gap"] = round(float(np.nanmean(g)), 3) if np.isfinite(g).any() else None
    out["p_gap"] = block_boot_p(g, BLOCK, N_BOOT, OS20_SEED)
    out["diff_gap_nogap"] = round(float(np.nanmean(g - ng)), 3) if np.isfinite(g - ng).any() else None
    out["p_diff"] = block_boot_p(g - ng, BLOCK, N_BOOT, OS20_SEED)
    out["net_raw_gap"] = round(float(np.nanmean(raw)) - OS20_COST, 3) if np.isfinite(raw).any() else None
    out["mean_n_gap"] = round(float(np.mean([r.get("n_OS20_GAP") or 0 for r in rows])), 1)
    for name, keep in (("down", lambda m: m < 0), ("up", lambda m: m >= 0)):
        x = [r["ex_OS20_GAP"] for r in rows if r.get("mkt20") is not None and keep(r["mkt20"]) and r.get("ex_OS20_GAP") is not None]
        out[f"ex_gap_{name}"] = {"dates": len(x), "mean": round(float(np.mean(x)), 3) if x else None,
                                 "p": block_boot_p(np.array(x), BLOCK, N_BOOT, OS20_SEED) if x else None}
    return out
