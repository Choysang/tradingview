"""Batch 8 worker CLI: E099 (Lhasa / one-day-seat FLAG as a short book's entry filter and next-day exit); pre-registration 2026-11-23, first decision day 2026-11-24.

Usage (from pulse3/)::

    uv run python -m pulse.workers.batch8 forward --date 2026-11-24 [--as-of ISO]   # decide every pending decision day up to the date, write observable outcomes and the paper overlay
    uv run python -m pulse.workers.batch8 judge [--today YYYY-MM-DD]                # REFUSES (exit 3, nothing written) before 60 sessions and 150 FLAG samples; else writes the judgement once
    uv run python -m pulse.workers.batch8 status [--json]                           # the same figures without writing anything
    uv run python -m pulse.workers.batch8 history [--no-publish] [--since D] [--until D]   # one-off robustness 2023-11-01 .. 2026-11-20; report only, never decides

This is a NEW worker beside batch7 (not an extension): different dataset, hypotheses, seed and sample rule; it reuses batch 7's raw-bar windows and exact limit flags and batch 6's fills,
matched baseline and block bootstrap. ``forward`` reads pulse3's own data only (raw daily bars, ``intel seats``, the fwdlog paper rows; no vendor request) and appends rows; a row is never
rewritten, so a repeated run is a no-op.
Exit codes: 0 rows appended / printed / judged; 2 a step failed; 3 nothing to do yet (before 2026-11-24, nothing pending, waiting for data, or ``judge`` refused).
Environment: ``PULSE_DSN``, ``PULSE_LAKE``.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.contracts import to_wire


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.batch8")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("forward", help="decide pending decision days, write observable outcomes and the paper overlay")
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("judge", help="refuse, or write the pre-registered judgement once")
    p.add_argument("--today", default=None)
    p = sub.add_parser("status", help="progress without writing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("history", help="robustness over 2023-11-01 .. 2026-11-20 (never decides)")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.add_argument("--as-of", default=None)
    return ap


def _as_of(v: str | None) -> datetime:
    if not v:
        return datetime.now(timezone.utc)
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SystemExit("--as-of needs a timezone offset")
    return t


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    from pulse.application import bf_batch8_reads as RD
    from pulse.application.bf_batch8_service import Batch8Service

    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        svc = Batch8Service(conn, LakeStore(a.lake or cfg.lake_root))
        if a.cmd == "forward":
            out = svc.forward(date.fromisoformat(a.date), as_of=_as_of(a.as_of))
            print(json.dumps(to_wire(out), ensure_ascii=False, default=str))
            return 0 if out.get("appended") else 3
        if a.cmd == "judge":
            ov = svc.judge(today=date.fromisoformat(a.today) if a.today else None)
            print(RD.describe(ov))
            return 3 if ov["cut"]["state"] == "refused" else 0
        if a.cmd == "status":
            ov = svc.status()
            print(json.dumps(to_wire(ov), ensure_ascii=False, default=str) if a.json else RD.describe(ov))
            return 0
        kw = {}
        if a.since:
            kw["since"] = date.fromisoformat(a.since)
        if a.until:
            kw["until"] = date.fromisoformat(a.until)
        out = svc.history(as_of=_as_of(a.as_of), publish=not a.no_publish, **kw)
        print(json.dumps(to_wire(out), ensure_ascii=False, default=str))
        return 0 if out.get("status") == "ok" else 2
    except Exception:                                        # a failed step is reported with its trace and exit 2 (the schedule continues)
        traceback.print_exc()
        return 2
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
