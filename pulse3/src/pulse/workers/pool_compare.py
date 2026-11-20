"""Pool comparison worker: the vendor's limit pools against pulse3's estimated seal facts for one session (READ ONLY).

Usage (from pulse3/)::

    uv run python -m pulse.workers.pool_compare --date 2026-11-13 [--json]

Needs both datasets of the session: ``limit_pools_eastmoney`` (recorded with ``intraday-record --limit-pools``) and ``intraday_seal_facts``
(``seal-facts``). It writes nothing and prints counts and the sets that differ; it does not decide anything. Exit codes: 0 report printed,
1 a dataset is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application.bf_live_service import LiveService
from pulse.application.intraday_pools import compare_pools, render


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pulse.workers.pool_compare")
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="session date (default: today, Asia/Shanghai)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    a = ap.parse_args(argv)
    cfg = config.load()
    day = a.date or datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        report = compare_pools(LiveService(conn, LakeStore(a.lake or cfg.lake_root)), day)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, default=str, indent=1) if a.json else render(report))
    return 1 if "sealed_at_close" not in report else 0


if __name__ == "__main__":
    sys.exit(run())
