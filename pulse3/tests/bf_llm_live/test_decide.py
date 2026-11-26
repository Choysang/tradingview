"""E092 ``decide`` offline: what is stored per answer, the request / spend / day caps, the auto-stop, stops on 401 / 429 / refusal, the timeout retry, the deadline and the ledger rule."""
from __future__ import annotations

import json

import httpx
import pytest

from pulse.application.bf_llm_live_service import ContextMissing
from pulse.application.bf_llm_live_store import MemoryLogStore, make_row
from pulse.adapters.models.responses_client import KeyProblem
from pulse.domain.llm_live import experiments as X
from pulse.domain.llm_live import rules as R
from bf_llm_live.support import DAY, EVENING, KEY, Clock, Recorder, build, kinds, plan, reply

ISO = DAY.isoformat()


def test_a_full_day_stores_one_answer_per_name_with_everything_needed_to_audit_it():
    svc, store, vendor, _ = build()
    out = svc.decide(DAY)
    assert out["status"] == "done" and out["asked"] == 40 and out["calls"] == 40 and len(vendor.requests) == 40
    assert all(vendor.auth_ok) and all(r["model"] == "gpt-6-luna" and r["reasoning"] == {"effort": "high"} and r["tools"] == [{"type": "web_search"}] for r in vendor.requests)
    assert len(kinds(store, "day")) == 1 and len(kinds(store, "call")) == 40 and len(kinds(store, "answer")) == 40 and len(kinds(store, "run")) == 1
    a = kinds(store, "answer")[0]["body"]
    assert a["model"] == "gpt-6-luna" and a["effort"] == "high" and len(a["prompt_sha256"]) == 64 and a["request_id"].startswith("req-r") and a["response_id"].startswith("r")
    assert a["usage"] == {"input_tokens": 2000, "output_tokens": 500} and a["searches"] == 2 and a["cost_usd"] == pytest.approx(0.01 + 0.0125 + 0.02)
    assert json.loads(a["raw_text"])["action"] == "watch" and a["valid"] is True and a["available_at"] < a["not_after"] and a["not_after"].startswith("2026-11-24T09:25")
    assert {r["body"]["group"] for r in kinds(store, "answer")} == {"top", "control"}
    day = kinds(store, "day")[0]["body"]
    assert day["spec_sha256"] == X.E100.spec_sha256 and day["experiment"] == "E100" and day["system"].startswith("你是一名经验丰富的 A 股交易员") and "今天是 2026-11-23" in day["system"] and len(day["jobs"]) == 40


def test_a_rerun_asks_nothing_and_a_partial_day_asks_only_the_missing_names():
    svc, store, vendor, _ = build()
    svc.decide(DAY)
    assert svc.decide(DAY)["status"] == "complete" and len(vendor.requests) == 40
    svc2, store2, vendor2, _ = build(vendor=Recorder(lambda n, b: reply(rid=f"r{n}") if n % 2 else httpx.Response(500)), cap_usd=100.0)
    first = svc2.decide(DAY)
    assert first["status"] in ("partial", "stopped") and first["asked"] < 40
    done = {r["key"] for r in kinds(store2, "answer")}
    vendor2.fn = lambda n, b: reply(rid=f"x{n}")
    second = svc2.decide(DAY)
    assert second["status"] in ("done", "partial", "stopped")
    again = [r["key"] for r in kinds(store2, "answer")]
    assert len(again) == len(set(again)) and done <= set(again)                      # an answered name is never asked (or stored) twice


def test_the_universe_is_frozen_on_the_first_run():
    calls = []
    p = plan()

    def ctx(day):
        calls.append(day)
        return p
    svc, store, _, _ = build(context=ctx, vendor=Recorder(lambda n, b: httpx.Response(500)))
    svc.decide(DAY)
    svc.decide(DAY, resume_after_stop=True)
    assert len(calls) == 1 and len(kinds(store, "day")) == 1


def test_at_most_60_requests_per_decision_day_across_reruns():
    store = MemoryLogStore()
    store.append([make_row(ISO, "day", ISO, "old", {"jobs": plan().jobs, "market": "m"}, at=EVENING)])
    store.append([make_row(ISO, "call", f"{ISO}_x", "old", {"n": n, "id": "x"}, at=EVENING) for n in range(1, 59)])      # 58 requests already on the ledger
    store.append([make_row(ISO, "error", "x", "old", {"id": "x", "call_n": n, "label": "http_500", "cost_usd": 0.0}, at=EVENING) for n in range(1, 59)])
    svc, store, vendor, _ = build(store=store)
    out = svc.decide(DAY)
    assert len(vendor.requests) == 2 and out["status"] == "stopped" and out["stop"] == "budget" and len(kinds(store, "call")) == 60
    again = svc.decide(DAY)
    assert again["status"] == "budget" and len(vendor.requests) == 2


def test_the_spend_cap_stops_the_day_before_it_is_exceeded():
    # one call costs 2.0 (1M output tokens at 2.0 per M); the cap is 5.0
    prices = R.Prices(in_per_m=0.0, out_per_m=2.0, search_per_call=0.0, reserve_per_call=0.25)
    svc, store, vendor, _ = build(vendor=Recorder(lambda n, b: reply(rid=f"r{n}", tin=0, tout=1_000_000)), prices=prices, cap_usd=5.0)
    out = svc.decide(DAY)
    assert len(vendor.requests) == 2 and out["status"] == "stopped" and out["stop"] == "cost_cap" and out["spent_usd"] == pytest.approx(4.0)
    assert svc.decide(DAY)["status"] == "cost_cap" and len(vendor.requests) == 2


def test_a_zero_cap_refuses_everything():
    svc, _, vendor, _ = build(cap_usd=0.0)
    assert svc.decide(DAY)["status"] == "cost_cap" and not vendor.requests


def test_the_21st_decision_day_is_refused_but_an_existing_day_may_finish():
    store = MemoryLogStore()
    for i in range(20):
        d = f"2026-10-{i + 1:02d}"
        store.append([make_row(d, "call", f"{d}_x", "old", {"n": 1, "id": "x"}, at=EVENING)])
    svc, store, vendor, _ = build(store=store)
    out = svc.decide(DAY)
    assert out["status"] == "limit" and out["decision_days"] == 20 and not vendor.requests and not kinds(store, "day")
    store.append([make_row(ISO, "call", f"{ISO}_x", "old", {"n": 1, "id": "x"}, at=EVENING)])      # today already is one of the 20
    store._rows = [r for r in store._rows if r["session"] != "2026-10-20"]                           # ... so 19 other days + today = 20
    assert svc.decide(DAY)["status"] in ("done", "partial", "stopped") and vendor.requests


def test_a_day_without_any_request_does_not_count_toward_the_limit():
    svc, store, _, _ = build(switch=False)
    svc.decide(DAY)
    assert svc.status()["decision_days"] == 0


@pytest.mark.parametrize("http,reason", [(401, "http_401"), (403, "http_403"), (429, "http_429")])
def test_a_vendor_refusal_stops_the_day_at_once_without_a_retry_storm(http, reason):
    svc, store, vendor, _ = build(vendor=Recorder(lambda n, b: httpx.Response(http, json={"error": f"bad key {KEY}"})), workers=4)
    out = svc.decide(DAY)
    assert out["status"] == "stopped" and out["stop"] == reason and 1 <= len(vendor.requests) <= 4          # in-flight workers may already have started; no more after the stop
    assert [r["body"]["reason"] for r in kinds(store, "stop")] == [reason] and kinds(store, "stop")[0]["body"]["hard"] is True
    n = len(vendor.requests)
    assert svc.decide(DAY)["status"] == "stopped_today" and len(vendor.requests) == n               # the same day is not hit again
    svc.decide(DAY, resume_after_stop=True)                                                          # only on an explicit flag, still under the caps
    assert len(vendor.requests) > n
    blob = json.dumps(store.rows(), ensure_ascii=False) + json.dumps(out, ensure_ascii=False)
    assert KEY not in blob


def test_a_model_refusal_stops_the_day():
    body = {"id": "resp_r", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 10}, "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    svc, store, vendor, _ = build(vendor=Recorder(lambda n, b: httpx.Response(200, json=body)))
    out = svc.decide(DAY)
    assert out["status"] == "stopped" and out["stop"] == "refusal" and len(vendor.requests) == 1
    assert kinds(store, "error")[0]["body"]["cost_usd"] is not None                                  # the refused call was paid for and is on the books


def test_a_timeout_is_asked_once_more_and_nothing_else_is_retried():
    def fn(n, body):
        if n == 1:
            raise httpx.ReadTimeout("t")
        return reply(rid=f"r{n}")
    svc, store, vendor, _ = build(vendor=Recorder(fn))
    svc.decide(DAY)
    assert len(vendor.requests) == 41 and vendor.requests[0]["input"] == vendor.requests[1]["input"]          # the first name twice, the others once
    assert len(kinds(store, "answer")) == 40 and [r["body"]["label"] for r in kinds(store, "error")] == ["timeout"]
    svc2, store2, vendor2, _ = build(vendor=Recorder(lambda n, b: httpx.Response(500)))
    out = svc2.decide(DAY)
    assert len(vendor2.requests) == 5 and out["stop"] == "failures" and out["errors"] == {"http_500": 5}       # one request per name, 5 in a row stop the day
    assert svc2.decide(DAY)["status"] == "stopped_today"


def test_a_second_timeout_is_not_retried_again():
    svc, _, vendor, _ = build(vendor=Recorder(lambda n, b: (_ for _ in ()).throw(httpx.ReadTimeout("t"))))
    svc.decide(DAY)
    per_prompt = {}
    for r in vendor.requests:
        per_prompt[r["input"]] = per_prompt.get(r["input"], 0) + 1
    assert max(per_prompt.values()) == 2


def test_an_unparseable_answer_is_kept_as_an_error_with_its_text_and_cost():
    svc, store, _, _ = build(vendor=Recorder(lambda n, b: reply("I think it is a buy, 70%")), prices=R.Prices())
    out = svc.decide(DAY)
    err = kinds(store, "error")[0]["body"]
    assert out["stop"] == "failures" and err["label"] == "bad_verdict" and err["raw_text"] == "I think it is a buy, 70%" and err["cost_usd"] > 0 and not kinds(store, "answer")


def test_nothing_is_asked_at_or_after_the_deadline_and_a_late_finish_is_stored_but_not_valid():
    clock = Clock(EVENING)
    svc, store, vendor, _ = build(clock=clock, workers=1)
    clock.now = R.deadline_after(DAY)
    assert svc.decide(DAY)["status"] == "late" and not vendor.requests

    clock2 = Clock(EVENING)

    def fn(n, body):
        clock2.now = R.deadline_after(DAY)                       # the call crosses the deadline
        return reply(rid=f"r{n}")
    svc2, store2, vendor2, _ = build(clock=clock2, vendor=Recorder(fn))
    out = svc2.decide(DAY)
    assert len(vendor2.requests) == 1 and out["stop"] == "late"
    a = kinds(store2, "answer")
    assert len(a) == 1 and a[0]["body"]["valid"] is False


def test_the_ledger_row_is_durable_before_the_request_and_no_ledger_means_no_request():
    seen = []

    def fn(n, body):
        seen.append(len(kinds(store, "call")))
        return reply(rid=f"r{n}")
    svc, store, vendor, _ = build(vendor=Recorder(fn))
    svc.decide(DAY)
    assert seen == list(range(1, 41))                            # when request n arrived, n ledger rows were already stored

    class NoLedger(MemoryLogStore):
        def append(self, rows):
            if any(r["kind"] == "call" for r in rows):
                raise OSError("disk full")
            super().append(rows)
    svc2, store2, vendor2, _ = build(store=NoLedger())
    out = svc2.decide(DAY)
    assert not vendor2.requests and out["status"] == "stopped" and out["stop"] == "ledger_error" and out["asked"] == 0


def test_check_and_a_missing_switch_never_call_and_never_write_the_day():
    svc, store, vendor, _ = build()
    out = svc.decide(DAY, check_only=True)
    assert out["status"] == "check" and out["todo"] == 40 and not vendor.requests and not store.rows()
    svc2, store2, vendor2, _ = build(switch=False)
    assert svc2.decide(DAY)["status"] == "switch_off" and not vendor2.requests and not store2.rows()


@pytest.mark.parametrize("code", ["no_key", "bad_key"])
def test_a_missing_or_unusable_key_is_a_refusal_before_anything_is_stored(code):
    def loader():
        raise KeyProblem(code)
    svc, store, vendor, _ = build(key_loader=loader)
    assert svc.decide(DAY)["status"] == code and not vendor.requests and not store.rows()


def test_inputs_not_ready_is_a_refusal_not_a_guess():
    def ctx(day):
        raise ContextMissing("scores_not_ready", "no score for the day")
    svc, store, vendor, _ = build(context=ctx)
    out = svc.decide(DAY)
    assert out["status"] == "not_ready" and out["code"] == "scores_not_ready" and not vendor.requests and not store.rows()


def test_only_one_run_at_a_time():
    svc, store, vendor, _ = build()
    with store.exclusive() as held:
        assert held and svc.decide(DAY)["status"] == "busy"
    assert not vendor.requests


def test_the_registry_hook_runs_before_the_first_paid_call_only():
    order = []
    svc, store, vendor, _ = build(register=lambda now: order.append(("register", len(vendor.requests))))
    svc.decide(DAY, check_only=True)
    assert order == []
    svc.decide(DAY)
    assert order == [("register", 0)]


def test_the_key_appears_nowhere_in_the_stored_rows_or_the_summary():
    svc, store, _, _ = build(vendor=Recorder(lambda n, b: httpx.Response(401, text=f"Incorrect API key provided: {KEY}")))
    out = svc.decide(DAY)
    assert KEY not in json.dumps(store.rows(), ensure_ascii=False) + json.dumps(out) and KEY[-8:] not in json.dumps(store.rows())


def test_max_output_tokens_is_sent_and_recorded_on_the_ledger():
    svc, store, vendor, _ = build()
    svc.decide(DAY)
    assert {r["max_output_tokens"] for r in vendor.requests} == {12000}
    assert {r["body"]["max_output_tokens"] for r in kinds(store, "call")} == {12000}
    svc2, store2, vendor2, _ = build()
    svc2.max_output_tokens = 5000
    svc2.decide(DAY)
    assert {r["max_output_tokens"] for r in vendor2.requests} == {5000}


def _raise(exc):
    def fn(n, body):
        raise exc
    return fn


@pytest.mark.parametrize("fn", [_raise(httpx.ReadError("r")), _raise(httpx.RemoteProtocolError("p")), lambda n, b: httpx.Response(503), lambda n, b: reply("not json at all")])
def test_a_failure_after_the_request_was_sent_counts_at_the_reserve(fn):
    prices = R.Prices(reserve_per_call=0.4)
    svc, store, _, _ = build(vendor=Recorder(fn), prices=prices, cap_usd=100.0)
    svc.decide(DAY)
    errs = kinds(store, "error")
    spent, _ = R.day_spend(store.rows(), ISO, prices)
    paid = sum(e["body"]["cost_usd"] for e in errs if e["body"]["cost_usd"] is not None)
    unknown = sum(1 for e in errs if e["body"]["cost_usd"] is None)
    assert errs and spent == pytest.approx(paid + unknown * 0.4) and spent > 0
    if errs[0]["body"]["label"] != "bad_verdict":
        assert all(e["body"]["cost_usd"] is None for e in errs)                      # no usage reported: unknown, so counted at the reserve


def test_in_flight_calls_are_counted_at_the_mean_known_cost_so_the_cap_holds_with_workers():
    prices = R.Prices(in_per_m=0.0, out_per_m=2.0, search_per_call=0.0, reserve_per_call=0.25)       # every call costs 2.0, the cap is 5.0
    svc, store, vendor, _ = build(vendor=Recorder(lambda n, b: reply(rid=f"r{n}", tin=0, tout=1_000_000)), prices=prices, cap_usd=5.0, workers=4)
    svc.decide(DAY)
    spent, _ = R.day_spend(store.rows(), ISO, prices)
    assert spent <= 5.0 + 1e-9 and len(vendor.requests) <= 4


def test_a_429_stop_row_says_whether_it_was_a_quota_or_a_rate_limit():
    def quota(n, b):
        return httpx.Response(429, json={"error": {"message": f"You exceeded your current quota {KEY}", "type": "insufficient_quota", "code": "insufficient_quota"}})
    svc, store, _, _ = build(vendor=Recorder(quota))
    svc.decide(DAY)
    d = kinds(store, "stop")[0]["body"]
    assert d["reason"] == "http_429" and d["detail"]["kind"] == "quota" and d["detail"]["error_code"] == "insufficient_quota"

    def rate(n, b):
        return httpx.Response(429, json={"error": {"message": "Rate limit reached for requests", "type": "requests", "code": "rate_limit_exceeded"}},
                              headers={"retry-after": "20", "x-ratelimit-remaining-requests": "0", "x-ratelimit-remaining-tokens": "1500"})
    svc2, store2, _, _ = build(vendor=Recorder(rate))
    svc2.decide(DAY)
    d2 = kinds(store2, "stop")[0]["body"]["detail"]
    assert d2 == {"retry_after": 20.0, "remaining_requests": 0.0, "remaining_tokens": 1500.0, "error_type": "requests", "error_code": "rate_limit_exceeded", "kind": "rate_limit"}
    assert KEY not in json.dumps(store.rows() + store2.rows())

    svc3, store3, _, _ = build(vendor=Recorder(lambda n, b: httpx.Response(429, text="slow down")))
    svc3.decide(DAY)
    assert kinds(store3, "stop")[0]["body"]["detail"]["kind"] == "unknown"
