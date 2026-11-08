"""Message recorder (M1): 7x24 news from three sources, guba follow-ups, and the 10 s watch list for intraday.py.

One process per trading day, separate from the quote recorder (a failure here never touches it):
* 同花顺 7×24 every 10 s (first source; its items carry linked stock codes);
* 东方财富 7×24 every 30 s (gap filling and comment counts);
* 财联社电报 every 30 s (cross-check; its web API wants a request signature, computed as the public web client
  and akshare's `stock_info_global_cls` do: md5(sha1(urlencode(params)))). Its `modified_time` is a real
  revision time.
Every source has an 8 s timeout, one attempt per poll, and a breaker: 3 bad answers in a row (HTTP 403 / 429 /
5xx, HTML, unparsable JSON, a non-zero errno such as a failed signature) stop that source for the day.

A new item linked to main-board stocks (at most PER_MESSAGE of them) does two things:
* the codes go into `watch.json` with an expiry WATCH_MINUTES ahead (at most WATCH_CAP codes in total, the
  newest kept); intraday.py polls them on its 10 s grid until they expire;
* guba post lists of those codes are fetched at +1, +3 and +5 minutes after our receipt: posts published
  since the receipt, their distinct authors, and the titles (title sentiment is computed later from them).

Times: `received_at` (UTC, our clock) is the only time trading may use; the source's own time is kept as
`source_published_at`. Output under `$PULSE_NEWS_DIR/<YYYY-MM-DD>/` (default `<data_dir>/news`):
`raw/<source>.jsonl.gz` (every answer), `messages.parquet` (one row per item version), `guba.parquet`,
`summary.json`. `watch.json` sits in the root, shared with intraday.py.
Usage: python -m backend.app.warehouse.news run | once
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
import polars as pl

from ..config import settings
from ..market.calendar import load_calendar

LOG = logging.getLogger("pulse.warehouse.news")

CST = timezone(timedelta(hours=8))
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 8.0
BREAK_AFTER = 3
STOP_AT = dtime(22, 0)  # the scheduled task starts the day at 08:00
PER_MESSAGE = 8
WATCH_MINUTES = 10
WATCH_CAP = 80
GUBA_OFFSETS = (60, 180, 300)
GUBA_PACE = 2.0  # seconds between guba requests
GUBA_BACKLOG = 60  # pending guba fetches beyond this are dropped (counted)
FLUSH_EVERY = 300
MESSAGE_SCHEMA = {"source_id": pl.Utf8, "item_id": pl.Utf8, "revision_id": pl.Utf8, "supersedes": pl.Utf8,
                  "content_hash": pl.Utf8, "title": pl.Utf8, "body": pl.Utf8, "url": pl.Utf8,
                  "source_published_at": pl.Datetime("ms", "UTC"), "source_modified_at": pl.Datetime("ms", "UTC"),
                  "received_at": pl.Datetime("ms", "UTC"), "codes": pl.List(pl.Utf8), "comments": pl.Int64,
                  "level": pl.Utf8}
GUBA_SCHEMA = {"source_item": pl.Utf8, "code": pl.Utf8, "offset_s": pl.Int64, "fetched_at": pl.Datetime("ms", "UTC"),
               "since": pl.Datetime("ms", "UTC"), "new_posts": pl.Int64, "new_authors": pl.Int64, "titles": pl.List(pl.Utf8),
               "comments_on_new": pl.Int64, "ok": pl.Boolean}


class SourceError(Exception):
    pass


def _utc(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def _cst(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(timezone.utc) if text else None
    except ValueError:
        return None


def _json(r: httpx.Response) -> Any:
    if r.status_code in (403, 429) or r.status_code >= 500:
        raise SourceError(f"HTTP {r.status_code}")
    if r.text[:1] not in "{[":
        raise SourceError(f"not JSON (HTTP {r.status_code}): {r.text[:60]!r}")
    try:
        return r.json()
    except ValueError as exc:
        raise SourceError(f"bad JSON: {exc}") from exc


def _codes(values: list[str]) -> list[str]:
    """6-digit A-share codes in order, de-duplicated."""
    out = []
    for v in values:
        m = re.fullmatch(r"(?:[a-zA-Z]{2}|\d\.)?(\d{6})", str(v).strip())
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


# --- sources: each returns item dicts with the MESSAGE_SCHEMA fields except revision / hash / received -----

def fetch_ths(c: httpx.Client) -> list[dict]:
    j = _json(c.get("https://news.10jqka.com.cn/tapp/news/push/stock/", params={"page": "1", "tag": "", "track": "website", "pagesize": "30"},
                    headers={"Referer": "https://news.10jqka.com.cn/realtimenews.html"}))
    rows = (j.get("data") or {}).get("list")
    if not isinstance(rows, list):
        raise SourceError("ths: no data.list")
    return [{"item_id": str(x["id"]), "title": x.get("title") or "", "body": x.get("digest") or "", "url": x.get("url"),
             "source_published_at": _utc(float(x["ctime"])) if x.get("ctime") else None, "source_modified_at": None,
             "codes": _codes([s.get("stockCode", "") for s in x.get("stock") or []]), "comments": None,
             "level": "important" if str(x.get("import")) == "1" else None} for x in rows if x.get("id")]


def fetch_em(c: httpx.Client) -> list[dict]:
    j = _json(c.get("https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
                    params={"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": "30",
                            "req_trace": str(int(time.time() * 1000))}, headers={"Referer": "https://kuaixun.eastmoney.com/"}))
    rows = (j.get("data") or {}).get("fastNewsList")
    if not isinstance(rows, list):
        raise SourceError(f"em: no fastNewsList (code {j.get('code')})")
    # stockList entries look like "1.600519" / "0.000001": market 0/1 are A shares, others (90.BK, 150.) are not
    return [{"item_id": str(x["code"]), "title": x.get("title") or "", "body": x.get("summary") or "", "url": None,
             "source_published_at": _cst(x.get("showTime")), "source_modified_at": None,
             "codes": _codes([s for s in x.get("stockList") or [] if str(s).split(".")[0] in ("0", "1")]),
             "comments": x.get("pinglun_Num"), "level": None} for x in rows if x.get("code")]


def cls_params(now: float) -> dict:
    p = {"app": "CailianpressWeb", "category": "", "last_time": int(now), "os": "web", "refresh_type": "1", "rn": "20", "sv": "8.4.6"}
    p["sign"] = hashlib.md5(hashlib.sha1(urlencode(p).encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
    return p


def fetch_cls(c: httpx.Client) -> list[dict]:
    j = _json(c.get("https://www.cls.cn/v1/roll/get_roll_list", params=cls_params(time.time()),
                    headers={"Referer": "https://www.cls.cn/telegraph"}))
    if str(j.get("errno")) != "0":
        raise SourceError(f"cls errno {j.get('errno')}: {j.get('msg')}")
    rows = (j.get("data") or {}).get("roll_data")
    if not isinstance(rows, list):
        raise SourceError("cls: no roll_data")
    out = []
    for x in rows:
        stocks = x.get("stock_list") or []
        out.append({"item_id": str(x["id"]), "title": x.get("title") or "", "body": x.get("content") or x.get("brief") or "",
                    "url": x.get("shareurl"), "source_published_at": _utc(x.get("ctime")), "source_modified_at": _utc(x.get("modified_time")),
                    "codes": _codes([s.get("StockID", "") if isinstance(s, dict) else s for s in stocks]),
                    "comments": x.get("comment_num"), "level": x.get("level")})
    return out


SOURCES: dict[str, tuple[Callable[[httpx.Client], list[dict]], int]] = {
    "ths.724": (fetch_ths, 10), "em.724": (fetch_em, 30), "cls.telegraph": (fetch_cls, 30)}


def guba_posts(c: httpx.Client, code: str) -> list[dict]:
    r = c.get(f"https://guba.eastmoney.com/list,{code}.html")
    if r.status_code != 200:
        raise SourceError(f"guba HTTP {r.status_code}")
    m = re.search(r"var article_list\s*=\s*(\{.*?\});", r.text, re.S)
    if not m:
        raise SourceError("guba: no article_list")
    return json.loads(m.group(1)).get("re") or []


def guba_since(posts: list[dict], since: datetime) -> dict:
    new = [p for p in posts if (_cst(p.get("post_publish_time")) or since) > since]
    return {"new_posts": len(new), "new_authors": len({p.get("user_id") for p in new if p.get("user_id")}),
            "titles": [str(p.get("post_title") or "")[:80] for p in new],
            "comments_on_new": sum(int(p.get("post_comment_count") or 0) for p in new)}


# --- watch list shared with intraday.py ----------------------------------------------------------

def watch_path(root: Path) -> Path:
    return root / "watch.json"


def update_watch(path: Path, codes: list[str], now: datetime, minutes: int = WATCH_MINUTES, cap: int = WATCH_CAP) -> dict[str, str]:
    """Merge `codes` with expiry now + minutes, drop expired ones, keep the `cap` latest-expiring; atomic write.
    Expiries are UTC ISO strings, so they compare as text."""
    now = now.astimezone(timezone.utc)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        current = {}
    expire = (now + timedelta(minutes=minutes)).isoformat()
    merged = {c: e for c, e in current.items() if isinstance(e, str) and e > now.isoformat()}
    for code in codes:
        merged[code] = max(expire, merged.get(code, ""))
    kept = dict(sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:cap])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return kept


def read_watch(path: Path, now: datetime, cap: int = WATCH_CAP) -> set[str]:
    """Unexpired watch codes; any problem reading the file means an empty set, never an exception."""
    now = now.astimezone(timezone.utc)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        live = sorted(((e, c) for c, e in data.items() if isinstance(e, str) and e > now.isoformat()), reverse=True)
        return {c for _, c in live[:cap]}
    except (OSError, ValueError, AttributeError):
        return set()


# --- recorder ------------------------------------------------------------------------------------

@dataclass
class Source:
    name: str
    fetch: Callable[[httpx.Client], list[dict]]
    every: int
    last: float = 0.0
    fails: int = 0
    stopped: str | None = None
    polls: int = 0
    new: int = 0
    revisions: int = 0


@dataclass
class NewsRecorder:
    day_dir: Path
    root: Path
    client: httpx.Client
    sources: list[Source]
    hashes: dict[tuple[str, str], str] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    guba_rows: list[dict] = field(default_factory=list)
    guba_queue: list[tuple[float, str, str, int, datetime]] = field(default_factory=list)  # due, item, code, offset, since
    guba_last: float = 0.0
    stats: dict[str, Any] = field(default_factory=lambda: {"guba_ok": 0, "guba_failed": 0, "guba_dropped": 0, "watch_updates": 0,
                                                           "errors": []})
    last_flush: float = field(default_factory=time.monotonic)
    seq: int = 0

    def __post_init__(self) -> None:
        (self.day_dir / "raw").mkdir(parents=True, exist_ok=True)
        for sub in ("messages", "guba"):
            (self.day_dir / sub).mkdir(exist_ok=True)
        self.resume()

    def resume(self) -> None:
        """A restart on the same day reloads the latest content hash of every item already recorded, so a source's
        backlog is not stored again as new items (with a later received_at) and cannot trigger watch or guba."""
        files = sorted((self.day_dir / "messages").glob("part-*.parquet"))
        if not files and (self.day_dir / "messages.parquet").exists():
            files = [self.day_dir / "messages.parquet"]
        frames = []
        for p in files:
            try:
                frames.append(pl.read_parquet(p, columns=["source_id", "item_id", "content_hash", "received_at"]))
            except (OSError, pl.exceptions.PolarsError) as exc:  # a part cut off by a kill: the rest still count
                self.error(f"resume {p.name}: {exc!r}")
        if not frames:
            return
        latest = pl.concat(frames).sort("received_at").unique(subset=["source_id", "item_id"], keep="last")
        self.hashes |= {(r[0], r[1]): r[2] for r in latest.select("source_id", "item_id", "content_hash").iter_rows()}
        self.stats["resumed_items"] = latest.height
        LOG.info("news: resumed %d items recorded earlier today", latest.height)

    def error(self, text: str) -> None:
        if len(self.stats["errors"]) < 50:
            self.stats["errors"].append(f"{datetime.now(CST):%H:%M:%S} {text}"[:300])

    def poll(self, s: Source, now_mono: float) -> None:
        s.last, s.polls = now_mono, s.polls + 1
        try:
            items = s.fetch(self.client)
        except (SourceError, httpx.HTTPError) as exc:
            s.fails += 1
            LOG.warning("news: %s failed: %s", s.name, exc)
            if s.fails >= BREAK_AFTER:
                s.stopped = f"{datetime.now(CST):%H:%M:%S} {exc}"
                self.error(f"{s.name} stopped: {exc}")
            return
        s.fails = 0
        received = datetime.now(timezone.utc)
        with gzip.open(self.day_dir / "raw" / f"{s.name}.jsonl.gz", "at", encoding="utf-8") as fh:
            fh.write(json.dumps({"received_at": received.isoformat(), "items": items}, ensure_ascii=False, default=str) + "\n")
        for it in items:
            digest = hashlib.sha256(f"{it['title']}\n{it['body']}".encode("utf-8")).hexdigest()
            key = (s.name, it["item_id"])
            old = self.hashes.get(key)
            if old == digest:
                continue
            self.hashes[key] = digest
            first = old is None
            s.new += first
            s.revisions += not first
            self.rows.append({"source_id": s.name, **it, "content_hash": digest, "revision_id": f"{it['item_id']}@{digest[:12]}",
                              "supersedes": None if first else f"{it['item_id']}@{old[:12]}", "received_at": received})
            if first and self.primed(s):
                self.on_new(s.name, it["item_id"], it["codes"], received)

    def primed(self, s: Source) -> bool:
        """The first poll of a source returns its backlog: recorded, but it does not trigger watch or guba."""
        return s.polls > 1

    def on_new(self, source: str, item_id: str, codes: list[str], received: datetime) -> None:
        main = [c for c in codes if c.startswith(MAIN_PREFIX)][:PER_MESSAGE]
        if not main:
            return
        try:
            update_watch(watch_path(self.root), main, received)
            self.stats["watch_updates"] += 1
        except OSError as exc:
            self.error(f"watch write: {exc!r}")
        base = time.monotonic()
        for offset in GUBA_OFFSETS:
            for code in main:
                if len(self.guba_queue) >= GUBA_BACKLOG:
                    self.stats["guba_dropped"] += 1
                    continue
                self.guba_queue.append((base + offset, f"{source}:{item_id}", code, offset, received))
        self.guba_queue.sort(key=lambda x: x[0])

    def guba_step(self, now_mono: float) -> None:
        if not self.guba_queue or self.guba_queue[0][0] > now_mono or now_mono - self.guba_last < GUBA_PACE:
            return
        _, item, code, offset, since = self.guba_queue.pop(0)
        self.guba_last = now_mono
        row = {"source_item": item, "code": code, "offset_s": offset, "since": since, "ok": False,
               "new_posts": None, "new_authors": None, "titles": [], "comments_on_new": None}
        try:
            posts = guba_posts(self.client, code)
            row |= guba_since(posts, since) | {"ok": True}
            self.stats["guba_ok"] += 1
        except (SourceError, httpx.HTTPError, ValueError) as exc:
            self.stats["guba_failed"] += 1
            LOG.warning("news: guba %s failed: %s", code, exc)
        row["fetched_at"] = datetime.now(timezone.utc)
        self.guba_rows.append(row)

    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_flush < FLUSH_EVERY:
            return
        self.last_flush = time.monotonic()
        for sub, rows, schema in (("messages", self.rows, MESSAGE_SCHEMA), ("guba", self.guba_rows, GUBA_SCHEMA)):
            if rows:
                self.seq += 1
                frame = pl.DataFrame([{k: r.get(k) for k in schema} for r in rows], schema=schema)
                frame.write_parquet(self.day_dir / sub / f"part-{datetime.now(CST):%H%M%S}-{self.seq:04d}.parquet")
                rows.clear()

    def finish(self, reason: str) -> None:
        self.flush(force=True)
        for sub in ("messages", "guba"):
            parts = sorted((self.day_dir / sub).glob("part-*.parquet"))
            if parts:
                pl.concat([pl.read_parquet(p) for p in parts], how="vertical_relaxed").write_parquet(self.day_dir / f"{sub}.parquet")
        summary = {"end": reason, "finished_at": datetime.now(CST).isoformat(timespec="seconds"),
                   "sources": {s.name: {"polls": s.polls, "new": s.new, "revisions": s.revisions, "stopped": s.stopped} for s in self.sources},
                   **{k: v for k, v in self.stats.items()}, "guba_pending": len(self.guba_queue)}
        (self.day_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


def output_root() -> Path:
    override = os.getenv("PULSE_NEWS_DIR")
    return Path(override) if override else settings.data_dir / "news"


def run(once: bool = False) -> int:
    today = datetime.now(CST).date()
    if not once and not load_calendar().is_trading_day(today):
        LOG.info("news: %s is not a trading day", today)
        return 0
    root = output_root()
    day_dir = root / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(day_dir / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    reason = "stop time"
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=False) as client:
        rec = NewsRecorder(day_dir, root, client, [Source(n, f, e) for n, (f, e) in SOURCES.items()])
        try:
            while True:
                now = datetime.now(CST)
                if once and all(s.polls for s in rec.sources):
                    reason = "once"
                    break
                if not once and now.time() >= STOP_AT:
                    break
                mono = time.monotonic()
                for s in rec.sources:
                    if s.stopped is None and mono - s.last >= s.every:
                        try:
                            rec.poll(s, mono)
                        except Exception as exc:  # one bad answer must not end the day
                            LOG.exception("news: poll %s failed", s.name)
                            rec.error(f"{s.name}: {exc!r}")
                try:
                    rec.guba_step(mono)
                    rec.flush()
                except Exception as exc:
                    LOG.exception("news: guba or flush failed")
                    rec.error(f"guba/flush: {exc!r}")
                if all(s.stopped for s in rec.sources):
                    reason = "all sources stopped"
                    break
                time.sleep(0.5)
        finally:
            rec.finish(reason)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("run", "once"))
    args = ap.parse_args(argv)
    return run(once=args.command == "once")


if __name__ == "__main__":
    sys.exit(main())
