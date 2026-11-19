"""Lab lane CLI: import the frozen experiment ledger, run queued batch comparisons.

Usage (from pulse3/)::

    uv run python -m pulse.workers.lab import-ledger      # idempotent; refuses a ledger file whose sha256 differs from lab_data/ledger_manifest.json
    uv run python -m pulse.workers.lab run-batch [--max N] # claim and run queued batch-comparison jobs (the API also runs them in the background)
    uv run python -m pulse.workers.lab show                # imported ledger versions and queued / finished batch runs

``import-ledger`` is a one-off deploy step (and again whenever a new ledger version is added to the manifest); it writes only immutable
``ops.ext_artifact`` rows and never touches ``research.experiment*``. Exit code 0 = done, 2 = the ledger file did not match the manifest.
"""
from __future__ import annotations

import argparse
import json
import sys

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application import bf_lab_ledger as LED
from pulse.application import bf_lab_service as LAB


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.lab")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-ledger")
    p = sub.add_parser("run-batch")
    p.add_argument("--max", type=int, default=5, help="at most this many queued jobs")
    sub.add_parser("show")
    return ap


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        if a.cmd == "import-ledger":
            try:
                out: object = [r.__dict__ for r in LED.import_ledger(conn)]
            except LED.LedgerHashMismatch as e:
                print(json.dumps({"error": "LEDGER_HASH_MISMATCH", "message": str(e)}, ensure_ascii=False))
                return 2
        elif a.cmd == "run-batch":
            lake = LakeStore(cfg.lake_root)
            done = []
            for _ in range(a.max):
                r = LAB.execute_pending(conn, lake)
                if r is None:
                    break
                done.append({"job_run_id": str(r[0]), "state": r[1]})
            out = done
        else:
            out = {"imports": LED.list_imports(conn), "not_imported": LED.not_imported(conn),
                   "batch_runs": [dict(r) for r in conn.execute(
                       "SELECT run_state, count(*) AS n FROM ops.job_run WHERE job_id=%s GROUP BY run_state ORDER BY run_state", (LAB.LAB_JOB,)).fetchall()]}
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
