"""Shenwan (申万) industry map for regime and theme work (backend venv, akshare): every third-level
industry's constituents with the date each stock joined it, mapped up to levels 2 and 1.
Membership is today's (a stock that changed industry shows only its current one; delisted stocks are
missing), so rows before a stock's join date are treated as unknown. Paced at ~1 request per second;
writes .work/lab/sw_industry.parquet (code, sw1, sw2, sw3, concept, joined)."""
from __future__ import annotations

import time

import akshare as ak
import polars as pl

import harness as H


def main() -> None:
    third = ak.sw_index_third_info()
    second = ak.sw_index_second_info()
    up2 = dict(zip(second["行业名称"], second["上级行业"]))
    rows, fails = [], 0
    t0 = time.perf_counter()
    for i, (code3, name3, parent2) in enumerate(zip(third["行业代码"], third["行业名称"], third["上级行业"])):
        time.sleep(1.0)
        try:
            cons = ak.sw_index_third_cons(symbol=code3)
        except Exception as exc:
            fails += 1
            print(f"{code3} {name3} failed: {exc!r}"[:160], flush=True)
            if fails >= 15:
                raise SystemExit("too many failures; stopping (possible block)")
            continue
        for _, r in cons.iterrows():
            rows.append({"code": str(r["股票代码"])[:6], "sw3": name3, "sw2": parent2, "sw1": up2.get(parent2),
                         "concept": str(r.get("细分概念") or ""), "joined": str(r["纳入时间"])[:10]})
        if i % 50 == 0:
            print(f"[{i}/{len(third)}] {len(rows)} rows {time.perf_counter() - t0:.0f}s", flush=True)
    out = pl.DataFrame(rows).unique("code", keep="first")
    out.write_parquet(H.LAB / "sw_industry.parquet")
    print(f"done: {out.height} stocks in {out['sw3'].n_unique()} industries, {fails} failures", flush=True)


if __name__ == "__main__":
    main()
