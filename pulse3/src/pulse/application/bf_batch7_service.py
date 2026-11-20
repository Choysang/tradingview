"""Batch 7 service: registration, the daily forward step, the judgement and the historical robustness run, glued to the lake / registry.

Why: one place for the commands of ``pulse.workers.batch7``. It adds no rule (rules: ``domain.batch7.rules``, steps: ``bf_batch7_forward`` / ``bf_batch7_reads`` / ``bf_batch7_history``).
Invariants: both experiments are registered (shadow) before their first row; ``forward`` never evaluates a decision day before 2026-11-23; a repeated run appends nothing new; the
frozen-prefix hash of the pre-registration is checked when the file is present (a changed document stops the run: a changed document is a new experiment).
Not covered: scheduling (``deploy/windows/run_fwdlog.ps1``), HTTP.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_forward as FW
from pulse.application import bf_batch7_history as HI
from pulse.application import bf_batch7_reads as RD
from pulse.application import bf_batch7_registry as REG
from pulse.application.data_service import DataService
from pulse.domain.batch7 import rules as R7


class PreregistrationChanged(RuntimeError):
    pass


def check_document(path: Path | None = None) -> str | None:
    """Verify the frozen prefix when the document is next to the code (a deployed copy may not carry docs/): the hash found, or None when absent."""
    path = path or Path(__file__).resolve().parents[4] / R7.SPEC_PATH
    if not path.exists():
        return None
    got = R7.frozen_hash(path.read_bytes())
    if got != R7.SPEC_SHA256:
        raise PreregistrationChanged(f"{path}: frozen prefix sha256 {got} != {R7.SPEC_SHA256}; a changed pre-registration is a new experiment")
    return got


class Batch7Service:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock=None, loader=None):
        self.conn, self.lake, self.loader = conn, lake, loader
        self.svc = DataService(conn, lake, clock=clock)

    def _anchor(self, as_of: datetime) -> dict:
        row = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id IN ('bars_native_raw_1d','bars_raw_1d') AND manifest_state='READY' AND ready_at <= %s "
                                "ORDER BY (dataset_id='bars_native_raw_1d') DESC, manifest_version DESC LIMIT 1", (as_of,)).fetchone()
        if row is None:
            raise RuntimeError("no READY raw daily bars manifest: nothing to anchor the registration to")
        return row

    def register(self, as_of: datetime) -> None:
        anchor = self._anchor(as_of)
        for key in ("E097", "E098"):
            REG.ensure_registered(self.conn, REG.BY_KEY[key], anchor, now=as_of)

    def forward(self, day: date, *, as_of: datetime | None = None) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        if day < R7.FORWARD_START:
            return {"through": day.isoformat(), "appended": False, "reason": f"before the first decision day {R7.FORWARD_START}"}
        self.register(as_of)
        rep = FW.step(self.conn, self.lake, self.svc, day, as_of=as_of, loader=self.loader)
        out = rep.wire()
        out["appended"] = bool(rep.decided.get("E097") or rep.decided.get("E098") or rep.outcomes.get("days_resolved") or rep.no_data)
        return out

    def judge(self, *, today: date | None = None, as_of: datetime | None = None) -> dict:
        check_document()
        return RD.judge_and_store(self.svc, self.conn, self.lake, today, as_of)

    def summary(self, today: date | None = None) -> dict:
        return RD.overview(self.conn, self.lake, today)

    def history(self, *, as_of: datetime | None = None, publish: bool = True, since: date = R7.HISTORY_START, until: date = R7.HISTORY_END) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        check_document()
        out = HI.run(self.conn, self.lake, as_of=as_of, since=since, until=until)
        if publish and out.get("status") == "ok":
            self.register(as_of)
            man = HI.publish(self.svc, out, as_of=as_of)
            out["snapshot_id"] = str(man["snapshot_id"])
        return out
