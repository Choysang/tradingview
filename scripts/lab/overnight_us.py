"""E072: US overnight moves -> A-share open and intraday (pre-registered in docs/research/2026-11-08-batch3-preregistration.md).

For A-share day D (previous A-share day P) the US return is the close-to-close move over the US sessions dated in
[P, D) (batch3_lib.us_window_return): every one of them has closed before D's opening auction. Targets are equal-
weight means over the main-board names tradable on P with a bar on D: gap (D open / P close), intraday (D close /
D open), next (D+1 close / D close). OLS of each on the US return per segment, Newey-West t (lag 5); conditional
means on |S&P| >= 1.5%; SOX -> intraday excess of 申万二级 半导体 members (joined on or before D) over the pool.
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe overnight_us.py
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
from harness import CODE, c

EXT = H.BASE / "ext"
OUT = H.LAB / "overnight_us.json"
PRE_END, DEV_START = date(2026, 6, 30), date(2026, 7, 1)
SHOCK, LAG = 0.015, 5


def ols_nw(x: np.ndarray, y: np.ndarray) -> dict:
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    n = x.size
    if n < 20:
        return {"n": int(n)}
    xm, ym = x - x.mean(), y - y.mean()
    sxx = float((xm ** 2).sum())
    b = float((xm * ym).sum() / sxx)
    e = ym - b * xm
    z = xm * e
    lrv = float((z ** 2).sum()) + 2 * sum((1 - k / (LAG + 1)) * float((z[k:] * z[:-k]).sum()) for k in range(1, LAG + 1))
    se = np.sqrt(max(lrv, 1e-18)) / sxx
    r2 = 1 - float((e ** 2).sum()) / float((ym ** 2).sum())
    return {"n": int(n), "beta": round(b, 4), "t": round(b / se, 2), "r2": round(r2, 3)}


def market(panel: pl.DataFrame) -> pl.DataFrame:
    p = panel.sort(CODE, "date").with_columns(
        c("close").shift(1).over(CODE).alias("pc"), H.tradable().shift(1).over(CODE).fill_null(False).alias("ok_prev"),
        c("close").shift(-1).over(CODE).alias("nc"))
    p = p.filter(c("ok_prev") & (c("pc") > 0) & (c("open") > 0)).with_columns(
        (c("open") / c("pc") - 1).alias("gap"), (c("close") / c("open") - 1).alias("intra"), (c("nc") / c("close") - 1).alias("next"))
    return p


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    p = market(panel)
    ew = p.group_by("date").agg(c("gap").mean(), c("intra").mean(), c("next").mean(), pl.len().alias("n")).sort("date")
    us = pl.read_parquet(EXT / "us_index.parquet")
    ret = {}
    for s in (".INX", ".IXIC", ".SOX", ".HXC"):
        u = us.filter(c("symbol") == s).sort("date")
        ret[s] = L.us_window_return(days, list(zip(u["date"].to_list(), u["close"].to_list())))
    ew = ew.with_columns(*(pl.col("date").replace_strict(ret[s], default=None, return_dtype=pl.Float64).alias(s) for s in ret))
    sw = pl.read_parquet(H.LAB / "sw_industry.parquet").filter(c("sw2") == "半导体").with_columns(c("joined").str.to_date().alias("joined"))
    semi = p.join(sw.select(CODE, "joined"), on=CODE).filter(c("joined") <= c("date")).group_by("date").agg(c("intra").mean().alias("semi_intra"),
                                                                                                         pl.len().alias("n_semi"))
    ew = ew.join(semi, on="date", how="left").with_columns((c("semi_intra") - c("intra")).alias("semi_ex"))
    res = {"days": ew.height}
    for seg, f in (("PRE", c("date") <= PRE_END), ("DEV", c("date") >= DEV_START)):
        x = ew.filter(f)
        r = {"days": x.height}
        for s in (".INX", ".IXIC", ".SOX", ".HXC"):
            for y in ("gap", "intra", "next"):
                r[f"{y}~{s}"] = ols_nw(x[s].to_numpy().astype(float), x[y].to_numpy().astype(float))
        r["semi_ex~.SOX"] = ols_nw(x[".SOX"].to_numpy().astype(float), x["semi_ex"].to_numpy().astype(float))
        for side, cond in (("up", c(".INX") >= SHOCK), ("down", c(".INX") <= -SHOCK)):
            g = x.filter(cond)
            r[f"shock_{side}"] = {"n": g.height, "gap": round(float(g["gap"].mean() or 0) * 100, 3) if g.height else None,
                                  "intra": round(float(g["intra"].mean() or 0) * 100, 3) if g.height else None,
                                  "next": round(float(g["next"].mean() or 0) * 100, 3) if g.height else None}
        res[seg] = r
    for name in ("intra~.INX", "intra~.IXIC", "semi_ex~.SOX"):
        a, b = res["PRE"].get(name, {}), res["DEV"].get(name, {})
        res[f"tradable_{name}"] = bool(a.get("t") is not None and b.get("t") is not None and np.sign(a["beta"]) == np.sign(b["beta"])
                                       and abs(a["t"]) >= 2 and abs(b["t"]) >= 2)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
