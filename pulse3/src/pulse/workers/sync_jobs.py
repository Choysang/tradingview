"""Data-sync job worker: run the sync runs queued from the web (kinds sync_daily_bars, sync_legacy_import, sync_reconcile).

Usage (from pulse3/)::

    uv run python -m pulse.workers.sync_jobs run [--max N]    # claim and run queued sync jobs (default 1)
    uv run python -m pulse.workers.sync_jobs show             # run counts by kind and state

Only the three sync job ids are ever claimed; other queued jobs are left to their own workers. Each run goes through the same code as
``python -m pulse.workers.ingest`` (rate-limited client, 1 request per second per host, stop at the first block, never retried).
Each finished run is printed as one JSON line ``{"job_run_id", "kind", "state"}``. Exit code 0 = done (a failed run is recorded on the
run with its exit status and summary and does not fail the command). The integrator schedules ``run --max 1`` as its own
task (a sync can take hours; it must not sit inside the short queue runner); an empty queue costs one indexed query. While a run works a
heartbeat thread renews its 15-minute lease, so a killed worker frees its kind after one lease and the dead run is closed as ``LEASE_EXPIRED``.
"""
from __future__ import annotations

import argparse
import json
import sys

from pulse import config
from pulse.adapters.postgres import db
from pulse.application import sync_job_service as S


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.sync_jobs")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--max", type=int, default=1, help="at most this many queued runs")
    sub.add_parser("show")
    return ap


def run(argv: list[str] | None = None, runner: S.Runner = S.ingest_runner) -> int:
    a = _parser().parse_args(argv)
    dsn = a.dsn or config.load().dsn
    conn = db.connect(dsn, autocommit=True)
    try:
        if a.cmd == "run":
            for _ in range(a.max):
                r = S.execute_pending(conn, runner, connect=lambda: db.connect(dsn, autocommit=True))
                if r is None:
                    break
                print(json.dumps({"job_run_id": str(r[0]), "kind": r[1], "state": r[2]}), flush=True)
        else:
            print(json.dumps([dict(r) for r in conn.execute(
                "SELECT l.kind, r.run_state, count(*) AS n FROM ops.sync_job_log l JOIN ops.job_run r USING (job_run_id)"
                " GROUP BY l.kind, r.run_state ORDER BY l.kind, r.run_state").fetchall()]))
        return 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
