"""Forward-log worker CLI (lane fwdlog): the daily step of E077 / E078 / E089 and the intraday paper replay (triggers v2.1), summary, reconciliation with the old logs.

Usage (from pulse3/)::

    uv run python -m pulse.workers.fwdlog run --date 2026-11-17 [--as-of ISO] [--only E077,E078,E089,PAPER]   # decide every session up to the date that has no row yet
    uv run python -m pulse.workers.fwdlog summary                                                         # the old scripts' summaries (pre-registered metrics, "pending until N")
    uv run python -m pulse.workers.fwdlog reconcile --old <folder> [--tolerance 0.0015]                    # compare with the old system's output for every common day

``run`` reads the imported recordings (lake datasets of the live lane: import the session first with ``pulse.workers.ingest legacy-intraday`` /
``legacy-ext``) and appends rows; a decided session is never rewritten, so a repeated run is a no-op. It never writes to the legacy tree and makes no network
request. ``reconcile`` prints every difference with a reason and exits 1 when one has no reason (an unexplained difference means not done).
Exit codes: 0 ok; 1 reconcile found an unexplained difference; 2 a session failed (see errors in the JSON); 3 no recordings imported.
Environment: ``PULSE_DSN``, ``PULSE_LAKE``, ``PULSE_LEGACY_DATA`` (old recorder tree, read only), ``PULSE_FWDLOG_EVENTS`` (old calendar store, read only).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.contracts import to_wire


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.fwdlog")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    ap.add_argument("--legacy-data", default=None, help="root of the old recorder data (default PULSE_LEGACY_DATA or C:/pulse/data)")
    ap.add_argument("--bars-dsn", default=None, help="read the daily bars from another database (local reconciliation only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="decide every session up to --date that has no row yet")
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", default=None, help="point-in-time cut-off (ISO instant with offset); default now")
    p.add_argument("--only", default=None, help="comma list of E077,E078,E089,PAPER")
    sub.add_parser("summary", help="pre-registered metrics of every experiment, provisional until its decision day")
    p = sub.add_parser("reconcile", help="compare with the old system's output for every common day")
    p.add_argument("--old", required=True, help="folder with e077_tail.jsonl, e078_halfhour.jsonl, e089_state.jsonl and paper/<day>/{opportunities,sells}.parquet")
    p.add_argument("--tolerance", default="0.0015", help="absolute tolerance of a rounded percent figure (float vs decimal rounding)")
    p.add_argument("--from", dest="start", default=None)
    p.add_argument("--to", dest="end", default=None)
    return ap


def _service(a):
    from pulse.application import bf_fwdlog_paper as PA
    from pulse.application.bf_fwdlog_service import FwdlogService

    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    loader = hist = None
    if a.bars_dsn:
        bconn = db.connect(a.bars_dsn, autocommit=True)
        loader = lambda c, as_of, until, since: PA.load_bars(bconn, as_of, until, since)      # noqa: E731
        hist = lambda c, as_of, until, since: PA.load_hist_cents(bconn, as_of, until, since)  # noqa: E731
    rec = LegacyRecordings(a.legacy_data) if a.legacy_data else LegacyRecordings()
    return conn, FwdlogService(conn, LakeStore(a.lake or cfg.lake_root), rec, bars_loader=loader, hist_loader=hist)


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    conn, svc = _service(a)
    try:
        if a.cmd == "run":
            as_of = datetime.fromisoformat(a.as_of.replace("Z", "+00:00")) if a.as_of else datetime.now(timezone.utc)
            if as_of.tzinfo is None:
                raise SystemExit("--as-of needs a timezone offset")
            rep = svc.run(date.fromisoformat(a.date), as_of=as_of, only=a.only.split(",") if a.only else None)
            print(json.dumps(rep.wire(), ensure_ascii=False))
            return 3 if not rep.sessions else 2 if rep.errors else 0
        if a.cmd == "summary":
            from pulse.application import bf_fwdlog_reads as R
            print(json.dumps(to_wire(R.summaries(svc)), ensure_ascii=False))
            return 0
        from pulse.application import bf_fwdlog_reconcile as RC
        out = RC.reconcile(svc, Path(a.old), tolerance=a.tolerance, start=a.start, end=a.end)
        print(RC.render(out))
        return 1 if out["unexplained"] else 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
