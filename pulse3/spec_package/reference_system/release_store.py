"""SQLite transaction demonstration, not a PostgreSQL migration or load test.

The real system publishes a declared manifest of required light projections;
charts and current account feasibility remain separately versioned overlays.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import sqlite3


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_value(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def time_value(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.utcoffset() is None:
        raise ValueError("timezone_required")
    return t


class ReleaseStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS releases (
          release_id TEXT PRIMARY KEY, context_key TEXT NOT NULL,
          decision_as_of TEXT NOT NULL, manifest_hash TEXT NOT NULL,
          required_json TEXT NOT NULL, identity_hash TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('staged','published')),
          base_release_id TEXT REFERENCES releases);
        CREATE TABLE IF NOT EXISTS views (
          release_id TEXT REFERENCES releases, kind TEXT, scope TEXT,
          manifest_hash TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
          PRIMARY KEY(release_id,kind,scope));
        CREATE TABLE IF NOT EXISTS pointers (context_key TEXT PRIMARY KEY, release_id TEXT REFERENCES releases);
        CREATE TABLE IF NOT EXISTS outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (consumer_id TEXT, event_id TEXT, payload_hash TEXT NOT NULL,
          PRIMARY KEY(consumer_id,event_id));
        CREATE TABLE IF NOT EXISTS effects (consumer_id TEXT, event_id TEXT, payload_json TEXT NOT NULL,
          PRIMARY KEY(consumer_id,event_id));
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def stage(self, release_id, context_key, decision_as_of, manifest, required_views):
        time_value(decision_as_of)
        required = sorted(set(tuple(x) for x in required_views))
        if not required or not all(len(x) == 2 and all(isinstance(s, str) and s for s in x) for x in required):
            raise ValueError("nonempty_required_views")
        identity = hash_value([context_key, decision_as_of, manifest, required])
        with self.transaction():
            existing = self.db.execute("SELECT identity_hash FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if existing:
                if existing[0] != identity:
                    raise ValueError("release_id_content_conflict")
                return False
            base_release_id = self.active(context_key)
            self.db.execute("INSERT INTO releases VALUES(?,?,?,?,?,?,?,?)",
                            (release_id, context_key, decision_as_of, hash_value(manifest),
                             encoded(required), identity, "staged", base_release_id))
        return True

    def put_view(self, release_id, kind, scope, manifest, payload):
        with self.transaction():
            r = self.db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if r is None:
                raise ValueError("unknown_release")
            if hash_value(manifest) != r["manifest_hash"]:
                raise ValueError("input_manifest_mismatch")
            if [kind, scope] not in json.loads(r["required_json"]):
                raise ValueError("undeclared_view")
            p_hash = hash_value(payload)
            old = self.db.execute("SELECT payload_hash FROM views WHERE release_id=? AND kind=? AND scope=?",
                                  (release_id, kind, scope)).fetchone()
            if old:
                if old[0] != p_hash:
                    raise ValueError("projection_content_conflict")
                return False
            if r["state"] != "staged":
                raise ValueError("published_release_immutable")
            self.db.execute("INSERT INTO views VALUES(?,?,?,?,?,?)",
                            (release_id, kind, scope, r["manifest_hash"], encoded(payload), p_hash))
        return True

    def active(self, context_key):
        row = self.db.execute("SELECT release_id FROM pointers WHERE context_key=?", (context_key,)).fetchone()
        return row[0] if row else None

    def publish(self, release_id, expected_active=None, fail_after_pointer=False):
        with self.transaction():
            r = self.db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if r is None:
                raise ValueError("unknown_release")
            if r["state"] == "published":
                return False  # Idempotency does not move the pointer back to an older release.
            current = self.active(r["context_key"])
            if current != expected_active:
                raise ValueError("stale_publication_compare_and_swap")
            if current != r["base_release_id"]:
                raise ValueError("calculation_base_changed_recompute_required")
            if current:
                t = self.db.execute("SELECT decision_as_of FROM releases WHERE release_id=?", (current,)).fetchone()[0]
                if time_value(r["decision_as_of"]) < time_value(t):
                    raise ValueError("older_analysis_cannot_overwrite_newer")
            required = {tuple(v) for v in json.loads(r["required_json"])}
            actual = {(row[0], row[1]) for row in self.db.execute("SELECT kind,scope FROM views WHERE release_id=?", (release_id,))}
            if actual != required:
                raise ValueError("incomplete_projection_set")
            self.db.execute("INSERT INTO pointers VALUES(?,?) ON CONFLICT(context_key) DO UPDATE SET release_id=excluded.release_id",
                            (r["context_key"], release_id))
            if fail_after_pointer:
                raise RuntimeError("simulated_process_failure")
            self.db.execute("UPDATE releases SET state='published' WHERE release_id=?", (release_id,))
            event = {"event_id": "release:" + release_id, "event_type": "analysis.release_published",
                     "schema_version": "3.0.0", "release_id": release_id,
                     "context_key": r["context_key"], "decision_as_of": r["decision_as_of"],
                     "input_manifest_hash": r["manifest_hash"]}
            self.db.execute("INSERT INTO outbox(event_id,payload_json,payload_hash) VALUES(?,?,?)",
                            (event["event_id"], encoded(event), hash_value(event)))
        return True

    def changes_after(self, sequence=0):
        return [{"sequence": row[0], "event": json.loads(row[1])}
                for row in self.db.execute("SELECT sequence,payload_json FROM outbox WHERE sequence>? ORDER BY sequence", (sequence,))]

    def consume(self, consumer_id, event, fail_after_effect=False):
        """Effect and dedupe receipt commit together, separately per consumer."""
        with self.transaction():
            old = self.db.execute("SELECT payload_hash FROM inbox WHERE consumer_id=? AND event_id=?",
                                  (consumer_id, event["event_id"])).fetchone()
            if old:
                if old[0] != hash_value(event):
                    raise ValueError("event_id_content_conflict")
                return False
            self.db.execute("INSERT INTO effects VALUES(?,?,?)",
                            (consumer_id, event["event_id"], encoded(event)))
            if fail_after_effect:
                raise RuntimeError("simulated_consumer_crash")
            self.db.execute("INSERT INTO inbox VALUES(?,?,?)",
                            (consumer_id, event["event_id"], hash_value(event)))
        return True

    def read_view(self, context_key, kind, scope, release_id=None):
        rid = release_id or self.active(context_key)
        row = self.db.execute('''SELECT v.payload_json,r.context_key,r.state
            FROM views v JOIN releases r USING(release_id)
            WHERE v.release_id=? AND v.kind=? AND v.scope=?''', (rid, kind, scope)).fetchone()
        if not row or row[1] != context_key or row[2] != "published":
            raise ValueError("published_projection_not_available")
        return {"release_id": rid, "payload": json.loads(row[0])}
