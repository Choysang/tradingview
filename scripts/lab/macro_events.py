"""E073: macro calendar event study, descriptive (pre-registered in docs/research/2026-11-08-batch3-preregistration.md).

Events from the Eastmoney calendar history (.work/lab/ext/calendar_hist.parquet, ext_daily.py), names normalised
(full-width colons -> ':') and grouped into classes; one event per class and reaction day. Reaction day:
batch3_lib.reaction_day (timed release: first session closing after it; date-only events and meetings: the next
session after the date). Market = equal-weight main-board names tradable the session before (overnight_us.market).
Per class and segment: n, mean close-to-close return, mean |return| and its ratio to non-event days (days that are
no class's reaction day), gap, intraday, the day before, and the next 5 sessions compounded. Flag: ratio >= 1.25 in
PRE (n >= 5) and DEV (n >= 3).
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe macro_events.py
"""
from __future__ import annotations

import json
import re
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
from harness import CODE, c
from overnight_us import DEV_START, PRE_END, market

EXT = H.BASE / "ext"
OUT = H.LAB / "macro_events.json"
CLASSES: tuple[tuple[str, str, bool], ...] = (  # (class, regex on the normalised name, date-only meeting)
    ("US_CPI", r"^美国:(核心)?CPI", False),
    ("US_NFP", r"^美国:新增非农就业人数", False),
    ("FOMC", r"^美国:联邦基金利率目标:上限", False),
    ("CN_CPI", r"^中国:CPI", False),
    ("CN_PMI", r"^中国:(制造业|非制造业)?PMI", False),
    ("CN_TSF", r"^中国:社会融资", False),
    ("CN_LPR", r"^中国:贷款市场报价利率\(LPR\):1年(\(|$)", False),
    ("POLITBURO", r"^中央政治局会议", True),
    ("STATE_COUNCIL", r"^国务院常务会议", True),
)
RATIO, MIN_PRE, MIN_DEV = 1.25, 5, 3


def events(days: list[date]) -> pl.DataFrame:
    cal = pl.read_parquet(EXT / "calendar_hist.parquet").with_columns(c("name").str.replace_all("：", ":").alias("norm"))
    rows = []
    for cls, pat, meeting in CLASSES:
        rx = re.compile(pat)
        for start, timed, norm in cal.select("start", "timed", "norm").iter_rows():
            if rx.search(norm):
                d = L.reaction_day(start, timed and not meeting, days)
                if d is not None:
                    rows.append({"cls": cls, "day": d, "release": start})
    return pl.DataFrame(rows).sort("release").unique(["cls", "day"], keep="first").sort("day")


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    p = market(panel).with_columns((c("close") / c("pc") - 1).alias("r"))
    ew = p.group_by("date").agg(c("r").mean(), c("gap").mean(), c("intra").mean()).sort("date")
    r = ew["r"].to_numpy()
    fwd5 = np.full(r.size, np.nan)
    for i in range(r.size - 5):
        fwd5[i] = np.prod(1 + r[i + 1:i + 6]) - 1
    ew = ew.with_columns(pl.Series("fwd5", fwd5), c("r").shift(1).alias("prev"))
    ev = events(days)
    any_day = set(ev["day"].to_list())
    res = {"events": ev.group_by("cls").len().sort("cls").rows()}
    for seg, f in (("PRE", c("date") <= PRE_END), ("DEV", c("date") >= DEV_START)):
        x = ew.filter(f)
        base = x.filter(~c("date").is_in(list(any_day)))
        b_abs = float(base["r"].abs().mean())
        # extra, not a pre-registered test: the all-day mean of the same weekday (FOMC always lands on a Thursday);
        # all days, because most Mondays are some class's reaction day and the non-event Mondays are too few
        wd = dict(x.group_by(c("date").dt.weekday().alias("w")).agg(c("r").mean()).iter_rows())
        out = {"non_event_days": base.height, "non_event_mean": round(float(base["r"].mean()) * 100, 3), "non_event_abs": round(b_abs * 100, 3)}
        for cls, _, _ in CLASSES:
            g = x.filter(c("date").is_in(ev.filter(c("cls") == cls)["day"].to_list())).with_columns(c("fwd5").fill_nan(None))
            if g.is_empty():
                out[cls] = {"n": 0}
                continue
            m = lambda col: round(float(g[col].mean()) * 100, 3) if g[col].drop_nulls().len() else None  # noqa: E731
            same_wd = float(np.mean([wd.get(d.isoweekday(), np.nan) for d in g["date"].to_list()]))
            rr = g["r"].to_numpy()
            t = float(rr.mean() / rr.std(ddof=1) * np.sqrt(rr.size)) if rr.size > 2 and rr.std(ddof=1) > 0 else None
            out[cls] = {"n": g.height, "mean": m("r"), "t": round(t, 2) if t is not None else None, "abs": round(float(g["r"].abs().mean()) * 100, 3),
                        "abs_ratio": round(float(g["r"].abs().mean()) / b_abs, 2), "gap": m("gap"), "intra": m("intra"),
                        "prev": m("prev"), "fwd5": m("fwd5"), "up_share": round(float((g["r"] > 0).mean()), 2),
                        "same_weekday_base": round(same_wd * 100, 3)}
        res[seg] = out
    res["flagged"] = [cls for cls, _, _ in CLASSES
                      if res["PRE"][cls].get("n", 0) >= MIN_PRE and res["DEV"][cls].get("n", 0) >= MIN_DEV
                      and res["PRE"][cls]["abs_ratio"] >= RATIO and res["DEV"][cls]["abs_ratio"] >= RATIO]
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
