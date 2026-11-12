"""AnalysisRelease on PostgreSQL: build, complete, publish and look up decision bundles (W02).

Why: FINAL_SYSTEM_SPEC / IMPLEMENTATION_GUIDE section 4 / INTEGRATION_GUIDE section 2 define the release
protocol; this module is the only writer of decision.release*, release_slot/stage/publication.
Implements ``pulse.application.ports.release.AnalysisRelease``.
Invariants:
  * publication runs in ONE READ COMMITTED transaction: lock the slot, lock the release, then read
    components in a *new* statement (fresh snapshot after the lock), validate, call
    ``decision.publish_release_stage`` (stage generation + base CAS), append the ``decision.published``
    outbox event, supersede the previous release;
  * required components must be READY, unexpired and backed by READY snapshots; optional components need
    a recorded degradation policy and are reported in ``degraded_components``;
  * a stage retry returns the stored publication (idempotent), never a second pointer move.
The database stays the last line of defence: the deferred triggers of 0001/0004 reject an incomplete
READY release or a publication without its matching outbox row even if this module is bypassed.
Not covered: rollback publication (package defines forward publication only), component computation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

import psycopg
from psycopg import errors as pgerr

from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.outbox import PgOutbox
from pulse.application.ports.delivery import OutboxEvent
from pulse.application.ports.errors import Conflict, IncompleteRelease, NotFound, ReleaseStateError, StaleStage
from pulse.application.ports.release import (
    COMPONENT_STATES,
    DEGRADATION_POLICIES,
    ComponentValue,
    CurrentRelease,
    PublishResult,
    ReleaseView,
    StageRef,
)

PUBLISH_NAMESPACE = UUID("6f0a7f1e-3f0d-4c52-9d8e-5a1b2c3d4e5f")
EVENT_SCHEMA_VERSION = "3.0.0"
_COMPONENT_COLS = ("component_key, component_state, artifact_ref, artifact_hash, snapshot_id, input_version_hash,"
                   " computed_at, valid_until")


def publication_event_id(stage_id: UUID) -> UUID:
    """Deterministic event id so every retry of one stage appends the identical event."""
    return uuid5(PUBLISH_NAMESPACE, f"decision.published:{stage_id}")


def compute_release_hash(release: Mapping[str, Any], components: Sequence[Mapping[str, Any]]) -> str:
    """Digest over every component reference and version plus the release identity fields."""
    doc = {
        "release_id": release["release_id"],
        "decision_as_of": release["decision_as_of"],
        "availability_policy_version": release["availability_policy_version"],
        "universe_policy_version": release["universe_policy_version"],
        "market_rules_version": release["market_rules_version"],
        "required_components": sorted(release["required_components"]),
        "components": sorted(
            ({k: c[k] for k in ("component_key", "component_state", "artifact_ref", "artifact_hash",
                                "snapshot_id", "input_version_hash", "computed_at", "valid_until")}
             for c in components),
            key=lambda c: c["component_key"],
        ),
    }
    return sha256_of(doc)


class PgAnalysisRelease:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self.outbox = PgOutbox(conn)

    # ---- building -----------------------------------------------------------------------------
    def ensure_slot(self, slot_key: str) -> None:
        self.conn.execute(
            "INSERT INTO decision.release_slot(slot_key, updated_at) VALUES (%s, clock_timestamp())"
            " ON CONFLICT (slot_key) DO NOTHING", (slot_key,))

    def create_release(self, *, release_id: UUID, decision_as_of: datetime, availability_policy_version: str,
                       universe_policy_version: str, market_rules_version: str,
                       required_components: Sequence[str],
                       optional_components: Mapping[str, str] | None = None,
                       created_at: datetime | None = None) -> ReleaseView:
        required = list(required_components)
        optional = dict(optional_components or {})
        if not required or len(set(required)) != len(required):
            raise ReleaseStateError("a release needs a nonempty list of distinct required components")
        if set(required) & set(optional):
            raise ReleaseStateError("a component cannot be both required and optional")
        bad = [p for p in optional.values() if p not in DEGRADATION_POLICIES]
        if bad:
            raise ReleaseStateError(f"unknown degradation policy {bad[0]!r}")
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT * FROM decision.release WHERE release_id=%s", (release_id,)).fetchone()
            if row is not None:
                same = (row["decision_as_of"] == decision_as_of
                        and row["availability_policy_version"] == availability_policy_version
                        and row["universe_policy_version"] == universe_policy_version
                        and row["market_rules_version"] == market_rules_version
                        and sorted(row["required_components"]) == sorted(required)
                        and self._optional(release_id) == optional)
                if not same:
                    raise Conflict(f"release {release_id} already exists with different content")
                return self.get_release(release_id)
            self.conn.execute(
                "INSERT INTO decision.release(release_id, release_state, decision_as_of, availability_policy_version,"
                " universe_policy_version, market_rules_version, required_components, created_at)"
                " VALUES (%s,'BUILDING',%s,%s,%s,%s,%s,COALESCE(%s, clock_timestamp()))",
                (release_id, decision_as_of, availability_policy_version, universe_policy_version,
                 market_rules_version, required, created_at))
            for key, policy in optional.items():
                self.conn.execute(
                    "INSERT INTO decision.release_optional_component(release_id, component_key, degradation_policy)"
                    " VALUES (%s,%s,%s)", (release_id, key, policy))
        return self.get_release(release_id)

    def _optional(self, release_id: UUID) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT component_key, degradation_policy FROM decision.release_optional_component WHERE release_id=%s",
            (release_id,)).fetchall()
        return {r["component_key"]: r["degradation_policy"] for r in rows}

    def put_component(self, release_id: UUID, component_key: str, *, component_state: str, artifact_ref: str,
                      input_version_hash: str, artifact_hash: str | None = None, snapshot_id: UUID | None = None,
                      computed_at: datetime | None = None, valid_until: datetime | None = None) -> ComponentValue:
        if component_state not in COMPONENT_STATES:
            raise ReleaseStateError(f"unknown component state {component_state!r}")
        if component_state == "READY" and (artifact_hash is None or computed_at is None):
            raise ReleaseStateError("a READY component needs artifact_hash and computed_at")
        with self.conn.transaction():
            rel = self.conn.execute(
                "SELECT release_state, required_components FROM decision.release WHERE release_id=%s FOR UPDATE",
                (release_id,)).fetchone()
            if rel is None:
                raise NotFound(f"release {release_id}")
            if rel["release_state"] != "BUILDING":
                raise ReleaseStateError(f"release {release_id} is {rel['release_state']}; components are frozen")
            if component_key not in rel["required_components"] and component_key not in self._optional(release_id):
                raise ReleaseStateError(
                    f"component {component_key!r} is neither required nor declared optional with a policy")
            row = self.conn.execute(
                "INSERT INTO decision.release_component(release_id, component_key, component_state, artifact_ref,"
                " artifact_hash, snapshot_id, input_version_hash, computed_at, valid_until)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (release_id, component_key) DO UPDATE SET component_state=EXCLUDED.component_state,"
                " artifact_ref=EXCLUDED.artifact_ref, artifact_hash=EXCLUDED.artifact_hash,"
                " snapshot_id=EXCLUDED.snapshot_id, input_version_hash=EXCLUDED.input_version_hash,"
                " computed_at=EXCLUDED.computed_at, valid_until=EXCLUDED.valid_until"
                f" RETURNING {_COMPONENT_COLS}",
                (release_id, component_key, component_state, artifact_ref, artifact_hash, snapshot_id,
                 input_version_hash, computed_at, valid_until)).fetchone()
        return ComponentValue(**row)

    def mark_ready(self, release_id: UUID, component_key: str, *, artifact_ref: str, artifact_hash: str,
                   input_version_hash: str, snapshot_id: UUID | None = None, computed_at: datetime | None = None,
                   valid_until: datetime | None = None) -> ComponentValue:
        return self.put_component(
            release_id, component_key, component_state="READY", artifact_ref=artifact_ref,
            artifact_hash=artifact_hash, input_version_hash=input_version_hash, snapshot_id=snapshot_id,
            computed_at=computed_at or datetime.now(timezone.utc), valid_until=valid_until)

    def fail_release(self, release_id: UUID) -> None:
        cur = self.conn.execute(
            "UPDATE decision.release SET release_state='FAILED' WHERE release_id=%s AND release_state='BUILDING'",
            (release_id,))
        if cur.rowcount == 0:
            raise ReleaseStateError(f"release {release_id} is not BUILDING")

    # ---- stage / publish ---------------------------------------------------------------------------
    def begin_stage(self, *, stage_id: UUID, slot_key: str, release_id: UUID, input_context_hash: str) -> StageRef:
        try:
            with self.conn.transaction():
                row = self.conn.execute(
                    "SELECT * FROM decision.begin_release_stage(%s,%s,%s,%s)",
                    (stage_id, slot_key, release_id, input_context_hash)).fetchone()
        except pgerr.NoDataFound as exc:
            raise NotFound(f"slot {slot_key!r} or release {release_id} does not exist") from exc
        except pgerr.RaiseException as exc:
            msg = str(exc)
            if "Stage retry content conflict" in msg:
                raise Conflict(f"stage {stage_id} retried with different content") from exc
            raise ReleaseStateError(msg.splitlines()[0]) from exc
        row.pop("created_at")
        return StageRef(**row)

    def publish(self, stage_id: UUID, *, producer_version: str, event_id: UUID | None = None) -> PublishResult:
        event_id = event_id or publication_event_id(stage_id)
        with self.conn.transaction():
            iso = self.conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            if iso != "read committed":
                raise ReleaseStateError(f"publication requires READ COMMITTED, session is {iso}")
            stage = self.conn.execute(
                "SELECT * FROM decision.release_stage WHERE stage_id=%s", (stage_id,)).fetchone()
            if stage is None:
                raise NotFound(f"stage {stage_id}")
            # 1. Parent lock. Every later statement takes a fresh snapshot, so it sees what the previous
            #    lock holder committed.
            slot = self.conn.execute(
                "SELECT * FROM decision.release_slot WHERE slot_key=%s FOR UPDATE", (stage["slot_key"],)).fetchone()
            pub = self.conn.execute(
                "SELECT * FROM decision.release_publication WHERE stage_id=%s", (stage_id,)).fetchone()
            if pub is not None:
                return self._replay(pub, event_id)
            if (stage["stage_generation"] != slot["issued_stage_generation"]
                    or stage["base_pointer_generation"] != slot["pointer_generation"]
                    or stage["base_release_id"] != slot["current_release_id"]):
                raise StaleStage(f"stage {stage_id} (generation {stage['stage_generation']}) is no longer the latest"
                                 f" issued on an unchanged base of slot {stage['slot_key']!r}")
            rel = self.conn.execute(
                "SELECT * FROM decision.release WHERE release_id=%s FOR UPDATE", (stage["release_id"],)).fetchone()
            if rel["release_state"] != "BUILDING":
                raise ReleaseStateError(f"release {rel['release_id']} is {rel['release_state']}, not BUILDING")
            comps = self.conn.execute(
                f"SELECT {_COMPONENT_COLS} FROM decision.release_component WHERE release_id=%s",
                (rel["release_id"],)).fetchall()
            degraded = self._validate(rel, comps)
            release_hash = compute_release_hash(rel, comps)
            try:
                publication = self.conn.execute(
                    "SELECT * FROM decision.publish_release_stage(%s,%s,%s)",
                    (stage_id, release_hash, event_id)).fetchone()
            except pgerr.RaiseException as exc:
                msg = str(exc)
                if "Stale stage" in msg or "backwards" in msg:
                    raise StaleStage(msg.splitlines()[0]) from exc
                if "retry" in msg:
                    raise Conflict(msg.splitlines()[0]) from exc
                raise ReleaseStateError(msg.splitlines()[0]) from exc
            self.outbox.append(OutboxEvent(
                event_id=event_id, event_type="decision.published", schema_version=EVENT_SCHEMA_VERSION,
                aggregate_type="release_slot", aggregate_id=stage["slot_key"],
                aggregate_version=publication["pointer_generation"], occurred_at=publication["published_at"],
                recorded_at=publication["published_at"], producer_version=producer_version,
                payload={"release_id": publication["release_id"], "stage_id": stage_id,
                         "slot_key": stage["slot_key"], "pointer_generation": publication["pointer_generation"],
                         "release_hash": release_hash, "decision_as_of": rel["decision_as_of"],
                         "event_time": publication["published_at"], "degraded_components": degraded}))
            if publication["previous_release_id"] is not None:
                self.conn.execute(
                    "UPDATE decision.release SET release_state='SUPERSEDED'"
                    " WHERE release_id=%s AND release_state='READY'", (publication["previous_release_id"],))
        return PublishResult(
            stage_id=stage_id, slot_key=stage["slot_key"], release_id=publication["release_id"],
            pointer_generation=publication["pointer_generation"],
            previous_release_id=publication["previous_release_id"], event_id=event_id,
            release_hash=release_hash, published_at=publication["published_at"],
            degraded_components=tuple(degraded))

    def _replay(self, pub: Mapping[str, Any], event_id: UUID) -> PublishResult:
        if pub["event_id"] != event_id:
            raise Conflict("publication retry used a different event id")
        rel = self.conn.execute(
            "SELECT release_hash FROM decision.release WHERE release_id=%s", (pub["release_id"],)).fetchone()
        return PublishResult(
            stage_id=pub["stage_id"], slot_key=pub["slot_key"], release_id=pub["release_id"],
            pointer_generation=pub["pointer_generation"], previous_release_id=pub["previous_release_id"],
            event_id=pub["event_id"], release_hash=rel["release_hash"], published_at=pub["published_at"],
            degraded_components=tuple(self._degraded(pub["release_id"])), replayed=True)

    def _degraded(self, release_id: UUID) -> list[str]:
        rows = self.conn.execute(
            "SELECT o.component_key FROM decision.release_optional_component o"
            " LEFT JOIN decision.release_component c USING (release_id, component_key)"
            " WHERE o.release_id=%s AND (c.component_state IS DISTINCT FROM 'READY')"
            " ORDER BY o.component_key", (release_id,)).fetchall()
        return [r["component_key"] for r in rows]

    def _validate(self, rel: Mapping[str, Any], comps: Sequence[Mapping[str, Any]]) -> list[str]:
        """Return the degraded optional components, or raise IncompleteRelease."""
        now = self.conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        by_key = {c["component_key"]: c for c in comps}
        snapshots = {
            c["snapshot_id"] for c in comps if c["component_state"] == "READY" and c["snapshot_id"] is not None
        }
        bad_snapshots: set[UUID] = set()
        if snapshots:
            rows = self.conn.execute(
                "SELECT snapshot_id FROM market.dataset_manifest WHERE snapshot_id = ANY(%s) AND manifest_state<>'READY'",
                (list(snapshots),)).fetchall()
            bad_snapshots = {r["snapshot_id"] for r in rows}

        def problem(c: Mapping[str, Any] | None) -> str | None:
            if c is None:
                return "unrecorded"
            if c["component_state"] != "READY":
                return c["component_state"]
            if c["valid_until"] is not None and c["valid_until"] <= now:
                return "EXPIRED"
            if c["snapshot_id"] in bad_snapshots:
                return "UNPUBLISHED_SNAPSHOT"
            return None

        missing = []
        for key in rel["required_components"]:
            p = problem(by_key.get(key))
            if p:
                missing.append(f"{key}:{p}")
        if missing:
            raise IncompleteRelease(
                f"release {rel['release_id']} cannot be published, required components not usable: "
                + ", ".join(missing), missing=tuple(missing))
        degraded: list[str] = []
        for key, policy in sorted(self._optional(rel["release_id"]).items()):
            c = by_key.get(key)
            p = problem(c)
            if p is None:
                continue
            allowed = (p == "MISSING" and policy == "ALLOW_MISSING") or policy == "ALLOW_ANY" and p != "unrecorded"
            if not allowed:
                raise IncompleteRelease(
                    f"optional component {key!r} is {p} and policy {policy} does not allow degraded publication",
                    missing=(f"{key}:{p}",))
            degraded.append(key)
        return degraded

    # ---- reading ---------------------------------------------------------------------------------
    def current(self, slot_key: str) -> CurrentRelease | None:
        row = self.conn.execute(
            "SELECT s.slot_key, s.pointer_generation, r.release_id, r.release_hash, r.decision_as_of, r.published_at"
            " FROM decision.release_slot s JOIN decision.release r ON r.release_id=s.current_release_id"
            " WHERE s.slot_key=%s", (slot_key,)).fetchone()
        if row is None:
            return None
        return CurrentRelease(slot_key=row["slot_key"], release_id=row["release_id"],
                              pointer_generation=row["pointer_generation"], release_hash=row["release_hash"],
                              decision_as_of=row["decision_as_of"], published_at=row["published_at"],
                              degraded_components=tuple(self._degraded(row["release_id"])))

    def get_release(self, release_id: UUID) -> ReleaseView:
        r = self.conn.execute("SELECT * FROM decision.release WHERE release_id=%s", (release_id,)).fetchone()
        if r is None:
            raise NotFound(f"release {release_id}")
        comps = self.conn.execute(
            f"SELECT {_COMPONENT_COLS} FROM decision.release_component WHERE release_id=%s ORDER BY component_key",
            (release_id,)).fetchall()
        return ReleaseView(
            release_id=r["release_id"], release_state=r["release_state"], decision_as_of=r["decision_as_of"],
            availability_policy_version=r["availability_policy_version"],
            universe_policy_version=r["universe_policy_version"], market_rules_version=r["market_rules_version"],
            required_components=tuple(r["required_components"]), optional_components=self._optional(release_id),
            release_hash=r["release_hash"], created_at=r["created_at"], published_at=r["published_at"],
            components=tuple(ComponentValue(**c) for c in comps))
