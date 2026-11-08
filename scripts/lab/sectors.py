"""Industry and concept membership from Sina (backend venv, akshare), for regime and theme work.

Shenwan (legulegu) and Eastmoney refuse this machine, so the map comes from Sina's own sector lists:
新浪行业 (about 49 industries) and 概念 (about 175 concepts). Membership is today's: a stock that moved,
or a concept created after a rally, is shown as it is now. Industry is stable enough for regime
features; concept membership is look-ahead prone (a stock often joins a concept after it has run),
so the concept map is used only for descriptive labels and flagged wherever it enters a feature.
Paced at 1 request per second, stops after 10 failures; writes .work/lab/sectors.parquet
(code, kind = "ind" | "concept", sector, label).
"""
from __future__ import annotations

import time

import akshare as ak
import polars as pl

import harness as H

PACE = 1.0


def main() -> None:
    rows, fails = [], 0
    t0 = time.perf_counter()
    for kind, indicator in (("ind", "新浪行业"), ("concept", "概念")):
        spot = ak.stock_sector_spot(indicator=indicator)
        time.sleep(PACE)
        for i, (label, name) in enumerate(zip(spot["label"], spot["板块"])):
            time.sleep(PACE)
            try:
                det = ak.stock_sector_detail(sector=label)
            except Exception as exc:
                fails += 1
                print(f"{kind} {label} {name} failed: {exc!r}"[:160], flush=True)
                if fails >= 10:
                    raise SystemExit("too many failures; stopping (possible block)")
                continue
            rows += [{"code": str(c)[-6:], "kind": kind, "sector": str(name), "label": str(label)} for c in det["code"]]
            if i % 25 == 0:
                print(f"[{kind} {i}/{len(spot)}] {len(rows)} rows {time.perf_counter() - t0:.0f}s", flush=True)
    out = pl.DataFrame(rows).unique(["code", "kind", "sector"])
    out.write_parquet(H.LAB / "sectors.parquet")
    by = out.group_by("kind").agg(pl.col("code").n_unique().alias("stocks"), pl.col("sector").n_unique().alias("sectors"))
    print(f"done in {time.perf_counter() - t0:.0f}s, {fails} failures\n{by}", flush=True)


if __name__ == "__main__":
    main()
