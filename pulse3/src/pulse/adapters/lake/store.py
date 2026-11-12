"""Local content-addressed lake: write protocol, immutable manifests, pins/leases, GC (RECON §4, M1-E03).

Why: the data directory holds raw responses and immutable Parquet. A snapshot is
its *member list*, never "files that are active now", so compaction cannot empty
history. Objects are written temp -> fsync -> sha256 -> atomic rename to a
content-addressed path; the member manifest is itself a content-addressed
immutable file; only then PG publishes manifest + dependencies + outbox.

Invariants:
* the catalog (SQLite file in the lake root) serializes pin acquisition and GC
  with ``BEGIN IMMEDIATE``; GC reads pins first, then asks PG for the manifests
  that are READY or COMMITTED_UNPUBLISHED (callback), all while holding the
  catalog lock, so ``publish`` can never lose objects to a concurrent GC: the
  stage pin is released only after the PG commit;
* an object is deleted only if no live pin (unexpired lease, or expired lease
  whose owner is still active) and no referenced manifest contains it;
* nothing is renamed across filesystems (tmp dir lives under the lake root);
* manifests are verified against their file name hash on read.
Not covered: DuckLake catalog, backup/archive targets, cross-host locking (one
host owns a lake root; a distributed deployment needs a shared catalog).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS object(sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, rel_path TEXT NOT NULL,
                                  size INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS manifest(sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, dataset_id TEXT NOT NULL,
                                    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS manifest_member(manifest_sha TEXT NOT NULL, object_sha TEXT NOT NULL,
                                           PRIMARY KEY(manifest_sha, object_sha));
CREATE TABLE IF NOT EXISTS pin(pin_id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, target_sha TEXT NOT NULL,
                               kind TEXT NOT NULL CHECK(kind IN ('stage','reader')), expires_at REAL NOT NULL,
                               created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS pin_owner ON pin(owner);
CREATE INDEX IF NOT EXISTS pin_target ON pin(target_sha);
"""


@dataclass(frozen=True)
class ObjectRef:
    sha256: str
    rel_path: str
    size: int
    kind: str = "data"


@dataclass(frozen=True)
class ManifestRef:
    sha256: str
    rel_path: str
    dataset_id: str
    members: tuple[dict, ...]
    meta: dict = field(default_factory=dict)

    @property
    def object_hashes(self) -> tuple[str, ...]:
        return tuple(m["sha256"] for m in self.members)


@dataclass(frozen=True)
class GCReport:
    deleted_objects: int
    deleted_manifests: int
    kept_pinned: int
    kept_referenced: int
    expired_pins_released: int
    orphan_files_deleted: int = 0


def _canonical(doc: object) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class LakeStore:
    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.clock = clock
        for sub in ("objects", "raw", "manifests", "tmp"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._catalog_path = self.root / "catalog.sqlite3"
        with self._tx():
            pass

    # -- catalog ---------------------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._catalog_path, timeout=60, isolation_level=None)
        try:
            conn.executescript(SCHEMA) if not self._schema_ok(conn) else None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @staticmethod
    def _schema_ok(conn: sqlite3.Connection) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE name='pin'").fetchone() is not None

    # -- low-level atomic write --------------------------------------------------------------------
    def _write_tmp(self, data: bytes) -> tuple[Path, str]:
        sha = hashlib.sha256(data).hexdigest()
        tmp = self.root / "tmp" / f"{uuid.uuid4().hex}.part"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return tmp, sha

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return  # Windows cannot open directories; rename durability is then best-effort
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _place(self, conn: sqlite3.Connection, tmp: Path, sha: str, size: int, kind: str, area: str, suffix: str) -> str:
        rel = f"{area}/{sha[:2]}/{sha[2:4]}/{sha}{suffix}" if area in ("objects", "raw") else f"{area}/{sha}{suffix}"
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, target)  # atomic: tmp lives under the same root/filesystem
            self._fsync_dir(target.parent)
        return rel

    def put_bytes(self, data: bytes, *, owner: str, lease_seconds: float = 3600.0, kind: str = "data",
                  suffix: str = "") -> ObjectRef:
        """Write ``data`` content-addressed and pin it for ``owner`` until the lease expires or is released."""
        tmp, sha = self._write_tmp(data)
        now = self.clock()
        with self._tx() as conn:
            rel = self._place(conn, tmp, sha, len(data), kind, "raw" if kind == "raw" else "objects", suffix)
            conn.execute("INSERT OR IGNORE INTO object(sha256,kind,rel_path,size,created_at) VALUES (?,?,?,?,?)",
                         (sha, kind, rel, len(data), now))
            conn.execute("INSERT INTO pin(owner,target_sha,kind,expires_at,created_at) VALUES (?,?,'stage',?,?)",
                         (owner, sha, now + lease_seconds, now))
        return ObjectRef(sha, rel, len(data), kind)

    def put_parquet(self, df, *, owner: str, lease_seconds: float = 3600.0, kind: str = "data") -> ObjectRef:
        buf = io.BytesIO()
        df.write_parquet(buf, compression="zstd")
        return self.put_bytes(buf.getvalue(), owner=owner, lease_seconds=lease_seconds, kind=kind, suffix=".parquet")

    # -- manifests ---------------------------------------------------------------------------------
    def commit_manifest(self, dataset_id: str, members: Iterable[ObjectRef], *, owner: str, meta: dict | None = None,
                        lease_seconds: float = 3600.0) -> ManifestRef:
        """Persist an immutable member list (content-addressed) and pin it + its members for ``owner``."""
        member_docs = [{"sha256": m.sha256, "path": m.rel_path, "size": m.size, "kind": m.kind} for m in members]
        if not member_docs:
            raise ValueError("a manifest needs at least one member (use an explicit empty marker object)")
        doc = {"format": 1, "dataset_id": dataset_id, "members": member_docs, "meta": meta or {}}
        data = _canonical(doc)
        tmp, sha = self._write_tmp(data)
        now = self.clock()
        with self._tx() as conn:
            rel = self._place(conn, tmp, sha, len(data), "manifest", "manifests", ".json")
            conn.execute("INSERT OR IGNORE INTO manifest(sha256,rel_path,dataset_id,created_at) VALUES (?,?,?,?)",
                         (sha, rel, dataset_id, now))
            for m in member_docs:
                if conn.execute("SELECT 1 FROM object WHERE sha256=?", (m["sha256"],)).fetchone() is None:
                    raise ValueError(f"manifest member {m['sha256']} is not a stored object")
                conn.execute("INSERT OR IGNORE INTO manifest_member(manifest_sha,object_sha) VALUES (?,?)", (sha, m["sha256"]))
                conn.execute("INSERT INTO pin(owner,target_sha,kind,expires_at,created_at) VALUES (?,?,'stage',?,?)",
                             (owner, m["sha256"], now + lease_seconds, now))
            conn.execute("INSERT INTO pin(owner,target_sha,kind,expires_at,created_at) VALUES (?,?,'stage',?,?)",
                         (owner, sha, now + lease_seconds, now))
        return ManifestRef(sha, rel, dataset_id, tuple(member_docs), doc["meta"])

    def read_manifest(self, sha_or_ref: str | ManifestRef) -> ManifestRef:
        sha = sha_or_ref.sha256 if isinstance(sha_or_ref, ManifestRef) else sha_or_ref
        path = self.root / "manifests" / f"{sha}.json"
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"manifest {sha} failed integrity check")
        doc = json.loads(data)
        return ManifestRef(sha, f"manifests/{sha}.json", doc["dataset_id"], tuple(doc["members"]), doc.get("meta", {}))

    def read_object(self, ref: ObjectRef | dict | str, *, verify: bool = True) -> bytes:
        rel = ref.rel_path if isinstance(ref, ObjectRef) else (ref["path"] if isinstance(ref, dict) else None)
        sha = ref.sha256 if isinstance(ref, ObjectRef) else (ref["sha256"] if isinstance(ref, dict) else ref)
        if rel is None:
            with self._tx() as conn:
                row = conn.execute("SELECT rel_path FROM object WHERE sha256=?", (sha,)).fetchone()
            if row is None:
                raise FileNotFoundError(sha)
            rel = row[0]
        data = (self.root / rel).read_bytes()
        if verify and hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"object {sha} failed integrity check")
        return data

    def read_snapshot_frames(self, manifest: ManifestRef | str):
        """Replay a snapshot from its frozen member list (never 'currently active' files)."""
        import polars as pl

        m = self.read_manifest(manifest)
        frames = [pl.read_parquet(io.BytesIO(self.read_object(x))) for x in m.members if x["path"].endswith(".parquet")]
        return pl.concat(frames) if frames else None

    # -- pins / leases -----------------------------------------------------------------------------
    def pin_snapshot(self, manifest_sha: str, *, owner: str, lease_seconds: float = 900.0) -> None:
        """Reader pin: protects the manifest and every member from GC until released/expired."""
        now = self.clock()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM manifest WHERE sha256=?", (manifest_sha,)).fetchone() is None:
                raise ValueError("unknown manifest")
            conn.execute("INSERT INTO pin(owner,target_sha,kind,expires_at,created_at) VALUES (?,?,'reader',?,?)",
                         (owner, manifest_sha, now + lease_seconds, now))

    def release(self, owner: str, kind: str | None = None) -> int:
        with self._tx() as conn:
            if kind:
                cur = conn.execute("DELETE FROM pin WHERE owner=? AND kind=?", (owner, kind))
            else:
                cur = conn.execute("DELETE FROM pin WHERE owner=?", (owner,))
            return cur.rowcount

    def renew(self, owner: str, lease_seconds: float) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE pin SET expires_at=? WHERE owner=?", (self.clock() + lease_seconds, owner))

    def pin_count(self, owner: str | None = None) -> int:
        with self._tx() as conn:
            if owner:
                return conn.execute("SELECT count(*) FROM pin WHERE owner=?", (owner,)).fetchone()[0]
            return conn.execute("SELECT count(*) FROM pin").fetchone()[0]

    def exists(self, sha: str) -> bool:
        with self._tx() as conn:
            return (conn.execute("SELECT 1 FROM object WHERE sha256=?", (sha,)).fetchone() is not None
                    or conn.execute("SELECT 1 FROM manifest WHERE sha256=?", (sha,)).fetchone() is not None)

    # -- garbage collection --------------------------------------------------------------------------
    def gc(self, referenced_manifests: Callable[[], Iterable[str]], *,
           owner_active: Callable[[str], bool] = lambda owner: False, min_age_seconds: float = 3600.0,
           referenced_objects: Callable[[], Iterable[str]] = lambda: ()) -> GCReport:
        """Delete unreferenced, unpinned objects. Holds the catalog lock across the reference query.

        Order matters: (1) drop expired pins whose owner is gone, (2) snapshot the live pins,
        (3) ask PG which manifests are READY / COMMITTED_UNPUBLISHED. A publish that commits in PG
        and then releases its pin is therefore always seen by (2) or (3).
        """
        now = self.clock()
        released = kept_pinned = kept_ref = deleted_o = deleted_m = orphans = 0
        with self._tx() as conn:
            for pin_id, owner in conn.execute("SELECT pin_id, owner FROM pin WHERE expires_at < ?", (now,)).fetchall():
                if not owner_active(owner):
                    conn.execute("DELETE FROM pin WHERE pin_id=?", (pin_id,))
                    released += 1
            pinned_targets = {r[0] for r in conn.execute("SELECT DISTINCT target_sha FROM pin")}
            protected = set(pinned_targets)
            for (msha,) in conn.execute("SELECT sha256 FROM manifest").fetchall():
                if msha in pinned_targets:
                    protected.update(r[0] for r in conn.execute("SELECT object_sha FROM manifest_member WHERE manifest_sha=?", (msha,)))
            refs = set(referenced_manifests())  # PG callback, evaluated after the pin snapshot
            ref_protected = set(refs) | set(referenced_objects())  # e.g. READY feature result artifacts
            for msha in refs:
                ref_protected.update(r[0] for r in conn.execute("SELECT object_sha FROM manifest_member WHERE manifest_sha=?", (msha,)))
            # manifests first (they never contain other manifests)
            for sha, rel, created in conn.execute("SELECT sha256, rel_path, created_at FROM manifest").fetchall():
                if sha in protected:
                    kept_pinned += 1
                elif sha in ref_protected:
                    kept_ref += 1
                elif now - created >= min_age_seconds:
                    (self.root / rel).unlink(missing_ok=True)
                    conn.execute("DELETE FROM manifest WHERE sha256=?", (sha,))
                    conn.execute("DELETE FROM manifest_member WHERE manifest_sha=?", (sha,))
                    deleted_m += 1
            # members still referenced by a surviving manifest stay (history must stay replayable)
            surviving = {r[0] for r in conn.execute("SELECT DISTINCT object_sha FROM manifest_member")}
            for sha, rel, created, okind in conn.execute("SELECT sha256, rel_path, created_at, kind FROM object").fetchall():
                if okind == "raw":
                    kept_ref += 1  # raw evidence is governed by retention policy, never by object GC
                elif sha in protected:
                    kept_pinned += 1
                elif sha in ref_protected or sha in surviving:
                    kept_ref += 1
                elif now - created >= min_age_seconds:
                    (self.root / rel).unlink(missing_ok=True)
                    conn.execute("DELETE FROM object WHERE sha256=?", (sha,))
                    deleted_o += 1
            known = {r[0] for r in conn.execute("SELECT rel_path FROM object")} | {r[0] for r in conn.execute("SELECT rel_path FROM manifest")}
            for area in ("objects", "manifests"):
                for p in (self.root / area).rglob("*"):
                    if p.is_file() and p.relative_to(self.root).as_posix() not in known and now - p.stat().st_mtime >= min_age_seconds:
                        p.unlink(missing_ok=True)
                        orphans += 1
            for p in (self.root / "tmp").glob("*.part"):
                if now - p.stat().st_mtime >= max(min_age_seconds, 60.0):
                    p.unlink(missing_ok=True)
        return GCReport(deleted_o, deleted_m, kept_pinned, kept_ref, released, orphans)
