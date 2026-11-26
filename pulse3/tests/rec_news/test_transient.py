"""Transport failures (timeouts) are not blocks: back off, degrade, resume, give up only after a long unbroken window; blocks still stop at once.

Evidence behind it: 2026-11-23 first live session, ths.724 stopped 13:22 and em.724 13:59 after three ConnectTimeouts each (~20 s per try), and
ths.724 again on 2026-11-24 09:33; the old rule "3 bad answers in a row stop the feed for the day" counted a timeout like a block.
The fake clock moves 20 s for every timeout (the real client's timeout), so the loop is held exactly as in production.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta

import pytest

from pulse.adapters.sources import native_news
from pulse.application.native_news_recorder import backoff_delay

from .support import CLS, EM, THS, MemSink
from .test_recorder import T0, make, state

CAP, GIVE_UP = native_news.TRANSIENT_BACKOFF_MAX_SECONDS, native_news.TRANSIENT_GIVE_UP_SECONDS


def gaps(times: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(times, times[1:])]


def test_backoff_schedule_is_the_feeds_cadence_doubled_up_to_the_cap():
    assert [backoff_delay(10, n, CAP) for n in range(1, 8)] == [10, 20, 40, 80, 160, CAP, CAP]
    assert [backoff_delay(30, n, CAP) for n in range(1, 6)] == [30, 60, 120, CAP, CAP]
    assert backoff_delay(10, 10_000, CAP) == CAP                                              # no overflow on a very long run
    assert backoff_delay(30, 0, CAP) == 30                                                    # never below the feed's own cadence
    assert CAP == 180 and GIVE_UP == 3600 and native_news.BREAK_AFTER == 3                    # the numbers the module comments justify


def test_a_timeout_burst_backs_off_degrades_and_resumes_when_the_host_answers_again(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    outage_end = T0 + timedelta(minutes=6)
    world.outage[THS] = (T0 + timedelta(seconds=30), outage_end)                              # ths unreachable for 5.5 minutes, em/cls fine
    rec.run(start=time(8, 0), until=time(8, 20))                                              # 08:05 -> 08:20 CST
    st = state(rec, THS)
    assert st.stopped is None, "a ConnectTimeout burst must not stop the feed for the day (old rule: 3 in a row)"
    assert st.transport_total == 5 > native_news.BREAK_AFTER                                  # more failures than the old breaker tolerated
    assert st.failing_since is None and st.retry_at is None and not st.degraded and st.transport_fails == 0     # resumed and healed
    down = [t for t in world.times[THS] if T0 + timedelta(seconds=30) <= t < outage_end]
    assert len(down) == 5                                                                     # 5 probes in 5.5 min, not 33 at a 10 s cadence
    g = gaps(down)
    assert g == sorted(g) and g[-1] > g[0] and g[-1] <= CAP + world.timeout_cost              # the wait never shrinks, and stops growing at the cap
    assert min(gaps(world.times[THS])) >= 1.0                                                 # per-host pacing stays >= 1 request / second
    later = [t for t in world.times[THS] if t >= outage_end]
    assert len(later) >= 30 and gaps(later)[-1] <= 12                                         # back on the 10 s cadence after the recovery
    summary = json.loads((tmp_path / "day" / "summary.json").read_text(encoding="utf-8"))
    assert summary["end"] == "until" and summary["feeds"][THS]["transport_failures"] == 5 and summary["feeds"][THS]["degraded"] is False
    assert not any("gave up" in e for e in rec.errors) and not rec.gave_up


def test_a_feed_is_degraded_while_it_backs_off_and_one_good_answer_resets_the_schedule(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, THS)
    world.outage[THS] = (T0, T0 + timedelta(hours=1))
    for _ in range(3):
        rec.poll(st)
        assert st.degraded and st.retry_at is not None
    assert st.transport_fails == 3 and st.fails == 0 and st.stopped is None                   # timeouts never count as unusable ANSWERS
    assert st.retry_at - clock.t == timedelta(seconds=40)                                     # 10 * 2^2 after the third failure
    world.outage.clear()
    clock.t = st.retry_at
    assert rec.poll(st) == 30 and not st.degraded and st.retry_at is None and st.transport_fails == 0
    world.outage[THS] = (clock.t, clock.t + timedelta(hours=1))
    rec.poll(st)
    assert st.retry_at - clock.t == timedelta(seconds=10) and st.transport_fails == 1         # the exponent starts over after a recovery


def test_the_backoff_gate_holds_back_polls_but_only_that_feeds(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    ths, em = state(rec, THS), state(rec, EM)
    world.outage[THS] = (T0, T0 + timedelta(hours=1))
    rec.poll(ths)
    assert not rec._due(ths, clock.t) and ths.retry_at > clock.t
    assert rec._due(em, clock.t)                                                              # another feed is untouched
    assert rec._due(ths, ths.retry_at)


@pytest.mark.parametrize("status", [403, 429, 456, 501])
def test_a_real_block_after_timeouts_still_stops_that_host_at_once_without_a_retry(tmp_path, status):
    rec, world, sink, clock, clients = make(tmp_path)
    st = state(rec, THS)
    world.outage[THS] = (T0, T0 + timedelta(seconds=25))
    rec.poll(st), rec.poll(st)
    assert st.degraded and st.stopped is None
    world.outage.clear()
    world.status[THS] = status
    clock.t = st.retry_at
    seen = world.requests[THS]
    rec.run(start=time(8, 0), until=time(8, 20))
    assert st.stopped == f"blocked: http_{status}" and world.requests[THS] == seen + 1        # exactly one more request: the blocked one
    assert json.loads((tmp_path / "day" / "blocked.json").read_text(encoding="utf-8")) == {THS: f"http_{status}"}
    assert rec.blocked and not rec.gave_up and clients[THS].circuit_open
    assert world.requests[EM] >= 3 and world.requests[CLS] >= 3                               # the others kept going


@pytest.mark.parametrize("origin", [THS, EM, CLS])
def test_an_html_page_still_stops_the_host_at_once_while_it_was_backing_off(tmp_path, origin):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, origin)
    world.outage[origin] = (T0, T0 + timedelta(seconds=25))
    rec.poll(st)
    world.outage.clear()
    world.bodies[origin] = b"<!DOCTYPE html><html><body>captcha</body></html>"
    clock.t = st.retry_at
    seen = world.requests[origin]
    rec.run(start=time(8, 0), until=time(8, 20))
    assert st.stopped == "blocked: html_page" and world.requests[origin] == seen + 1 and sink.blocked_pages == [origin]


def test_a_failing_feed_never_stops_or_starves_the_others(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    world.outage[THS] = (T0, T0 + timedelta(hours=3))                                         # ths dead for the whole run
    rec.run(start=time(8, 0), until=time(8, 40))                                              # 35 minutes
    ths, em, cls = state(rec, THS), state(rec, EM), state(rec, CLS)
    assert ths.stopped is None and ths.degraded                                               # still alive and degraded, not given up (35 min < 60)
    assert em.stopped is None and cls.stopped is None and em.fails == cls.fails == 0 and not em.degraded and not cls.degraded
    assert em.polls >= 60 and cls.polls >= 60                                                 # 35 min at 30 s is 70 polls; a ths timeout costs 20 s now and then
    assert len(sink.all_messages(EM)) == 50 and len(sink.all_messages(CLS)) == 50 and not sink.all_messages(THS)
    assert world.requests[THS] <= 18                                                          # ~15 probes, not the 210 of a 10 s cadence
    assert json.loads((tmp_path / "day" / "summary.json").read_text(encoding="utf-8"))["end"] == "until"


def test_a_block_on_one_host_while_another_backs_off_stops_only_the_blocked_one(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    world.outage[EM] = (T0, T0 + timedelta(hours=1))
    world.status[CLS] = 403
    rec.run(start=time(8, 0), until=time(8, 20))
    assert state(rec, CLS).stopped == "blocked: http_403" and world.requests[CLS] == 1
    assert state(rec, EM).stopped is None and state(rec, EM).degraded and sink.all_messages(THS)
    assert state(rec, THS).stopped is None


def test_the_feed_is_given_up_only_after_the_unbroken_failure_window_and_the_others_continue(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    world.outage[THS] = (T0, T0 + timedelta(hours=5))
    rec.run(start=time(8, 0), until=time(9, 4))                                               # 59 min after the first failure: not yet
    st = state(rec, THS)
    assert st.stopped is None and st.degraded and (clock.t - st.failing_since).total_seconds() < GIVE_UP

    rec2, world2, sink2, clock2, _ = make(tmp_path / "two")
    world2.outage[THS] = (T0, T0 + timedelta(hours=5))
    reason = rec2.run(start=time(8, 0), until=time(9, 30))                                    # well past the window
    st2 = state(rec2, THS)
    assert reason == "until" and st2.stopped.startswith("gave up") and "ConnectTimeout" in st2.stopped
    assert 20 <= st2.transport_total <= 28                                                    # ~23 probes in the hour, not hundreds
    span = world2.times[THS][-1] - world2.times[THS][0]
    assert timedelta(seconds=GIVE_UP) <= span < timedelta(seconds=GIVE_UP + CAP + 2 * world2.timeout_cost)   # it waited the whole window, no longer
    assert rec2.gave_up and not rec2.blocked and any("gave up" in e for e in rec2.errors)
    assert not (tmp_path / "two" / "day" / "blocked.json").exists()                           # a give-up is not a block: nothing is remembered
    assert state(rec2, EM).stopped is None and state(rec2, CLS).stopped is None and world2.requests[EM] > 100
    # a restart the same day tries the host again (unlike a block)
    rec3, world3, _, _, _ = make(tmp_path / "two", sink=MemSink())
    assert state(rec3, THS).stopped is None
    rec3.once()
    assert world3.requests[THS] == 1


def test_a_recovery_before_the_window_ends_restarts_the_window(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, THS)
    world.outage[THS] = (T0, T0 + timedelta(minutes=40))
    rec.run(start=time(8, 0), until=time(8, 50))                                              # fails for 40 min, then answers for a few minutes
    assert st.failing_since is None and st.stopped is None
    world.outage[THS] = (clock.t, clock.t + timedelta(hours=2))                               # a second outage: the first 40 min do not count
    rec.run(start=time(8, 0), until=time(9, 30))
    assert st.stopped is None and st.degraded and (clock.t - st.failing_since).total_seconds() < GIVE_UP


def test_a_server_error_is_transient_too_but_unusable_answers_still_stop_after_three(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    cls = state(rec, CLS)
    world.status[CLS] = 503                                                                   # a gateway fault: a transport failure, not a block
    for _ in range(4):
        rec.poll(cls)
    assert cls.stopped is None and cls.degraded and cls.transport_fails == 4 and cls.fails == 0 and not rec.blocked
    em = state(rec, EM)
    world.bodies[EM] = b"Service Unavailable"                                                 # the host does answer, with rubbish: the old breaker stays
    for _ in range(3):
        rec.poll(em)
    assert em.stopped and "3 bad answers" in em.stopped and em.transport_fails == 0 and not rec.gave_up


def test_health_shows_degraded_during_the_backoff_and_healthy_after_the_first_good_answer():
    """The recorder relies on the sink's ``transport_error`` record per failed poll (``IntelligenceService._fetch``) and on its heartbeat/write after
    the next answer; this pins what the health state machine makes of that sequence (no circuit is opened by a timeout, so nothing waits on a cool-down)."""
    from pulse.domain.evidence.health import HealthState, apply_attempt

    now = T0
    s = HealthState(runtime_state="healthy")
    s = apply_attempt(s, "transport_error", now, error_code="ConnectTimeout: _ssl.c:1015")
    assert s.runtime_state == "degraded" and s.circuit == "closed"
    for _ in range(2):
        s = apply_attempt(s, "transport_error", now, error_code="ConnectTimeout: _ssl.c:1015")
    assert s.runtime_state == "unavailable" and s.circuit == "closed"                         # a timeout never opens the circuit (only a block does)
    s = apply_attempt(s, "success_empty", now + timedelta(minutes=5), newest_content_at=now + timedelta(minutes=4), canonical_ready=False)
    assert s.consecutive_failures == 0 and s.runtime_state in ("healthy", "degraded") and s.circuit == "closed"
    blocked = apply_attempt(HealthState(runtime_state="healthy"), "blocked", now, error_code="http_403")
    assert blocked.circuit == "open" and blocked.next_attempt_at == now + timedelta(seconds=3600)


@pytest.mark.parametrize("status", [400, 401, 404, 412, 418])
def test_a_client_error_status_is_a_bad_answer_not_a_transient_failure(tmp_path, status):
    """The client raises TransportFailure for every status >= 400 outside BLOCK_STATUS; 412/418 are often WAF pages, so only 5xx and 'no answer' back off."""
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, CLS)
    world.status[CLS] = status
    rec.poll(st), rec.poll(st)
    assert st.fails == 2 and st.transport_fails == 0 and st.transport_total == 0 and st.retry_at is None and not st.degraded and st.stopped is None
    world.status[CLS] = 200
    rec.poll(st)
    assert st.fails == 0                                                                       # a good answer resets the count, as before
    world.status[CLS] = status
    for _ in range(3):
        rec.poll(st)
    assert st.stopped and "3 bad answers" in st.stopped and not rec.blocked and not rec.gave_up and not st.degraded
    assert (tmp_path / "day" / "blocked.json").exists() is False                               # a bad answer is not remembered as a block


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_a_server_error_status_backs_off_and_never_counts_as_a_bad_answer(tmp_path, status):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, CLS)
    world.status[CLS] = status
    for _ in range(4):
        rec.poll(st)
    assert st.stopped is None and st.degraded and st.transport_fails == 4 and st.fails == 0 and st.retry_at is not None


def test_a_client_error_after_timeouts_ends_the_transport_run_and_counts_as_an_answer(tmp_path):
    rec, world, sink, clock, _ = make(tmp_path)
    st = state(rec, CLS)
    world.outage[CLS] = (T0, T0 + timedelta(seconds=25))
    rec.poll(st), rec.poll(st)
    assert st.degraded
    world.status[CLS] = 412
    clock.t = st.retry_at
    rec.poll(st)
    assert not st.degraded and st.transport_fails == 0 and st.retry_at is None and st.fails == 1 and st.stopped is None
