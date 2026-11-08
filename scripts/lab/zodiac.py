"""Zodiac name-pun speculation (生肖谐音炒作) before the Chinese New Year, main board (lab venv, polars only).

A-share retail money likes stocks whose names carry the coming year's animal or a homophone (龙年 -> 龙/隆,
马年 -> 马/码). The data holds three past turns of the year (龙 2024-02-10, 蛇 2025-01-29, 马 2026-02-17) and the
run-up to 羊 (2027-02-06). For each animal's name group (exact character, and homophones) and each calendar month
from five months before its New Year to one month after: equal-weight return of the group minus the main-board
equal-weight market, limit-up days per stock-month against the market's, and group size. The same group in the
other years is the control: a pun effect should show only before its own year.
Names are today's (the panel keeps one name per stock): a stock renamed later would be misplaced, which is rare;
ST is the price-based flag; main board, listed at least 60 days.
Output .work/lab/zodiac.json and printed tables.
"""
from __future__ import annotations

import json
import os
from datetime import date

import polars as pl

LAB = os.path.join(os.path.dirname(__file__), "..", "..", ".work", "lab")
MAIN = ("600", "601", "603", "605", "000", "001", "002", "003")
YEARS = {"龙": date(2024, 2, 10), "蛇": date(2025, 1, 29), "马": date(2026, 2, 17), "羊": date(2027, 2, 6)}
GROUPS = {"龙": ("龙",), "龙谐音": ("隆", "珑", "泷", "陇"), "蛇": ("蛇",), "马": ("马",), "马谐音": ("码", "玛", "蚂"),
          "羊": ("羊",), "羊谐音": ("阳", "洋", "扬", "杨", "央")}


def month_add(d: date, k: int) -> str:
    m = d.month - 1 + k
    return f"{d.year + m // 12}-{m % 12 + 1:02d}"


def main() -> None:
    p = pl.read_parquet(os.path.join(LAB, "panel.parquet"), columns=["code", "date", "name", "close", "prev_close", "is_limit_up", "days_listed", "st_like"])
    p = p.filter(pl.col("code").str.slice(0, 3).is_in(MAIN) & ~pl.col("st_like") & (pl.col("days_listed") >= 60))  # price-based ST
    p = p.with_columns((pl.col("close") / pl.col("prev_close") - 1).alias("r"), pl.col("date").dt.strftime("%Y-%m").alias("month"))
    p = p.filter(pl.col("r").is_finite() & (pl.col("r").abs() < 0.35))
    mkt = p.group_by("date").agg(pl.col("r").mean().alias("m"), pl.col("is_limit_up").mean().alias("zt_m"))
    out = {}
    for g, chars in GROUPS.items():
        pat = "|".join(chars)
        x = p.filter(pl.col("name").str.contains(pat))
        daily = x.group_by("date", "month").agg(pl.col("r").mean().alias("g"), pl.col("is_limit_up").mean().alias("zt_g"),
                                                pl.col("code").n_unique().alias("n")).join(mkt, on="date")
        mon = daily.group_by("month").agg(
            (((1 + pl.col("g")).product() / (1 + pl.col("m")).product() - 1) * 100).alias("ex"),
            (pl.col("zt_g").mean() / pl.col("zt_m").mean()).alias("zt_ratio"), pl.col("n").max().alias("n")).sort("month")
        out[g] = {r["month"]: r for r in mon.to_dicts()}
    res = {}
    print("monthly excess % of each name group vs the main-board equal-weight market (limit-up rate ratio; stocks)")
    for z, ny in YEARS.items():
        months = [month_add(ny, k) for k in range(-5, 2)]
        print(f"\n{z}年 (New Year {ny}): months {months[0]}..{months[-1]}")
        print("  group     " + " ".join(f"{m:>16s}" for m in months))
        res[z] = {}
        for g in GROUPS:
            cells = [out[g].get(m) for m in months]
            res[z][g] = {m: c for m, c in zip(months, cells) if c}
            mark = " <-" if g.startswith(z) else ""
            print(f"  {g:6s}{mark:3s} " + " ".join(f"{c['ex']:+6.1f} ({c['zt_ratio']:.1f};{c['n']:3d})" if c else " " * 16 for c in cells))
    json.dump({"years": {k: v.isoformat() for k, v in YEARS.items()}, "groups": GROUPS, "table": res},
              open(os.path.join(LAB, "zodiac.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
