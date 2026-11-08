"""E070 data: market-wide cninfo announcements by title keyword, month by month (backend venv).

cninfo's category filter returns nothing for a market-wide query (2026-11-08 probe), so we search title keywords.
Each (keyword, month) is paged 30 at a time; every page is cached as JSON under .work/lab/ext/cninfo/<kw>/, so a
rerun resumes and fetches nothing twice. One request per PACE seconds; an HTTP error or a non-JSON answer counts
as a failure, and 3 in a row stop the job at once (exit 2), keeping what is cached. No retries, no bypass.
`parse` merges the cache into .work/lab/ext/cninfo_ann.parquet (code, ann_date Beijing, title, kw, id).
`recheck` refetches, once and more slowly, pages that came back empty although page 1 reported more results.
Usage: python ann_fetch.py fetch | recheck | parse
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import requests

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".work" / "lab" / "ext" / "cninfo"
OUT = ROOT / ".work" / "lab" / "ext" / "cninfo_ann.parquet"
URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"}
KEYWORDS = ("立案", "处罚", "预亏", "预减", "首亏", "续亏", "退市风险", "风险警示", "减持", "问询函", "解除限售")
START, END = date(2023, 10, 1), date(2026, 10, 31)
PACE, MAX_FAILS, PAGE = 1.2, 3, 30
CST = timezone(timedelta(hours=8))


def months() -> list[tuple[date, date]]:
    out, d = [], START
    while d <= END:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out.append((d, min(nxt - timedelta(days=1), END)))
        d = nxt
    return out


class Fetcher:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.last = 0.0
        self.fails = 0
        self.requests = 0

    def page(self, kw: str, a: date, b: date, n: int) -> dict:
        f = CACHE / kw / f"{a:%Y-%m}_p{n}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        wait = PACE - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.requests += 1
        payload = {"pageNum": str(n), "pageSize": str(PAGE), "column": "szse", "tabName": "fulltext", "plate": "", "stock": "",
                   "searchkey": kw, "secid": "", "category": "", "trade": "", "seDate": f"{a}~{b}", "sortName": "", "sortType": "",
                   "isHLtitle": "true"}
        try:
            r = self.s.post(URL, data=payload, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            j = r.json()
            if not isinstance(j, dict) or "totalAnnouncement" not in j:
                raise RuntimeError("unexpected answer")
        except Exception as exc:  # network, refusal or non-JSON: count it, stop after MAX_FAILS in a row
            self.fails += 1
            print(f"[fail {self.fails}] {kw} {a:%Y-%m} p{n}: {exc!r}"[:200], flush=True)
            if self.fails >= MAX_FAILS:
                print(f"stopping after {self.fails} failures in a row; {self.requests} requests made", flush=True)
                sys.exit(2)
            return {}
        self.fails = 0
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        return j


def fetch() -> None:
    fx = Fetcher()
    t0 = time.time()
    for kw in KEYWORDS:
        total = 0
        for a, b in months():
            first = fx.page(kw, a, b, 1)
            n_total = int(first.get("totalAnnouncement") or 0)
            total += n_total
            for n in range(2, math.ceil(n_total / PAGE) + 1):
                fx.page(kw, a, b, n)
        print(f"{kw}: {total} announcements; {fx.requests} requests so far, {time.time() - t0:.0f}s", flush=True)
    print("done", flush=True)


def recheck() -> None:
    """One slower pass over pages that came back empty although page 1 reported more results (cninfo does this
    now and then): delete each such cached page and fetch it once more at 2 s spacing; an empty answer is kept."""
    global PACE
    PACE = 2.0
    fx = Fetcher()
    redo = recovered = 0
    for kw in KEYWORDS:
        for a, b in months():
            p1 = CACHE / kw / f"{a:%Y-%m}_p1.json"
            if not p1.exists():
                continue
            n_total = int(json.loads(p1.read_text(encoding="utf-8")).get("totalAnnouncement") or 0)
            for n in range(2, math.ceil(n_total / PAGE) + 1):
                f = CACHE / kw / f"{a:%Y-%m}_p{n}.json"
                if f.exists() and not (json.loads(f.read_text(encoding="utf-8")).get("announcements") or []):
                    f.unlink()
                    redo += 1
                    recovered += bool(fx.page(kw, a, b, n).get("announcements"))
    print(f"recheck: {redo} empty pages refetched, {recovered} now have rows; {fx.requests} requests", flush=True)


def parse() -> None:
    rows = []
    for f in CACHE.glob("*/*.json"):
        j = json.loads(f.read_text(encoding="utf-8"))
        for x in j.get("announcements") or []:
            ts = x.get("announcementTime")
            if not ts or not x.get("secCode"):
                continue
            rows.append({"id": str(x.get("announcementId")), "code": str(x["secCode"]), "kw": f.parent.name,
                         "ann_date": datetime.fromtimestamp(ts / 1000, CST).date(), "title": x.get("announcementTitle") or ""})
    df = pl.DataFrame(rows).unique(["id", "kw"]).sort("ann_date", "code")
    df.write_parquet(OUT)
    print(df.height, "rows;", df["id"].n_unique(), "announcements;", df.group_by("kw").len().sort("kw").rows())


if __name__ == "__main__":
    {"fetch": fetch, "recheck": recheck, "parse": parse}[sys.argv[1]]()
