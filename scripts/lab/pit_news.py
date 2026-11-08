"""Point-in-time news for leak-free LLM backtests (backend venv, uses akshare).

The OpenAI web_search tool has no date filter (a `published_before` filter is rejected as an unknown
parameter), so a backtest cannot stop the model from reading later pages. Instead, we build an archive
of items stamped with their publication date and give the model only items dated on or before the
decision day D (after D's close, before the next open):
- company announcements from cninfo (巨潮资讯): titles and dates for [D-30, D];
- 新闻联播 (CCTV evening news) titles for D-1 and D, as the policy backdrop.
Paced at about 1 request per second and cached in .work/lab/pit_news/, so a rerun fetches nothing.
Usage: python pit_news.py   (reads .work/lab/judge_cards.jsonl)
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import akshare as ak

import harness as H

CACHE = H.LAB / "pit_news"
CACHE.mkdir(exist_ok=True)
PACE = 1.0
_last = [0.0]


def _pace() -> None:
    wait = PACE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def announcements(code: str, day: date) -> list[dict]:
    f = CACHE / f"ann_{code}_{day.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    _pace()
    df = ak.stock_zh_a_disclosure_report_cninfo(symbol=code, market="沪深京", category="",
                                                start_date=(day - timedelta(days=30)).strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"))
    rows = [{"date": str(r["公告时间"])[:10], "title": str(r["公告标题"])} for _, r in df.iterrows()]
    rows = [r for r in rows if r["date"] <= day.isoformat()]  # never anything after D
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def cctv(day: date) -> list[str]:
    f = CACHE / f"cctv_{day.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    _pace()
    try:
        df = ak.news_cctv(date=day.strftime("%Y%m%d"))
        titles = [str(t) for t in df["title"].tolist()]
    except Exception as exc:  # holiday or source gap: record empty, never invent
        print(f"cctv {day} failed: {exc!r}"[:160], flush=True)
        titles = []
    f.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
    return titles


def main() -> None:
    cards = [json.loads(x) for x in (H.LAB / "judge_cards.jsonl").open(encoding="utf-8")]
    days = sorted({c["date"] for c in cards})
    t0 = time.perf_counter()
    for d in days:
        day = date.fromisoformat(d)
        cctv(day)
        cctv(day - timedelta(days=1))
    fails = 0
    for i, c in enumerate(cards):
        try:
            announcements(c["code"], date.fromisoformat(c["date"]))
        except Exception as exc:
            fails += 1
            print(f"ann {c['id']} failed: {exc!r}"[:200], flush=True)
            if fails >= 20:
                raise SystemExit("too many failures; stopping (possible block)")
        if i % 50 == 0:
            print(f"[{i}/{len(cards)}] {time.perf_counter() - t0:.0f}s", flush=True)
    print(f"done: {len(cards)} cards, {len(days)} days, {fails} failures, {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
