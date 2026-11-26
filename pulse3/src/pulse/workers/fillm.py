"""CLI of the fill-market lane: ``python -m pulse.workers.fillm import-calendar [--path events.jsonl] | calendar-status``.

``import-calendar`` reads the old calendar store (read only) and publishes it as the dataset ``legacy_calendar_events`` when its bytes
changed. Schedule it after the old system's calendar sync (daily is enough: the store changes when the old sync runs).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db

ENV_EVENTS = "PULSE_FWDLOG_EVENTS"                      # the same variable the forward-log lane uses for the old store path
DEFAULT_EVENTS = "C:\\pulse3\\var\\legacy_calendar\\events.jsonl"      # frozen copy (retire_legacy.ps1); same default as bf_fwdlog_inputs


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.fillm")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-calendar")
    p.add_argument("--path", default=None, help=f"old store file (default: ${ENV_EVENTS} or {DEFAULT_EVENTS})")
    sub.add_parser("calendar-status")
    return ap


def run(argv: list[str] | None = None) -> int:
    from pulse.application import bf_fillm_calendar as C

    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    lake = LakeStore(cfg.lake_root)
    try:
        if a.cmd == "import-calendar":
            s = C.import_store(conn, lake, Path(a.path or os.environ.get(ENV_EVENTS) or DEFAULT_EVENTS))
            print(s.render())
            return 3 if s.counts.get("unreadable") else 0
        print(json.dumps(C.status(conn, lake), ensure_ascii=False, default=str, indent=1))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
