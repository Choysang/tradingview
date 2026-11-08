"""E067: rolling view and a single-break test of the frozen score, no split chosen in advance (pre-registered in
docs/research/2026-11-07-batch2-preregistration.md).

Daily series on the pinned scores, 2024-08-20 ~ 2026-10-30, label = D+1 open -> D+5 close (sameday_base):
- ic: rank IC of the score against the label over the pool;
- top_sv: mean label of the top 10% by score minus the same-day size x volatility quintile cell mean (E062 baseline).
Report 1: non-overlapping 60-session blocks, mean and Newey-West t (lag 5).
Report 2: sup |t| single break: for every split with >= 60 sessions each side, t of the mean difference (NW lag 5 on
the pooled demeaned series); null by circular block bootstrap of the demeaned series (block 20, 1,000 draws).
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe rolling_break.py
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

import harness as H
import sameday_base as B
from harness import CODE, c

PINNED = H.LAB / "pinned" / "fusion_static40_safe_698e3aa7.parquet"
OUT = H.LAB / "rolling_break.json"
BLOCK, MIN_SIDE, N_BOOT, LAG, WIN = 20, 60, 1000, 5, 60
SEED = 20261109


def series() -> pl.DataFrame:
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    out = B.add_style(B.outcomes(panel), panel)
    p = out.filter(c("in_pool") & c("r5").is_finite() & c("size5").is_not_null() & c("vol5").is_not_null())
    p = p.with_columns(c("r5").mean().over("date", "size5").alias("m_s"), c("r5").mean().over("date", "size5", "vol5").alias("m_sv0"),
                       pl.len().over("date", "size5", "vol5").alias("n_sv"))
    p = p.with_columns(pl.when(c("n_sv") >= B.MIN_CELL).then(c("m_sv0")).otherwise(c("m_s")).alias("m_sv"))
    s = pl.read_parquet(PINNED)
    j = p.join(s, on=[CODE, "date"], how="inner").with_columns((c("score").rank() / c("score").count()).over("date").alias("q"))
    return (j.group_by("date").agg(pl.corr(c("score").rank(), c("r5").rank()).alias("ic"),
                                   (c("r5") - c("m_sv")).filter(c("q") > 0.9).mean().alias("top_sv"), pl.len().alias("n"))
            .filter(c("n") >= 100).sort("date"))


def nw_t(x: np.ndarray) -> float:
    return float(H.nw_t(x, LAG) or 0.0)


def blocks(df: pl.DataFrame, col: str) -> list[dict]:
    x, d = df[col].to_numpy(), df["date"].to_list()
    out = []
    for i in range(0, len(x) - WIN + 1, WIN):
        v = x[i:i + WIN]
        out.append({"start": str(d[i]), "end": str(d[i + WIN - 1]), "mean": round(float(v.mean()), 4), "t": round(nw_t(v), 2)})
    return out


def sup_t(x: np.ndarray) -> tuple[float, int]:
    n = len(x)
    best, at = 0.0, -1
    cs = np.cumsum(np.r_[0.0, x])
    for b in range(MIN_SIDE, n - MIN_SIDE + 1):
        m1, m2 = cs[b] / b, (cs[n] - cs[b]) / (n - b)
        r = np.r_[x[:b] - m1, x[b:] - m2]
        # long-run variance of the residuals (Bartlett, LAG) for the difference of means
        g0 = r.var()
        lrv = g0 + 2 * sum((1 - k / (LAG + 1)) * np.mean(r[k:] * r[:-k]) for k in range(1, LAG + 1))
        se = np.sqrt(max(lrv, 1e-12) * (1 / b + 1 / (n - b)))
        t = abs(m2 - m1) / se
        if t > best:
            best, at = t, b
    return best, at


def break_test(df: pl.DataFrame, col: str, rng: np.random.Generator) -> dict:
    x = df[col].to_numpy().astype(float)
    stat, at = sup_t(x)
    z = x - x.mean()
    n = len(z)
    null = []
    for _ in range(N_BOOT):
        starts = rng.integers(0, n, size=int(np.ceil(n / BLOCK)))
        idx = np.concatenate([(s + np.arange(BLOCK)) % n for s in starts])[:n]
        null.append(sup_t(z[idx])[0])
    null = np.array(null)
    d = df["date"].to_list()
    return {"sup_t": round(stat, 2), "break_date": str(d[at]), "p": round(float(((null >= stat).sum() + 1) / (N_BOOT + 1)), 4),
            "mean_before": round(float(x[:at].mean()), 4), "mean_after": round(float(x[at:].mean()), 4),
            "null_p95": round(float(np.percentile(null, 95)), 2)}


def main() -> None:
    df = series()
    rng = np.random.default_rng(SEED)
    res = {"days": df.height, "start": str(df["date"].min()), "end": str(df["date"].max())}
    for col in ("ic", "top_sv"):
        bl = blocks(df, col)
        res[col] = {"overall_mean": round(float(df[col].mean()), 4), "overall_t": round(nw_t(df[col].to_numpy()), 2),
                    "blocks": bl, "share_positive": round(sum(b["mean"] > 0 for b in bl) / len(bl), 3),
                    "break": break_test(df, col, rng)}
        print(col, json.dumps({k: v for k, v in res[col].items() if k != "blocks"}, ensure_ascii=False), flush=True)
        for b in bl:
            print("  ", b)
    res["q1_top_sv_two_thirds_positive"] = res["top_sv"]["share_positive"] >= 2 / 3
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
