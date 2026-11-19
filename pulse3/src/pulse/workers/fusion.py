"""Fusion worker CLI (lane ``fusion``, legacy triage batch 3): the frozen static40 score, its books and forward experiments.

Usage (from pulse3/, after the daily data import of ``run_daily.ps1``)::

    uv run python -m pulse.workers.fusion bootstrap                  # one time: score the whole history, seed the datasets
    uv run python -m pulse.workers.fusion daily [--as-of ISO]        # the daily step: score, gate, promote, books, monitor, X20, OS20
    uv run python -m pulse.workers.fusion show                       # state of the datasets
    uv run python -m pulse.workers.fusion register                   # record the forward experiments in the research registry
    uv run python -m pulse.workers.fusion audit-level-safety         # re-run the level check and list differences with the frozen data
    uv run python -m pulse.workers.fusion reconcile --pc D:/.../.work/lab [--scores-cache f.parquet]   # compare with the PC's files

Exit codes: 0 done, 1 failure, 2 the run was refused and recorded (coverage, history, reproduction gate).
Not covered: scheduling (``deploy/windows`` is the integrator's), mining of new factors.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.fusion")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("bootstrap", "daily"):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default=None, help="point-in-time cutoff (ISO instant with offset); default now")
        p.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
        p.add_argument("--quiet", action="store_true")
    sub.add_parser("show")
    sub.add_parser("register")
    p = sub.add_parser("audit-level-safety")
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("reconcile")
    p.add_argument("--pc", required=True, help="the PC's .work/lab directory (private; read only)")
    p.add_argument("--as-of", default=None)
    p.add_argument("--scores-cache", default=None, help="parquet file: reuse these pulse3 scores instead of recomputing; written when missing")
    return ap


def _t(s: str | None) -> datetime | None:
    return None if not s else datetime.fromisoformat(s.replace("Z", "+00:00"))


def _progress(quiet: bool):
    t0 = time.perf_counter()

    def p(i: int, n: int, name: str) -> None:
        if not quiet and (i % 40 == 0 or i == n):
            print(f"  factors {i}/{n} {name} {time.perf_counter() - t0:.0f}s", flush=True)

    return p


def run(argv: list[str] | None = None) -> int:
    from pulse.application import bf_fusion_service as S

    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    lake = LakeStore(cfg.lake_root)
    try:
        svc = S.FusionService(conn, lake)
        if a.cmd in ("bootstrap", "daily"):
            fn = svc.bootstrap if a.cmd == "bootstrap" else svc.daily
            try:
                out = fn(_t(a.as_of), progress=_progress(a.quiet), dry_run=a.dry_run)
            except S.FusionStop as exc:
                print(json.dumps({"status": "stopped", "code": exc.code, "detail": exc.detail}, ensure_ascii=False))
                return 2
            print(json.dumps(out, ensure_ascii=False, default=str, indent=1))
            return 0
        if a.cmd == "show":
            from pulse.application import bf_fusion_read as R

            print(json.dumps(R.read_status(conn, lake), ensure_ascii=False, default=str, indent=1))
            return 0
        if a.cmd == "register":
            from pulse.application import bf_fusion_registry as G

            print(json.dumps(G.register_forward_experiments(conn, lake), ensure_ascii=False, default=str, indent=1))
            return 0
        if a.cmd == "audit-level-safety":
            from pulse.application import bf_fusion_reconcile as C

            print(json.dumps(C.audit_level_safety(conn, lake, _t(a.as_of)), ensure_ascii=False, indent=1))
            return 0
        if a.cmd == "reconcile":
            from pulse.application import bf_fusion_reconcile as C

            return C.reconcile(conn, lake, Path(a.pc), _t(a.as_of), scores_cache=Path(a.scores_cache) if a.scores_cache else None)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
