"""Model capability port: manifest/PIT guard, enums-and-refs-only output, run persistence, replay, optional OpenAI provider (fake transport).

Spec: FINAL §7 (model run binding), §5.1 (model output is derived, never fact); contracts ModelRun.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pulse import contracts
from pulse.adapters.models import openai_compat, port
from pulse.adapters.models.rules import LOCATOR, RulesProvider
from pulse.adapters.models.runner import ModelRunError, ModelRunner
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.domain.intelligence import feeds

from .helpers import recorded

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 12, 45, tzinfo=UTC)
SECRET = "sk-test-SECRET-0123456789"


def _records(n=5):
    return feeds.parse_eastmoney_news(recorded("eastmoney_news_p1.json")).records[:n]


def _request(n=5, policy=None, as_of=NOW):
    recs = _records(n)
    manifest = tuple(port.ManifestRef("document_revision", f"doc-{r.source_record_id}", port.sha256_hex(r.title.encode()), r.published_at) for r in recs)
    inputs = {f"doc-{r.source_record_id}": r for r in recs}
    return port.ModelRequest(policy or port.claim_extraction_policy("rules", LOCATOR), manifest, as_of, inputs)


def test_manifest_with_material_after_analysis_as_of_is_refused():
    with pytest.raises(ValueError):
        _request(as_of=NOW - timedelta(days=3))


def test_rules_provider_is_deterministic_and_guarded_output_is_enums_and_refs():
    req = _request()
    a = RulesProvider().run(req).output
    b = RulesProvider().run(req).output
    assert port.canonical_bytes(a) == port.canonical_bytes(b)
    assert port.guard_output(req, a) is a
    assert port.input_context_hash(req) == port.input_context_hash(_request())


def test_guard_rejects_floats_unknown_refs_and_free_text():
    req = _request(2)
    good = RulesProvider().run(req).output
    bad_ref = json.loads(json.dumps(good))
    bad_ref["extractions"][0]["document_ref"] = "not-in-manifest"
    with pytest.raises(port.OutputRejected):
        port.guard_output(req, bad_ref)
    with_score = json.loads(json.dumps(good))
    with_score["extractions"][0]["claims"][0]["score"] = 0.93
    with pytest.raises(port.OutputRejected):
        port.guard_output(req, with_score)
    free_text = json.loads(json.dumps(good))
    free_text["extractions"][0]["claims"][0]["category"] = "this stock will surely rise"
    with pytest.raises(port.OutputRejected):
        port.guard_output(req, free_text)
    float_span = json.loads(json.dumps(good))
    float_span["extractions"][0]["claims"][0]["evidence_spans"][0]["end"] = 3.5
    with pytest.raises(port.OutputRejected):
        port.guard_output(req, float_span)


# ---- optional OpenAI-compatible provider -------------------------------------------------------------------------------------
def test_openai_provider_is_disabled_unless_the_key_file_is_readable(tmp_path):
    assert openai_compat.OpenAICompatProvider.from_env({}) is None
    assert openai_compat.OpenAICompatProvider.from_env({"PULSE_OPENAI_KEY_FILE": str(tmp_path / "missing.txt")}) is None
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n")
    assert openai_compat.OpenAICompatProvider.from_env({"PULSE_OPENAI_KEY_FILE": str(empty)}) is None
    k = tmp_path / "key.txt"
    k.write_text(SECRET)
    p = openai_compat.OpenAICompatProvider.from_env({"PULSE_OPENAI_KEY_FILE": str(k), "PULSE_OPENAI_MODEL": "m1"})
    assert p is not None and SECRET not in repr(p) and SECRET not in json.dumps(p.locator)


def _fake_provider(tmp_path, reply, status=200, seen=None):
    k = tmp_path / "key.txt"
    k.write_text(SECRET)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(status, json=reply)

    return openai_compat.OpenAICompatProvider(key_file=k, base_url="https://api.example.invalid/v1", model="m1", transport=httpx.MockTransport(handler))


def _llm_reply(req):
    out = RulesProvider().run(req).output
    return {"choices": [{"message": {"content": json.dumps(out)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_openai_provider_sends_key_only_in_header_and_returns_structured_output(tmp_path):
    req = _request(3)
    seen: list = []
    p = _fake_provider(tmp_path, _llm_reply(req), seen=seen)
    res = p.run(req)
    url, auth, body = seen[0]
    assert url.endswith("/chat/completions") and auth == f"Bearer {SECRET}"
    assert SECRET not in json.dumps(body, ensure_ascii=False) and body["response_format"]["json_schema"]["strict"] is True
    assert res.usage["tokens"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert port.guard_output(req, res.output)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_openai_http_errors_never_leak_the_key(tmp_path, status):
    p = _fake_provider(tmp_path, {"error": {"message": f"bad key {SECRET}"}}, status=status)
    with pytest.raises(port.ProviderError) as ei:
        p.run(_request(1))
    assert ei.value.code == f"http_{status}" and SECRET not in str(ei.value)


# ---- persistence + replay (real PostgreSQL) -----------------------------------------------------------------------------
class Counting:
    provider_id = "rules"

    def __init__(self):
        self.calls = 0
        self.inner = RulesProvider()

    def run(self, req):
        self.calls += 1
        return self.inner.run(req)


@pytest.mark.pg
def test_run_persists_policy_manifest_hashes_times_and_replay_reads_stored_output(conn, lake):
    prov = Counting()
    runner = ModelRunner(conn, lake, {"rules": prov}, clock=lambda: NOW)
    req = _request(4)
    r1 = runner.run(req)
    assert not r1.reused and prov.calls == 1
    row = repo.get_model_run(conn, r1.model_run_id)
    assert row["run_state"] == "succeeded" and row["model_policy_id"] == req.policy.model_policy_id and row["model_policy_version"] == 1
    assert row["input_context_hash"] == port.input_context_hash(req) and row["analysis_as_of"] == NOW
    assert row["started_at"] and row["completed_at"] >= row["started_at"] and row["output_ref"] and len(row["output_hash"]) == 64
    assert len(row["input_manifest"]["entries"]) == 4 and all(e["content_hash"] for e in row["input_manifest"]["entries"])
    pol = conn.execute("SELECT * FROM analysis.model_policy WHERE model_policy_id=%s", (req.policy.model_policy_id,)).fetchone()
    assert pol["prompt_digest"] == req.policy.prompt_digest and pol["retrieval_policy_version"] == "single-document-no-retrieval-v1"
    assert pol["response_contract_id"] == "pulse.intel.claim_extraction"
    # same policy + same manifest: reused, provider not called again
    r2 = runner.run(_request(4))
    assert r2.reused and r2.model_run_id == r1.model_run_id and prov.calls == 1 and r2.output == r1.output
    # replay never calls any provider
    cold = ModelRunner(conn, lake, {}, clock=lambda: NOW)
    assert cold.replay(r1.model_run_id) == r1.output
    # ModelRun wire contract
    wire = {"model_run_id": str(row["model_run_id"]), "model_policy_id": row["model_policy_id"], "model_policy_version": row["model_policy_version"],
            "stage_id": None, "input_context_hash": row["input_context_hash"], "analysis_as_of": "2026-11-11T12:45:00Z", "run_state": "succeeded",
            "publication_eligibility": "unchecked",
            "output": {"ref": row["output_ref"], "content_hash": row["output_hash"], "contract_id": "pulse.intel.claim_extraction", "schema_version": "1"},
            "started_at": "2026-11-11T12:45:00Z", "completed_at": "2026-11-11T12:45:00Z", "error_code": None}
    contracts.check("ModelRun", wire)


@pytest.mark.pg
def test_tampered_stored_output_fails_the_hash_check(conn, lake):
    runner = ModelRunner(conn, lake, {"rules": RulesProvider()}, clock=lambda: NOW)
    r = runner.run(_request(2))
    row = repo.get_model_run(conn, r.model_run_id)
    (lake.root / row["output_ref"]).write_bytes(b'{"extractions":[]}')
    with pytest.raises(ValueError):
        runner.replay(r.model_run_id)


class Bad:
    provider_id = "rules"

    def __init__(self, out):
        self.out = out

    def run(self, req):
        return port.ProviderResult(self.out, {})


@pytest.mark.pg
def test_invalid_provider_output_is_a_failed_run_with_no_stored_output(conn, lake):
    runner = ModelRunner(conn, lake, {"rules": Bad({"extractions": [{"document_ref": "ghost", "claims": []}]})}, clock=lambda: NOW)
    with pytest.raises(ModelRunError) as ei:
        runner.run(_request(2))
    row = repo.get_model_run(conn, ei.value.run_id)
    assert row["run_state"] == "failed" and row["error_code"] == "output_rejected" and row["output_ref"] is None and row["output_hash"] is None
    assert conn.execute("SELECT count(*) AS n FROM analysis.model_run WHERE run_state='succeeded'").fetchone()["n"] == 0


@pytest.mark.pg
def test_provider_failure_records_code_without_secrets(conn, lake, tmp_path):
    prov = _fake_provider(tmp_path, {"error": {"message": SECRET}}, status=500)
    policy = port.claim_extraction_policy("openai_compat", prov.locator, rollout_state="candidate")
    runner = ModelRunner(conn, lake, {"openai_compat": prov}, clock=lambda: NOW)
    with pytest.raises(ModelRunError) as ei:
        runner.run(_request(1, policy=policy))
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM analysis.model_run").fetchall()], default=str)
    dump += json.dumps([dict(r) for r in conn.execute("SELECT * FROM analysis.model_policy").fetchall()], default=str)
    assert ei.value.code == "http_500" and SECRET not in dump and "Bearer" not in dump
