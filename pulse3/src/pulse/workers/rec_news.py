"""Native 7x24 news recorder CLI (lane C). NOT scheduled before the M6 cutover: the old system records meanwhile.

Why: a thin command line over :class:`pulse.application.native_news_recorder.NewsRecorder` with the PostgreSQL sink. It polls
Tonghuashun (10 s), Eastmoney (30 s) and CLS (30 s) 7x24, records message versions as document revisions of the source
``native_news_recorder`` and extracts claims after each poll that stored something (the alert scan is ``pulse.workers.alerts scan``).

Usage (from pulse3/)::

    uv run python -m pulse.workers.rec_news record --until 22:00          # the old recorder's day: start 08:00, stop 22:00, weekdays
    uv run python -m pulse.workers.rec_news once                           # every feed once, any time (smoke run)

Files of the day (raw answers of polls that produced new versions, ``summary.json``, ``blocked.json``) go to
``PULSE_NATIVE_NEWS_DIR`` (default ``<lake>/../native_news/<YYYY-MM-DD>``), never into the old system's data tree.
Exit codes: 0 ok, 1 failure, 2 a host blocked us (that host is stopped for the day, the others continue; nothing is retried).
Not covered: scheduling, guba follow-ups, alert scan and delivery.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources import native_news
from pulse.adapters.sources.http import RateLimitedClient
from pulse.application.native_news_recorder import NewsRecorder, day_dir_for, default_feeds
from pulse.application.native_news_service import NativeNewsWriter

CST = ZoneInfo("Asia/Shanghai")
ENV_DIR = "PULSE_NATIVE_NEWS_DIR"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.rec_news")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    ap.add_argument("--news-dir", default=None, help=f"root of the day files (default ${ENV_DIR} or <lake>/../native_news)")
    ap.add_argument("--min-interval", type=float, default=1.0, help="seconds between requests to one host (default 1.0, never lower in production)")
    ap.add_argument("--no-claims", action="store_true", help="documents only; skip claim extraction")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record", help="poll until --until (weekdays)")
    p.add_argument("--from", dest="start", type=time.fromisoformat, default=time.fromisoformat(native_news.START_AT),
                   help="do not poll before this Asia/Shanghai time (default 08:00)")
    p.add_argument("--until", type=time.fromisoformat, default=time.fromisoformat(native_news.STOP_AT), help="stop at this Asia/Shanghai time (default 22:00)")
    p.add_argument("--any-day", action="store_true", help="also record on Saturdays and Sundays (the old recorder skipped non-trading days)")
    sub.add_parser("once", help="poll every feed once")
    return ap


def news_root(a, lake_root: Path) -> Path:
    return Path(a.news_dir or os.environ.get(ENV_DIR) or lake_root.parent / "native_news")


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    a = _parser().parse_args(argv)
    today = datetime.now(timezone.utc).astimezone(CST).date()
    if a.cmd == "record" and not a.any_day and today.weekday() >= 5:
        print(f"{today} is a weekend: nothing to record (--any-day overrides)")
        return 0
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    lake = LakeStore(a.lake or cfg.lake_root)
    clients = {origin: RateLimitedClient(min_interval_seconds=a.min_interval) for origin in native_news.POLL_EVERY_SECONDS}   # one per host
    try:
        writer = NativeNewsWriter(conn, lake)
        writer.bootstrap()
        day_dir = day_dir_for(news_root(a, Path(a.lake) if a.lake else cfg.lake_root), today)
        rec = NewsRecorder(day_dir, writer, default_feeds(clients), run_claims=not a.no_claims)
        reason = rec.run(start=a.start, until=a.until) if a.cmd == "record" else rec.once()
        for st in rec.states:
            print(f"{st.feed.origin}: polls={st.polls} new={st.new} revisions={st.revisions} stopped={st.stopped}")
        print(f"end: {reason}; claims: {rec.claim_totals}; requests: {sum(c.requests_made for c in clients.values())}")
        for e in rec.errors:
            print(f"error: {e}", file=sys.stderr)
        return 2 if rec.blocked else 0
    finally:
        for c in clients.values():
            c.close()
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
