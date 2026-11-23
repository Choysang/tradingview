"""Batch 8 service: registration, the daily forward step, the judgement, the status and the historical robustness run, glued to the lake / registry.

Why: one place for the commands of ``pulse.workers.batch8``. It adds no rule (rules: ``domain.batch8``; steps: ``bf_batch8_forward`` / ``bf_batch8_reads`` / ``bf_batch8_history``).
Invariants: E099 is registered (shadow) before its first row; ``forward`` never evaluates a decision day before 2026-11-24; a repeated run appends nothing new; the frozen-prefix hash of the
pre-registration is checked on every command against the copy shipped as package data (fail closed: a missing or changed copy stops the run; a changed document is a new experiment).
Not covered: scheduling (``deploy/windows/run_fwdlog.ps1``), HTTP.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch8_forward as FW
from pulse.application import frozen_specs as FS
from pulse.application import bf_batch8_history as HI
from pulse.application import bf_batch8_reads as RD
from pulse.application import bf_batch8_registry as REG
from pulse.application.data_service import DataService
from pulse.domain.batch8 import rules as R


PreregistrationChanged = FS.FrozenSpecError
SPEC_FILE = Path(R.SPEC_PATH).name                    # shipped as package data in ``pulse/research_specs/``


def check_document(specs_dir: Path | None = None, repo_root: Path | None = None) -> str:
    """Fail-closed check of the frozen prefix against the pinned sha256: the packaged copy must exist and match (``docs/research`` is not deployed to the server), and the repository
    copy must match too when present. Returns the hash; raises ``FrozenSpecError`` otherwise (never None)."""
    return FS.verify(SPEC_FILE, R.SPEC_SHA256, R.frozen_hash, repo_path=R.SPEC_PATH, specs_dir=specs_dir, repo_root=repo_root)


class Batch8Service:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock=None, loader=None, flags_loader=None):
        self.conn, self.lake, self.loader, self.flags_loader = conn, lake, loader, flags_loader
        self.svc = DataService(conn, lake, clock=clock)

    def _anchor(self, as_of: datetime) -> dict:
        row = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id IN ('bars_native_raw_1d','bars_raw_1d') AND manifest_state='READY' AND ready_at <= %s "
                                "ORDER BY (dataset_id='bars_native_raw_1d') DESC, manifest_version DESC LIMIT 1", (as_of,)).fetchone()
        if row is None:
            raise RuntimeError("no READY raw daily bars manifest: nothing to anchor the registration to")
        return row

    def register(self, as_of: datetime) -> None:
        REG.ensure_registered(self.conn, self._anchor(as_of), now=as_of)

    def forward(self, day: date, *, as_of: datetime | None = None) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        if day < R.FORWARD_START:
            return {"through": day.isoformat(), "appended": False, "reason": f"before the first decision day {R.FORWARD_START}"}
        self.register(as_of)
        rep = FW.step(self.conn, self.lake, self.svc, day, as_of=as_of, loader=self.loader, flags_loader=self.flags_loader)
        out = rep.wire()
        out["appended"] = bool(rep.decided or rep.resolved or rep.overlay_days or rep.lost_days)
        return out

    def judge(self, *, today: date | None = None, as_of: datetime | None = None) -> dict:
        check_document()
        return RD.judge_and_store(self.svc, self.conn, self.lake, today, as_of)

    def status(self, today: date | None = None) -> dict:
        return RD.overview(self.conn, self.lake, today)

    def history(self, *, as_of: datetime | None = None, publish: bool = True, since: date = R.HISTORY_START, until: date = R.HISTORY_END) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        out = HI.run(self.conn, self.lake, as_of=as_of, since=since, until=until, flags=None)
        if publish and out.get("status") == "ok":
            self.register(as_of)
            man = HI.publish(self.svc, out, as_of=as_of)
            out["snapshot_id"] = str(man["snapshot_id"])
        return out
