"""E070: conditional reversal (pre-registered in docs/research/2026-11-08-batch3-preregistration.md).

Oversold (OS) = bottom 10% of the industry-adjusted 5-day return inside the pool on day D; overbought (OB) = top 10%.
Industry = the panel's Sina industry (current snapshot, as E062); fewer than 5 pool names in an industry -> pool mean.
NEG = a main-board cninfo announcement of a negative class (batch3_lib.neg_class) dated in [the session 5 back, D];
GAP = a session in the window opening <= -5% below the previous close or closing limit-down (pct_chg <= -9.5).
Groups OS_ALL / OS_NEG / OS_CLEAN / OS_GAP / OS_NOGAP / OS_CLEAN_NOGAP / OB_ALL / OB_CLEAN, plus the 20-day version
(OS20_*) as a secondary view. Matched excess over the size x volatility cell (sameday_base.evaluate_matched), holds 5
and 10; H3 adds the frozen score's quintile to the cell (dates with scores, from 2024-08-20).
Needs .work/lab/ext/cninfo_ann.parquet (ann_fetch.py fetch, then parse).
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe cond_reversal.py
"""
from __future__ import annotations

import bisect
import json
from datetime import date

import numpy as np
import polars as pl

import batch3_lib as L
import harness as H
import sameday_base as B
from harness import CODE, c

EXT = H.BASE / "ext"
OUT = H.LAB / "cond_reversal.json"
PINNED = H.LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
HOLDS = (5, 10)
SCORE_START = date(2024, 8, 20)


def neg_dates() -> dict[str, list[date]]:
    ann = pl.read_parquet(EXT / "cninfo_ann.parquet").filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    ann = ann.with_columns(c("title").map_elements(L.neg_class, return_dtype=pl.Utf8).alias("cls")).filter(c("cls").is_not_null())
    ann = ann.unique(["id"])
    print("negative announcements (main board):", ann.height, ann.group_by("cls").len().sort("cls").rows(), flush=True)
    return {code: sorted(g["ann_date"].to_list()) for (code,), g in ann.partition_by(CODE, as_dict=True).items()}


def flags(panel: pl.DataFrame, out: pl.DataFrame, n: int, neg: dict[str, list[date]], days: list[date]) -> pl.DataFrame:
    """Per (code, D) in the pool: decile of the industry-adjusted n-day return, NEG and GAP flags over the window."""
    p = panel.sort(CODE, "date").with_columns(
        ((c("open") / c("close").shift(1).over(CODE) - 1 <= -0.05) | (c("pct_chg") <= -9.5)).fill_null(False).alias("_g"))
    p = p.with_columns(c("_g").cast(pl.Int8).rolling_max(n).over(CODE).fill_null(0).cast(pl.Boolean).alias("gap"))
    q = L.adj_quantile(panel, out.filter(c("in_pool")).select(CODE, "date", "industry"), n)
    j = q.join(p.select(CODE, "date", "gap"), on=[CODE, "date"])
    j = j.filter((c("q") <= 0.1) | (c("q") > 0.9))
    pos = {d: i for i, d in enumerate(days)}
    starts = [days[max(0, pos[d] - n)] for d in j["date"].to_list()]
    has = []
    for code, d, s in zip(j[CODE].to_list(), j["date"].to_list(), starts):
        a = neg.get(code)
        has.append(bool(a) and bisect.bisect_right(a, d) > bisect.bisect_left(a, s))
    return j.with_columns(pl.Series("neg", has), (c("q") <= 0.1).alias("os")).select(CODE, "date", "os", "neg", "gap")


def groups(f: pl.DataFrame, prefix: str) -> dict[str, pl.DataFrame]:
    os_, ob = f.filter(c("os")), f.filter(~c("os"))
    return {f"{prefix}_ALL": os_, f"{prefix}_NEG": os_.filter(c("neg")), f"{prefix}_CLEAN": os_.filter(~c("neg")),
            f"{prefix}_GAP": os_.filter(c("gap")), f"{prefix}_NOGAP": os_.filter(~c("gap")),
            f"{prefix}_CLEAN_NOGAP": os_.filter(~c("neg") & ~c("gap")),
            "OB_ALL" if prefix == "OS" else "OB20_ALL": ob, "OB_CLEAN" if prefix == "OS" else "OB20_CLEAN": ob.filter(~c("neg"))}


def with_score_cell(out: pl.DataFrame, sig: pl.DataFrame, name: str) -> list[dict]:
    """H3: excess over the size x volatility x score-quintile cell (fallback: size x volatility, then size)."""
    s = pl.read_parquet(PINNED).select(CODE, "date", "score")
    pool = out.filter(c("in_pool") & c("period").is_not_null() & c("size5").is_not_null() & c("vol5").is_not_null() & (c("date") >= SCORE_START))
    pool = pool.join(s, on=[CODE, "date"], how="inner").with_columns(((c("score").rank() / c("score").count()).over("date") * 5).ceil().clip(1, 5).alias("sc5"))
    pool = pool.join(sig.select(CODE, "date").unique().with_columns(pl.lit(True).alias("_sig")), on=[CODE, "date"], how="left").with_columns(c("_sig").fill_null(False))
    rng = np.random.default_rng(B.SEED)
    rows = []
    for per in ("PRE", "DEV"):
        for h in HOLDS:
            r = f"r{h}"
            ph = pool.filter((c("period") == per) & c(r).is_finite())
            ph = ph.with_columns(c(r).mean().over("date", "size5").alias("m_s"), c(r).mean().over("date", "size5", "vol5").alias("m_sv0"),
                                 pl.len().over("date", "size5", "vol5").alias("n_sv"), c(r).mean().over("date", "size5", "vol5", "sc5").alias("m_svs0"),
                                 pl.len().over("date", "size5", "vol5", "sc5").alias("n_svs"))
            ph = ph.with_columns(pl.when(c("n_sv") >= B.MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv"))
            ph = ph.with_columns(pl.when(c("n_svs") >= B.MIN_CELL).then(c("m_svs0")).otherwise(c("m_sv")).alias("m_svs"))
            sg = ph.filter(c("_sig"))
            row = {"rule": name, "period": per, "hold": h, "n": sg.height}
            if sg.height:
                d = sg.group_by("date").agg((c(r) - c("m_svs")).mean().alias("e"), pl.len().alias("k")).sort("date")
                row["ex_svs"] = round(float((sg[r] - sg["m_svs"]).mean()), 3)
                row["p_svs"] = round(B._boot(d["e"].to_numpy(), d["k"].to_numpy().astype(float), rng), 4)
                if B.BLOCK_ON:
                    pb = B._boot_block(d["date"].to_list(), d["e"].to_numpy(), d["k"].to_numpy().astype(float),
                                       sorted(ph["date"].unique().to_list()), h, B.SEED + h)
                    row["p_svs_blk"] = round(pb, 4) if pb is not None else None
            rows.append(row)
    return rows


def main() -> None:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    days = sorted(panel["date"].unique().to_list())
    out = B.add_style(B.outcomes(panel), panel)
    neg = neg_dates()
    res: dict = {"groups": {}}
    for n, prefix in ((5, "OS"), (20, "OS20")):
        f = flags(panel, out, n, neg, days)
        for name, sig in groups(f, prefix).items():
            rows = B.evaluate_matched(sig, out, name, holds=HOLDS)
            raw = out.join(sig.select(CODE, "date"), on=[CODE, "date"]).filter(c("in_pool") & c("period").is_not_null())
            for row in rows:
                x = raw.filter((c("period") == row["period"]) & c(f"r{row['hold']}").is_finite())[f"r{row['hold']}"]
                row["mean"] = round(float(x.mean()), 3) if x.len() else None
                row["net_0.25"] = round(row["mean"] - 0.25, 3) if row["mean"] is not None else None
            res["groups"][name] = rows
            print(name, [(r["period"], r["hold"], r["n"], r.get("ex_sv"), r.get("p_sv"), r.get("mean")) for r in rows], flush=True)
        if prefix == "OS":
            res["H3_OS_CLEAN"] = with_score_cell(out, groups(f, prefix)["OS_CLEAN"], "OS_CLEAN")
            print("H3", res["H3_OS_CLEAN"], flush=True)
    g = res["groups"]
    by = lambda name, per, h: next(r for r in g[name] if r["period"] == per and r["hold"] == h)  # noqa: E731
    res["H1"] = B.survives(g["OS_CLEAN"], hold=10)
    res["H2"] = all(by("OS_CLEAN", per, 10)["ex_sv"] - by("OS_NEG", per, 10)["ex_sv"] > 0 for per in ("PRE", "DEV"))
    h3 = {(r["period"], r["hold"]): r for r in res["H3_OS_CLEAN"]}
    res["H3"] = all(h3[(per, 10)].get("ex_svs", -1) > 0 for per in ("PRE", "DEV"))
    pre, dev = by("OB_ALL", "PRE", 5), by("OB_ALL", "DEV", 5)
    res["H4"] = bool(pre["ex_sv"] < 0 and pre["p_sv"] >= 0.95 and dev["ex_sv"] <= 0)
    res["verdict"] = "works" if res["H1"] and res["H2"] and res["H3"] else "see H1-H4"
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print({k: res[k] for k in ("H1", "H2", "H3", "H4", "verdict")})


if __name__ == "__main__":
    main()
