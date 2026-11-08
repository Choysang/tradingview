"""Calendar effects on the 沪深主板 (design supplement 2026-11-02, E01/E02 of its list): weekday and expiry days.

Daily equal-weight return of tradable main-board stocks (close to close), and the same for the top quintile by
turnover x volatility on the prior day (the high-turnover, high-volatility names the T+1 paper reports as
most affected). Per weekday: mean, share of down days, 10% worst-day mean. Expiry days: the third Friday of
each month (股指期货 last trading day, moved to the next trading day on holidays), against the other Fridays.
Also the open-to-close return of each group, because a T+1 buy at the open can only earn the rest of the day.
t-stats are plain day-level (one row per day). Periods PRE and DEV (2026-07..10, a development set).
Writes calendar_effects.json in the lab folder.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl

import harness as H
from harness import CODE, LAB, MAIN_PREFIX, c, ctx, float_cap_pit, tradable

OUT = LAB / "calendar_effects.json"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")


def third_fridays(dates: list[date]) -> set[date]:
    trading = set(dates)
    out = set()
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in range(1, 13):
            d = date(y, m, 1)
            fridays = [d + timedelta(days=i) for i in range(31) if (d + timedelta(days=i)).month == m and (d + timedelta(days=i)).weekday() == 4]
            target = fridays[2]
            while target not in trading and target <= dates[-1]:
                target += timedelta(days=1)
            if target in trading:
                out.add(target)
    return out


def daily() -> pl.DataFrame:
    k = ctx()
    p = k.panel.lazy().filter(c(CODE).str.slice(0, 3).is_in(MAIN_PREFIX)).sort(CODE, "date").with_columns(
        tradable().fill_null(False).alias("ok"),
        (c("amount") / float_cap_pit() * 100).alias("turn"),
        ((c("close") / c("open") - 1) * 100).alias("oc"))
    p = p.with_columns((c("turn") * c("volatility_20")).shift(1).over(CODE).alias("tv_prev")).filter(c("ok"))
    p = p.with_columns((c("tv_prev").rank().over("date") / c("tv_prev").count().over("date")).alias("tv_q"))
    g = p.group_by("date").agg(c("pct_chg").mean().alias("all"), c("oc").mean().alias("all_oc"),
                               c("pct_chg").filter(c("tv_q") > 0.8).mean().alias("hot"),
                               c("oc").filter(c("tv_q") > 0.8).mean().alias("hot_oc")).sort("date").collect()
    fridays = third_fridays(k.dates)
    return g.with_columns(c("date").dt.weekday().alias("wd"), c("date").is_in(sorted(fridays)).alias("expiry"))


def stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if len(x) < 5:
        return {"n": len(x)}
    worst = np.sort(x)[: max(1, len(x) // 10)]
    return {"n": len(x), "mean": round(float(x.mean()), 3), "t": round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2),
            "down_share": round(float((x < 0).mean()), 3), "worst10_mean": round(float(worst.mean()), 3)}


def main() -> None:
    d = daily()
    res: dict = {}
    for period in ("PRE", "DEV"):
        lo, hi = H.period_bounds(period)
        part = d.filter(c("date").is_between(lo, hi))
        r: dict = {}
        for col in ("all", "hot", "all_oc", "hot_oc"):
            base = part[col].to_numpy()
            r[col] = {"all_days": stats(base)}
            for i, name in enumerate(DAYS, start=1):
                x = part.filter(c("wd") == i)[col].to_numpy()
                r[col][name] = stats(x)
                rest = part.filter(c("wd") != i)[col].to_numpy()
                r[col][name]["minus_rest"] = round(float(np.nanmean(x) - np.nanmean(rest)), 3) if len(x) else None
            fri = part.filter(c("wd") == 5)
            r[col]["expiry_fri"] = stats(fri.filter(c("expiry"))[col].to_numpy())
            r[col]["other_fri"] = stats(fri.filter(~c("expiry"))[col].to_numpy())
        res[period] = r
        print(f"--- {period} {lo}..{hi}")
        for col in ("all", "hot", "hot_oc"):
            row = " ".join(f"{n} {r[col][n].get('mean', float('nan')):+.2f}({r[col][n].get('t')})/{r[col][n].get('down_share')}" for n in DAYS)
            print(f"  {col:6s} {row} | expiry {r[col]['expiry_fri']} | other Fri {r[col]['other_fri']}")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
