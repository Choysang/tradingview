"""LHB seats comparison worker: the old warehouse's dragon-tiger seats against pulse3's own, per session (READ ONLY).

Usage (from pulse3/)::

    uv run python -m pulse.workers.lhb_compare --since 2026-11-10 --until 2026-11-21 [--legacy-warehouse PATH] [--json]

``--legacy-warehouse`` is the old ``warehouse\\live`` directory holding ``lhb_stocks`` and ``lhb_seats`` (default: the env var
``PULSE_LEGACY_WAREHOUSE``, on the server ``C:\\pulse\\app\\backend\\data\\warehouse\\live``). The new side is read from PostgreSQL
(``intelligence.seat_report`` rows of ``intel seats``). The worker makes no HTTP request and writes nothing; the database session is
read-only. Per session it prints the stocks listed on one side only, the seat rows and amounts that differ (tolerance 0.5% or 1 yuan),
institution and Lhasa seat counts per side and a SAME / DIFF verdict, and counts the newest consecutive SAME sessions (10 = the old
task PulseLhbDaily may be retired, docs/m6/LEGACY_RETIREMENT.md).
Exit codes: 0 every session SAME, 1 at least one DIFF or no session found, 2 an input is unusable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulse import config
from pulse.adapters.postgres import db
from pulse.application.lhb_compare import NEW_SOURCE_ID, LhbCompareError, render, run_compare

DEFAULT_WINDOW_DAYS = 20


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pulse.workers.lhb_compare")
    ap.add_argument("--since", type=date.fromisoformat, default=None, help=f"first session (default: {DEFAULT_WINDOW_DAYS} days before --until)")
    ap.add_argument("--until", type=date.fromisoformat, default=None, help="last session (default: today, Asia/Shanghai)")
    ap.add_argument("--legacy-warehouse", default=None, help="old warehouse 'live' directory (default: PULSE_LEGACY_WAREHOUSE)")
    ap.add_argument("--source-id", default=NEW_SOURCE_ID, help="source id of the new side's seat reports")
    ap.add_argument("--max-lines", type=int, default=30, help="detail lines per DIFF session in the text report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dsn", default=None)
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", **({} if stream.isatty() else {"encoding": "utf-8"}))
    cfg = config.load()
    until = a.until or datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
    since = a.since or until - timedelta(days=DEFAULT_WINDOW_DAYS)
    wh = a.legacy_warehouse or os.environ.get("PULSE_LEGACY_WAREHOUSE") or (str(cfg.legacy_warehouse) if cfg.legacy_warehouse else None)
    if not wh:
        print("no old warehouse: pass --legacy-warehouse or set PULSE_LEGACY_WAREHOUSE", file=sys.stderr)
        return 2
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        conn.execute("SET default_transaction_read_only = on")
        result = run_compare(conn, Path(wh), since, until, a.source_id)
    except LhbCompareError as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(result.to_dict(), ensure_ascii=False, default=str, indent=1) if a.json else render(result, a.max_lines))
    return 0 if result.all_same else 1


if __name__ == "__main__":
    sys.exit(run())
