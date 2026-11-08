"""R7-2: market state v2 (size style, trend, sentiment), used two ways on the main book (backend venv).

State on day D, from data up to D's close only (main board, not ST-like, listed >= 60 days, traded):
* size: SMB20 = 20-day sum of (equal-weight daily return of the smallest 30% by float cap) minus (largest
  30%). Float cap on D = latest float cap x close(D) / latest close. small style when SMB20 > 0.
* trend: 上证 close above its 60-day mean.
* sentiment: 5-day mean of yesterday's limit-ups' return today >= 0.
All thresholds were fixed before the run (0, the 60-day mean, 0); nothing is tuned.

Tests the user asked for (11-01):
(a) routing: on small-style days the day's NEW buys come from the price < 100 and float cap < 100 亿 pool,
    otherwise from the full list. Held positions are never sold for a state change (no switch turnover).
(b) state exposure: w = 1.0 when trend and sentiment are both good, 0.25 when one is bad, 0 when both
    are; alone and combined with the R7-4 equity-curve gate (20 days / 0.25) as min(w_state, w_gate).
Usage: python r7_2_regime.py   (writes .work/lab/main/regime_v2.parquet and r7_2.json)
"""
from __future__ import annotations

import json

import polars as pl

import cap_filter as CF
import curve_gate as G
import harness as H
import style_test as S

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
SHARE, SMB_DAYS, PREM_DAYS, MA_DAYS = 0.3, 20, 5, 60
HOLD, SLOTS, OFFER, LOOK, LOW = 5, 20, 40, 20, 0.25


def regime() -> pl.DataFrame:
    p = pl.read_parquet(H.BASE / "panel.parquet", columns=["code", "date", "close", "float_cap", "pct_chg", "amount",
                                                           "days_listed", "st_like", "is_limit_up"])
    p = p.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX) & ~pl.col("st_like").fill_null(False)
                 & (pl.col("days_listed") >= 60) & (pl.col("amount") > 0))
    last = p.sort("date").group_by("code").agg(pl.col("close").last().alias("last_close"))
    p = p.join(last, on="code").with_columns((pl.col("float_cap") * pl.col("close") / pl.col("last_close")).alias("fcap"))
    p = p.sort("code", "date").with_columns(pl.col("is_limit_up").shift(1).over("code").alias("zt_prev"))
    rank = pl.col("fcap").rank().over("date") / pl.col("fcap").count().over("date")
    day = (p.with_columns(rank.alias("q"))
           .group_by("date").agg(
               small=pl.col("pct_chg").filter(pl.col("q") <= SHARE).mean(),
               big=pl.col("pct_chg").filter(pl.col("q") > 1 - SHARE).mean(),
               premium=pl.col("pct_chg").filter(pl.col("zt_prev").fill_null(False)).mean())
           .sort("date"))
    b = pl.read_parquet(H.BASE / "benchmark_sh.parquet").sort("date").select(
        "date", (pl.col("close") > pl.col("close").rolling_mean(MA_DAYS)).alias("trend_up"))
    day = day.join(b, on="date", how="left").with_columns(
        (pl.col("small") - pl.col("big")).rolling_sum(SMB_DAYS).alias("smb20"),
        pl.col("premium").rolling_mean(PREM_DAYS).alias("prem5"))
    good = pl.col("trend_up").fill_null(False).cast(pl.Int8) + (pl.col("prem5") >= 0).fill_null(False).cast(pl.Int8)
    return day.with_columns(
        (pl.col("smb20") > 0).fill_null(False).alias("small_style"),
        pl.when(good == 2).then(1.0).when(good == 1).then(0.25).otherwise(0.0).alias("w_state"))


def routed(scores: pl.DataFrame, reg: pl.DataFrame) -> pl.DataFrame:
    small_days = reg.filter(pl.col("small_style")).select("date")
    small = CF.funnel(scores).join(small_days, on="date", how="semi")
    full = scores.join(small_days, on="date", how="anti")
    return pl.concat([small, full]).sort("date", "code")


def run(scores: pl.DataFrame, label: str, p: str, w: pl.DataFrame | None) -> dict:
    res = H.evaluate(scores, f"r7_2:{label}:{p}", hold=HOLD, positions=SLOTS, offer=OFFER, periods=(p,), note=label, exposure=w)
    r = res["periods"][p]
    print(f"  {label:22s} total {r['total']:+7.2f}%  mdd {r['mdd']:6.1f}%  sharpe {r['sharpe']:+.2f}  trades {r['trades']}", flush=True)
    return r


def main() -> None:
    reg = regime()
    reg.write_parquet(H.LAB / "regime_v2.parquet")
    month = (reg.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m")).group_by("m")
             .agg(pl.col("small_style").mean().round(2).alias("small"), pl.col("smb20").last().round(1).alias("smb20_end"),
                  pl.col("w_state").mean().round(2).alias("w_state")).sort("m"))
    print("[state] month: share of small-style days, SMB20 at month end (pct points), mean state weight")
    for r in month.iter_rows(named=True):
        print(f"  {r['m']}  small {r['small']:.0%}  smb20 {r['smb20_end']:+6.1f}  w {r['w_state']:.2f}")
    s40 = pl.read_parquet(H.LAB / "fusion_static40_safe.parquet")
    route = routed(s40, reg)
    w_state = reg.select("date", pl.col("w_state").alias("w"))
    out: dict = {"month": month.to_dicts(), "runs": {}}
    for p in S.PERIODS:
        print(f"\n[{p}]", flush=True)
        g_base = G.gate(H.equity(s40, p, hold=HOLD, positions=SLOTS, offer=OFFER), look=LOOK, low=LOW)
        g_route = G.gate(H.equity(route, p, hold=HOLD, positions=SLOTS, offer=OFFER), look=LOOK, low=LOW)
        both = lambda g: g.join(w_state, on="date", how="left", suffix="_s").select(
            "date", pl.min_horizontal("w", pl.col("w_s").fill_null(1.0)).alias("w"))
        variants = {
            "base": (s40, None), "gate": (s40, g_base),
            "route": (route, None), "route+gate": (route, g_route),
            "state": (s40, w_state), "state+gate": (s40, both(g_base)),
            "route+state+gate": (route, both(g_route)),
        }
        for label, (sc, w) in variants.items():
            out["runs"][f"{label}|{p}"] = run(sc, label, p, w)
    (H.LAB / "r7_2.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
