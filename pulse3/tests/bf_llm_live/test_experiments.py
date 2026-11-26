"""E100 / E092 on the shared lane (offline): separation per experiment, E092 closed, the cost probe, the luna request body, per-model prices, and the model allow-list guard of both clients."""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from pulse.adapters.models import alert_llm as AL
from pulse.adapters.models import model_guard as MG
from pulse.adapters.models.responses_client import ResponsesClient
from pulse.application import alert_llm_service as LS
from pulse.application.bf_llm_live_store import MemoryLogStore
from pulse.domain.alerts import tiers as T
from pulse.domain.llm_live import experiments as X
from pulse.domain.llm_live import rules as R
from bf_llm_live.support import DAY, KEY, Recorder, build, kinds, plan, reply
from bf_llm_live.test_track_judge import seeded_store

PROBE_DAY = date(2026, 11, 20)
OTHER = dataclasses.replace(X.E100, key="E101", dataset="llm_live_log_e101")                    # a second open experiment, to prove nothing is shared but the lane lock
TERRA = dataclasses.replace(X.E100, key="E999", model="gpt-6.1-terra", dataset="llm_live_log_terra")


# -- separation -------------------------------------------------------------------------------------------------------------
def test_each_experiment_counts_its_own_decision_days_and_requests():
    full = MemoryLogStore()
    for i in range(20):
        d = f"2026-10-{i + 1:02d}"
        full.append([{"session": d, "kind": "call", "key": "k", "written_at": "x", "run_id": "r", "body": {"n": 1, "id": "k"}}])
    a, _, vendor_a, _ = build(store=full)                          # E100 with 20 decision days already on ITS log
    b, store_b, vendor_b, _ = build(experiment=OTHER)              # another experiment with its own empty log
    assert a.decide(DAY)["status"] == "limit" and not vendor_a.requests
    out = b.decide(DAY)
    assert out["status"] == "done" and out["experiment"] == "E101" and len(vendor_b.requests) == 40 and b.status()["decision_days"] == 1 and a.status()["decision_days"] == 20


def test_status_and_judgement_are_labelled_and_keyed_by_experiment():
    a, store_a, _, _ = build(store=seeded_store(20), experiment=X.E100)
    b, store_b, _, _ = build(store=seeded_store(20), experiment=OTHER)
    assert a.status()["experiment"] == "E100" and a.status()["model"] == "gpt-6-luna" and b.status()["experiment"] == "E101"
    a.judge()
    b.judge()
    assert [(r["key"], r["body"]["experiment"], r["body"]["spec_sha256"]) for r in kinds(store_a, "judgement")] == [("E100", "E100", X.E100.spec_sha256)]
    assert [r["key"] for r in kinds(store_b, "judgement")] == ["E101"]


def test_experiments_have_their_own_dataset_model_and_document():
    assert X.E092.dataset == "llm_live_log" and X.E100.dataset == "llm_live_log_e100" and X.E092.dataset != X.E100.dataset
    assert (X.E092.model, X.E100.model) == ("gpt-6.1-terra", "gpt-6-luna") and X.E100.effort == "high" and X.E100.service_tier is None
    assert X.DEFAULT_EXPERIMENT == "E100" and X.get("E100") is X.E100
    with pytest.raises(ValueError):
        X.get("E1")


# -- E092 is closed ---------------------------------------------------------------------------------------------------------
def test_e092_decide_and_probe_are_refused_even_when_its_model_is_allowed(monkeypatch):
    monkeypatch.setenv(MG.ENV_ALLOWED, "gpt-6.1-terra,gpt-6-luna")
    svc, store, vendor, _ = build(experiment=X.E092)
    out = svc.decide(DAY)
    assert out["status"] == "closed" and "closed by the user 2026-11-23" in out["detail"] and out["experiment"] == "E092"
    assert svc.probe(PROBE_DAY, 3, allow=True)["status"] == "closed"
    assert not vendor.requests and not store.rows()


def test_e092_rows_can_still_be_tracked_judged_and_read():
    svc, store, _, _ = build(store=seeded_store(20), experiment=X.E092)
    st = svc.status()
    assert st["closed"] and st["decision_days"] == 20 and st["experiment"] == "E092"
    assert svc.judge()["status"] == "judged" and [r["key"] for r in kinds(store, "judgement")] == ["E092"]
    assert svc.track(date(2026, 12, 31))["status"] in ("nothing_due", "tracked", "waiting_for_data")


def test_the_worker_refuses_e092_decide_and_registration(monkeypatch, capsys):
    from pulse.workers import llm_live as W
    from pulse.application import bf_llm_live_registry as REG

    with pytest.raises(RuntimeError, match="closed"):
        REG.register(None, None, X.E092)
    p = W._parser()
    assert p.parse_args(["decide", "--experiment", "E092"]).experiment == "E092" and p.parse_args(["decide"]).experiment == "E100"
    assert W._exit_code("decide", {"status": "closed"}) == 3 and W._exit_code("probe", {"status": "probed"}) == 0 and W._exit_code("probe", {"status": "probe_partial"}) == 2


def test_run_daily_passes_the_experiment_to_both_steps():
    text = (Path(__file__).resolve().parents[2] / "deploy" / "windows" / "run_daily.ps1").read_text(encoding="utf-8")
    assert "'decide', '--experiment', 'E100', '--date', $day" in text and "'track', '--experiment', 'E100', '--today', $day" in text


# -- the luna request ---------------------------------------------------------------------------------------------------------
def test_a_decide_day_sends_the_luna_request_with_web_search_and_no_priority_tier():
    svc, _, vendor, _ = build()
    svc.decide(DAY)
    body = vendor.requests[0]
    assert body["model"] == "gpt-6-luna" and body["reasoning"] == {"effort": "high"} and body["tools"] == [{"type": "web_search"}]
    assert "service_tier" not in body and body["max_output_tokens"] == 12000 and body["instructions"].startswith("你是一名经验丰富的 A 股交易员")
    assert {r["model"] for r in vendor.requests} == {"gpt-6-luna"} and all("service_tier" not in r for r in vendor.requests)


def test_the_candidate_rules_and_the_prompt_are_the_e092_ones():
    scores = {f"60{i:04d}": float(i) for i in range(200)}
    assert R.pick_universe(scores, DAY) == R.pick_universe(scores, DAY) and (R.N_TOP, R.N_CONTROLS, R.MAX_CALLS, R.MAX_DECISION_DAYS) == (30, 10, 60, 20)
    assert "今天是 2026-11-23" in R.system_text(DAY) and "gpt" not in R.system_text(DAY)


# -- the cost probe -----------------------------------------------------------------------------------------------------------
PROBE_PRICES = R.Prices(in_per_m=1.0, out_per_m=4.0, search_per_call=0.01, reserve_per_call=0.25)


def probe_build(**kw):
    kw.setdefault("vendor", Recorder(lambda n, b: reply(rid=f"p{n}", tin=40000, tout=1800, searches=3)))
    return build(context=lambda d: plan(d), prices=PROBE_PRICES, **kw)


def test_the_probe_sends_three_calls_ledger_first_and_prints_usage_and_a_day_estimate():
    seen = []
    svc, store, vendor, _ = probe_build(vendor=Recorder(lambda n, b: (seen.append(len([r for r in store.rows() if r["kind"] == "probe" and r["body"]["phase"] == "ledger"])), reply(rid=f"p{n}", tin=40000, tout=1800, searches=3))[1]))
    out = svc.probe(PROBE_DAY, 3, allow=True)
    assert out["status"] == "probed" and len(vendor.requests) == 3 and seen == [1, 2, 3]                 # the ledger row of call n was stored before request n
    assert [c["usage"] for c in out["per_call"]] == [{"input_tokens": 40000, "output_tokens": 1800}] * 3 and out["per_call"][0]["searches"] == 3 and out["per_call"][0]["parsed"] is True
    per_call = 40000 / 1e6 * 1.0 + 1800 / 1e6 * 4.0 + 3 * 0.01
    assert out["mean_call_usd"] == pytest.approx(per_call, abs=1e-6) and out["estimate_day_usd"] == pytest.approx(per_call * 40, abs=1e-3)
    assert out["within_cap"] is True and out["cap_usd"] == 5.0 and out["mean_input_tokens"] == 40000 and out["mean_output_tokens"] == 1800 and "confirm" in out["prices_are_placeholders"]
    assert len({r["input"] for r in vendor.requests}) == 3                                        # three different names
    results = [r["body"] for r in kinds(store, "probe") if r["body"]["phase"] == "result"]
    assert len(results) == 3 and all(r["raw_text"] and r["response_id"] and r["request_id"] and r["model"] == "gpt-6-luna" for r in results) and KEY not in json.dumps(store.rows())


def test_a_probe_over_the_cap_says_so():
    svc, _, _, _ = probe_build(vendor=Recorder(lambda n, b: reply(rid=f"p{n}", tin=400000, tout=18000, searches=5)))
    out = svc.probe(PROBE_DAY, 3, allow=True)
    assert out["estimate_day_usd"] > 5.0 and out["within_cap"] is False


def test_probe_rows_never_count_toward_calls_days_spend_or_the_score():
    svc, store, vendor, _ = probe_build()
    svc.probe(PROBE_DAY, 3, allow=True)
    assert not kinds(store, "call") and not kinds(store, "answer") and not kinds(store, "day") and not kinds(store, "outcome")
    st = svc.status()
    assert st["decision_days"] == 0 and st["calls_today"] == 0 and st["spent_today_usd"] == 0 and st["scored"] == 0 and st["probe"]["calls"] == 3 and st["probe"]["cost_usd"] > 0
    assert R.day_spend(store.rows(), PROBE_DAY.isoformat(), PROBE_PRICES) == (0.0, 0)
    out = svc.decide(DAY)                                                  # the real day is unaffected: a full 40, numbered from 1, the 60-request cap intact
    assert out["status"] == "done" and out["calls"] == 40 and sorted(r["body"]["n"] for r in kinds(store, "call"))[0] == 1 and svc.status()["decision_days"] == 1 and len(vendor.requests) == 43


def test_the_probe_needs_the_explicit_flag_and_not_the_switch_file():
    svc, store, vendor, _ = probe_build(switch=False)
    out = svc.probe(PROBE_DAY, 3, allow=False)
    assert out["status"] == "probe_not_allowed" and len(out["would_ask"]) == 3 and not vendor.requests and not store.rows()
    assert svc.probe(PROBE_DAY, 3, allow=True)["status"] == "probed" and len(vendor.requests) == 3                  # no switch file needed: the probe runs before go-live


def test_a_second_probe_run_is_refused_once_three_calls_exist():
    svc, store, vendor, _ = probe_build()
    svc.probe(PROBE_DAY, 3, allow=True)
    again = svc.probe(PROBE_DAY, 3, allow=True)
    assert again["status"] == "probe_done" and len(vendor.requests) == 3 and again["probe"]["calls"] == 3
    svc2, _, vendor2, _ = probe_build()
    svc2.probe(PROBE_DAY, 2, allow=True)
    assert svc2.probe(PROBE_DAY, 2, allow=True)["status"] == "probe_limit" and len(vendor2.requests) == 2
    assert svc2.probe(PROBE_DAY, 1, allow=True)["status"] == "probed" and len(vendor2.requests) == 3


@pytest.mark.parametrize("n", [0, 4, -1])
def test_a_probe_asks_between_one_and_three_calls(n):
    svc, store, vendor, _ = probe_build()
    assert svc.probe(PROBE_DAY, n, allow=True)["status"] == "bad_n" and not vendor.requests and not store.rows()


def test_a_vendor_refusal_stops_the_probe_at_once_and_is_recorded():
    svc, store, vendor, _ = probe_build(vendor=Recorder(lambda n, b: httpx.Response(401, text=f"bad key {KEY}")))
    out = svc.probe(PROBE_DAY, 3, allow=True)
    assert out["status"] == "probe_partial" and len(vendor.requests) == 1 and out["per_call"][0]["label"] == "http_401"
    assert KEY not in json.dumps(store.rows()) + json.dumps(out)


def test_the_probe_refuses_a_missing_key_a_busy_lane_and_unready_inputs():
    from pulse.adapters.models.responses_client import KeyProblem
    from pulse.application.bf_llm_live_service import ContextMissing

    def no_key():
        raise KeyProblem("no_key")
    svc, store, vendor, _ = probe_build(key_loader=no_key)
    assert svc.probe(PROBE_DAY, 3, allow=True)["status"] == "no_key"
    svc2, store2, vendor2, _ = probe_build()
    with store2.exclusive():
        assert svc2.probe(PROBE_DAY, 3, allow=True)["status"] == "busy"

    def missing(day):
        raise ContextMissing("scores_not_ready", "none")
    svc3, _, vendor3, _ = build(context=missing)
    assert svc3.probe(PROBE_DAY, 3, allow=True)["status"] == "not_ready" and not (vendor.requests or vendor2.requests or vendor3.requests)


# -- prices per model -----------------------------------------------------------------------------------------------------------
def test_prices_come_from_the_models_own_variables_then_the_general_ones_then_the_default():
    env = {"PULSE_LLM_PRICE_IN_PER_M": "5", "PULSE_LLM_PRICE_OUT_PER_M": "25", "PULSE_LLM_PRICE_IN_PER_M_GPT_6_LUNA": "0.5", "PULSE_LLM_PRICE_SEARCH_PER_CALL_GPT_6_LUNA": "0.02"}
    luna = R.prices_from_env(env, "gpt-6-luna")
    assert (luna.in_per_m, luna.out_per_m, luna.search_per_call, luna.cached_in_per_m) == (0.5, 25.0, 0.02, None)          # model first, then general (out), default (cached: the input price)
    terra = R.prices_from_env(env, "gpt-6.1-terra")
    assert (terra.in_per_m, terra.out_per_m, terra.search_per_call) == (5.0, 25.0, 0.01)
    assert R.prices_from_env({}, "gpt-6-luna") == R.Prices() and R.prices_from_env(env).in_per_m == 5.0 and R.model_suffix("gpt-6.1-terra") == "GPT_6_1_TERRA"
    with pytest.raises(ValueError, match="PULSE_LLM_PRICE_OUT_PER_M_GPT_6_LUNA"):
        R.prices_from_env({"PULSE_LLM_PRICE_OUT_PER_M_GPT_6_LUNA": "abc"}, "gpt-6-luna")


def test_cached_input_tokens_are_charged_at_the_cached_price_or_else_the_input_price():
    usage = {"input_tokens": 40000, "output_tokens": 1000, "input_tokens_details": {"cached_tokens": 30000}}
    same = R.Prices(in_per_m=1.0, out_per_m=4.0, search_per_call=0.0)
    assert R.estimate_cost(usage, 0, same) == pytest.approx(0.04 + 0.004)                                  # no cached price: everything at the input price
    cheap = R.Prices(in_per_m=1.0, out_per_m=4.0, search_per_call=0.0, cached_in_per_m=0.1)
    assert R.estimate_cost(usage, 0, cheap) == pytest.approx(10000 / 1e6 * 1.0 + 30000 / 1e6 * 0.1 + 0.004)
    odd = {"input_tokens": 100, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 500}}
    assert R.estimate_cost(odd, 0, cheap) == pytest.approx(100 / 1e6 * 1.0)                                  # an impossible cached count is ignored, never a negative cost
    assert R.estimate_cost({"input_tokens": 10, "output_tokens": 0, "input_tokens_details": "x"}, 0, cheap) == pytest.approx(10 / 1e6)


# -- the allow-list guard -------------------------------------------------------------------------------------------------------
def test_the_allow_list_defaults_to_luna_and_reads_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv(MG.ENV_ALLOWED, raising=False)
    assert MG.allowed_models() == ("gpt-6-luna",) and MG.allowed_models({MG.ENV_ALLOWED: " , "}) == ("gpt-6-luna",)
    assert MG.allowed_models({MG.ENV_ALLOWED: "a, b ,,c"}) == ("a", "b", "c")
    assert MG.require_allowed("gpt-6-luna") == "gpt-6-luna"
    for bad in ("gpt-6.1-terra", "gpt-6-luna ", "GPT-6-LUNA", "", None):
        with pytest.raises(MG.ModelNotAllowed):
            MG.require_allowed(bad)
    monkeypatch.setenv(MG.ENV_ALLOWED, "gpt-6.1-terra")
    assert MG.require_allowed("gpt-6.1-terra") and MG.allowed_models() == ("gpt-6.1-terra",)
    with pytest.raises(MG.ModelNotAllowed):
        MG.require_allowed("gpt-6-luna")


def test_the_responses_client_refuses_a_model_outside_the_list_before_any_network_io():
    rec = Recorder()
    c = ResponsesClient(KEY, transport=httpx.MockTransport(rec))
    with pytest.raises(MG.ModelNotAllowed) as e:
        c.ask("p", model="gpt-6.1-terra", effort="high", system="s")
    assert not rec.requests and "gpt-6.1-terra" in str(e.value) and KEY not in str(e.value)
    assert c.ask("p", model="gpt-6-luna", effort="high", system="s").text and len(rec.requests) == 1


@pytest.mark.parametrize("fn", ["decide", "probe"])
def test_a_refused_model_raises_before_the_key_the_universe_any_ledger_row_or_request(fn):
    called = []
    svc, store, vendor, _ = build(experiment=TERRA, key_loader=lambda: called.append("key") or KEY, context=lambda d: called.append("ctx") or plan(d))
    out = svc.decide(DAY) if fn == "decide" else svc.probe(PROBE_DAY, 3, allow=True)
    assert out["status"] == "model_not_allowed" and "gpt-6.1-terra" in out["detail"] and called == [] and not vendor.requests and not store.rows()


def test_the_model_is_checked_again_before_every_ledger_row(monkeypatch):
    monkeypatch.setenv(MG.ENV_ALLOWED, "gpt-6.1-terra")
    holder = {}

    def fn(n, body):
        monkeypatch.setenv(MG.ENV_ALLOWED, "gpt-6-luna")               # the allow-list changes while the day runs
        return reply(rid=f"r{n}")
    svc, store, vendor, _ = build(experiment=TERRA, vendor=Recorder(fn))
    out = svc.decide(DAY)
    assert out["status"] == "stopped" and out["stop"] == "model_not_allowed" and len(vendor.requests) == 1 and len(kinds(store, "call")) == 1 and holder == {}


def test_an_allowed_non_luna_model_still_runs_only_where_the_list_says_so(monkeypatch):
    monkeypatch.setenv(MG.ENV_ALLOWED, "gpt-6.1-terra")
    svc, _, vendor, _ = build(experiment=TERRA)
    assert svc.decide(DAY)["status"] == "done" and {r["model"] for r in vendor.requests} == {"gpt-6.1-terra"}


# -- the alert fallback uses the same guard -------------------------------------------------------------------------------------------
def key_file(tmp_path):
    p = tmp_path / "openai.key"
    p.write_text("sk-test-alert-key\n", encoding="utf-8")
    return p


def test_the_alert_client_refuses_terra_from_the_environment_and_by_construction(tmp_path):
    kf = key_file(tmp_path)
    env = {"PULSE_OPENAI_KEY_FILE": str(kf), "PULSE_ALERT_MODEL": "gpt-6.1-terra"}
    with pytest.raises(MG.ModelNotAllowed):
        AL.AlertLlmClient.from_env(env)
    with pytest.raises(MG.ModelNotAllowed):
        AL.AlertLlmClient(key_file=kf, model="gpt-6.1-terra")
    assert AL.AlertLlmClient.from_env({"PULSE_OPENAI_KEY_FILE": str(kf)}).model == "gpt-6-luna"


def test_the_alert_client_rechecks_before_reading_the_key_or_sending(tmp_path):
    rec = Recorder()
    c = AL.AlertLlmClient(key_file=tmp_path / "missing.key", transport=httpx.MockTransport(rec))
    c.model = "gpt-6.1-terra"                                          # a mutated client: the guard in ask() still stops it
    with pytest.raises(MG.ModelNotAllowed):
        c.ask("s", "p")
    assert not rec.requests


def test_the_alert_fallback_is_off_with_a_log_line_when_the_configured_model_is_refused(tmp_path, monkeypatch, caplog):
    kf = key_file(tmp_path)
    switch = tmp_path / "alert_llm.enabled"
    switch.write_text("on", encoding="utf-8")
    env = {"PULSE_OPENAI_KEY_FILE": str(kf), "PULSE_ALERT_MODEL": "gpt-6.1-terra", LS.ENV_SWITCH: str(switch)}
    with caplog.at_level(logging.ERROR, logger="pulse.alert_llm"):
        assert LS.AlertFallback.from_env(env) is None
    assert "gpt-6.1-terra" in caplog.text and "sk-test" not in caplog.text
    ok = LS.AlertFallback.from_env({**env, "PULSE_ALERT_MODEL": "gpt-6-luna"})
    assert ok is not None and ok.client.model == "gpt-6-luna"


def test_the_alert_fallback_never_reaches_the_ledger_with_a_refused_model(tmp_path):
    kf = key_file(tmp_path)
    rec = Recorder()
    client = AL.AlertLlmClient(key_file=kf, transport=httpx.MockTransport(rec))
    fb = LS.AlertFallback(client, sleep=lambda s: None)
    client.model = "gpt-6.1-terra"
    claim = T.ClaimView("c1", "r1", 1, "某公司召开投资者交流会", None, "none", "unverified", "independently_reported", "2026-11-23T01:00:00+00:00", None, None, ())

    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"the database was touched ({name}) although the model is refused")
    assert fb.judge(NoDatabase(), claim, {}, __import__("datetime").datetime(2026, 11, 23, 12, tzinfo=__import__("datetime").timezone.utc)) is None
    assert fb.skipped["model_not_allowed"] == 1 and not rec.requests
