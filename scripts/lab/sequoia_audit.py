"""E058: Sequoia-X strategy audit against the same-day, same-pool random baseline (sameday_base.py).

Upstream definitions were read from github.com/sngyai/Sequoia-X (branch master, sequoia_x/strategy/*.py) on
2026-11-07; "_up" is the upstream rule, "_spec" the user's wording where it differs. Main board only, signals at
D's close, bought at the D+1 open. Amount is the panel's amount (1e8 yuan).

Rules
- HTF_up: high40/low40 > 1.6, high10/low10 < 1.15, low10 >= 0.8*high40, volume < 0.6 * mean(volume of the 20 bars
  before today). Windows include today. HTF_fix adds the time lock argmin(low[-40:-10]) < argmax(high[-40:-10]).
- TURTLE_up: close > max(high of the prior 20 bars), amount > 1e8, close > open, close > prev close.
- MAVOL_up: MA5 crosses above MA20 today, volume > 1.5 * 20-day mean volume (incl. today).
- RPS_up: 120-row return ranks >= 90th percentile of the day's pool, close >= 0.9 * max(high, 120 bars incl. today).
  RPS_spec: same rank, close > max(high of the prior 120 bars) (a real new high).
- SHAKE_up: prev close >= 1.095 * prev2 close, close < open, volume > 2 * prev volume, low >= prev close.
  SHAKE_spec: prev sealed limit-up, close < open, volume > prev volume, close >= midpoint of prev bar's body and
  close >= MA5.
- ULD_up: prev MA20 > prev MA60, close <= 0.905 * prev close, volume > 2 * 20-day mean volume (incl. today).
  ULD_spec: MA20 > MA60, closes at the limit-down, volume > 1.5 * 20-day mean volume.
- APRON (停机坪, not in the upstream repo; own definition): limit-up on D-3; D-2..D each close >= that limit-up
  close * 0.97, max(high)/min(low) over D-2..D < 1.06, mean volume of D-2..D < the limit-up day's volume.
- MA250 (回踩年线, not upstream; own definition): MA250 above its value 20 bars ago, max close of the prior 60 bars
  >= 1.10 * MA250, today's low <= 1.02 * MA250 and close >= MA250, volume < 0.7 * 20-day mean volume.

Conversions
(a) HTF grid with the time lock: rise {1.45,1.50,1.60} x 10-day range {1.15,1.18,1.20} x level {0.78,0.80}; and
    htf_score = mean of the cross-sectional percentile ranks of log(high40/low40), -log(high10/low10),
    low10/high40 and -log(volume / prior-20 mean), times the time lock (0 when not ordered); daily Rank IC
    against the D+1-open-to-D+h-close return.
(b) Pullback entry for TURTLE_up / MAVOL_up / RPS_up: 3..8 bars after a breakout day B, the first day with
    low <= MA_n * 1.01 and close >= MA_n (n = 10 or 20), low > B's low and volume < min(B's volume * 0.6,
    20-day mean volume); one entry per breakout.
(c) Coverage gate (measurement only): daily main-board coverage = codes with a bar on D / codes with a bar on
    both D-1 and D+1 or on D (a proxy for "listed and not suspended"); days under 98% and 95%.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe sequoia_audit.py
Writes .work/lab/main/sequoia_audit.json and prints markdown tables.
"""
from __future__ import annotations

import json
import time

import numpy as np
import polars as pl
from numpy.lib.stride_tricks import sliding_window_view as win

import harness as H
import sameday_base as B
from harness import CODE, c

OUT = H.LAB / "sequoia_audit.json"


def _time_lock(g_low: np.ndarray, g_high: np.ndarray) -> np.ndarray:
    """argmin(low[-40:-10]) < argmax(high[-40:-10]) for every row with 40 bars of history (else False)."""
    n = len(g_low)
    out = np.zeros(n, dtype=bool)
    if n < 40:
        return out
    lw, hw = win(g_low, 40)[:, :30], win(g_high, 40)[:, :30]
    out[39:] = lw.argmin(axis=1) < hw.argmax(axis=1)
    return out


def features() -> pl.DataFrame:
    k = H.ctx()
    p = k.panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)).sort(CODE, "date")
    lock = np.concatenate([_time_lock(g["low"].to_numpy(), g["high"].to_numpy())
                           for _, g in p.select(CODE, "low", "high").group_by(CODE, maintain_order=True)])
    p = p.with_columns(pl.Series("lock", lock))
    o = lambda e: e.over(CODE)  # noqa: E731
    p = p.with_columns(
        o(c("high").rolling_max(40)).alias("h40"), o(c("low").rolling_min(40)).alias("l40"),
        o(c("high").rolling_max(10)).alias("h10"), o(c("low").rolling_min(10)).alias("l10"),
        o(c("volume").shift(1).rolling_mean(20)).alias("vprev20"),
        o(c("volume").rolling_mean(20)).alias("vma20"),
        o(c("high").shift(1).rolling_max(20)).alias("hprev20"),
        o(c("high").shift(1).rolling_max(120)).alias("hprev120"),
        o(c("high").rolling_max(120, min_samples=60)).alias("h120"),
        o((c("close") / c("close").shift(120) - 1)).alias("ret120"),
        o(c("close").shift(1)).alias("pc1"), o(c("close").shift(2)).alias("pc2"),
        o(c("open").shift(1)).alias("po1"), o(c("volume").shift(1)).alias("pv1"),
        o(c("ma5").shift(1)).alias("pma5"), o(c("ma20").shift(1)).alias("pma20"), o(c("ma60").shift(1)).alias("pma60"),
        o(c("is_limit_up").shift(1)).alias("pzt"),
        o(c("ma250").shift(20)).alias("ma250_20"), o(c("close").shift(1).rolling_max(60)).alias("cprev60"),
        o(c("is_limit_up").shift(3)).alias("zt3"), o(c("close").shift(3)).alias("czt3"), o(c("volume").shift(3)).alias("vzt3"),
        o(c("close").rolling_min(3)).alias("cmin3"), o(c("high").rolling_max(3)).alias("hmax3"),
        o(c("low").rolling_min(3)).alias("lmin3"), o(c("volume").rolling_mean(3)).alias("vmean3"),
    )
    ok = H.tradable().fill_null(False)
    p = p.with_columns(pl.when(ok).then(c("ret120")).otherwise(None).alias("_r120"))
    p = p.with_columns((c("_r120").rank() / c("_r120").count()).over("date").alias("rps"))
    return p


def htf(rise: float = 1.6, tight: float = 1.15, level: float = 0.8, locked: bool = False) -> pl.Expr:
    e = (c("h40") / c("l40") > rise) & (c("h10") / c("l10") < tight) & (c("l10") >= level * c("h40")) & (c("volume") < 0.6 * c("vprev20"))
    return e & c("lock") if locked else e


def rules() -> dict[str, pl.Expr]:
    return {
        "HTF_up": htf(),
        "HTF_fix": htf(locked=True),
        "TURTLE_up": (c("close") > c("hprev20")) & (c("amount") > 1.0) & (c("close") > c("open")) & (c("close") > c("pc1")),
        "MAVOL_up": (c("pma5") < c("pma20")) & (c("ma5") > c("ma20")) & (c("volume") > 1.5 * c("vma20")),
        "RPS_up": (c("rps") >= 0.9) & (c("close") >= 0.9 * c("h120")),
        "RPS_spec": (c("rps") >= 0.9) & (c("close") > c("hprev120")),
        "SHAKE_up": (c("pc1") >= 1.095 * c("pc2")) & (c("close") < c("open")) & (c("volume") > 2 * c("pv1")) & (c("low") >= c("pc1")),
        "SHAKE_spec": c("pzt") & (c("close") < c("open")) & (c("volume") > c("pv1")) & (c("close") >= (c("po1") + c("pc1")) / 2) & (c("close") >= c("ma5")),
        "ULD_up": (c("pma20") > c("pma60")) & (c("close") <= 0.905 * c("pc1")) & (c("volume") > 2 * c("vma20")),
        "ULD_spec": (c("ma20") > c("ma60")) & c("is_limit_down") & (c("volume") > 1.5 * c("vma20")),
        "APRON": c("zt3") & (c("cmin3") >= 0.97 * c("czt3")) & (c("hmax3") / c("lmin3") < 1.06) & (c("vmean3") < c("vzt3")),
        "MA250": (c("ma250") > c("ma250_20")) & (c("cprev60") >= 1.10 * c("ma250")) & (c("low") <= 1.02 * c("ma250"))
                 & (c("close") >= c("ma250")) & (c("volume") < 0.7 * c("vma20")),
    }


def sig(p: pl.DataFrame, e: pl.Expr) -> pl.DataFrame:
    return p.filter(e.fill_null(False)).select(CODE, "date")


def pullbacks(p: pl.DataFrame, brk: pl.Expr, n: int) -> pl.DataFrame:
    """First pullback to MA_n 3..8 bars after each breakout (one entry per breakout)."""
    q = p.select(CODE, "date", "low", "close", "volume", "vma20", c(f"ma{n}").alias("ma"), brk.fill_null(False).alias("brk"))
    cands = []
    for k in range(3, 9):
        sh = lambda x: c(x).shift(k).over(CODE)  # noqa: E731
        cond = (sh("brk") & (c("low") <= 1.01 * c("ma")) & (c("close") >= c("ma")) & (c("low") > sh("low"))
                & (c("volume") < pl.min_horizontal(0.6 * sh("volume"), c("vma20"))))
        cands.append(q.with_columns(c("date").shift(k).over(CODE).alias("bdate"), cond.alias("hit"))
                     .filter(c("hit")).select(CODE, "date", "bdate"))
    allc = pl.concat(cands).sort(CODE, "bdate", "date")
    return allc.unique([CODE, "bdate"], keep="first").select(CODE, "date").unique()


def rank_ic(p: pl.DataFrame, out: pl.DataFrame, col: str) -> dict:
    j = p.select(CODE, "date", col).join(out.filter(c("in_pool")), on=[CODE, "date"])
    res = {}
    for per in ("PRE", "DEV"):
        q = j.filter(c("period") == per)
        for h in B.HOLDS:
            r = f"r{h}"
            # sorted by date: the Newey-West t below needs the series in time order (unsorted until E082)
            d = (q.drop_nulls([col, r]).group_by("date").agg(pl.corr(c(col).rank(), c(r).rank()).alias("ic"), pl.len().alias("n"))
                 .filter(c("n") >= 30).sort("date"))
            ic = d["ic"].drop_nans().to_numpy()
            res[f"{per}_{h}"] = {"ic": round(float(ic.mean()), 4), "t": round(float(H.nw_t(ic, h) or 0), 2), "days": int(ic.size)}
    return res


def coverage(p: pl.DataFrame) -> dict:
    """Bars on D / codes alive on D (a bar on D, or bars on both a day before and a day after D)."""
    dates = sorted(p["date"].unique().to_list())
    span = p.group_by(CODE).agg(c("date").min().alias("a"), c("date").max().alias("b"))
    have = p.group_by("date").len().rename({"len": "got"})
    alive = []
    for d in dates:
        alive.append((d, int(span.filter((c("a") <= d) & (c("b") >= d)).height)))
    cov = have.join(pl.DataFrame(alive, schema=["date", "alive"], orient="row"), on="date").with_columns((c("got") / c("alive")).alias("cov")).sort("date")
    low98, low95 = cov.filter(c("cov") < 0.98), cov.filter(c("cov") < 0.95)
    return {"days": cov.height, "min": round(float(cov["cov"].min()), 4), "median": round(float(cov["cov"].median()), 4),
            "below_98": low98.height, "below_95": low95.height,
            "worst": [(str(d), round(v, 4)) for d, v in cov.sort("cov").head(5).select("date", "cov").iter_rows()]}


def main() -> None:
    t0 = time.perf_counter()
    p = features()
    out = B.outcomes(H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)))
    print(f"[E058] features + outcomes {time.perf_counter() - t0:.0f}s", flush=True)
    res: dict = {"rules": {}, "grid": {}, "pullback": {}, "verdicts": {}}
    R = rules()
    for name, e in R.items():
        rows = B.evaluate(sig(p, e), out, name)
        res["rules"][name] = rows
        res["verdicts"][name] = B.verdict(rows)
        print(f"[E058] {name} {time.perf_counter() - t0:.0f}s", flush=True)
    for rise in (1.45, 1.50, 1.60):
        for tight in (1.15, 1.18, 1.20):
            for lv in (0.78, 0.80):
                key = f"HTF_fix_{rise}_{tight}_{lv}"
                res["grid"][key] = B.evaluate(sig(p, htf(rise, tight, lv, True)), out, key, holds=(20,))
    print(f"[E058] grid {time.perf_counter() - t0:.0f}s", flush=True)
    ok = H.tradable().fill_null(False)
    comps = {"_a": (c("h40") / c("l40")).log(), "_b": -(c("h10") / c("l10")).log(), "_c": c("l10") / c("h40"),
             "_d": -(c("volume") / c("vprev20")).log()}
    ps = p.with_columns(*(pl.when(ok).then(e).otherwise(None).alias(k) for k, e in comps.items()))
    ps = ps.with_columns(*((c(k).rank() / c(k).count()).over("date").alias(k) for k in comps))
    ps = ps.with_columns((pl.mean_horizontal(*comps) * c("lock").cast(pl.Float64)).alias("htf_score"),
                         pl.mean_horizontal(*comps).alias("htf_score_nolock"))
    res["htf_score_ic"] = {"locked": rank_ic(ps, out, "htf_score"), "no_lock": rank_ic(ps, out, "htf_score_nolock")}
    for base in ("TURTLE_up", "MAVOL_up", "RPS_up"):
        for n in (10, 20):
            key = f"{base}_pb{n}"
            res["pullback"][key] = B.evaluate(pullbacks(p, R[base], n), out, key)
            res["verdicts"][key] = B.verdict(res["pullback"][key])
    print(f"[E058] pullbacks {time.perf_counter() - t0:.0f}s", flush=True)
    res["coverage"] = coverage(H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)).select(CODE, "date"))
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    flat = [r for v in res["rules"].values() for r in v]
    print(B.table(flat))
    print(B.table([r for v in res["grid"].values() for r in v], ("rule", "period", "hold", "n", "excess", "p_mc", "win")))
    print(B.table([r for v in res["pullback"].values() for r in v]))
    print(json.dumps({"verdicts": res["verdicts"], "htf_score_ic": res["htf_score_ic"], "coverage": res["coverage"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
