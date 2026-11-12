"""User assets: layouts, watchlists, annotations (ui.* tables).

Why: FINAL_SYSTEM_SPEC §4.2 makes layouts, drawings, watchlists and notes
persistent user assets in PG; browser cache is display only. Writes use
optimistic versions (expected version 0 = create only) and are owned by a
principal; another principal's object is reported as not found.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


class VersionConflict(Exception):
    pass


class NotFound(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PgUiRepo:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # layouts --------------------------------------------------------------
    def get_layout(self, principal: UUID, layout_id: UUID) -> dict[str, Any]:
        row = self.conn.execute(
            "select layout_id, layout_version, display_name, layout_document, updated_at from ui.workspace_layout"
            " where layout_id=%s and principal_id=%s", (layout_id, principal)).fetchone()
        if not row:
            raise NotFound(layout_id)
        return dict(row)

    def save_layout(self, principal: UUID, layout_id: UUID, update: dict[str, Any]) -> dict[str, Any]:
        exp = update["expected_layout_version"]
        now = _now()
        if exp == 0:
            try:
                self.conn.execute(
                    "insert into ui.workspace_layout(layout_id, principal_id, layout_version, display_name,"
                    " layout_document, created_at, updated_at) values (%s,%s,1,%s,%s,%s,%s)",
                    (layout_id, principal, update["display_name"], Jsonb(update["layout_document"]), now, now))
            except psycopg.errors.UniqueViolation as e:
                raise VersionConflict("layout already exists") from e
        else:
            cur = self.conn.execute(
                "update ui.workspace_layout set layout_version=layout_version+1, display_name=%s,"
                " layout_document=%s, updated_at=%s where layout_id=%s and principal_id=%s and layout_version=%s",
                (update["display_name"], Jsonb(update["layout_document"]), now, layout_id, principal, exp))
            if cur.rowcount != 1:
                raise VersionConflict("layout version mismatch")
        return self.get_layout(principal, layout_id)

    # watchlists -----------------------------------------------------------
    def _members(self, wid: UUID) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "select instrument_id, sort_position, added_release_id, note, added_at from ui.watchlist_member"
            " where watchlist_id=%s order by sort_position", (wid,))]

    def _watchlist(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"watchlist_id": row["watchlist_id"], "watchlist_version": row["watchlist_version"],
                "display_name": row["display_name"], "description": row["description"],
                "members": self._members(row["watchlist_id"]), "updated_at": row["updated_at"]}

    def list_watchlists(self, principal: UUID) -> list[dict[str, Any]]:
        rows = self.conn.execute("select * from ui.watchlist where principal_id=%s order by created_at",
                                 (principal,)).fetchall()
        return [self._watchlist(r) for r in rows]

    def save_watchlist(self, principal: UUID, wid: UUID, update: dict[str, Any]) -> dict[str, Any]:
        exp, now = update["expected_watchlist_version"], _now()
        with self.conn.transaction():
            if exp == 0:
                try:
                    self.conn.execute(
                        "insert into ui.watchlist(watchlist_id, principal_id, watchlist_version, display_name,"
                        " description, created_at, updated_at) values (%s,%s,1,%s,%s,%s,%s)",
                        (wid, principal, update["display_name"], update["description"], now, now))
                except psycopg.errors.UniqueViolation as e:
                    raise VersionConflict("watchlist already exists") from e
            else:
                cur = self.conn.execute(
                    "update ui.watchlist set watchlist_version=watchlist_version+1, display_name=%s, description=%s,"
                    " updated_at=%s where watchlist_id=%s and principal_id=%s and watchlist_version=%s",
                    (update["display_name"], update["description"], now, wid, principal, exp))
                if cur.rowcount != 1:
                    raise VersionConflict("watchlist version mismatch")
            old = {r["instrument_id"]: r["added_at"] for r in self._members(wid)}
            self.conn.execute("delete from ui.watchlist_member where watchlist_id=%s", (wid,))
            for m in update["members"]:
                iid = UUID(str(m["instrument_id"]))
                self.conn.execute(
                    "insert into ui.watchlist_member(watchlist_id, instrument_id, sort_position, added_release_id,"
                    " note, added_at) values (%s,%s,%s,%s,%s,%s)",
                    (wid, iid, m["sort_position"], m["added_release_id"], m["note"], old.get(iid, now)))
        row = self.conn.execute("select * from ui.watchlist where watchlist_id=%s", (wid,)).fetchone()
        return self._watchlist(row)

    # annotations ----------------------------------------------------------
    _ANN = ("annotation_id, annotation_version, instrument_id, watchlist_id, annotation_kind, anchor_release_id,"
            " anchor_snapshot_id, body, updated_at")

    def list_annotations(self, principal: UUID, instrument_id: UUID | None, watchlist_id: UUID | None) -> list:
        sql = f"select {self._ANN} from ui.annotation where principal_id=%s"
        args: list[Any] = [principal]
        if instrument_id:
            sql += " and instrument_id=%s"
            args.append(instrument_id)
        if watchlist_id:
            sql += " and watchlist_id=%s"
            args.append(watchlist_id)
        return [dict(r) for r in self.conn.execute(sql + " order by updated_at desc limit 500", args)]

    def save_annotation(self, principal: UUID, aid: UUID, u: dict[str, Any]) -> dict[str, Any]:
        exp, now = u["expected_annotation_version"], _now()
        vals = (u["instrument_id"], u["watchlist_id"], u["annotation_kind"], u["anchor_release_id"],
                u["anchor_snapshot_id"], Jsonb(u["body"]))
        if exp == 0:
            try:
                self.conn.execute(
                    "insert into ui.annotation(annotation_id, principal_id, annotation_version, instrument_id,"
                    " watchlist_id, annotation_kind, anchor_release_id, anchor_snapshot_id, body, created_at,"
                    " updated_at) values (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)", (aid, principal, *vals, now, now))
            except psycopg.errors.UniqueViolation as e:
                raise VersionConflict("annotation already exists") from e
        else:
            cur = self.conn.execute(
                "update ui.annotation set annotation_version=annotation_version+1, instrument_id=%s, watchlist_id=%s,"
                " annotation_kind=%s, anchor_release_id=%s, anchor_snapshot_id=%s, body=%s, updated_at=%s"
                " where annotation_id=%s and principal_id=%s and annotation_version=%s",
                (*vals, now, aid, principal, exp))
            if cur.rowcount != 1:
                raise VersionConflict("annotation version mismatch")
        row = self.conn.execute(f"select {self._ANN} from ui.annotation where annotation_id=%s", (aid,)).fetchone()
        return dict(row)
