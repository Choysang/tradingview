"""Tushare limit-board history: `limit_list_d` (每日涨跌停明细: first / last seal time, opens, seal money) and
`limit_list_ths` (同花顺涨停原因 and pools, as published that day), for calibrating historical queue fills
and for point-in-time themes (round 7, R7-3). `daily_basic` (每日指标) gives the daily free-float share count
(free_share, 万股) and free-float turnover (turnover_rate_f), point in time: the P0 free-float source.

The token is read from the environment variable TUSHARE_TOKEN only and is never written anywhere.
One request per trading day (and per pool type for limit_list_ths), paced (--per-minute, default 120).
Each answer is cached as .work/tushare/<api>/<YYYYMMDD>[_<type>].parquet, empty days included, so a rerun
resumes. A permission or token error stops at once; a rate-limit answer waits 65 s and retries once;
5 failures in a row stop the run. `--merge` writes .work/tushare/<api>.parquet from the cache.
Trading days come from the lab panel (.work/lab/panel.parquet), so the range ends at its last date.

Usage (lab or backend venv):
    TUSHARE_TOKEN=... python tushare_limits.py limit_list_d [--start 20231101] [--end 20261030]
    TUSHARE_TOKEN=... python tushare_limits.py limit_list_ths
    TUSHARE_TOKEN=... python tushare_limits.py daily_basic
    python tushare_limits.py limit_list_d --merge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".work" / "tushare"
URL = "http://api.tushare.pro"
TYPES = {"limit_list_d": [None], "daily_basic": [None], "limit_list_ths": ["涨停池", "连扳池", "冲刺涨停", "炸板池", "跌停池"]}
TYPE_PARAM = {"limit_list_d": "limit_type", "limit_list_ths": "limit_type"}
MAX_FAILS = 5
RATE_WAIT = 65.0
# Answers that mean "stop": no permission for this API, bad or expired token, not enough points.
FATAL = ("权限", "token", "积分", "无效", "invalid")
RATE = ("每分钟", "频率", "too many", "rate")


class Fatal(Exception):
    pass


def call(api: str, token: str, params: dict) -> pl.DataFrame:
    body = json.dumps({"api_name": api, "token": token, "params": params, "fields": ""}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 0:
        msg = str(payload.get("msg", ""))
        if any(k in msg.lower() for k in RATE):
            raise TimeoutError(msg)
        if any(k in msg.lower() for k in FATAL):
            raise Fatal(msg)
        raise RuntimeError(f"code {payload.get('code')}: {msg}")
    data = payload.get("data") or {}
    fields, items = data.get("fields") or [], data.get("items") or []
    return pl.DataFrame(items, schema=fields, orient="row", infer_schema_length=None) if fields else pl.DataFrame()


def days(start: str, end: str) -> list[str]:
    panel = pl.read_parquet(ROOT / ".work" / "lab" / "panel.parquet", columns=["date"])
    ds = sorted({d.strftime("%Y%m%d") for d in panel["date"].unique().to_list()})
    return [d for d in ds if start <= d <= end]


def target(api: str, day: str, kind: str | None) -> Path:
    return OUT / api / (f"{day}_{kind}.parquet" if kind else f"{day}.parquet")


def fetch(api: str, start: str, end: str, per_minute: float) -> int:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        print("TUSHARE_TOKEN is not set", file=sys.stderr)
        return 2
    (OUT / api).mkdir(parents=True, exist_ok=True)
    gap, fails, done, todo = 60.0 / per_minute, 0, 0, []
    for day in days(start, end):
        for kind in TYPES[api]:
            if not target(api, day, kind).exists():
                todo.append((day, kind))
    print(f"[{api}] {len(todo)} requests to make", flush=True)
    t0 = time.perf_counter()
    for i, (day, kind) in enumerate(todo):
        params = {"trade_date": day, **({TYPE_PARAM[api]: kind} if kind else {})}
        for attempt in (1, 2):
            time.sleep(gap)
            try:
                frame = call(api, token, params)
                break
            except TimeoutError as exc:
                if attempt == 2:
                    print(f"rate limit twice at {day} {kind}: {exc}; stopping", flush=True)
                    return 1
                print(f"rate limit: {exc}; waiting {RATE_WAIT:.0f}s", flush=True)
                time.sleep(RATE_WAIT)
            except Fatal as exc:
                print(f"stopping: {exc}", flush=True)
                return 1
            except (urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
                fails += 1
                print(f"{day} {kind} failed ({fails} in a row): {exc!r}"[:200], flush=True)
                if fails >= MAX_FAILS:
                    print("too many failures in a row; stopping", flush=True)
                    return 1
                frame = None
                break
        if frame is None:
            continue
        fails = 0
        frame.write_parquet(target(api, day, kind))
        done += 1
        if i % 50 == 0:
            print(f"[{i}/{len(todo)}] {day} {kind or ''} rows {frame.height} {time.perf_counter() - t0:.0f}s", flush=True)
    print(f"[{api}] done {done}/{len(todo)}", flush=True)
    return 0


def merge(api: str) -> int:
    files = sorted((OUT / api).glob("*.parquet"))
    frames = [pl.read_parquet(f) for f in files]
    frames = [f for f in frames if f.height]
    if not frames:
        print("nothing cached")
        return 1
    out = pl.concat(frames, how="diagonal_relaxed")
    out.write_parquet(OUT / f"{api}.parquet")
    print(f"{api}: {out.height} rows from {len(files)} files, columns {out.columns}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("api", choices=sorted(TYPES))
    ap.add_argument("--start", default="20231101")
    ap.add_argument("--end", default="20991231")
    ap.add_argument("--per-minute", type=float, default=120.0)
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    return merge(a.api) if a.merge else fetch(a.api, a.start, a.end, a.per_minute)


if __name__ == "__main__":
    sys.exit(main())
