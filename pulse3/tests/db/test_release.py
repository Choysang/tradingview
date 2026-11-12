"""AnalysisRelease on real PostgreSQL: completeness, CAS publication, concurrency (M1-E03, DB-A12, DB-A13)."""
from __future__ import annotations

import threading
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import h, new_release, now, ready_all, seed_manifest, staged

from pulse import contracts
from pulse.adapters.postgres import db
from pulse.adapters.postgres.outbox import PgOutbox, encode_cursor, to_update_page
from pulse.adapters.postgres.release import PgAnalysisRelease, publication_event_id
from pulse.application.ports.errors import (
    Conflict,
    IncompleteRelease,
    NotFound,
    ReleaseStateError,
    StaleStage,
)

pytestmark = pytest.mark.pg


def slot() -> str:
    return f"slot:{uuid4().hex[:10]}"


def stream(s: str) -> str:
    return f"release_slot:{s}"


def test_publish_requires_every_required_component(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    rid, stage = staged(rel, s)
    ready_all(rel, rid, ["ranking"])                      # "plans" still missing
    with pytest.raises(IncompleteRelease) as e:
        rel.publish(stage.stage_id, producer_version="t1")
    assert e.value.missing == ("plans:unrecorded",)
    assert rel.current(s) is None
    assert rel.get_release(rid).release_state == "BUILDING"
    # a component that exists but is not READY does not count either
    rel.put_component(rid, "plans", component_state="FAILED", artifact_ref="err", input_version_hash=h("x"))
    with pytest.raises(IncompleteRelease) as e:
        rel.publish(stage.stage_id, producer_version="t1")
    assert e.value.missing == ("plans:FAILED",)
    assert rel.current(s) is None


def test_publish_ready_writes_pointer_outbox_and_supersedes(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    r1, st1 = staged(rel, s)
    ready_all(rel, r1, ["ranking", "plans"])
    res = rel.publish(st1.stage_id, producer_version="t1")
    assert (res.pointer_generation, res.previous_release_id, res.replayed) == (1, None, False)
    cur = rel.current(s)
    assert cur.release_id == r1 and cur.release_hash == res.release_hash
    assert rel.get_release(r1).release_state == "READY"
    # outbox event written in the same transaction, discoverable through the delivery stream
    ev = PgOutbox(conn)._fetch_after(stream(s), "1", 0, 10)
    assert [e.event_id for e in ev] == [res.event_id] == [publication_event_id(st1.stage_id)]
    assert ev[0].release_id == str(r1) and ev[0].aggregate_version == 1
    # second generation supersedes the first
    r2 = new_release(rel)
    st2 = rel.begin_stage(stage_id=uuid4(), slot_key=s, release_id=r2, input_context_hash=h("2"))
    assert (st2.base_pointer_generation, st2.base_release_id) == (1, r1)
    ready_all(rel, r2, ["ranking", "plans"])
    res2 = rel.publish(st2.stage_id, producer_version="t1")
    assert (res2.pointer_generation, res2.previous_release_id) == (2, r1)
    assert rel.get_release(r1).release_state == "SUPERSEDED"
    assert rel.current(s).release_id == r2
    conn.commit()


def test_publish_retry_is_idempotent(conn):
    rel = PgAnalysisRelease(conn)
    rid, stage = staged(rel, slot())
    ready_all(rel, rid, ["ranking", "plans"])
    a = rel.publish(stage.stage_id, producer_version="t1")
    b = rel.publish(stage.stage_id, producer_version="t1")
    assert b.replayed
    assert (a.release_hash, a.event_id, a.pointer_generation) == (b.release_hash, b.event_id, b.pointer_generation)
    with pytest.raises(Conflict):
        rel.publish(stage.stage_id, producer_version="t1", event_id=uuid4())
    n = conn.execute("SELECT count(*) AS n FROM decision.release_publication WHERE stage_id=%s",
                     (stage.stage_id,)).fetchone()["n"]
    assert n == 1


def test_stage_retry_with_other_content_conflicts(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    rid, stage = staged(rel, s)
    again = rel.begin_stage(stage_id=stage.stage_id, slot_key=s, release_id=rid, input_context_hash=stage.input_context_hash)
    assert again == stage
    with pytest.raises(Conflict):
        rel.begin_stage(stage_id=stage.stage_id, slot_key=s, release_id=rid, input_context_hash=h("other"))


def test_stale_stage_cannot_replace_newer(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    r_old, st_old = staged(rel, s)
    r_new = new_release(rel)
    st_new = rel.begin_stage(stage_id=uuid4(), slot_key=s, release_id=r_new, input_context_hash=h("n"))
    ready_all(rel, r_old, ["ranking", "plans"])
    ready_all(rel, r_new, ["ranking", "plans"])
    rel.publish(st_new.stage_id, producer_version="t")
    with pytest.raises(StaleStage):
        rel.publish(st_old.stage_id, producer_version="t")        # older stage finishing late
    assert rel.current(s).release_id == r_new
    assert rel.get_release(r_old).release_state == "BUILDING"


def test_older_stage_is_stale_even_before_newer_publishes(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    r_old, st_old = staged(rel, s)
    r_new = new_release(rel)
    rel.begin_stage(stage_id=uuid4(), slot_key=s, release_id=r_new, input_context_hash=h("n"))
    ready_all(rel, r_old, ["ranking", "plans"])
    with pytest.raises(StaleStage):
        rel.publish(st_old.stage_id, producer_version="t")
    assert rel.current(s) is None


def test_as_of_cannot_move_backwards(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    r1, st1 = staged(rel, s, as_of=now())
    ready_all(rel, r1, ["ranking", "plans"])
    rel.publish(st1.stage_id, producer_version="t")
    r2 = new_release(rel, as_of=now() - timedelta(hours=1))
    st2 = rel.begin_stage(stage_id=uuid4(), slot_key=s, release_id=r2, input_context_hash=h("b"))
    ready_all(rel, r2, ["ranking", "plans"])
    with pytest.raises(StaleStage):
        rel.publish(st2.stage_id, producer_version="t")
    assert rel.current(s).release_id == r1


def test_optional_components_follow_explicit_policy(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    rid, stage = staged(rel, s, optional={"evidence": "ALLOW_MISSING", "bars": "ALLOW_ANY"})
    ready_all(rel, rid, ["ranking", "plans"])
    rel.put_component(rid, "bars", component_state="FAILED", artifact_ref="err", input_version_hash=h("b"))
    with pytest.raises(IncompleteRelease, match="evidence"):      # no row recorded: never silently omitted
        rel.publish(stage.stage_id, producer_version="t")
    rel.put_component(rid, "evidence", component_state="STALE", artifact_ref="x", input_version_hash=h("e"))
    with pytest.raises(IncompleteRelease, match="ALLOW_MISSING"):  # STALE is not MISSING
        rel.publish(stage.stage_id, producer_version="t")
    rel.put_component(rid, "evidence", component_state="MISSING", artifact_ref="absent:evidence",
                      input_version_hash=h("e"))
    res = rel.publish(stage.stage_id, producer_version="t")
    assert res.degraded_components == ("bars", "evidence")
    assert rel.current(s).degraded_components == ("bars", "evidence")
    ev = PgOutbox(conn)._fetch_after(stream(s), "1", 0, 5)[0]
    assert ev.payload["degraded_components"] == ["bars", "evidence"]


def test_undeclared_or_frozen_components_rejected(conn):
    rel = PgAnalysisRelease(conn)
    rid, stage = staged(rel, slot())
    with pytest.raises(ReleaseStateError, match="neither required nor declared"):
        rel.put_component(rid, "mystery", component_state="MISSING", artifact_ref="x", input_version_hash=h("m"))
    ready_all(rel, rid, ["ranking", "plans"])
    rel.publish(stage.stage_id, producer_version="t")
    with pytest.raises(ReleaseStateError):
        rel.mark_ready(rid, "ranking", artifact_ref="y", artifact_hash=h("y"), input_version_hash=h("y"))
    # the database refuses the same mutation when the repository is bypassed
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE decision.release_component SET artifact_ref='z' WHERE release_id=%s", (rid,))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE decision.release SET universe_policy_version='u2' WHERE release_id=%s", (rid,))
    conn.rollback()


def test_optional_policy_is_frozen_and_disjoint_from_required(conn):
    rel = PgAnalysisRelease(conn)
    with pytest.raises(ReleaseStateError, match="both required and optional"):
        rel.create_release(release_id=uuid4(), decision_as_of=now(), availability_policy_version="a",
                           universe_policy_version="u", market_rules_version="r",
                           required_components=["x"], optional_components={"x": "ALLOW_ANY"})
    rid = new_release(rel, required=("a",))
    with pytest.raises(psycopg.errors.RaiseException, match="required"):
        conn.execute("INSERT INTO decision.release_optional_component VALUES (%s,'a','ALLOW_ANY')", (rid,))
    conn.rollback()


def test_expired_and_unpublished_snapshot_components_block(conn):
    rel = PgAnalysisRelease(conn)
    rid, stage = staged(rel, slot())
    ready_all(rel, rid, ["ranking"])
    rel.mark_ready(rid, "plans", artifact_ref="p", artifact_hash=h("p"), input_version_hash=h("p"),
                   valid_until=now() - timedelta(seconds=1))
    with pytest.raises(IncompleteRelease, match="plans:EXPIRED"):
        rel.publish(stage.stage_id, producer_version="t")
    bad = seed_manifest(conn, "COMMITTED_UNPUBLISHED")
    rel.mark_ready(rid, "plans", artifact_ref="p", artifact_hash=h("p"), input_version_hash=h("p"), snapshot_id=bad)
    with pytest.raises(IncompleteRelease, match="plans:UNPUBLISHED_SNAPSHOT"):
        rel.publish(stage.stage_id, producer_version="t")
    good = seed_manifest(conn, "READY")
    rel.mark_ready(rid, "plans", artifact_ref="p", artifact_hash=h("p"), input_version_hash=h("p"), snapshot_id=good)
    assert rel.publish(stage.stage_id, producer_version="t").pointer_generation == 1


def test_database_rejects_incomplete_ready_even_if_repository_bypassed(dsn):
    """DB-A12: the deferred triggers are the backstop for any caller that skips PgAnalysisRelease."""
    c = db.connect(dsn)
    rel = PgAnalysisRelease(c)
    s = slot()
    rid, stage = staged(rel, s)
    ready_all(rel, rid, ["ranking"])                      # plans missing
    c.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="missing or unready"):
        c.execute("SELECT * FROM decision.publish_release_stage(%s,%s,%s)", (stage.stage_id, h("hh"), uuid4()))
        c.commit()
    c.rollback()
    # all components present but no matching outbox row: the publication/outbox deferred checks reject COMMIT
    ready_all(rel, rid, ["plans"])
    c.commit()
    with pytest.raises(psycopg.Error):
        c.execute("SELECT * FROM decision.publish_release_stage(%s,%s,%s)", (stage.stage_id, h("hh"), uuid4()))
        c.commit()
    c.rollback()
    assert PgAnalysisRelease(c).current(s) is None
    assert PgAnalysisRelease(c).get_release(rid).release_state == "BUILDING"
    c.close()


def test_missing_objects_raise_typed_errors(conn):
    rel = PgAnalysisRelease(conn)
    with pytest.raises(NotFound):
        rel.get_release(uuid4())
    with pytest.raises(NotFound):
        rel.publish(uuid4(), producer_version="t")
    rel.ensure_slot("s-nf")
    with pytest.raises(NotFound):
        rel.begin_stage(stage_id=uuid4(), slot_key="s-nf", release_id=uuid4(), input_context_hash=h("q"))


def test_published_event_validates_as_update_page(conn):
    rel = PgAnalysisRelease(conn)
    s = slot()
    rid, stage = staged(rel, s)
    ready_all(rel, rid, ["ranking", "plans"])
    rel.publish(stage.stage_id, producer_version="t")
    box = PgOutbox(conn)
    first = box.poll(stream(s), None)
    assert first.reset_required and first.events == ()
    page = box.poll(stream(s), encode_cursor(stream(s), first.stream_epoch, 0))
    wire = to_update_page(page)
    contracts.check("UpdatePage", wire)
    assert wire["events"][0]["event_type"] == "decision.published"
    assert wire["events"][0]["payload"]["release_id"] == str(rid)


# ---------------------------------------------------------------------------------------------
# Concurrency: real second sessions
# ---------------------------------------------------------------------------------------------
def _build(dsn, s):
    c = db.connect(dsn)
    rel = PgAnalysisRelease(c)
    rel.ensure_slot(s)
    rid = new_release(rel)
    ready_all(rel, rid, ["ranking", "plans"])
    c.commit()
    stage = rel.begin_stage(stage_id=uuid4(), slot_key=s, release_id=rid, input_context_hash=h(str(rid)))
    c.commit()
    c.close()
    return rid, stage


def test_two_publishers_of_one_stage_yield_one_pointer(dsn):
    s = slot()
    rid, stage = _build(dsn, s)
    results, errors = [], []
    barrier = threading.Barrier(2)

    def run():
        c = db.connect(dsn)
        try:
            barrier.wait()
            results.append(PgAnalysisRelease(c).publish(stage.stage_id, producer_version="t"))
            c.commit()
        except Exception as e:      # noqa: BLE001 - collected and asserted below
            errors.append(e)
        finally:
            c.close()

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert sorted(r.replayed for r in results) == [False, True]
    c = db.connect(dsn)
    row = c.execute("SELECT pointer_generation AS g, current_release_id AS r FROM decision.release_slot"
                    " WHERE slot_key=%s", (s,)).fetchone()
    assert row == {"g": 1, "r": rid}
    assert c.execute("SELECT count(*) AS n FROM decision.release_publication WHERE slot_key=%s", (s,)).fetchone()["n"] == 1
    assert c.execute("SELECT count(*) AS n FROM ops.outbox WHERE aggregate_id=%s", (s,)).fetchone()["n"] == 1
    c.close()


def test_two_stages_race_older_one_loses_and_pointer_is_consistent(dsn):
    for _ in range(5):
        s = slot()
        c0 = db.connect(dsn)
        rel0 = PgAnalysisRelease(c0)
        rel0.ensure_slot(s)
        ra, rb = new_release(rel0), new_release(rel0)
        ready_all(rel0, ra, ["ranking", "plans"])
        ready_all(rel0, rb, ["ranking", "plans"])
        c0.commit()
        sa = rel0.begin_stage(stage_id=uuid4(), slot_key=s, release_id=ra, input_context_hash=h("a"))
        sb = rel0.begin_stage(stage_id=uuid4(), slot_key=s, release_id=rb, input_context_hash=h("b"))
        c0.commit()
        c0.close()
        barrier = threading.Barrier(2)
        outcome = {}

        def run(stage, outcome=outcome, barrier=barrier):
            c = db.connect(dsn)
            try:
                barrier.wait()
                outcome[stage.stage_id] = PgAnalysisRelease(c).publish(stage.stage_id, producer_version="t")
                c.commit()
            except StaleStage as e:
                outcome[stage.stage_id] = e
            finally:
                c.close()

        ts = [threading.Thread(target=run, args=(x,)) for x in (sa, sb)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert isinstance(outcome[sa.stage_id], StaleStage)       # sa has an older generation than sb
        assert outcome[sb.stage_id].pointer_generation == 1
        c = db.connect(dsn)
        assert PgAnalysisRelease(c).current(s).release_id == rb
        c.close()


def test_begin_stage_waits_for_slot_lock_then_captures_committed_pointer(dsn):
    """Session 1 holds the slot lock inside an uncommitted publication; session 2 must block, then read fresh."""
    s = slot()
    r1, st1 = _build(dsn, s)
    c1, c2, c0 = db.connect(dsn), db.connect(dsn), db.connect(dsn)
    rel0 = PgAnalysisRelease(c0)
    r2 = new_release(rel0)
    ready_all(rel0, r2, ["ranking", "plans"])
    c0.commit()
    tx = c1.transaction()
    tx.__enter__()
    res1 = PgAnalysisRelease(c1).publish(st1.stage_id, producer_version="t")     # uncommitted
    holder = {}

    def second():
        holder["st2"] = PgAnalysisRelease(c2).begin_stage(
            stage_id=uuid4(), slot_key=s, release_id=r2, input_context_hash=h("2"))
        c2.commit()

    t = threading.Thread(target=second)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive(), "begin_stage must wait for the slot lock"
    tx.__exit__(None, None, None)                                                   # COMMIT session 1
    t.join(timeout=10)
    assert not t.is_alive()
    st2 = holder["st2"]
    assert (st2.base_pointer_generation, st2.base_release_id) == (1, r1)            # fresh snapshot after lock
    res2 = PgAnalysisRelease(c2).publish(st2.stage_id, producer_version="t")
    c2.commit()
    assert (res1.pointer_generation, res2.pointer_generation) == (1, 2)
    for c in (c0, c1, c2):
        c.close()


def test_component_write_racing_publication_cannot_alter_ready_release(dsn):
    """DB-A13: a component write waits on the release lock and is rejected once the release is READY."""
    s = slot()
    rid, stage = _build(dsn, s)
    c1, c2 = db.connect(dsn), db.connect(dsn)
    tx = c1.transaction()
    tx.__enter__()
    res = PgAnalysisRelease(c1).publish(stage.stage_id, producer_version="t")       # holds release lock, uncommitted
    outcome = {}

    def writer():
        try:
            PgAnalysisRelease(c2).mark_ready(rid, "ranking", artifact_ref="changed", artifact_hash=h("changed"),
                                             input_version_hash=h("c"))
            c2.commit()
            outcome["w"] = "written"
        except ReleaseStateError as e:
            c2.rollback()
            outcome["w"] = e

    t = threading.Thread(target=writer)
    t.start()
    t.join(timeout=1.0)
    assert t.is_alive(), "component write must wait for the release row lock"
    tx.__exit__(None, None, None)
    t.join(timeout=10)
    assert isinstance(outcome["w"], ReleaseStateError)
    view = PgAnalysisRelease(c1).get_release(rid)
    assert view.release_state == "READY" and view.release_hash == res.release_hash
    assert next(c for c in view.components if c.component_key == "ranking").artifact_ref == "file:///ranking"
    c1.close()
    c2.close()
