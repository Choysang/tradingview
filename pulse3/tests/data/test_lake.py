"""M1-E03: atomic content-addressed writes, immutable manifests, historical replay, pins/leases, GC vs publish race."""
from __future__ import annotations

import threading
from pathlib import Path

import polars as pl
import pytest

from pulse.adapters.lake import LakeStore


def frame(start: int, n: int = 3) -> pl.DataFrame:
    return pl.DataFrame({"k": list(range(start, start + n)), "v": [float(i) for i in range(start, start + n)]})


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_write_is_content_addressed_atomic_and_leaves_no_temp(lake):
    a = lake.put_bytes(b"hello", owner="job1")
    b = lake.put_bytes(b"hello", owner="job2")
    assert a.sha256 == b.sha256 and a.rel_path == b.rel_path
    assert (lake.root / a.rel_path).read_bytes() == b"hello"
    assert list((lake.root / "tmp").iterdir()) == []
    assert lake.read_object(a) == b"hello"


def test_corrupted_object_and_manifest_fail_integrity(lake):
    ref = lake.put_bytes(b"payload", owner="j")
    m = lake.commit_manifest("ds", [ref], owner="j")
    (lake.root / ref.rel_path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        lake.read_object(ref)
    (lake.root / m.rel_path).write_bytes(b'{"format":1}')
    with pytest.raises(ValueError, match="integrity"):
        lake.read_manifest(m.sha256)


def test_manifest_is_immutable_member_closure_and_rejects_unknown_members(lake):
    r1, r2 = lake.put_parquet(frame(0), owner="j"), lake.put_parquet(frame(10), owner="j")
    m = lake.commit_manifest("ds", [r1, r2], owner="j", meta={"rows": 6})
    again = lake.commit_manifest("ds", [r1, r2], owner="j", meta={"rows": 6})
    assert m.sha256 == again.sha256  # same member list -> same identity
    assert m.object_hashes == (r1.sha256, r2.sha256)
    from pulse.adapters.lake import ObjectRef

    with pytest.raises(ValueError, match="not a stored object"):
        lake.commit_manifest("ds", [ObjectRef("0" * 64, "objects/x", 1)], owner="j")


def test_historical_snapshot_replays_frozen_members_after_compaction(lake):
    a, b = lake.put_parquet(frame(0), owner="j"), lake.put_parquet(frame(3), owner="j")
    m1 = lake.commit_manifest("ds", [a, b], owner="j")
    compacted = lake.put_parquet(pl.concat([frame(0), frame(3)]), owner="j")
    m2 = lake.commit_manifest("ds", [compacted], owner="j")
    lake.release("j")
    before = lake.read_snapshot_frames(m1).sort("k")
    # compaction retires a and b from the *current* view, but both manifests stay referenced (READY)
    report = lake.gc(lambda: {m1.sha256, m2.sha256}, min_age_seconds=0)
    assert report.deleted_objects == 0
    assert lake.read_snapshot_frames(m1).sort("k").equals(before)
    assert lake.read_snapshot_frames(m2).sort("k").equals(before)
    # only when the old snapshot is released (no READY reference, no pin) may its exclusive members go
    report = lake.gc(lambda: {m2.sha256}, min_age_seconds=0)
    assert report.deleted_manifests == 1 and report.deleted_objects == 2
    assert lake.read_snapshot_frames(m2).sort("k").equals(before)
    with pytest.raises(FileNotFoundError):
        lake.read_manifest(m1.sha256)


def test_reader_pin_blocks_gc_until_released(lake):
    a = lake.put_parquet(frame(0), owner="writer")
    m = lake.commit_manifest("ds", [a], owner="writer")
    lake.release("writer")
    lake.pin_snapshot(m.sha256, owner="reader", lease_seconds=600)
    assert lake.gc(lambda: set(), min_age_seconds=0).deleted_objects == 0
    lake.release("reader")
    assert lake.gc(lambda: set(), min_age_seconds=0).deleted_objects == 1


def test_staged_batch_is_pinned_until_lease_expiry_and_owner_is_checked():
    import tempfile

    clock = Clock()
    with tempfile.TemporaryDirectory() as d:
        lake = LakeStore(Path(d) / "lake", clock=clock)
        ref = lake.put_parquet(frame(0), owner="batch-7", lease_seconds=100)
        m = lake.commit_manifest("ds", [ref], owner="batch-7", lease_seconds=100)
        # unpublished + unexpired lease: never deleted
        assert lake.gc(lambda: set(), min_age_seconds=0).deleted_objects == 0
        clock.t += 500  # lease expired, but the job still has a live owner -> keep and re-check later
        assert lake.gc(lambda: set(), owner_active=lambda o: o == "batch-7", min_age_seconds=0).deleted_objects == 0
        # expired lease, no owner, not referenced: reclaimed
        r = lake.gc(lambda: set(), owner_active=lambda o: False, min_age_seconds=0)
        assert r.deleted_objects == 1 and r.deleted_manifests == 1 and r.expired_pins_released >= 2
        assert not lake.exists(ref.sha256) and not lake.exists(m.sha256)


def test_committed_unpublished_manifest_is_kept_via_reference_query(lake):
    ref = lake.put_parquet(frame(0), owner="j")
    m = lake.commit_manifest("ds", [ref], owner="j")
    lake.release("j")  # worker died after PG recorded COMMITTED_UNPUBLISHED
    assert lake.gc(lambda: {m.sha256}, min_age_seconds=0).deleted_objects == 0
    assert lake.read_snapshot_frames(m).height == 3


def test_publish_gc_race_never_loses_a_member(lake):
    """GC runs in a tight loop while a publisher stages, 'commits to PG', then unpins. Nothing may vanish."""
    ready: set[str] = set()
    ready_lock = threading.Lock()
    stop = threading.Event()
    errors: list[str] = []

    def gc_loop() -> None:
        while not stop.is_set():
            try:
                lake.gc(lambda: set(ready), min_age_seconds=0)
                stop.wait(0.003)  # SQLite write locks are not fair; give the publisher a window
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(repr(exc))
                return

    t = threading.Thread(target=gc_loop)
    t.start()
    published = []
    try:
        for i in range(25):
            owner = f"pub-{i}"
            members = [lake.put_parquet(frame(i * 100 + j * 10), owner=owner) for j in range(3)]
            m = lake.commit_manifest("race", members, owner=owner)
            with ready_lock:  # the "PG commit": manifest becomes READY ...
                ready.add(m.sha256)
            lake.release(owner)  # ... and only afterwards is the stage pin released
            published.append(m)
    finally:
        stop.set()
        t.join()
    assert not errors
    for m in published:
        assert lake.read_snapshot_frames(m).height == 9
    lake.gc(lambda: set(ready), min_age_seconds=0)
    assert all(lake.read_snapshot_frames(m).height == 9 for m in published)


def test_releasing_the_pin_before_the_reference_exists_is_unsafe(lake):
    """Negative control: the protocol order (pin until READY) is what protects the data."""
    ref = lake.put_parquet(frame(0), owner="early")
    m = lake.commit_manifest("ds", [ref], owner="early")
    lake.release("early")  # wrong: unpinned before PG knows about the manifest
    lake.gc(lambda: set(), min_age_seconds=0)
    with pytest.raises(FileNotFoundError):
        lake.read_snapshot_frames(m)


def test_raw_objects_are_never_collected_by_object_gc(lake):
    raw = lake.put_bytes(b"<raw response>", owner="j", kind="raw")
    lake.release("j")
    lake.gc(lambda: set(), min_age_seconds=0)
    assert lake.read_object(raw) == b"<raw response>"
