"""Batch 6 lane service: registration, the historical run, the daily forward step and the field check, glued to the lake / registry.

Why: one place for the three commands of the worker (``history``, ``forward``, ``fieldcheck``). It adds no rule; rules live in ``domain.batch6`` and the experiment modules.
Invariants: an experiment is registered (shadow) before its first result; ``forward`` decides a session once (a repeated run appends nothing; ``exit_deferred`` signals are closed
by later ``resolution`` rows); a session before ``FIRST_EVALUATED`` is never evaluated; ``fieldcheck`` stores its conclusion once and ``forward`` reads it (not computable -> the
dependent feature is empty).
Not covered: scheduling, HTTP.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch6_fieldcheck as FC
from pulse.application import bf_batch6_forward as FW
from pulse.application import bf_batch6_history as HI
from pulse.application import bf_batch6_inputs as IN
from pulse.application import bf_batch6_registry as REG
from pulse.application.bf_live_service import DATASET_QUOTES, LiveService
from pulse.application.data_service import DataService


class Batch6Service:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, history_loader=None, clock=None, live: LiveService | None = None):
        self.conn, self.lake = conn, lake
        self.history_loader = history_loader
        self.svc = DataService(conn, lake, clock=clock)
        self.live = live or LiveService(conn, lake, clock=clock)

    def _bars_manifest(self, as_of: datetime) -> dict:
        row = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id='bars_raw_1d' AND manifest_state='READY' AND ready_at <= %s ORDER BY manifest_version DESC LIMIT 1",
                                (as_of,)).fetchone()
        if row is None:
            raise RuntimeError("no READY daily bars manifest: nothing to anchor the registration to")
        return row

    def history(self, *, as_of: datetime | None = None, publish: bool = True, since: date | None = None, source: str = "adjusted") -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        if publish:
            first = self._bars_manifest(as_of)
            for key in ("E093", "E094"):
                REG.ensure_registered(self.conn, REG.BY_KEY[key], first, now=as_of)
        inp = IN.prepare_history(self.conn, self.lake, as_of=as_of, since=since, source=source)
        out = HI.run(inp)
        if publish:
            man = HI.publish(self.svc, out, as_of=as_of)
            out["snapshot_id"] = str(man["snapshot_id"])
        return out

    def fieldcheck(self, *, as_of: datetime | None = None) -> tuple[dict, bool]:
        as_of = as_of or datetime.now(timezone.utc)
        return FC.publish_once(self.svc, self.live, as_of=as_of)

    def forward(self, day: date, *, as_of: datetime | None = None, force_ready: bool = False) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        if day.isoformat() in FW.decided_sessions(self.conn):
            return {"session": day.isoformat(), "decided": False, "reason": "already decided (append only)", "rows": 0}
        man = self.live.latest_ready(DATASET_QUOTES, day, as_of)
        if man is not None and day >= FW.FIRST_EVALUATED:
            for key in ("E095", "E096"):
                REG.ensure_registered(self.conn, REG.BY_KEY[key], man, now=as_of)
        field, _ = self.fieldcheck(as_of=as_of)
        unmatched_ok = bool(field["overall"]["unmatched_buy_computable"])
        rep, rows = FW.run_day(self.svc, self.live, day, as_of=as_of, unmatched_ok=unmatched_ok, force_ready=force_ready, hist_loader=self.history_loader)
        out = {"session": rep.session, "decided": rep.decided, "reason": rep.reason, "rows": rep.rows, "hypotheses": rep.hypotheses, "unmatched_buy_computable": unmatched_ok}
        before = FW.stored_rows(self.conn, self.lake)
        late = FW.deferred_resolutions(self.conn, before, day, as_of)
        if not rows and not late:
            return out
        FW.publish(self.svc, rows + late, days=[day] if rows else [], as_of=as_of)
        out["resolutions"] = len(late)
        return out
