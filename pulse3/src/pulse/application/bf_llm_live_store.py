"""Append-only log of the E092 live LLM judgement: a lake dataset of delta snapshots, and an in-memory twin for tests.

Why: every request, answer, error, stop, outcome and judgement of the experiment must survive a crash and never be rewritten (pre-registration section 2: the call ledger is written BEFORE a
request is sent). The house pattern (``bf_batch7_forward``) is a derived dataset ``llm_live_log`` whose READY snapshots are deltas: a row is written once, in one snapshot, and the log is the
concatenation of the snapshots in manifest order.
Row = dict(session=decision date ISO, kind, key, written_at ISO, run_id, body=dict). Kinds: ``day`` (universe, prompts, deadline; once per decision day), ``call`` (ledger row, before the request),
``answer``, ``error``, ``stop``, ``run``, ``outcome`` (once per answer), ``judgement`` (once).
Invariants: ``append`` returns only after the rows are durable (an exception means nothing was added); there is no update or delete; ``exclusive`` is a session advisory lock in PostgreSQL, so two
processes (a decide and a track, or a rerun by hand) never write at once, and a crashed process releases it with its connection.
Not covered: the meaning of the rows (``bf_llm_live_service``), the experiment's registry rows (``bf_llm_live_registry``).
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Protocol

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application.data_service import DataService

DATASET = "llm_live_log"
CONTRACT = "pulse.analysis_artifact"
METHOD = "llm-live-log-v1"
LOCK_KEY = 0x4C4C4D4C495645                                   # "LLMLIVE": the advisory lock of the lane
SCHEMA = {"session": pl.String, "kind": pl.String, "key": pl.String, "written_at": pl.String, "run_id": pl.String, "body": pl.String, "body_sha256": pl.String}
KINDS = ("day", "call", "answer", "error", "stop", "run", "outcome", "judgement")


class LogStore(Protocol):
    def rows(self) -> list[dict]: ...
    def refresh(self) -> None: ...
    def append(self, rows: Sequence[dict]) -> None: ...
    def exclusive(self) -> Any: ...                           # context manager yielding whether this process holds the lane lock


def make_row(session: str, kind: str, key: str, run_id: str, body: dict, *, at: datetime) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown log kind {kind!r}")
    return {"session": session, "kind": kind, "key": key, "written_at": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id, "body": body}


def _encode(rows: Sequence[dict]) -> pl.DataFrame:
    out = []
    for r in rows:
        text = json.dumps(r["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        out.append({**{k: r[k] for k in ("session", "kind", "key", "written_at", "run_id")}, "body": text, "body_sha256": sha256(text.encode("utf-8")).hexdigest()})
    return pl.DataFrame(out, schema=SCHEMA) if out else pl.DataFrame(schema=SCHEMA)


def _decode(frame: pl.DataFrame) -> list[dict]:
    return [{**{k: r[k] for k in ("session", "kind", "key", "written_at", "run_id")}, "body": json.loads(r["body"])} for r in frame.iter_rows(named=True)]


class MemoryLogStore:
    """Same contract in memory (tests, dry runs). ``fail_next`` makes the next append raise, to prove that a failed ledger write stops the request."""

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._guard = threading.Lock()
        self._held = False
        self.fail_next = False

    def rows(self) -> list[dict]:
        return list(self._rows)

    def refresh(self) -> None:
        return None

    def append(self, rows: Sequence[dict]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("ledger write failed")
        self._rows.extend(json.loads(json.dumps(r, default=str)) for r in rows)         # a copy through JSON, as the lake would hold it

    @contextmanager
    def exclusive(self) -> Iterator[bool]:
        with self._guard:
            if self._held:
                yield False
                return
            self._held = True
        try:
            yield True
        finally:
            with self._guard:
                self._held = False


class LakeLogStore:
    def __init__(self, svc: DataService):
        self.svc, self.conn, self.lake = svc, svc.conn, svc.lake
        self._rows: list[dict] = []
        self.refresh()

    def refresh(self) -> None:
        mans = self.conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (DATASET,)).fetchall()
        rows: list[dict] = []
        for m in mans:
            frame = self.lake.read_snapshot_frames(m["content_hash"])
            if frame is not None and frame.height:
                rows += _decode(frame)
        self._rows = rows

    def rows(self) -> list[dict]:
        return list(self._rows)

    def append(self, rows: Sequence[dict]) -> None:
        if not rows:
            return
        frame = _encode(rows)
        now = self.svc.clock()
        cov = {"dataset": DATASET, "method": METHOD, "rows": frame.height, "evaluated_at": now.isoformat(),
               "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
        B.publish_derived(self.svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=now)
        self._rows.extend(_decode(frame))

    @contextmanager
    def exclusive(self) -> Iterator[bool]:
        got = self.conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK_KEY,)).fetchone()["got"]
        try:
            yield bool(got)
        finally:
            if got:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def open_lake_store(conn: psycopg.Connection, lake: LakeStore) -> LakeLogStore:
    return LakeLogStore(DataService(conn, lake))
