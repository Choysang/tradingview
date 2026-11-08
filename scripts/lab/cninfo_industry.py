"""Shenwan industry per stock with change dates, from cninfo (巨潮资讯) via akshare (backend venv).

Sina's industry list covers only about half the market (few STAR stocks), and Shenwan's own site and
Eastmoney refuse this machine. cninfo serves each stock's classification history under several standards;
the rows of the current Shenwan standard (申银万国行业分类标准, code 008003) give level 1 (行业门类),
level 2 (行业次类) and level 3 (行业大类) with the date each took effect, so membership is point in time.
One request per stock, paced (PACE seconds between requests), each reply cached in .work/lab/cninfo_ind/ so a
rerun resumes; stops after 8 consecutive failures (a block). With LAB_UNIVERSE=main only main-board
stocks are fetched. Writes .work/lab/sw_industry.parquet
(code, sw1, sw2, sw3, concept, joined) in the schema regime.py and demon.py read: `joined` is the date of the
stock's latest Shenwan change, and days before it count as unknown for that stock.
"""
from __future__ import annotations

import json
import socket
import sys
import time

import akshare as ak
import polars as pl

import harness as H

CACHE = H.BASE / "cninfo_ind"
CACHE.mkdir(exist_ok=True)
PACE = 1.2
socket.setdefaulttimeout(30)  # akshare's requests carry no timeout; a stalled reply hung the run for 30+ min
SW_NEW = "008003"


def fetch(code: str) -> list[dict]:
    f = CACHE / f"{code}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    try:
        df = ak.stock_industry_change_cninfo(symbol=code, start_date="20150101", end_date="20261031")
    except KeyError as exc:  # akshare raises KeyError('变更日期') when cninfo has no record for the stock
        if "变更日期" not in str(exc):
            raise
        df = None
    rows =[{k: (None if v != v else str(v)) for k, v in r.items()} for r in df.to_dict("records")] if df is not None else []
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def build(codes: list[str]) -> pl.DataFrame:
    out = []
    for code in codes:
        f = CACHE / f"{code}.json"
        if not f.exists():
            continue
        sw = [r for r in json.loads(f.read_text(encoding="utf-8")) if r.get("分类标准编码") == SW_NEW]
        if not sw:
            continue
        r = max(sw, key=lambda x: x.get("变更日期") or "")
        out.append({"code": code, "sw1": r.get("行业门类"), "sw2": r.get("行业次类"), "sw3": r.get("行业大类") or r.get("行业中类"),
                    "concept": "", "joined": (r.get("变更日期") or "")[:10]})
    return pl.DataFrame(out)


def main() -> None:
    codes = sorted(pl.read_parquet(H.BASE / "panel.parquet", columns=["code"])["code"].unique().to_list())
    if H.UNIVERSE == "main":  # main board first: the current study's universe
        codes = [x for x in codes if x[:3] in H.MAIN_PREFIX]
    t0, streak, done = time.perf_counter(), 0, 0
    for i, code in enumerate(codes):
        if (CACHE / f"{code}.json").exists():
            done += 1
            continue
        time.sleep(PACE)
        try:
            fetch(code)
            streak = 0
            done += 1
        except Exception as exc:
            streak += 1
            print(f"{code} failed: {exc!r}"[:160], flush=True)
            if streak >= 8:
                print("8 consecutive failures; stopping (possible block)", flush=True)
                break
        if i % 200 == 0:
            print(f"[{i}/{len(codes)}] cached {done} {time.perf_counter() - t0:.0f}s", flush=True)
    sw = build(codes)
    if sw.height:
        sw.write_parquet(H.BASE / ("sw_industry.parquet" if done == len(codes) else "sw_industry_partial.parquet"))
    print(f"done: {done}/{len(codes)} cached, {sw.height} with Shenwan rows, {time.perf_counter() - t0:.0f}s", flush=True)
    return 0 if done == len(codes) else 1


if __name__ == "__main__":
    sys.exit(main())
