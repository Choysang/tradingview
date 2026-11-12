"""Workspace revisions, deletion and exports (PEX14; D22-D24).

Why: layouts, watchlists and annotations live in current-state tables; alerts and exports must be able to point at the EXACT
version they were built on, and a delete must not let an old id come back. This module seals versions into
``ui.workspace_revision`` (immutable, owner-scoped), writes tombstones, lists a user's objects by kind and runs exports from
fixed inputs.
Invariants:
* a version is sealed once (``object_version`` of the current row), by hash of its body; a tombstone gets version+1, keeps the
  object's identity, has no body and is replayed (not re-applied) when the same delete arrives again; a DB trigger refuses to
  re-insert a deleted id into the current-state tables;
* deleting an annotation does not delete the alert rules that pin it: their current version is saved again with
  ``enabled = false`` (no new triggers), every historic version and alert stays;
* everything is owner-scoped: someone else's object id is "not found", never "forbidden";
* an export request is stored whole (spec artifact + ``ops.export_request``) BEFORE the job exists; the job reads only that spec and
  the sealed revisions, never the current layout; data exports are produced from the named READY snapshots only and carry
  as-of/adjustment/version metadata; PNG output is a small pure-code raster whose metadata sits in PNG text chunks (no font
  renderer is available, so no on-image text).
Not covered: parquet/csv of datasets other than raw daily bars (the job fails with ``EXPORT_DATASET_NOT_SUPPORTED`` rather than
inventing columns), per-source export entitlement (not confirmed for any public source - stated, not enforced).
"""
from __future__ import annotations

import base64
import csv
import io
import struct
import uuid
import zlib
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.adapters.postgres.outbox import PgOutbox
from pulse.adapters.postgres.ui_repo import PgUiRepo
from pulse.application import alert_service, ops_service
from pulse.application.ports.delivery import OutboxEvent

KINDS = ("layout", "watchlist", "annotation")
INDEX_KINDS = ("watchlists", "layouts", "chart_states", "drawings", "notes", "reviews")
BODY_CONTRACT = {"layout": "pulse.workspace.layout", "watchlist": "pulse.workspace.watchlist", "annotation": "pulse.workspace.annotation"}
EXPORT_CONTRACT, EXPORT_VERSION = "pulse.ext_c.export_spec", "1"
EXPORT_RETENTION = timedelta(days=7)
MAX_EXPORT_ROWS = 50_000
WS_NS = uuid.UUID("6a2f9c14-0b8e-4d73-a5c1-3e7d9f1b2a40")
_TABLE = {"layout": ("ui.workspace_layout", "layout_id", "layout_version"), "watchlist": ("ui.watchlist", "watchlist_id", "watchlist_version"),
          "annotation": ("ui.annotation", "annotation_id", "annotation_version")}


class WorkspaceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise WorkspaceError(400, "INVALID_CONTRACT", f"object_kind 只能是 {', '.join(KINDS)}")


def _current(conn: psycopg.Connection, principal: UUID, kind: str, oid: UUID) -> dict | None:
    table, idc, _ = _TABLE[kind]
    return conn.execute(f"SELECT * FROM {table} WHERE {idc}=%s AND principal_id=%s", (oid, principal)).fetchone()


def _body_of(conn: psycopg.Connection, kind: str, row: dict) -> dict:
    if kind == "layout":
        return {"display_name": row["display_name"], "layout_document": row["layout_document"]}
    if kind == "watchlist":
        members = [dict(m) for m in conn.execute("SELECT instrument_id, sort_position, added_release_id, note FROM ui.watchlist_member WHERE watchlist_id=%s ORDER BY sort_position", (row["watchlist_id"],))]
        return {"display_name": row["display_name"], "description": row["description"], "members": members}
    return {k: row[k] for k in ("instrument_id", "watchlist_id", "annotation_kind", "anchor_release_id", "anchor_snapshot_id", "body")}


def seal_current(conn: psycopg.Connection, principal: UUID, kind: str, oid: UUID, now: datetime | None = None) -> dict | None:
    """Seal the current version of a live object (idempotent). Returns the revision row, None when the object does not exist."""
    now = now or ops_service.now_utc()
    row = _current(conn, principal, kind, oid)
    if row is None:
        return None
    version = row[_TABLE[kind][2]]
    have = conn.execute("SELECT * FROM ui.workspace_revision WHERE principal_id=%s AND object_kind=%s AND object_id=%s AND object_version=%s", (principal, kind, oid, version)).fetchone()
    if have:
        return have
    art = ops_service.put_artifact(conn, kind=f"workspace-{kind}", contract_id=BODY_CONTRACT[kind], schema_version="0.1", body=contracts.to_wire(_body_of(conn, kind, row)), owner=principal)
    conn.execute("INSERT INTO ui.workspace_revision(principal_id, object_kind, object_id, object_version, operation, body_contract_id, body_schema_version, body_artifact, recorded_at)"
                 " VALUES (%s,%s,%s,%s,'upsert',%s,'0.1',%s,%s)", (principal, kind, oid, version, BODY_CONTRACT[kind], art["ref"], now))
    return conn.execute("SELECT * FROM ui.workspace_revision WHERE principal_id=%s AND object_kind=%s AND object_id=%s AND object_version=%s", (principal, kind, oid, version)).fetchone()


def seal_all(conn: psycopg.Connection, principal: UUID) -> int:
    n = 0
    for kind, (table, idc, _v) in _TABLE.items():
        for r in conn.execute(f"SELECT {idc} AS i FROM {table} WHERE principal_id=%s", (principal,)).fetchall():
            seal_current(conn, principal, kind, r["i"])
            n += 1
    return n


def _latest_revision(conn: psycopg.Connection, principal: UUID, kind: str, oid: UUID) -> dict | None:
    return conn.execute("SELECT * FROM ui.workspace_revision WHERE principal_id=%s AND object_kind=%s AND object_id=%s ORDER BY object_version DESC LIMIT 1", (principal, kind, oid)).fetchone()


def delete_item(conn: psycopg.Connection, principal: UUID, req: dict, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    kind, oid, expected = req["object_kind"], req["object_id"], req["expected_version"]
    _check_kind(kind)
    row = _current(conn, principal, kind, oid)
    if row is None:
        last = _latest_revision(conn, principal, kind, oid)
        if last is None:
            raise WorkspaceError(404, "NOT_FOUND", "对象不存在")
        if last["operation"] == "delete" and expected == last["object_version"] - 1:       # same delete again: replay, do not re-apply
            return {"object_id": oid, "version": last["object_version"], "content_hash": sha256_of({"tombstone": [kind, str(oid), last["object_version"]]})}
        raise WorkspaceError(409, "VERSION_CONFLICT", "对象已被删除或已被其他设备修改")
    current = row[_TABLE[kind][2]]
    if expected != current:
        raise WorkspaceError(409, "VERSION_CONFLICT", f"对象当前版本是 {current}")
    seal_current(conn, principal, kind, oid, now)
    version = current + 1
    conn.execute("INSERT INTO ui.workspace_revision(principal_id, object_kind, object_id, object_version, operation, body_contract_id, body_schema_version, body_artifact, recorded_at)"
                 " VALUES (%s,%s,%s,%s,'delete',%s,'0.1',NULL,%s)", (principal, kind, oid, version, BODY_CONTRACT[kind], now))
    table, idc, _v = _TABLE[kind]
    if kind == "watchlist":
        conn.execute("DELETE FROM ui.watchlist_member WHERE watchlist_id=%s", (oid,))
    conn.execute(f"DELETE FROM {table} WHERE {idc}=%s AND principal_id=%s", (oid, principal))
    stopped = 0
    if kind == "annotation":                     # linked alerts stop triggering; their history stays
        for r in alert_service.current_rules(conn, principal):
            b = r["body"]
            if str(b.get("annotation_id")) == str(oid) and b["enabled"]:
                alert_service.save_rule(conn, principal, {"rule_id": r["rule_id"], "expected_rule_version": r["rule_version"], "body": {**b, "enabled": False}}, now)
                stopped += 1
    n = conn.execute("SELECT count(*) n FROM ui.workspace_revision WHERE principal_id=%s AND operation='delete'", (principal,)).fetchone()["n"]
    PgOutbox(conn).append(OutboxEvent(event_id=uuid.uuid5(WS_NS, f"deleted|{principal}|{kind}|{oid}|{version}"), event_type="workspace.item_deleted", schema_version="1",
                                      aggregate_type="workspace", aggregate_id=str(principal), aggregate_version=n, occurred_at=now, producer_version="workspace-service-v1",
                                      payload={"object_kind": kind, "object_id": str(oid), "version": version, "alert_rules_stopped": stopped}))
    return {"object_id": oid, "version": version, "content_hash": sha256_of({"tombstone": [kind, str(oid), version]})}


def query_revision(conn: psycopg.Connection, principal: UUID, kind: str, oid: UUID, version: int) -> dict:
    _check_kind(kind)
    seal_current(conn, principal, kind, oid)
    r = conn.execute("SELECT * FROM ui.workspace_revision WHERE principal_id=%s AND object_kind=%s AND object_id=%s AND object_version=%s", (principal, kind, oid, version)).fetchone()
    if r is None:
        raise WorkspaceError(404, "NOT_FOUND", "版本不存在或未封存（旧系统未留历史的版本不可恢复）")
    if r["operation"] == "delete":
        return {"body": None, "deleted": True}
    a = conn.execute("SELECT ref, content_hash, contract_id, schema_version FROM ops.ext_artifact WHERE ref=%s", (r["body_artifact"],)).fetchone()
    return {"body": {"body_contract_id": r["body_contract_id"], "body_schema_version": r["body_schema_version"], "body_artifact": dict(a)}, "deleted": False}


def query_index(conn: psycopg.Connection, principal: UUID, kind: str, limit: int, offset: int) -> tuple[dict, bool]:
    if kind not in INDEX_KINDS:
        raise WorkspaceError(400, "INVALID_CONTRACT", f"kind 只能是 {', '.join(INDEX_KINDS)}")
    out: dict[str, list] = {"layouts": [], "watchlists": [], "annotations": []}
    more = False
    if kind == "watchlists":
        rows = PgUiRepo(conn).list_watchlists(principal)
        out["watchlists"], more = rows[offset:offset + limit], offset + limit < len(rows)
    elif kind in ("layouts", "chart_states"):
        rows = [dict(r) for r in conn.execute("SELECT layout_id, layout_version, display_name, layout_document, updated_at FROM ui.workspace_layout WHERE principal_id=%s ORDER BY updated_at DESC, layout_id", (principal,))]
        rows = [r for r in rows if (r["layout_document"].get("body_type") == "chart_state") == (kind == "chart_states")]
        out["layouts"], more = rows[offset:offset + limit], offset + limit < len(rows)
    else:
        ak = {"drawings": "drawing", "notes": "note", "reviews": "review"}[kind]
        rows = [dict(r) for r in conn.execute("SELECT annotation_id, annotation_version, instrument_id, watchlist_id, annotation_kind, anchor_release_id, anchor_snapshot_id, body, updated_at"
                                              " FROM ui.annotation WHERE principal_id=%s AND annotation_kind=%s ORDER BY updated_at DESC, annotation_id", (principal, ak))]
        out["annotations"], more = rows[offset:offset + limit], offset + limit < len(rows)
    return out, more


# ---- exports ---------------------------------------------------------------------------------------------------------------------------
def schedule_export(conn: psycopg.Connection, principal: UUID, req: dict, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    if req["format"] not in ("parquet", "csv", "png"):
        raise WorkspaceError(400, "INVALID_CONTRACT", "format 只能是 parquet / csv / png")
    if req["format"] == "png" and not req["input_snapshot_refs"]:
        raise WorkspaceError(400, "INVALID_CONTRACT", "PNG 导出需要固定的行情快照")
    datasets = set()
    for ref in req["input_snapshot_refs"]:
        m = conn.execute("SELECT manifest_state, content_hash, dataset_id, manifest_version FROM market.dataset_manifest WHERE snapshot_id=%s", (ref["snapshot_id"],)).fetchone()
        if m is None or m["manifest_state"] != "READY" or m["content_hash"] != ref["content_hash"] or m["dataset_id"] != ref["dataset_id"]:
            raise WorkspaceError(409, "STALE_INPUT", "输入快照不存在、未就绪或哈希不符")
        datasets.add(m["dataset_id"])
    for fp in req["field_projection"]:
        if fp["dataset_id"] not in datasets:
            raise WorkspaceError(400, "INVALID_CONTRACT", f"字段投影的数据集 {fp['dataset_id']} 不在输入快照中")
    objs = []
    for o in req["workspace_objects"]:
        rev = conn.execute("SELECT r.*, a.content_hash FROM ui.workspace_revision r LEFT JOIN ops.ext_artifact a ON a.ref=r.body_artifact"
                           " WHERE r.principal_id=%s AND r.object_id=%s AND r.object_version=%s", (principal, o["object_id"], o["version"])).fetchone()
        if rev is None:
            for kind in KINDS:                                   # a live current version may not be sealed yet
                if _current(conn, principal, kind, o["object_id"]) is not None:
                    seal_current(conn, principal, kind, o["object_id"], now)
            rev = conn.execute("SELECT r.*, a.content_hash FROM ui.workspace_revision r LEFT JOIN ops.ext_artifact a ON a.ref=r.body_artifact"
                               " WHERE r.principal_id=%s AND r.object_id=%s AND r.object_version=%s", (principal, o["object_id"], o["version"])).fetchone()
        if rev is None or rev["operation"] == "delete":
            raise WorkspaceError(404, "NOT_FOUND", "工作区对象版本不存在")
        if rev["content_hash"] != o["content_hash"]:
            raise WorkspaceError(409, "STALE_INPUT", "工作区对象内容哈希与该版本不符")
        objs.append({"kind": rev["object_kind"], **o})
    spec = {k: v for k, v in req.items() if k != "idempotency_key"}
    spec["workspace_objects"] = objs
    spec["render"] = {"adjustment": "raw", "as_of": now, "signature": "pulse3/workspace-export-v1"}
    spec_h = sha256_of({k: v for k, v in spec.items() if k != "render"})
    export_id = uuid.uuid5(WS_NS, f"export|{principal}|{spec_h}")
    existing = conn.execute("SELECT job_run_id FROM ops.export_request WHERE export_id=%s", (export_id,)).fetchone()
    if existing:
        return {**PgJobStore(conn).receipt(existing["job_run_id"]), "resource_id": export_id, "poll_operation_id": "queryExportArtifact"}
    art = ops_service.put_artifact(conn, kind="export-spec", contract_id=EXPORT_CONTRACT, schema_version=EXPORT_VERSION, body=contracts.to_wire(spec), owner=principal)
    run_id = uuid.uuid5(WS_NS, f"export-run|{export_id}")
    ops_service.ensure_jobs(conn)
    d = ops_service.DEFINITIONS["WorkspaceService.run_export"]
    PgJobStore(conn).enqueue(job_run_id=run_id, job_id=d.job_id, job_version=d.job_version, idempotency_key=f"export:{export_id}", parameter_hash=spec_h,
                             scheduled_for=now, resource_type="workspace_export", resource_id=export_id)
    conn.execute("INSERT INTO ops.export_request(export_id, principal_id, input_snapshot_refs, field_projection, format, export_spec_artifact, job_run_id, expires_at)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (export_id, principal, Jsonb(contracts.to_wire(req["input_snapshot_refs"])), Jsonb(req["field_projection"]), req["format"],
                                                          art["ref"], run_id, now + EXPORT_RETENTION))
    conn.execute("INSERT INTO ops.job_run_argument(job_run_id, principal_id, parameter_contract_id, parameter_schema_version, parameter_artifact, parameter_hash, input_snapshot_refs, release_id)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)", (run_id, principal, EXPORT_CONTRACT, EXPORT_VERSION, art["ref"], art["content_hash"], Jsonb(contracts.to_wire(req["input_snapshot_refs"]))))
    return {**PgJobStore(conn).receipt(run_id), "poll_operation_id": "queryExportArtifact"}


def query_export(conn: psycopg.Connection, principal: UUID, export_id: UUID, now: datetime | None = None) -> dict:
    now = now or ops_service.now_utc()
    e = conn.execute("SELECT * FROM ops.export_request WHERE export_id=%s", (export_id,)).fetchone()
    if e is None or e["principal_id"] != principal:
        raise WorkspaceError(404, "NOT_FOUND", "导出不存在")
    job = PgJobStore(conn).job_run_wire(e["job_run_id"])
    art = None
    if e["output_artifact"] and now < e["expires_at"]:
        a = conn.execute("SELECT ref, content_hash, contract_id, schema_version FROM ops.ext_artifact WHERE ref=%s", (e["output_artifact"],)).fetchone()
        art = dict(a)
    return {"job": job, "artifact": art, "expires_at": e["expires_at"]}


@ops_service.register_handler("WorkspaceService.run_export")
def run_export(conn: psycopg.Connection, run, args: dict) -> dict:
    e = conn.execute("SELECT * FROM ops.export_request WHERE job_run_id=%s", (run.job_run_id,)).fetchone()
    if e is None:
        raise LookupError("export request missing")
    spec = conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (e["export_spec_artifact"],)).fetchone()["body"]
    fmt = spec["format"]
    if not spec["input_snapshot_refs"]:
        raise ValueError("EXPORT_HAS_NO_INPUT")
    snaps = [r["snapshot_id"] for r in spec["input_snapshot_refs"]]
    fields = [f for fp in spec["field_projection"] for f in fp["field_names"]] or ["trade_date", "instrument_id", "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw", "volume_shares"]
    allowed = {"trade_date", "instrument_id", "timeframe", "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw", "volume_shares", "turnover_amount_cny", "is_final", "quality_status", "available_at"}
    if not set(fields) <= allowed or not all(d["dataset_id"].startswith("bars_raw") for d in spec["input_snapshot_refs"]):
        raise ValueError("EXPORT_DATASET_NOT_SUPPORTED")
    cols = ", ".join(dict.fromkeys(fields))
    rows = conn.execute(f"SELECT {cols} FROM market.bar_projection WHERE snapshot_id = ANY(%s) AND timeframe='1d' ORDER BY trade_date, instrument_id LIMIT %s", (snaps, MAX_EXPORT_ROWS + 1)).fetchall()
    if len(rows) > MAX_EXPORT_ROWS:
        raise ValueError("EXPORT_TOO_LARGE")
    meta = {"as_of": spec["render"]["as_of"], "adjustment": "raw", "timeframe": "1d", "snapshots": spec["input_snapshot_refs"], "workspace_objects": spec["workspace_objects"],
            "signature": spec["render"]["signature"], "rows": len(rows)}
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(list(dict.fromkeys(fields)))
        for r in rows:
            w.writerow([contracts.to_wire(v) if v is not None else "" for v in r.values()])
        data = buf.getvalue().encode("utf-8")
    elif fmt == "parquet":
        import polars as pl
        frame = pl.DataFrame([{k: (str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v) for k, v in r.items()} for r in rows])
        b = io.BytesIO()
        frame.write_parquet(b)
        data = b.getvalue()
    else:
        data = _png(rows, meta)
    body = {"format": fmt, "meta": contracts.to_wire(meta), "size_bytes": len(data), "sha256": __import__("hashlib").sha256(data).hexdigest(), "data_b64": base64.b64encode(data).decode("ascii")}
    art = ops_service.put_artifact(conn, kind=f"export-{fmt}", contract_id="pulse.ext_c.export_output", schema_version="1", body=body, owner=e["principal_id"])
    conn.execute("UPDATE ops.export_request SET output_artifact=%s WHERE export_id=%s", (art["ref"], e["export_id"]))
    return {"export_id": str(e["export_id"]), "rows": len(rows), "bytes": len(data)}


def _png(rows: list[dict], meta: dict) -> bytes:
    """A 640x240 RGB raster of the close series with the metadata in tEXt chunks (no font renderer: nothing is drawn as text)."""
    w, h = 640, 240
    closes = [float(r["close_price_raw"]) for r in rows if r.get("close_price_raw") is not None] or [0.0]
    lo, hi = min(closes), max(closes)
    px = [[(255, 255, 255)] * w for _ in range(h)]
    n = len(closes)
    prev = None
    for i, c in enumerate(closes):
        x = int(i * (w - 1) / max(n - 1, 1))
        y = h - 1 - int((c - lo) / (hi - lo or 1) * (h - 20)) - 10
        if prev:
            x0, y0 = prev
            for t in range(max(abs(x - x0), abs(y - y0), 1) + 1):
                xx = x0 + (x - x0) * t // max(abs(x - x0), abs(y - y0), 1)
                yy = y0 + (y - y0) * t // max(abs(x - x0), abs(y - y0), 1)
                px[yy][xx] = (30, 90, 200)
        prev = (x, y)
    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    texts = {"Software": "pulse3 workspace export", "Adjustment": meta["adjustment"], "Timeframe": meta["timeframe"], "AsOf": str(meta["as_of"]),
             "Versions": ",".join(f"{o['object_id']}@{o['version']}" for o in meta["workspace_objects"]) or "none", "Signature": meta["signature"],
             "Snapshots": ",".join(s["snapshot_id"] for s in meta["snapshots"])}
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    for k, v in texts.items():
        out += chunk(b"tEXt", k.encode("latin-1") + b"\x00" + v.encode("latin-1", "replace"))
    return out + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
