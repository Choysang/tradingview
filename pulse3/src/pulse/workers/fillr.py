"""Fill-research lane CLI: run the queued custom playbook runs, factor series and analog studies (the API only queues them).

Usage (from pulse3/)::

    uv run python -m pulse.workers.fillr run [--max N]            # claim and run queued fillr jobs (custom_run, factor_series, analog_study)
    uv run python -m pulse.workers.fillr enqueue-factor-series    # queue the daily IC / long-short series of every completed factor experiment that has none
    uv run python -m pulse.workers.fillr show                     # job counts by state

The integrator schedules ``run`` (suggested: every 5 minutes; an empty queue costs one indexed query). Each finished job is printed as one JSON line
``{"job_run_id", "kind", "state"}``. Exit code 0 = done (individual job failures are recorded on the job, state ``failed``, and do not fail the command).
"""
from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application import bf_fillr_service as F


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.fillr")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--max", type=int, default=5, help="at most this many queued jobs")
    sub.add_parser("enqueue-factor-series")
    sub.add_parser("show")
    return ap


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        if a.cmd == "run":
            lake = LakeStore(cfg.lake_root)
            for _ in range(a.max):
                r = F.execute_pending(conn, lake)
                if r is None:
                    break
                print(json.dumps({"job_run_id": str(r[0]), "kind": r[1], "state": r[2]}), flush=True)
        elif a.cmd == "enqueue-factor-series":
            system = UUID(int=0)
            rows = conn.execute(F._FACTOR_EXP_SQL + " WHERE d.specification->>'catalog_id' LIKE 'FL:%%' AND r.run_state='completed'").fetchall()
            for r in rows:
                if F._series_artifact(conn, r["experiment_id"]) is None:
                    out = F.create_factor_series(conn, system, {"experiment_id": str(r["experiment_id"])}, F.utc_now())
                    print(json.dumps({"experiment_id": str(r["experiment_id"]), "state": out["state"]}), flush=True)
        else:
            print(json.dumps([dict(r) for r in conn.execute(
                "SELECT run_state, count(*) AS n FROM ops.job_run WHERE job_id=%s GROUP BY run_state ORDER BY run_state", (F.FILLR_JOB,)).fetchall()]))
        return 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
