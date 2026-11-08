"""E083 / E084: held-out index tests on 2010-01-01 .. 2023-10-31 (docs/research/2026-11-08-batch4-preregistration.md).

E083: FOMC reaction day R (first A-share session on or after the Beijing release date, jin10 '美联储利率决议报告');
      intra(R) = close / open - 1 versus every other session. H1: the difference < 0 with p < 0.05 for 上证指数
      (sh000001) and the same sign for 中证500 (sh000905). H2 (report): open of R -> close of R+4.
E084: A-share day D with the S&P 500 window return (sessions dated in [P, D), batch3_lib.us_window_return) <= -1.5%;
      next = close(D+1) / close(D) - 1 versus every session. H1: difference > 0 with p < 0.05 for 上证 and the same
      sign for 中证500. Report: net of 0.252%, the <= -2.5% subset, D's gap and intra.
Bootstrap: events and the comparison days resampled independently (single-day returns do not overlap), 10,000
draws, seed 20261108. The 2023-11 .. 2026-10 segment (seen by E072 / E073) is reported separately for reference.
Data: .work/lab/ext/hist/ (cn_sh000001 / cn_sh000905 / us_inx parquet, fed_rate.csv; akshare, fetched 2026-11-08).
Run: cd scripts/lab && PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe hist_index_events.py
"""
from __future__ import annotations

import bisect
import json
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

import batch3_lib as L

HIST = Path(__file__).resolve().parents[2] / ".work" / "lab" / "ext" / "hist"
OUT = Path(__file__).resolve().parents[2] / ".work" / "lab" / "main" / "hist_index_events.json"
SEGMENTS = {"HOLDOUT": (date(2010, 1, 1), date(2023, 10, 31)), "SEEN": (date(2023, 11, 1), date(2026, 10, 30))}
N_BOOT, SEED, COST = 10_000, 20261108, 0.00252
DROP, BIG_DROP = -0.015, -0.025


def index(sym: str) -> pl.DataFrame:
    d = pl.read_parquet(HIST / f"cn_{sym}.parquet").with_columns(pl.col("date").cast(pl.Date)).sort("date")
    return d.with_columns((pl.col("close") / pl.col("open") - 1).alias("intra"),
                          (pl.col("open") / pl.col("close").shift(1) - 1).alias("gap"),
                          (pl.col("close").shift(-1) / pl.col("close") - 1).alias("next"),
                          (pl.col("close").shift(-4) / pl.col("open") - 1).alias("fwd5_open"))


def boot_diff(ev: np.ndarray, other: np.ndarray, seed: int = SEED) -> tuple[float, float, float]:
    """(difference of means, share of resampled differences <= 0, share >= 0)."""
    ev, other = ev[np.isfinite(ev)], other[np.isfinite(other)]
    rng = np.random.default_rng(seed)
    a = ev[rng.integers(0, ev.size, (N_BOOT, ev.size))].mean(axis=1)
    b = np.array([other[rng.integers(0, other.size, other.size)].mean() for _ in range(200)])  # comparison mean is tight
    d = a - rng.choice(b, N_BOOT)
    return float(ev.mean() - other.mean()), float((d <= 0).mean()), float((d >= 0).mean())


def fomc_dates() -> list[date]:
    f = pl.read_csv(HIST / "fed_rate.csv").with_columns(pl.col("日期").str.to_date())
    return sorted(set(f["日期"].to_list()))


def reaction_days(days: list[date], release: list[date]) -> list[date]:
    out = []
    for r in release:
        i = bisect.bisect_left(days, r)
        if i < len(days):
            out.append(days[i])
    return sorted(set(out))


def e083(ix: dict[str, pl.DataFrame]) -> dict:
    res = {}
    for seg, (a, b) in SEGMENTS.items():
        res[seg] = {}
        for sym, d in ix.items():
            s = d.filter((pl.col("date") >= a) & (pl.col("date") <= b))
            days = s["date"].to_list()
            rd = set(reaction_days(days, [x for x in fomc_dates() if a <= x <= b]))
            ev, ot = s.filter(pl.col("date").is_in(list(rd))), s.filter(~pl.col("date").is_in(list(rd)))
            diff, p_le, p_ge = boot_diff(ev["intra"].to_numpy(), ot["intra"].to_numpy())
            d5, _, p5 = boot_diff(ev["fwd5_open"].to_numpy(), ot["fwd5_open"].to_numpy())
            res[seg][sym] = {"events": ev.height, "intra_event_pct": round(float(ev["intra"].mean()) * 100, 3),
                             "intra_other_pct": round(float(ot["intra"].mean()) * 100, 3), "diff_pct": round(diff * 100, 3),
                             "p_diff_ge_0": round(p_ge, 4), "up_share": round(float((ev["intra"] > 0).mean()), 2),
                             "gap_event_pct": round(float(ev["gap"].mean()) * 100, 3),
                             "fwd5_open_diff_pct": round(d5 * 100, 3), "p_fwd5_ge_0": round(p5, 4)}
    h = res["HOLDOUT"]
    res["H1"] = bool(h["sh000001"]["diff_pct"] < 0 and h["sh000001"]["p_diff_ge_0"] < 0.05 and h["sh000905"]["diff_pct"] < 0)
    return res


def e084(ix: dict[str, pl.DataFrame]) -> dict:
    us = pl.read_parquet(HIST / "us_inx.parquet").with_columns(pl.col("date").cast(pl.Date)).sort("date")
    pairs = list(zip(us["date"].to_list(), us["close"].to_list()))
    res = {}
    for seg, (a, b) in SEGMENTS.items():
        res[seg] = {}
        for sym, d in ix.items():
            s = d.filter((pl.col("date") >= a) & (pl.col("date") <= b))
            w = L.us_window_return(s["date"].to_list(), pairs)
            s = s.with_columns(pl.col("date").replace_strict(w, default=None, return_dtype=pl.Float64).alias("us"))
            ev = s.filter(pl.col("us") <= DROP)
            big = s.filter(pl.col("us") <= BIG_DROP)
            diff, p_le, _ = boot_diff(ev["next"].to_numpy(), s["next"].to_numpy())
            res[seg][sym] = {"events": ev.height, "next_event_pct": round(float(ev["next"].mean()) * 100, 3),
                             "next_all_pct": round(float(s["next"].mean()) * 100, 3), "diff_pct": round(diff * 100, 3),
                             "p_diff_le_0": round(p_le, 4), "up_share": round(float((ev["next"] > 0).mean()), 2),
                             "net_next_pct": round((float(ev["next"].mean()) - COST) * 100, 3),
                             "gap_pct": round(float(ev["gap"].mean()) * 100, 3), "intra_pct": round(float(ev["intra"].mean()) * 100, 3),
                             "big_events": big.height, "big_next_pct": round(float(big["next"].mean()) * 100, 3) if big.height else None}
    h = res["HOLDOUT"]
    res["H1"] = bool(h["sh000001"]["diff_pct"] > 0 and h["sh000001"]["p_diff_le_0"] < 0.05 and h["sh000905"]["diff_pct"] > 0)
    return res


def main() -> None:
    ix = {s: index(s) for s in ("sh000001", "sh000905")}
    res = {"E083": e083(ix), "E084": e084(ix)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
