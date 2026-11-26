"""The Responses client: key file handling, what goes on the wire, the stop / failure labels, and that the key never leaks (no network: ``httpx.MockTransport``)."""
from __future__ import annotations

import httpx
import pytest

from pulse.adapters.models import responses_client as RC
from bf_llm_live.support import KEY, Recorder, reply


def client(fn, key=KEY):
    rec = Recorder(fn)
    return RC.ResponsesClient(key, transport=httpx.MockTransport(rec)), rec


def ask(c):
    return c.ask("the prompt", model="gpt-6-luna", effort="high", system="the system")


def test_key_file_is_plain_utf8_first_line(tmp_path):
    p = tmp_path / "k.txt"
    p.write_bytes(b"sk-abc123\nsecond line\n")
    assert RC.read_key(p) == "sk-abc123"
    p.write_bytes(b"\xef\xbb\xbfsk-abc123\n")                         # a BOM from a PowerShell write
    with pytest.raises(RC.KeyProblem) as e:
        RC.read_key(p)
    assert e.value.code == "bad_key"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(RC.KeyProblem) as e:
        RC.read_key(p)
    assert e.value.code == "no_key"
    with pytest.raises(RC.KeyProblem) as e:
        RC.read_key(tmp_path / "missing.txt")
    assert e.value.code == "no_key"
    with pytest.raises(RC.KeyProblem):
        RC.read_key(env={})


def test_request_has_the_preregistered_shape_and_the_key_only_in_the_header():
    c, rec = client(lambda n, b: reply(rid="resp_9"))
    r = ask(c)
    assert rec.urls == [RC.URL] and KEY not in rec.urls[0] and rec.auth_ok == [True]
    body = rec.requests[0]
    assert body == {"model": "gpt-6-luna", "input": "the prompt", "instructions": "the system", "reasoning": {"effort": "high"}, "tools": [{"type": "web_search"}]}
    assert KEY not in str(body)
    assert (r.response_id, r.request_id, r.searches, r.status) == ("resp_9", "req-resp_9", 2, "completed")
    assert r.usage == {"input_tokens": 2000, "output_tokens": 500} and '"action"' in r.text


@pytest.mark.parametrize("code", [401, 403, 429])
def test_401_403_429_stop_the_day_and_the_error_never_quotes_the_body(code):
    c, _ = client(lambda n, b: httpx.Response(code, json={"error": {"message": f"Incorrect API key provided: {KEY}"}}))
    with pytest.raises(RC.CallStop) as e:
        ask(c)
    assert e.value.reason == f"http_{code}" and KEY not in str(e.value) and KEY not in repr(e.value) and KEY not in repr(c)


def test_other_failures_are_labels_only():
    for status, label in ((500, "http_500"), (400, "http_400")):
        c, _ = client(lambda n, b, s=status: httpx.Response(s, text=f"boom {KEY}"))
        with pytest.raises(RC.CallFailed) as e:
            ask(c)
        assert e.value.label == label and KEY not in str(e.value)
    c, _ = client(lambda n, b: httpx.Response(200, text="not json"))
    with pytest.raises(RC.CallFailed) as e:
        ask(c)
    assert e.value.label == "malformed_reply"


def test_timeout_and_transport_errors_are_labelled_without_messages():
    def boom(n, b):
        raise httpx.ReadTimeout(f"timed out talking to host with {KEY}")
    with pytest.raises(RC.CallFailed) as e:
        ask(client(boom)[0])
    assert e.value.label == "timeout" and KEY not in str(e.value)

    def conn(n, b):
        raise httpx.ConnectError(f"cannot connect {KEY}")
    with pytest.raises(RC.CallFailed) as e:
        ask(client(conn)[0])
    assert e.value.label == "ConnectError" and KEY not in str(e.value)


def test_a_refusal_stops_the_day_and_keeps_the_paid_usage():
    body = {"id": "resp_r", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "I can't help"}]}]}
    c, _ = client(lambda n, b: httpx.Response(200, json=body))
    with pytest.raises(RC.CallStop) as e:
        ask(c)
    assert e.value.reason == "refusal" and e.value.reply.usage["input_tokens"] == 10
    body = {"id": "resp_f", "status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "output": []}
    with pytest.raises(RC.CallStop):
        ask(client(lambda n, b: httpx.Response(200, json=body))[0])


def test_an_unfinished_reply_is_a_failure_not_an_answer():
    c, _ = client(lambda n, b: reply(status="incomplete"))
    with pytest.raises(RC.CallFailed) as e:
        ask(c)
    assert e.value.label == "status_incomplete" and e.value.reply is not None


def test_max_output_tokens_is_sent_only_when_given():
    c, rec = client(lambda n, b: reply())
    c.ask("p", model="gpt-6-luna", effort="high", system="s", max_output_tokens=4321)
    assert rec.requests[0]["max_output_tokens"] == 4321
    ask(c)
    assert "max_output_tokens" not in rec.requests[1]


@pytest.mark.parametrize("output", ["a string", [1, 2], [None], [{"type": "message", "content": ["x"]}], [{"type": "message", "content": "text"}]])
def test_a_reply_with_the_wrong_structure_is_a_failure_with_its_usage_not_an_exception(output):
    body = {"id": "resp_m", "status": "completed", "usage": {"input_tokens": 7, "output_tokens": 3}, "output": output}
    c, _ = client(lambda n, b: httpx.Response(200, json=body))
    with pytest.raises(RC.CallFailed) as e:
        ask(c)
    assert e.value.label == "malformed_reply" and e.value.reply.usage == {"input_tokens": 7, "output_tokens": 3}


def test_odd_scalar_fields_do_not_raise():
    body = {"id": 5, "status": ["x"], "usage": "none", "incomplete_details": "oops", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}
    c, _ = client(lambda n, b: httpx.Response(200, json=body))
    r = ask(c)
    assert r.text == "{}" and r.usage == {} and r.response_id is None and r.status is None
