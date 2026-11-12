"""Transactional outbox, pollUpdates cursor and durable consumers on real PostgreSQL (DB-A10, DB-A11)."""
from __future__ import annotations

import threading
from uuid import uuid4

import psycopg
import pytest
from w02_helpers import now

from pulse import contracts
from pulse.adapters.postgres import db
from pulse.adapters.postgres.outbox import PgOutbox, decode_cursor, encode_cursor, to_update_page
from pulse.application.ports.delivery import OutboxEvent
from pulse.application.ports.errors import Conflict, CursorInvalid

pytestmark = pytest.mark.pg


def agg() -> str:
    return f"agg-{uuid4().hex[:10]}"


def ev(aggregate_id: str, version: int, **payload) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid4(), event_type="decision.published", schema_version="3.0.0", aggregate_type="test_scope",
        aggregate_id=aggregate_id, aggregate_version=version, occurred_at=now(), producer_version="t",
        payload={"release_id": str(uuid4()), "n": version, **payload})


def stream(a: str) -> str:
    return f"test_scope:{a}"


def fill(conn, a: str, n: int) -> list[OutboxEvent]:
    box = PgOutbox(conn)
    events = [ev(a, i) for i in range(1, n + 1)]
    for e in events:
        box.append(e)
    conn.commit()
    return events


def test_append_is_idempotent_and_detects_conflicts(conn):
    box = PgOutbox(conn)
    a = agg()
    e = ev(a, 1)
    assert box.append(e).created is True
    assert box.append(e).created is False                         # identical retry
    other_payload = OutboxEvent(**{**e.__dict__, "payload": {"changed": True}})
    with pytest.raises(Conflict):
        box.append(other_payload)                                  # same id, different content
    with pytest.raises(Conflict):
        box.append(ev(a, 1))                                       # different id claiming the same aggregate version
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM ops.outbox WHERE aggregate_id=%s", (a,)).fetchone()["n"] == 1


def test_outbox_content_is_immutable_but_bookkeeping_is_not(conn):
    a = agg()
    e = fill(conn, a, 1)[0]
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute("UPDATE ops.outbox SET payload='{}' WHERE event_id=%s", (e.event_id,))
    conn.rollback()
    conn.execute("UPDATE ops.outbox SET published_at=clock_timestamp(), publication_attempts=1 WHERE event_id=%s",
                 (e.event_id,))
    conn.commit()


def test_rollback_leaves_no_event_and_no_sequence_hole(conn, dsn):
    a = agg()
    box = PgOutbox(conn)
    box.append(ev(a, 1))
    with pytest.raises(RuntimeError):
        with conn.transaction():                                   # the caller's business transaction
            box.append(ev(a, 2))
            raise RuntimeError("business step failed")
    box.append(ev(a, 2))
    head = box.head(stream(a))
    assert head.last_sequence == 2
    seqs = [e.stream_sequence for e in box._fetch_after(stream(a), head.stream_epoch, 0, 10)]
    assert seqs == [1, 2]


def test_stream_sequence_follows_commit_order_not_start_order(dsn):
    """DB-A11: a transaction that started first but commits later must not be skipped by a cursor."""
    a = agg()
    c1, c2, cr = db.connect(dsn), db.connect(dsn), db.connect(dsn)
    e1, e2 = ev(a, 1), ev(a, 2)
    tx1 = c1.transaction()
    tx1.__enter__()
    PgOutbox(c1).append(e1)                                        # tx1 holds the stream counter, uncommitted
    done = threading.Event()

    def second():
        PgOutbox(c2).append(e2)
        done.set()

    t = threading.Thread(target=second)
    t.start()
    assert not done.wait(1.0), "second writer must wait for the first writer's commit"
    box = PgOutbox(cr)
    assert box.head(stream(a)) is None or box.head(stream(a)).last_sequence == 0   # nothing visible yet
    tx1.__exit__(None, None, None)                                 # COMMIT tx1
    t.join(timeout=10)
    assert done.is_set()
    head = box.head(stream(a))
    got = box._fetch_after(stream(a), head.stream_epoch, 0, 10)
    assert [(g.stream_sequence, g.event_id) for g in got] == [(1, e1.event_id), (2, e2.event_id)]
    for c in (c1, c2, cr):
        c.close()


def test_poll_cursor_roundtrip_and_update_page_contract(conn):
    a = agg()
    events = fill(conn, a, 5)
    box = PgOutbox(conn)
    first = box.poll(stream(a), None)
    assert first.reset_required and first.events == ()            # client must fetch the snapshot, then resume here
    assert decode_cursor(first.resume_cursor) == (stream(a), "1", 5)
    start = encode_cursor(stream(a), "1", 0)
    p1 = box.poll(stream(a), start, limit=2)
    assert [e.event_id for e in p1.events] == [events[0].event_id, events[1].event_id] and not p1.reset_required
    p2 = box.poll(stream(a), p1.resume_cursor, limit=10)
    assert [e.event_id for e in p2.events] == [e.event_id for e in events[2:]]
    p3 = box.poll(stream(a), p2.resume_cursor)
    assert p3.events == () and p3.resume_cursor == p2.resume_cursor and not p3.reset_required
    wire = to_update_page(p2)
    contracts.check("UpdatePage", wire)
    last = p2.events[-1]
    assert (last.scope, last.release_id, last.event_time) == (stream(a), events[-1].payload["release_id"], last.occurred_at)
    # new events appear after the same cursor (resumable)
    PgOutbox(conn).append(ev(a, 6))
    conn.commit()
    assert len(box.poll(stream(a), p3.resume_cursor).events) == 1


def test_poll_rejects_foreign_or_malformed_cursor(conn):
    a, b = agg(), agg()
    fill(conn, a, 1)
    box = PgOutbox(conn)
    with pytest.raises(CursorInvalid):
        box.poll(stream(a), encode_cursor(stream(b), "1", 0))
    with pytest.raises(CursorInvalid):
        box.poll(stream(a), "not-a-cursor!!")
    assert box.poll(stream(a), encode_cursor(stream(a), "1", 99)).reset_required      # from the future


def test_expired_cursor_requires_full_refetch(conn):
    a = agg()
    fill(conn, a, 6)
    box = PgOutbox(conn)
    assert box.prune(stream(a), keep_last=2) == 4
    head = box.head(stream(a))
    assert (head.retained_from_sequence, head.last_sequence) == (5, 6)
    conn.commit()
    expired = box.poll(stream(a), encode_cursor(stream(a), "1", 2))
    assert expired.reset_required and expired.events == ()
    assert decode_cursor(expired.resume_cursor)[2] == 6
    ok = box.poll(stream(a), encode_cursor(stream(a), "1", 4))                    # exactly at the retention edge
    assert not ok.reset_required and [e.stream_sequence for e in ok.events] == [5, 6]
    # outbox rows survive pruning (publication facts reference them)
    assert conn.execute("SELECT count(*) AS n FROM ops.outbox WHERE aggregate_id=%s", (a,)).fetchone()["n"] == 6


def test_epoch_rotation_invalidates_old_cursors(conn):
    a = agg()
    fill(conn, a, 3)
    box = PgOutbox(conn)
    old = encode_cursor(stream(a), "1", 3)
    new_epoch = conn.execute("SELECT ops.rotate_outbox_epoch(%s) AS e", (stream(a),)).fetchone()["e"]
    conn.commit()
    res = box.poll(stream(a), old)
    assert res.reset_required and res.stream_epoch == new_epoch
    PgOutbox(conn).append(ev(a, 4))
    conn.commit()
    assert box.head(stream(a)).last_sequence == 1                                  # new sequence space


def test_consumers_have_independent_cursors(conn):
    a = agg()
    events = fill(conn, a, 4)
    box = PgOutbox(conn)
    fast = box.next_batch("fast", stream(a))
    for e in fast.events:
        box.consume("fast", e, lambda c, e: None)
    slow = box.next_batch("slow", stream(a), limit=1)
    box.consume("slow", slow.events[0], lambda c, e: None)
    conn.commit()
    assert box.checkpoint("fast", stream(a)).contiguous_stream_sequence == 4
    assert box.checkpoint("slow", stream(a)).contiguous_stream_sequence == 1
    nxt = box.next_batch("slow", stream(a))
    assert [e.event_id for e in nxt.events] == [e.event_id for e in events[1:]]
    assert box.next_batch("fast", stream(a)).events == ()


def test_duplicate_delivery_runs_effect_once_and_hash_mismatch_conflicts(conn):
    a = agg()
    fill(conn, a, 1)
    box = PgOutbox(conn)
    e = box.next_batch("c", stream(a)).events[0]
    calls = []
    assert box.consume("c", e, lambda c, ev_: calls.append(ev_.event_id)).status == "applied"
    again = box.consume("c", e, lambda c, ev_: calls.append("DUP"))
    assert again.status == "duplicate" and calls == [e.event_id]
    tampered = type(e)(**{**e.__dict__, "payload_hash": "0" * 64})
    with pytest.raises(Conflict):
        box.consume("c", tampered, lambda c, ev_: calls.append("X"))
    conn.rollback()
    # a different logical consumer still applies the same event once
    assert box.consume("other", e, lambda c, ev_: calls.append("other")).status == "applied"
    assert calls == [e.event_id, "other"]


def test_failed_handler_rolls_back_inbox_so_event_is_redelivered(conn):
    a = agg()
    fill(conn, a, 1)
    box = PgOutbox(conn)
    e = box.next_batch("c", stream(a)).events[0]

    def boom(c, ev_):
        c.execute("CREATE TEMP TABLE IF NOT EXISTS fx(x int)")
        raise RuntimeError("effect failed")

    with pytest.raises(RuntimeError):
        box.consume("c", e, boom)
    conn.rollback()
    assert box.next_batch("c", stream(a)).events[0].event_id == e.event_id
    assert box.consume("c", e, lambda c, ev_: None).status == "applied"


def test_out_of_order_completion_keeps_contiguous_checkpoint(conn):
    """DB-A11: events 10 and 12 done while 11 is pending -> checkpoint stays 10."""
    a = agg()
    events = fill(conn, a, 12)
    box = PgOutbox(conn)
    byseq = {e.stream_sequence: e for e in box._fetch_after(stream(a), "1", 0, 20)}
    noop = lambda c, e: None  # noqa: E731
    for s in range(1, 10):
        box.consume("c", byseq[s], noop)
    assert box.consume("c", byseq[10], noop).checkpoint == 10
    assert box.consume("c", byseq[12], noop).checkpoint == 10
    cp = box.checkpoint("c", stream(a))
    assert cp.contiguous_stream_sequence == 10 and cp.oldest_pending_at is not None
    pending = box.next_batch("c", stream(a))
    assert [e.stream_sequence for e in pending.events] == [11]                    # 12 already done, not re-offered
    assert box.consume("c", byseq[11], noop).checkpoint == 12
    assert len(events) == 12


def test_parallel_workers_of_one_consumer_apply_each_event_once(dsn):
    a = agg()
    c0 = db.connect(dsn)
    fill(c0, a, 8)
    delivered = PgOutbox(c0)._fetch_after(stream(a), "1", 0, 20)
    c0.close()
    applied, lock, errors = [], threading.Lock(), []

    def worker(offset):
        c = db.connect(dsn)
        try:
            box = PgOutbox(c)
            for e in delivered[offset::2] + delivered:                              # second pass = redelivery
                box.consume("pc", e, lambda cn, ev_: (lock.acquire(), applied.append(ev_.stream_sequence), lock.release()))
        except Exception as exc:    # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert sorted(applied) == list(range(1, 9))
    c = db.connect(dsn)
    assert PgOutbox(c).checkpoint("pc", stream(a)).contiguous_stream_sequence == 8
    c.close()


def test_consumer_behind_retention_must_resync(conn):
    a = agg()
    fill(conn, a, 6)
    box = PgOutbox(conn)
    box.prune(stream(a), keep_last=2)
    batch = box.next_batch("late", stream(a))
    assert batch.reset_required and batch.events == ()
    cp = box.resync("late", stream(a))
    assert cp.contiguous_stream_sequence == 6
    assert not box.next_batch("late", stream(a)).reset_required
