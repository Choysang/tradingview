"""Same-day, same-pool random baseline for daily signals (E058 evaluation base; reused by E060).

A signal on day D (known at D's close) buys at the D+1 open and sells at the close of D+h (h = 5 / 10 / 20 trading
days after D, counted in the stock's own rows). The pool on D is the lab's tradable main board (harness.tradable:
amount >= 0.5e8, listed >= 120 days, price >= 2, not ST, main-board prefix) whose D+1 bar exists and does not open
sealed at the limit (not buyable). Signals outside the pool are dropped, so both sides face the same filter.

Baseline: for every day t with k_t signals, draw k_t names from that day's pool (with replacement; k_t is far
below the pool size) and average over all draws, N_MC = 1000 times. Report:
  n, signal days, mean return, random mean, excess (mean - random mean), p_mc = share of random draws whose mean
  is at least the signal mean, a day-block bootstrap of the per-day excess (p_boot = share of resampled means <= 0),
  and the three legs (lesson a-share-split-intraday-overnight-legs): D+1 open -> D+1 close, D+1 close -> D+2 open,
  D+2 open -> exit, each also as excess over the pool's same-day mean leg.
Gates: PRE (<= 2026-06-30) triggers < 200 -> may not go live as a standalone daily rule; excess < 0 at zero cost in
both PRE and DEV -> "structural fail".
Costs are a round trip subtracted from every trade: 0 and 0.45%. They do not change the excess (both sides pay
the same), only the levels.
With env LAB_BOOT_BLOCK=1 every bootstrap p also gets a `*_blk` twin (E082, docs/research/2026-11-08-block-bootstrap-
recompute.md): a moving-block bootstrap over all trading days of the segment, block = the hold, because the holding
windows of neighbouring decision days overlap and the day-level resampling understates the variance. The original
fields, their algorithm and the random stream are unchanged.
"""
from __future__ import annotations

import math
import os
from datetime import date

import numpy as np
import polars as pl

import harness as H
from harness import CODE, c

HOLDS = (5, 10, 20)
COSTS = (0.0, 0.45)
N_MC, N_BOOT = 1000, 2000
MIN_PRE = 200
PRE_END, DEV_START = H.PRE_END, H.DEV_START
SEED = 20261107
BLOCK_ON = os.environ.get("LAB_BOOT_BLOCK") == "1"


def period() -> pl.Expr:
    return pl.when(c("date") <= PRE_END).then(pl.lit("PRE")).when(c("date") >= DEV_START).then(pl.lit("DEV")).otherwise(None)


def outcomes(panel: pl.DataFrame) -> pl.DataFrame:
    """Per (code, date): in_pool and the returns of buying at the D+1 open (percent, before costs)."""
    o1 = c("open").shift(-1).over(CODE)
    cols = [
        H.tradable().fill_null(False).alias("_ok"),
        o1.alias("o1"),
        c("open_at_limit").shift(-1).over(CODE).fill_null(True).alias("_blocked"),
        ((c("close").shift(-1).over(CODE) / o1 - 1) * 100).alias("leg_intraday"),
        ((c("open").shift(-2).over(CODE) / c("close").shift(-1).over(CODE) - 1) * 100).alias("leg_overnight"),
    ]
    cols += [((c("close").shift(-h).over(CODE) / o1 - 1) * 100).alias(f"r{h}") for h in HOLDS]
    cols += [((c("close").shift(-h).over(CODE) / c("open").shift(-2).over(CODE) - 1) * 100).alias(f"leg_rest{h}") for h in HOLDS]
    out = panel.lazy().sort(CODE, "date").with_columns(cols).with_columns(
        (c("_ok") & ~c("_blocked") & c("o1").is_not_null() & (c("o1") > 0)).alias("in_pool"))
    keep = [CODE, "date", "in_pool", "leg_intraday", "leg_overnight"] + [f"r{h}" for h in HOLDS] + [f"leg_rest{h}" for h in HOLDS]
    return out.select(keep).with_columns(period().alias("period")).collect()


def _mc(pool_by_day: dict[date, np.ndarray], k_by_day: dict[date, int], rng: np.random.Generator) -> np.ndarray:
    """N_MC random means: each draw takes k_t names from day t's pool for every signal day."""
    total = np.zeros(N_MC)
    n = 0
    for d, k in k_by_day.items():
        pool = pool_by_day.get(d)
        if pool is None or pool.size == 0:
            continue
        idx = rng.integers(0, pool.size, size=(N_MC, k))
        total += pool[idx].sum(axis=1)
        n += k
    return total / max(n, 1)


def evaluate(signals: pl.DataFrame, out: pl.DataFrame, name: str, holds: tuple[int, ...] = HOLDS,
             weights: str | None = None) -> list[dict]:
    """signals: (code, date[, weight]) rows known at D's close. Returns one dict per (period, hold)."""
    s = signals.select(CODE, "date", *([weights] if weights else [])).unique([CODE, "date"])
    j = out.join(s.with_columns(pl.lit(True).alias("_sig")), on=[CODE, "date"], how="left").with_columns(
        c("_sig").fill_null(False))
    pool = j.filter(c("in_pool") & c("period").is_not_null())
    rows = []
    rng = np.random.default_rng(SEED)
    for per in ("PRE", "DEV"):
        p = pool.filter(c("period") == per)
        for h in holds:
            r = f"r{h}"
            ph = p.filter(c(r).is_not_null() & c(r).is_finite())
            sig = ph.filter(c("_sig"))
            if sig.is_empty():
                rows.append({"rule": name, "period": per, "hold": h, "n": 0})
                continue
            day_mean = ph.group_by("date").agg(c(r).mean().alias("_pm"), *(c(x).mean().alias(f"_p_{x}") for x in
                                                ("leg_intraday", "leg_overnight", f"leg_rest{h}")))
            sg = sig.join(day_mean, on="date")
            # sorted: group_by order is not stable, and the random draws follow this order (E082: runs did not repeat)
            k_by_day = dict(sg.group_by("date").len().sort("date").iter_rows())
            pool_by_day = {d: g[r].to_numpy() for (d,), g in ph.select("date", r).partition_by("date", as_dict=True).items()}
            mc = _mc(pool_by_day, k_by_day, rng)
            mean = sg[r].mean()
            if weights:
                w = sg[weights].to_numpy().astype(float)
                wmean = float((sg[r].to_numpy() * w).sum() / w.sum())
                wex = float(((sg[r] - sg["_pm"]).to_numpy() * w).sum() / w.sum())
            ex_day = sg.group_by("date").agg((c(r) - c("_pm")).mean().alias("e"), pl.len().alias("k")).sort("date")
            e, k = ex_day["e"].to_numpy(), ex_day["k"].to_numpy().astype(float)
            bi = rng.integers(0, e.size, size=(N_BOOT, e.size))
            boot = (e[bi] * k[bi]).sum(axis=1) / k[bi].sum(axis=1)
            row = {"rule": name, "period": per, "hold": h, "n": sig.height, "days": int(ex_day.height),
                   "mean": round(mean, 3), "random": round(float(mc.mean()), 3), "excess": round(mean - float(mc.mean()), 3),
                   "p_mc": round(float((mc >= mean).mean()), 4), "p_boot": round(float((boot <= 0).mean()), 4),
                   "win": round(float((sg[r] > 0).mean()), 3), "win_vs_pool": round(float((sg[r] > sg["_pm"]).mean()), 3),
                   "net_0.45": round(mean - 0.45, 3),
                   "leg_intraday": round(sg["leg_intraday"].mean(), 3), "leg_overnight": round(sg["leg_overnight"].mean(), 3),
                   "leg_rest": round(sg[f"leg_rest{h}"].mean(), 3),
                   "ex_intraday": round((sg["leg_intraday"] - sg["_p_leg_intraday"]).mean(), 3),
                   "ex_overnight": round((sg["leg_overnight"] - sg["_p_leg_overnight"]).mean(), 3),
                   "ex_rest": round((sg[f"leg_rest{h}"] - sg[f"_p_leg_rest{h}"]).mean(), 3)}
            if weights:
                row.update({"w_mean": round(wmean, 3), "w_excess": round(wex, 3)})
            if BLOCK_ON:
                pb = _boot_block(ex_day["date"].to_list(), e, k, sorted(ph["date"].unique().to_list()), h, SEED + h)
                row["p_boot_blk"] = round(pb, 4) if pb is not None else None
            rows.append(row)
    return rows


MIN_CELL = 5


def add_style(out: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """E062: per-day quintiles of float cap (point in time) and 20-day volatility inside the pool, plus industry
    (the panel's Sina industry, a current snapshot: slow-moving, slight look-ahead)."""
    st = panel.select(CODE, "date", "industry", H.float_cap_pit().alias("_cap"), c("volatility_20").alias("_vol"))
    j = out.join(st, on=[CODE, "date"], how="left")
    q = lambda x: pl.when(c("in_pool") & c(x).is_not_null()).then(  # noqa: E731
        ((c(x).rank() / c(x).count()).over("date", "in_pool") * 5).ceil().clip(1, 5)).otherwise(None)
    return j.with_columns(q("_cap").alias("size5"), q("_vol").alias("vol5"), c("industry").fill_null("NA")).drop("_cap", "_vol")


def _boot(e: np.ndarray, k: np.ndarray, rng: np.random.Generator) -> float:
    bi = rng.integers(0, e.size, size=(N_BOOT, e.size))
    boot = (e[bi] * k[bi]).sum(axis=1) / k[bi].sum(axis=1)
    return float((boot <= 0).mean())


def _boot_block(sig_days: list[date], e: np.ndarray, k: np.ndarray, all_days: list[date], block: int, seed: int) -> float | None:
    """Moving-block bootstrap of the signal-weighted mean excess over every trading day of the segment (days without
    a signal carry k = 0). Blocks of `block` consecutive trading days are drawn and joined to the segment's length;
    each draw's mean = sum(e * k) / sum(k); draws without a signal are dropped. Returns the share of means <= 0."""
    pos = {d: i for i, d in enumerate(all_days)}
    ek, kk = np.zeros(len(all_days)), np.zeros(len(all_days))
    for d, ei, ki in zip(sig_days, e, k):
        i = pos.get(d)
        if i is not None:
            ek[i] += ei * ki
            kk[i] += ki
    n = len(all_days)
    if n == 0 or kk.sum() == 0:
        return None
    b = min(block, n)
    starts = np.random.default_rng(seed).integers(0, n - b + 1, size=(N_BOOT, math.ceil(n / b)))
    idx = (starts[:, :, None] + np.arange(b)).reshape(N_BOOT, -1)[:, :n]
    num, den = ek[idx].sum(axis=1), kk[idx].sum(axis=1)
    ok = den > 0
    return float((num[ok] / den[ok] <= 0).mean()) if ok.any() else None


def evaluate_matched(signals: pl.DataFrame, out: pl.DataFrame, name: str, holds: tuple[int, ...] = HOLDS) -> list[dict]:
    """Excess over three same-day baselines: the whole pool (ex_all), the same size x volatility quintile cell
    (ex_sv, the main one), and the same size x volatility x industry cell (ex_svi). A cell with fewer than
    MIN_CELL names falls back to the coarser cell (size only, then size x volatility). p_* = day-block bootstrap
    share of resampled mean excess <= 0. `out` must carry add_style's columns."""
    s = signals.select(CODE, "date").unique()
    j = out.join(s.with_columns(pl.lit(True).alias("_sig")), on=[CODE, "date"], how="left").with_columns(c("_sig").fill_null(False))
    pool = j.filter(c("in_pool") & c("period").is_not_null() & c("size5").is_not_null() & c("vol5").is_not_null())
    rng = np.random.default_rng(SEED)
    rows = []
    for per in ("PRE", "DEV"):
        p = pool.filter(c("period") == per)
        for h in holds:
            r = f"r{h}"
            ph = p.filter(c(r).is_not_null() & c(r).is_finite())
            ph = ph.with_columns(
                c(r).mean().over("date").alias("m_all"), c(r).mean().over("date", "size5").alias("m_s"),
                c(r).mean().over("date", "size5", "vol5").alias("m_sv0"), pl.len().over("date", "size5", "vol5").alias("n_sv"),
                c(r).mean().over("date", "size5", "vol5", "industry").alias("m_svi0"), pl.len().over("date", "size5", "vol5", "industry").alias("n_svi"))
            ph = ph.with_columns(pl.when(c("n_sv") >= MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv"))
            ph = ph.with_columns(pl.when(c("n_svi") >= MIN_CELL).then(c("m_svi0")).otherwise(c("m_sv")).alias("m_svi"))
            sg = ph.filter(c("_sig"))
            row = {"rule": name, "period": per, "hold": h, "n": sg.height}
            if sg.height:
                d = sg.group_by("date").agg(*((c(r) - c(m)).mean().alias(k) for k, m in (("all", "m_all"), ("sv", "m_sv"), ("svi", "m_svi"))),
                                            pl.len().alias("k")).sort("date")
                kk = d["k"].to_numpy().astype(float)
                row["days"] = d.height
                for k, m in (("all", "m_all"), ("sv", "m_sv"), ("svi", "m_svi")):
                    row[f"ex_{k}"] = round(float((sg[r] - sg[m]).mean()), 3)
                    row[f"p_{k}"] = round(_boot(d[k].to_numpy(), kk, rng), 4)
                if BLOCK_ON:
                    all_days = sorted(ph["date"].unique().to_list())
                    for k in ("all", "sv", "svi"):
                        pb = _boot_block(d["date"].to_list(), d[k].to_numpy(), kk, all_days, h, SEED + h)
                        row[f"p_{k}_blk"] = round(pb, 4) if pb is not None else None
            rows.append(row)
    return rows


def survives(rows: list[dict], hold: int = 10) -> bool:
    """E062 pre-registered rule: PRE matched excess > 0 with p < 0.05 and DEV matched excess >= 0."""
    by = {r["period"]: r for r in rows if r["hold"] == hold}
    pre, dev = by.get("PRE", {}), by.get("DEV", {})
    return bool(pre.get("n") and pre["ex_sv"] > 0 and pre["p_sv"] < 0.05 and dev.get("n") and dev["ex_sv"] >= 0)


def verdict(rows: list[dict], hold: int = 20) -> dict:
    """Gates at one hold: PRE count and the zero-cost excess in PRE and DEV."""
    by = {r["period"]: r for r in rows if r["hold"] == hold}
    pre, dev = by.get("PRE", {}), by.get("DEV", {})
    n_pre = pre.get("n", 0)
    ex = [x.get("excess") for x in (pre, dev) if x.get("n")]
    structural = bool(ex) and all(e < 0 for e in ex)
    return {"hold": hold, "n_pre": n_pre, "standalone_ok": n_pre >= MIN_PRE and not structural,
            "too_few": n_pre < MIN_PRE, "structural_fail": structural}


def table(rows: list[dict], keys: tuple[str, ...] = ("rule", "period", "hold", "n", "days", "mean", "random", "excess",
                                                      "p_mc", "p_boot", "win", "ex_intraday", "ex_overnight", "ex_rest")) -> str:
    head = "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n"
    return head + "\n".join("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows)
