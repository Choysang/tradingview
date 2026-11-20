"""Batch 7 worker CLI: E097 (H1 prime, exact limit-up retest of E093 H1) and E098 (H4 prime, institution net buying as risk); pre-registration 2026-11-20.

Usage (from pulse3/)::

    uv run python -m pulse.workers.batch7 forward --date 2026-11-23 [--as-of ISO]   # decide every pending decision day up to the date and write the outcomes that are observable
    uv run python -m pulse.workers.batch7 judge [--today YYYY-MM-DD]                # progress, or the judgement of section 2.3 / 3 (written once when the sample condition holds)
    uv run python -m pulse.workers.batch7 summary [--json]                          # the same figures without writing anything
    uv run python -m pulse.workers.batch7 history [--no-publish] [--since YYYY-MM-DD]   # one-off E097 robustness over the raw history next to batch 6 section 7.4; never decides

``forward`` reads pulse3's own data only (raw daily bars: native refdata dataset first, legacy raw bars as the fallback; seats of ``intel seats``; no request to any vendor) and appends rows; a
row is never rewritten, so a repeated run is a no-op. A decision day with no data is recorded as such only after two later sessions have passed.
Exit codes: 0 rows appended / printed; 2 a step failed; 3 nothing to decide yet (before 2026-11-23, nothing pending, or waiting for data).
Environment: ``PULSE_DSN``, ``PULSE_LAKE``, ``PULSE_REFDATA_SOURCE`` (only the research adjusted bars of ``history`` follow it).
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
    ap = argparse.ArgumentParser(prog="pulse.workers.batch7")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("forward", help="decide pending decision days and write observable outcomes")
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("judge", help="progress or the pre-registered judgement")
    p.add_argument("--today", default=None)
    p = sub.add_parser("summary", help="progress without writing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("history", help="E097 robustness over the raw history (never decides)")
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--since", default=None, help="first session wanted (default 2022-12-01)")
    p.add_argument("--until", default=None, help="last decision day (default 2026-11-18, the day batch 6 section 7.4 ends)")
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
    from pulse.application import bf_batch7_reads as RD
    from pulse.application.bf_batch7_service import Batch7Service

    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        svc = Batch7Service(conn, LakeStore(a.lake or cfg.lake_root))
        if a.cmd == "forward":
            out = svc.forward(date.fromisoformat(a.date), as_of=_as_of(a.as_of))
            print(json.dumps(to_wire(out), ensure_ascii=False, default=str))
            return 0 if out.get("appended") else 3
        if a.cmd == "judge":
            ov = svc.judge(today=date.fromisoformat(a.today) if a.today else None)
            print(RD.describe(ov))
            return 0
        if a.cmd == "summary":
            ov = svc.summary()
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
