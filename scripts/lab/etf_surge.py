"""E074: broad-ETF volume surge days, descriptive (pre-registered in docs/research/2026-11-08-batch3-preregistration.md).

Seven broad ETFs (.work/lab/ext/etf_daily.parquet, Sina): surge day = their summed amount / the median of the
previous 20 sessions' sums >= 2.0. For each surge day: the ratio, the 上证 index and main-board equal-weight return
that day, the next 1 / 5 / 20 sessions compounded, large (size quintile 5) minus small (quintile 1) equal-weight over
the next 5 / 20, and the percentile of the next-5 / next-20 market return among all days. Known 汇金 announcement
days are checked for detection.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe etf_surge.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import harness as H
from harness import CODE, c
from overnight_us import market

EXT = H.BASE / "ext"
OUT = H.LAB / "etf_surge.json"
RATIO, WIN = 2.0, 20
KNOWN = (date(2024, 2, 6), date(2025, 4, 7))


def compound(r: np.ndarray, i: int, h: int) -> float | None:
    return float(np.prod(1 + r[i + 1:i + 1 + h]) - 1) if i + h < r.size else None


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    p = market(panel).with_columns((c("close") / c("pc") - 1).alias("r"), H.float_cap_pit().alias("cap"))
    p = p.with_columns(((c("cap").rank() / c("cap").count()).over("date") * 5).ceil().clip(1, 5).alias("size5"))
    ew = p.group_by("date").agg(c("r").mean().alias("ew"), c("r").filter(c("size5") == 5).mean().alias("big"),
                                c("r").filter(c("size5") == 1).mean().alias("small")).sort("date")
    sh = pl.read_parquet(H.BASE / "benchmark_sh.parquet").sort("date") if (H.BASE / "benchmark_sh.parquet").exists() else None
    etf = pl.read_parquet(EXT / "etf_daily.parquet").filter(c("date").is_in(days)).group_by("date").agg(c("amount").sum().alias("amt"),
                                                                                                    pl.len().alias("n_etf")).sort("date")
    etf = etf.with_columns(c("amt").shift(1).rolling_median(WIN).alias("med")).with_columns((c("amt") / c("med")).alias("ratio"))
    j = ew.join(etf, on="date", how="left")
    if sh is not None:
        cols = sh.columns
        close = "close" if "close" in cols else cols[-1]
        j = j.join(sh.select(c("date"), (c(close) / c(close).shift(1) - 1).alias("sh")), on="date", how="left")
    r, big, small = j["ew"].to_numpy(), j["big"].to_numpy(), j["small"].to_numpy()
    shr = j["sh"].to_numpy() if "sh" in j.columns else None
    f5 = np.array([compound(r, i, 5) or np.nan for i in range(r.size)])
    f20 = np.array([compound(r, i, 20) or np.nan for i in range(r.size)])
    rows = []
    for i, row in enumerate(j.iter_rows(named=True)):
        if row["ratio"] is None or row["ratio"] < RATIO or row["n_etf"] < 7:
            continue
        g = lambda arr, h: round(compound(arr, i, h) * 100, 2) if compound(arr, i, h) is not None else None  # noqa: E731
        rows.append({"date": str(row["date"]), "ratio": round(row["ratio"], 2), "ew_day": round(row["ew"] * 100, 2),
                     "sh_day": round(row["sh"] * 100, 2) if shr is not None and row.get("sh") is not None else None,
                     "ew_1": g(r, 1), "ew_5": g(r, 5), "ew_20": g(r, 20),
                     "sh_5": g(shr, 5) if shr is not None else None, "sh_20": g(shr, 20) if shr is not None else None,
                     "big_minus_small_5": round((compound(big, i, 5) - compound(small, i, 5)) * 100, 2) if compound(big, i, 5) is not None else None,
                     "big_minus_small_20": round((compound(big, i, 20) - compound(small, i, 20)) * 100, 2) if compound(big, i, 20) is not None else None,
                     "pct_ew_5": round(float((f5[np.isfinite(f5)] < f5[i]).mean()), 2) if np.isfinite(f5[i]) else None,
                     "pct_ew_20": round(float((f20[np.isfinite(f20)] < f20[i]).mean()), 2) if np.isfinite(f20[i]) else None})
    known = {str(d): next((x for x in rows if x["date"] == str(d)), None) is not None for d in KNOWN}
    ratio_on_known = {str(d): (round(float(j.filter(c("date") == d)["ratio"][0]), 2) if j.filter(c("date") == d).height else None) for d in KNOWN}
    res = {"surge_days": len(rows), "rows": rows, "known_detected": known, "ratio_on_known": ratio_on_known,
           "all_days_mean_ew_5": round(float(np.nanmean(f5)) * 100, 2), "all_days_mean_ew_20": round(float(np.nanmean(f20)) * 100, 2)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
