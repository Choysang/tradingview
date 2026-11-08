"""P0 Lhasa tests (round 8 plan, step 3): is a crowd of 拉萨 seats a risk signal, and can it be foreseen at 14:50?

(a) Decay. Events are main-board stock-days on the 龙虎榜. Groups: 3+ distinct 拉萨 buy seats, 1-2 拉萨 seats,
    none; and crowded one-day seats (trip_buyers >= 1; 2+ occurs once in three years, the panel's point-in-time seat score). For each group:
    the D+1 gap (open over D close) and buy at the D+1 open, sell at the D+5 / D+10 close, as excess over the
    same-day mean of all tradable main-board stocks. Means with t-stats clustered by date, by period.
(b) Foresight. Target: 3+ 拉萨 buy seats on D. Features from D's daily bar only (turnover, broken board,
    upper shadow, amplitude, change, volume ratio, volume without price gain, board height). The daily bar
    is the close, not 14:50: this is an upper bound for a 14:50 model; the true test needs the intraday
    recorder (from 2026-11-02). Logistic regression refitted monthly on all earlier months (walk forward),
    scored by AUC and by the hit rate among each day's top 20 predicted names.

Seats come from the server copy of the warehouse (env LHB_WAREHOUSE, a warehouse root holding live/lhb_seats
and live/lhb_stocks): it is fuller than the local one, whose panel columns lhb_on / trip_buyers are mostly empty.
lhb_on and trip_buyers are recomputed from that copy with the backend's own point-in-time seat scoring
(crowd._seats: a seat is a one-day seat once 60%+ of its 5+ settled buys were sold the next day). Writes lhasa.json in the lab folder (.work/lab/main/ under LAB_UNIVERSE=main).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import polars as pl


import harness as H
from harness import CODE, LAB, MAIN_PREFIX, c, ctx, float_cap_pit, tradable
from backend.app.playbooks.crowd import _seats  # noqa: E402 (harness puts the repo on sys.path)
from backend.app.warehouse.store import Warehouse  # noqa: E402

WAREHOUSE = Path(os.environ.get("LHB_WAREHOUSE", str(H.ROOT / "backend/data/warehouse")))
SEATS = WAREHOUSE / "live" / "lhb_seats"
OUT = LAB / "lhasa.json"  # LAB is .work/lab/main under LAB_UNIVERSE=main
TOP = 20
FEATURES = ("turn", "broken", "upper", "amp", "pct", "vr", "vol_no_gain", "streak", "limit_up", "one_word")


def lhasa_counts() -> pl.DataFrame:
    s = pl.scan_parquet(f"{SEATS}/*.parquet").select(CODE, "date", "side", "seat").collect()
    buys = s.filter((c("side") == "buy") & c("seat").str.contains("拉萨")).unique([CODE, "date", "seat"])
    return buys.group_by(CODE, "date").agg(pl.len().cast(pl.Int32).alias("lhasa"))


def frame() -> pl.DataFrame:
    k = ctx()
    p = k.panel.lazy().filter(c(CODE).str.slice(0, 3).is_in(MAIN_PREFIX)).sort(CODE, "date").with_columns(
        tradable().fill_null(False).alias("ok"), float_cap_pit().alias("fcap"))
    nxt = c("open").shift(-1).over(CODE)
    p = p.with_columns(
        ((nxt / c("close") - 1) * 100).alias("gap"),
        ((c("close").shift(-5).over(CODE) / nxt - 1) * 100).alias("r5"),
        ((c("close").shift(-10).over(CODE) / nxt - 1) * 100).alias("r10"),
        c("open_at_limit").shift(-1).over(CODE).fill_null(False).alias("blocked"),
        (c("amount") / c("fcap") * 100).alias("turn"),
        c("is_broken").cast(pl.Float64).alias("broken"),
        ((c("high") - pl.max_horizontal("open", "close")) / c("prev_close") * 100).alias("upper"),
        ((c("high") - c("low")) / c("prev_close") * 100).alias("amp"),
        c("pct_chg").alias("pct"),
        (c("volume") / c("vol_ma5")).alias("vr"),
        ((c("volume") / c("vol_ma5") > 2) & (c("pct_chg") < 2)).cast(pl.Float64).alias("vol_no_gain"),
        c("limit_streak").fill_null(0).cast(pl.Float64).alias("streak"),
        c("is_limit_up").fill_null(False).cast(pl.Float64).alias("limit_up"),
        c("one_word").fill_null(False).cast(pl.Float64).alias("one_word"),
    ).filter(c("ok"))
    p = p.with_columns(*(pl.when(c(x).is_finite()).then(c(x)).alias(x) for x in ("gap", "r5", "r10")))
    p = p.with_columns(*((c(x) - c(x).mean().over("date")).alias(f"{x}_ex") for x in ("gap", "r5", "r10")))
    df = p.select(CODE, "date", "open", "close", "blocked", "gap_ex", "r5_ex", "r10_ex", *FEATURES).collect()
    seats = _seats(Warehouse(WAREHOUSE / "live", "live"), k.panel.select(CODE, "date", "open", "close").sort(CODE, "date"))
    df = df.join(seats.select(CODE, "date", "lhb_on", "trip_buyers"), on=[CODE, "date"], how="left")
    return df.join(lhasa_counts(), on=[CODE, "date"], how="left").with_columns(c("lhasa").fill_null(0), c("lhb_on").fill_null(False))


def clustered(x: pl.DataFrame, y: str) -> dict:
    g = x.drop_nulls(y).group_by("date").agg(c(y).sum().alias("s"), pl.len().alias("n"))
    n = int(g["n"].sum())
    if n < 10:
        return {"n": n}
    m = float(g["s"].sum() / n)
    # cluster-robust standard error of the mean, clusters = dates
    resid = x.drop_nulls(y).with_columns((c(y) - m).alias("_e")).group_by("date").agg(c("_e").sum())["_e"].to_numpy()
    se = float(np.sqrt((resid ** 2).sum()) / n)
    return {"n": n, "mean": round(m, 3), "t": round(m / se, 2) if se else None}


def decay(df: pl.DataFrame) -> dict:
    groups = {
        "lhasa_3plus": c("lhasa") >= 3, "lhasa_1_2": c("lhasa").is_between(1, 2), "lhb_no_lhasa": c("lhb_on") & (c("lhasa") == 0),
        "trip_1plus": c("trip_buyers").fill_null(0) >= 1, "trip_1plus_no_lhasa": (c("trip_buyers").fill_null(0) >= 1) & (c("lhasa") == 0),
    }
    out = {}
    for period in ("PRE", "SELECT", "VERIFY", "ALL"):
        lo, hi = (df["date"].min(), df["date"].max()) if period == "ALL" else H.period_bounds(period)
        part = df.filter(c("date").is_between(lo, hi) & ~c("blocked"))
        out[period] = {name: {y: clustered(part.filter(m), y) for y in ("gap_ex", "r5_ex", "r10_ex")} for name, m in groups.items()}
    return out


def _fit(x: np.ndarray, y: np.ndarray, l2: float = 1.0, steps: int = 300) -> np.ndarray:
    w = np.zeros(x.shape[1])
    for _ in range(steps):  # Newton steps on the L2-penalised log loss
        p = 1 / (1 + np.exp(-x @ w))
        g = x.T @ (p - y) + l2 * w
        h = (x * (p * (1 - p))[:, None]).T @ x + l2 * np.eye(len(w))
        step = np.linalg.solve(h, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def _auc(score: np.ndarray, y: np.ndarray) -> float:
    r = pl.Series(score).rank().to_numpy()
    pos = y == 1
    return float((r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum()))


def foresight(df: pl.DataFrame) -> dict:
    d = df.drop_nulls(list(FEATURES)).with_columns((c("lhasa") >= 3).cast(pl.Int8).alias("y"),
                                                    c("date").dt.strftime("%Y-%m").alias("month"))
    d = d.with_columns(c("turn").clip(0, 60), c("upper").clip(0, 20), c("amp").clip(0, 25), c("vr").clip(0, 10).log1p().alias("vr"))
    months = sorted(d["month"].unique())
    preds = []
    for i, m in enumerate(months):
        if i < 6:
            continue
        tr, te = d.filter(c("month") < m), d.filter(c("month") == m)
        xt = tr.select(FEATURES).to_numpy()
        mu, sd = xt.mean(0), xt.std(0) + 1e-9
        add1 = lambda a: np.hstack([np.ones((len(a), 1)), (a - mu) / sd])  # noqa: E731
        w = _fit(add1(xt), tr["y"].to_numpy().astype(float))
        preds.append(te.select(CODE, "date", "y", "lhb_on", "r5_ex", "gap_ex").with_columns(
            pl.Series("score", add1(te.select(FEATURES).to_numpy()) @ w)))
        last_w = dict(zip(("const", *FEATURES), np.round(w, 3).tolist()))
    pr = pl.concat(preds)
    pr.select(CODE, "date", "score").write_parquet(LAB / "lhasa_preds.parquet")  # for the book test (lhasa_book.py)
    pr = pr.with_columns(c("score").rank("ordinal", descending=True).over("date").alias("rk"))
    top = pr.filter(c("rk") <= TOP)
    out = {"rows": pr.height, "positives": int(pr["y"].sum()), "base_rate": round(float(pr["y"].mean()), 5),
           "auc": round(_auc(pr["score"].to_numpy(), pr["y"].to_numpy()), 3),
           f"top{TOP}_hit": round(float(top["y"].mean()), 4), f"top{TOP}_share_of_positives": round(float(top["y"].sum() / pr["y"].sum()), 3),
           f"top{TOP}_gap_ex": clustered(top, "gap_ex"), f"top{TOP}_r5_ex": clustered(top, "r5_ex"),
           "weights_last_fit": last_w, "first_month": months[6]}
    lhb = pr.filter(c("lhb_on"))  # among names that make the list anyway: does the model still rank them?
    out["auc_within_lhb"] = round(_auc(lhb["score"].to_numpy(), lhb["y"].to_numpy()), 3) if lhb["y"].sum() else None
    by_year = {}
    for yr in sorted(pr["date"].dt.year().unique()):
        q = pr.filter(c("date").dt.year() == yr)
        if q["y"].sum() >= 10:
            by_year[str(yr)] = round(_auc(q["score"].to_numpy(), q["y"].to_numpy()), 3)
    out["auc_by_year"] = by_year
    return out


def main() -> None:
    df = frame()
    df.select(CODE, "date", "lhasa", "trip_buyers", "lhb_on").write_parquet(LAB / "lhasa_flags.parquet")
    print(f"rows {df.height}; lhb stock-days {int(df['lhb_on'].sum())}; lhasa 3+ {int((df['lhasa'] >= 3).sum())}", flush=True)
    res = {"seats": str(SEATS), "decay": decay(df), "foresight": foresight(df)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for period, groups in res["decay"].items():
        print(f"--- decay {period}")
        for name, ys in groups.items():
            print(f"  {name:20s}", "  ".join(f"{y} {v.get('mean', float('nan')):+.2f} (t {v.get('t')}, n {v['n']})" for y, v in ys.items()))
    print("--- foresight", json.dumps(res["foresight"], ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
