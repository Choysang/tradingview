"""Native extended-intraday recorder CLI (step 2, #104/#186): ETFs, indices, CFFEX index futures, A50, Hang Seng futures, CNH, gold, crude.

Usage (from pulse3/)::

    uv run python -m pulse.workers.rec_ext record --until 15:05 [--cffex-seconds 300] [--date 2026-11-20]   # one session on the old schedule
    uv run python -m pulse.workers.rec_ext once [--dry]                                                     # one poll of every host, now

NOT scheduled before the M6 cutover: the old system records the same session meanwhile and a second recorder would double the requests from
the server IP (``deploy/windows/run_ext_record.ps1`` exists, unregistered). ``record`` publishes into the dataset ``ext_quote_snapshots`` (the one
``legacy-ext`` fills today), so run it INSTEAD of ``legacy-ext`` for a day, never both. ``once`` publishes kind "once" rows into the day's dataset
unless ``--dry`` (prints the parsed rows, publishes nothing).
Exit codes: 0 ok, 1 failure, 2 a host was closed for the run (it refused us, or 20 polls in a row failed; the others were recorded), 3 nothing could be reached.
Environment: ``PULSE_DSN``, ``PULSE_LAKE``.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources import ext_quotes as X
from pulse.application.bf_ext_record import STOP_AT, Cadence, ExtRecorder
from pulse.application.bf_live_service import LiveService


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.rec_ext")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("record", "once"):
        p = sub.add_parser(name)
        p.add_argument("--date", type=date.fromisoformat, default=None, help="session date (default: today, Asia/Shanghai)")
        p.add_argument("--tencent-seconds", type=int, default=60, help="seconds between Tencent polls (default 60, the old cadence)")
        p.add_argument("--cffex-seconds", type=int, default=60, help="seconds between CFFEX polls (default 60, the old cadence; 300 per the E089 note)")
        if name == "record":
            p.add_argument("--until", type=time.fromisoformat, default=STOP_AT, help="hard stop, Asia/Shanghai time (default 16:10)")
            p.add_argument("--flush-seconds", type=float, default=900.0)
        else:
            p.add_argument("--dry", action="store_true", help="print the parsed rows, publish nothing")
    return ap


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request would bury the log
    a = _parser().parse_args(argv)
    cfg = config.load()
    day = a.date or datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    fetcher = X.ExtFetcher()
    try:
        svc = LiveService(conn, LakeStore(a.lake or cfg.lake_root))
        svc.bootstrap()
        cadence = Cadence(tencent_seconds=a.tencent_seconds, cffex_seconds=a.cffex_seconds,
                          **({"flush_seconds": a.flush_seconds} if a.cmd == "record" else {}))
        rec = ExtRecorder(svc, fetcher, cadence=cadence)
        if a.cmd == "record":
            s, rows = rec.run(day, a.until), []
        else:
            s, rows = rec.once(day, publish=not a.dry)
        for r in rows:
            print(f"{r['symbol']:<12}{r['asset']:<10}{str(r['contract'] or ''):<8}{r['price']}\t{r['prev_close']} ({r['prev_kind']})\t{r['quote_ts']}\t{r['name']}")
        print(s.render())
        if s.counts.get("hosts_blocked"):
            return 2
        if s.counts.get("http_requests") and not s.counts.get("rows_polled"):
            return 3
        return 0
    finally:
        fetcher.close()
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
