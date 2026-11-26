"""Recorder core against an in-memory sink: dedupe, revision chain, source vs received time, blocks, bad answers, cadence, restart."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, time, timezone

import pytest

from pulse.adapters.sources import native_news
from pulse.application.native_news_recorder import NewsRecorder, content_hash, default_feeds

from .support import CLS, EM, THS, Clock, MemSink, World

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 0, 5, tzinfo=UTC)      # 08:05 CST on a Thursday


def make(tmp_path, world=None, sink=None, clock=None, **kw):
    world, sink, clock = world or World(), sink or MemSink(), clock or Clock(T0)
    clients = world.clients(clock)
    rec = NewsRecorder(tmp_path / "day", sink, default_feeds(clients), clock=clock, sleep=clock.sleep, **kw)
    return rec, world, sink, clock, clients


def state(rec, origin):
    return next(s for s in rec.states if s.feed.origin == origin)


def test_first_poll_records_the_backlog_with_our_receipt_time_and_the_feeds_own_times(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    assert rec.poll(state(rec, CLS)) == 50
    msgs = sink.all_messages(CLS)
    assert len(msgs) == 50 and {m["received_at"] for m in msgs} == {T0}                     # received_at = our poll's response time
    m = msgs[1]
    assert m["supersedes"] is None and m["revision_id"] == f"{m['item_id']}@{m['content_hash'][:12]}"
    assert m["content_hash"] == content_hash(m["title"], m["body"])
    assert m["source_published_at"] < m["source_modified_at"] < m["received_at"]            # the feed's times are kept apart from ours
    assert m["source_time_zone"] == "tz-aware" and m["origin_source_id"] == native_news.ORIGIN_CLS


def test_the_same_answer_twice_stores_nothing_new_and_one_new_item_is_one_message(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, THS)
    assert rec.poll(st) == 30 and rec.poll(st) == 0                                          # dedupe across polls
    world.edit(THS, lambda rows: rows.insert(0, {**rows[0], "id": "9999999", "title": "新消息", "digest": "新消息内容", "ctime": "1795052400"}))
    clock.advance(seconds=10)
    assert rec.poll(st) == 1
    assert [m["item_id"] for m in sink.all_messages(THS)][-1] == "9999999" and st.new == 31 and st.revisions == 0


def test_a_changed_title_or_body_is_a_revision_that_supersedes_the_previous_one(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, CLS)
    rec.poll(st)
    first = sink.versions[(CLS, "2531857")][-1]
    world.edit(CLS, lambda rows: rows[1].update(content=rows[1]["content"] + "（更正：数据有误）", modified_time=rows[1]["modified_time"] + 600))
    clock.advance(seconds=30)
    assert rec.poll(st) == 1
    second = sink.versions[(CLS, "2531857")][-1]
    assert second["supersedes"] == first["revision_id"] and second["revision_id"] != first["revision_id"]
    assert second["received_at"] == T0.replace(second=30) and second["body"].endswith("（更正：数据有误）")
    world.edit(CLS, lambda rows: rows[1].update(content=rows[1]["content"].replace("（更正：数据有误）", "（再次更正）")))
    clock.advance(seconds=30)
    rec.poll(st)
    third = sink.versions[(CLS, "2531857")][-1]
    assert third["supersedes"] == second["revision_id"] and st.revisions == 2
    # a change of only a time field is not a new content state (the old recorder hashes title + body)
    world.edit(CLS, lambda rows: rows[1].update(modified_time=1))
    clock.advance(seconds=30)
    assert rec.poll(st) == 0


def test_duplicate_item_ids_inside_one_answer_count_once(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    world.edit(THS, lambda rows: rows.append(dict(rows[0])))
    assert rec.poll(state(rec, THS)) == 30


def test_raw_answer_is_kept_only_for_polls_with_new_versions_and_lineage_points_at_it(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    rec.poll(state(rec, EM))
    rec.poll(state(rec, EM))                                                                  # nothing new: no second raw line
    with gzip.open(tmp_path / "day" / "raw" / "em.724.jsonl.gz", "rt", encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh]
    assert len(lines) == 1 and lines[0]["sha256"] == hashlib.sha256(world.bodies[EM]).hexdigest()
    m = sink.all_messages(EM)[0]
    assert m["part_sha256"] == lines[0]["sha256"] and m["part"] == f"raw/em.724.jsonl.gz#{lines[0]['sha256'][:12]}"
    assert json.loads(lines[0]["body"])["data"]["fastNewsList"][0]["code"] == m["item_id"]


def test_a_failed_write_is_retried_by_the_next_poll_with_the_same_items(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    sink.fail_writes = 1
    st = state(rec, THS)
    with pytest.raises(RuntimeError):
        rec.poll(st)
    assert not sink.writes and rec.known == {}
    rec.poll_safely(st)                                                                       # the loop's wrapper: nothing thrown, items stored now
    assert len(sink.all_messages(THS)) == 30


def test_a_failed_write_inside_the_loop_wrapper_is_an_error_entry_not_a_crash(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    sink.fail_writes = 1
    rec.poll_safely(state(rec, THS))
    assert rec.errors and "database down" in rec.errors[0] and not sink.writes


def test_a_restart_asks_the_store_and_does_not_record_the_backlog_again(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    rec.poll(state(rec, THS))
    n = len(sink.all_messages())
    rec2, _, _, _, _ = make(tmp_path / "again", world=world, sink=sink, clock=clock)
    clock.advance(minutes=3)
    assert rec2.poll(state(rec2, THS)) == 0 and len(sink.all_messages()) == n
    world.edit(THS, lambda rows: rows[2].update(title=rows[2]["title"] + "（修订）"))
    assert rec2.poll(state(rec2, THS)) == 1
    new = sink.all_messages(THS)[-1]
    assert new["supersedes"] == sink.versions[(THS, new["item_id"])][-2]["revision_id"]


@pytest.mark.parametrize("status", [403, 429, 456, 501])
def test_a_block_stops_that_host_for_the_day_without_a_retry_and_the_others_go_on(tmp_path, status):
    rec, world, sink, clock, clients = make(tmp_path)
    world.status[THS] = status
    rec.run(start=time(8, 0), until=time(8, 6))                                               # one minute of the day
    assert state(rec, THS).stopped.startswith("blocked") and world.requests[THS] == 1       # exactly one request, never retried
    assert world.requests[EM] >= 2 and world.requests[CLS] >= 2 and sink.all_messages(EM) and sink.all_messages(CLS)
    assert rec.blocked and json.loads((tmp_path / "day" / "blocked.json").read_text(encoding="utf-8")) == {THS: f"http_{status}"}
    assert clients[THS].circuit_open
    # a restart on the same day does not touch the blocked host at all
    rec2, world2, _, _, _ = make(tmp_path, sink=MemSink())
    assert state(rec2, THS).stopped.startswith("blocked earlier today")
    rec2.once()
    assert world2.requests[THS] == 0 and world2.requests[EM] == 1


def test_three_bad_answers_in_a_row_stop_a_feed_but_a_good_one_resets_the_count(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, EM)
    good = world.bodies[EM]
    world.bodies[EM] = b"Service Unavailable"
    rec.poll(st), rec.poll(st)
    assert st.fails == 2 and not st.stopped
    world.bodies[EM] = good
    rec.poll(st)
    assert st.fails == 0
    world.bodies[EM] = b"Service Unavailable"
    for _ in range(3):
        rec.poll(st)
    assert st.stopped and "3 bad answers" in st.stopped and sink.bad == [EM] * 5
    world.status[CLS] = 503                                                                    # a gateway fault is a transport failure: it backs off (see test_transient)
    for _ in range(3):
        rec.poll(state(rec, CLS))
    assert not state(rec, CLS).stopped and state(rec, CLS).degraded and not rec.blocked


@pytest.mark.parametrize("origin", [THS, EM, CLS])
def test_an_html_page_with_http_200_is_a_block_not_a_bad_answer_so_the_host_stops_at_once(tmp_path, origin):
    rec, world, sink, clock, _ = make(tmp_path)
    world.bodies[origin] = b"<!DOCTYPE html><html><body>Access denied (WAF)</body></html>"
    rec.run(start=time(8, 0), until=time(8, 6))
    st = state(rec, origin)
    assert st.stopped == "blocked: html_page" and st.fails == 0 and world.requests[origin] == 1       # one request, never retried
    assert sink.blocked_pages == [origin] and sink.bad == [] and rec.blocked
    assert json.loads((tmp_path / "day" / "blocked.json").read_text(encoding="utf-8")) == {origin: "html_page"}
    others = [o for o in (THS, EM, CLS) if o != origin]
    assert all(world.requests[o] >= 2 for o in others)
    rec2, world2, _, _, _ = make(tmp_path, sink=MemSink())                                            # a restart the same day stays away
    rec2.once()
    assert world2.requests[origin] == 0


def test_an_open_circuit_in_the_store_keeps_that_feed_stopped_from_the_start(tmp_path):
    sink = MemSink()
    sink.open_circuits[CLS] = "http_429 until 2026-11-12T10:00"
    rec, world, _, _, _ = make(tmp_path, sink=sink)
    assert state(rec, CLS).stopped.startswith("circuit open")
    rec.once()
    assert world.requests[CLS] == 0 and world.requests[THS] == 1


def test_cadence_and_window_follow_the_old_recorder(tmp_path):
    clock = Clock(datetime(2026, 11, 11, 23, 58, tzinfo=UTC))                                  # 07:58 CST: before the 08:00 start
    rec, world, sink, _, _ = make(tmp_path, clock=clock)
    rec.run(start=time(8, 0), until=time(8, 1))
    assert world.requests[THS] == 6 and world.requests[EM] == 2 and world.requests[CLS] == 2   # 10 s / 30 s / 30 s for the first minute
    assert all(m["received_at"] >= datetime(2026, 11, 12, 0, 0, tzinfo=UTC) for m in sink.all_messages())
    assert json.loads((tmp_path / "day" / "summary.json").read_text(encoding="utf-8"))["end"] == "until"
    assert native_news.POLL_EVERY_SECONDS == {THS: 10, EM: 30, CLS: 30} and (native_news.START_AT, native_news.STOP_AT) == ("08:00", "22:00")


def test_one_client_per_host_keeps_every_request_on_its_own_host(tmp_path):
    rec, world, sink, clock, clients = make(tmp_path)          # World.handler_for asserts the host of every request
    rec.once()
    assert [c.requests_made for c in clients.values()] == [1, 1, 1]


def test_claims_run_only_after_a_poll_that_stored_something_and_can_be_switched_off(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, THS)
    rec.poll(st), rec.poll(st)
    assert sink.claim_calls == 1
    rec2, _, sink2, _, _ = make(tmp_path / "x", run_claims=False)
    rec2.once()
    assert sink2.claim_calls == 0 and len(sink2.all_messages()) == 130


def test_idle_polls_send_a_heartbeat_at_most_once_a_minute(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, THS)
    rec.poll(st)
    for _ in range(7):
        clock.advance(seconds=10)
        rec.poll(st)
    assert len(sink.heartbeats) == 1                                                            # 70 s after the write: one heartbeat
