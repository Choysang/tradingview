"""E101 worker CLI: US macro releases (core PCE, CPI, payrolls, FOMC); pre-registration 2026-11-25, forward from 2026-11-26.

Usage (from pulse3/)::

    uv run python -m pulse.workers.e101 forward --date 2026-12-04 [--as-of ISO]   # record market rows, events, D0 / R outcomes that are observable
    uv run python -m pulse.workers.e101 plan d0 --date 2026-12-04 [--out-dir DIR]  # D0 report (14:30); exit 3 when no release has this D0
    uv run python -m pulse.workers.e101 plan r --date 2026-12-07 [--out-dir DIR]   # R report (09:00); exit 3 when no release has this R
    uv run python -m pulse.workers.e101 judge [--today YYYY-MM-DD]                  # REFUSES (exit 3) before 40 resolved events; else writes the H1 / H3 judgement once
    uv run python -m pulse.workers.e101 status [--json]
    uv run python -m pulse.workers.e101 history --calendar-file PATH [--no-publish]  # one-off robustness (H1, H3), report only

Reads pulse3's own data only (raw daily bars, the calendar); no vendor request. Rows are never rewritten.
Exit codes: 0 done; 2 a step failed; 3 nothing to do yet; 4 (plan) the previous session's bars are not ready.
Environment: ``PULSE_DSN``, ``PULSE_LAKE``.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.contracts import to_wire
from pulse.domain.e101 import rules as R


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.e101")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("forward")
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("plan")
    p.add_argument("kind", choices=["d0", "r"])
    p.add_argument("--date", default=None)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("judge")
    p.add_argument("--today", default=None)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("history")
    p.add_argument("--calendar-file", required=True)
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--as-of", default=None)
    return ap


def _as_of(v: str | None) -> datetime:
    if not v:
        return datetime.now(timezone.utc)
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SystemExit("--as-of needs a timezone offset")
    return t


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(R.CST).date()


def _print(x) -> None:
    print(json.dumps(to_wire(x), ensure_ascii=False, default=str))


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    from pulse.application import bf_e101_forward as FW
    from pulse.application.bf_e101_service import E101Service

    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        svc = E101Service(conn, LakeStore(a.lake or cfg.lake_root))
        if a.cmd == "forward":
            out = svc.forward(date.fromisoformat(a.date), as_of=_as_of(a.as_of))
            _print(out)
            return 0 if out.get("appended") else 3
        if a.cmd == "plan":
            out_dir = Path(a.out_dir) if a.out_dir else Path(a.lake or cfg.lake_root).parent / "reports" / "e101"
            out = svc.plan(a.kind, date.fromisoformat(a.date) if a.date else _today(), out_dir=out_dir, as_of=_as_of(a.as_of))
            _print(out)
            return 0 if out.get("appended") else 4 if out.get("not_ready") else 3
        if a.cmd == "judge":
            ov = svc.judge(date.fromisoformat(a.today) if a.today else _today())
            print(FW.describe(ov))
            return 3 if ov["h1h3"]["state"] == "refused" else 0
        if a.cmd == "status":
            ov = svc.status(_today())
            print(json.dumps(to_wire(ov), ensure_ascii=False, default=str) if a.json else FW.describe(ov))
            return 0
        out = svc.history(calendar_file=Path(a.calendar_file), as_of=_as_of(a.as_of), publish=not a.no_publish)
        _print(out)
        return 0 if out.get("status") == "ok" else 2
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
