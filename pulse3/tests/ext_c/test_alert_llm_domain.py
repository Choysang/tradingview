"""Alert LLM fallback, pure parts (no database, no network): who is asked, the question, the answer, the client."""
from __future__ import annotations

import json

import httpx
import pytest

from pulse.adapters.models import alert_llm as AL
from pulse.application import alert_llm_service as LS
from pulse.domain.alerts import llm_tier as LT
from pulse.domain.alerts import tiers as T

from . import llm_support as H

HELD, WATCHED, OTHER = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b", "00000000-0000-0000-0000-00000000000c"
CTX = T.Context(holdings=frozenset({HELD}), watch=frozenset({WATCHED}), main_board=frozenset({HELD, WATCHED, OTHER}),
                holding_industries={"房地产": frozenset({HELD})})


def view(headline="某公司召开投资者交流会", polarity="none", ids=(HELD,), lag=None, claim_id="c1") -> T.ClaimView:
    return T.ClaimView(claim_id, "r1", 1, headline, None, polarity, "unverified", "independently_reported", "2026-11-23T01:00:00+00:00", None, lag, tuple(ids))


# ---- who is asked ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("claim, expected", [
    (view(), True),                                                      # names a holding, rules found no direction
    (view(polarity="mixed"), True),                                      # conflicting words: no direction either
    (view(polarity="-"), False),                                         # the rules decided a direction (P0 follows by the rules, not the model)
    (view(polarity="+"), False),                                         # positive on a holding: decided, no tier, nothing to ask
    (view(ids=(OTHER,)), False),                                         # touches neither a holding nor a watch-list name
    (view(ids=()), False),                                               # names nothing (a macro item is not asked: the brief says "touches")
    (view(headline=""), False),                                          # no text to judge
    (view(lag=3 * 3600), False),                                         # backfill never alerts
    (view("A股早报：精选个股一览"), False),                                 # digests are never judged
    (view("每日盘点：今日涨停复盘"), False),
    (view("某公司：明年前瞻业务交流"), True),                               # the speaker alone decides: "前瞻" after the colon does not make a digest
], ids=lambda x: x if isinstance(x, bool) else None)
def test_who_is_undecided(claim, expected):
    assert LT.is_undecided(claim, CTX, T.decide(claim, CTX)) is expected


def test_a_rule_hit_always_wins():
    claim = view(ids=(WATCHED,))                                         # a watch-list name: the rules already give P2 whatever the polarity
    hits = T.decide(claim, CTX)
    assert [h.tier for h in hits] == ["P2"] and LT.is_undecided(claim, CTX, hits) is False
    assert LT.is_undecided(claim, CTX, []) is True                       # the same claim with no rule hit would be undecided


@pytest.mark.parametrize("text, digest", [("早报：今日要闻", True), ("午评：沪指收涨", True), ("某券商精选金股", True), ("公司拟回购股份", False),
                                          ("某公司：周报业务介绍", False), ("某公司召开交流会", False), ("ＡＢ证券：午评", False)])   # a speaker's own words are never a digest (old rule)
def test_digest_rule_is_the_old_one(text, digest):
    assert LT.is_digest(text) is digest


# ---- the question ----------------------------------------------------------------------------------------------------------------
def test_prompt_names_only_the_claims_own_instruments_never_the_holdings():
    claim = view(ids=(HELD, OTHER))
    cands = LT.candidates_of(claim, {HELD: {"code": "600513-ab12cd", "name": "星河制药"}, OTHER: {"code": None, "name": ""}})
    assert [c.key for c in cands] == ["600513", "id:" + OTHER.replace("-", "")[:8]]
    prompt = LT.build_prompt(claim, cands, ["房地产", "银行"])
    assert "600513 星河制药" in prompt and "某公司召开投资者交流会" in prompt and "房地产、银行" in prompt
    assert LT.prompt_hash(prompt) == LT.prompt_hash(prompt) != LT.prompt_hash(prompt + " ")
    assert "confidence" in LT.SYSTEM_PROMPT and len(LT.prompt_hash(prompt)) == 64


def test_message_key_is_per_claim_and_text():
    a = view()
    assert LT.message_key(a) == LT.message_key(view()) != LT.message_key(view(claim_id="c2")) != LT.message_key(view(headline="更正后的标题"))
    assert LT.message_key(view("ＡＢ")) == LT.message_key(view("AB"))     # NFKC: one spelling


# ---- the answer ---------------------------------------------------------------------------------------------------------------------
def test_parse_answer_keeps_only_what_was_offered():
    cands = [LT.Candidate("600513", HELD, "星河制药")]
    ok = LT.parse_answer('noise {"direction":"NEGATIVE","magnitude":9,"confidence":1.7,"codes":["600513","999999"],"industries":["房地产","没有"],"reason":"x"} noise',
                         cands, {"房地产"})
    assert ok == LT.Judgement("negative", 3, 1.0, (HELD,), ("房地产",), "x")
    old = LT.parse_answer(json.dumps({"direction": "positive", "magnitude": 0, "codes": [], "industries": []}), cands, set())
    assert old is not None and old.confidence is None and old.magnitude == 1       # an answer in the old format (no confidence) is still valid
    assert LT.parse_answer('{"direction":"positive","confidence":"high"}', cands, set()).confidence is None
    assert LT.parse_answer('{"direction":"positive","confidence":true}', cands, set()).confidence is None
    for bad in ("", "not json", '{"direction":"bogus"}', "[1,2]", '{"magnitude":2}', '{"direction":"negative","magnitude":"x"}',
                '{"direction":"negative","magnitude":1e999}', '{"direction":"negative","magnitude":-1e999}',          # inf: json.loads accepts it
                '{"direction":"negative","magnitude":1%s}' % ("0" * 400), '{"direction":"negative","confidence":1%s}' % ("0" * 400)):   # int too big for a float
        assert LT.parse_answer(bad, cands, set()) is None
    nul = LT.parse_answer(r'{"direction":"negative","reason":"a\u0000b\ud800c"}', cands, set())
    assert nul is not None and nul.reason == "ab?c"                                                        # NUL and a lone surrogate cannot reach PostgreSQL
    assert LT.clean_json({"k\x00": ["a\x00b", 1, {"z": "\x00"}]}) == {"k": ["ab", 1, {"z": ""}]}


# ---- answer -> tiers ------------------------------------------------------------------------------------------------------------------
def judged(direction="negative", ids=(HELD,), inds=(), conf=0.7) -> LT.Judgement:
    return LT.Judgement(direction, 2, conf, tuple(ids), tuple(inds), "r", "jid", "m")


def test_negative_on_a_holding_is_p0_as_in_the_old_system():
    assert LT.LLM_MAY_RAISE_P0 is True                                   # kept from news_alerts.decide_levels; the report says so
    hits = LT.decide_llm(view(), CTX, judged())
    assert [h.tier for h in hits] == ["P0"] and hits[0].instrument_ids == (HELD,) and hits[0].trigger["llm"] is True and hits[0].trigger["confidence"] == 0.7


def test_p0_can_be_switched_off_and_the_answer_is_lowered_to_p1(monkeypatch):
    monkeypatch.setattr(LT, "LLM_MAY_RAISE_P0", False)
    hits = LT.decide_llm(view(), CTX, judged())
    assert [h.tier for h in hits] == ["P1"] and hits[0].industries == ("房地产",) and hits[0].held_in_industry == (HELD,)
    assert LT.decide_llm(view(), T.Context(CTX.holdings, CTX.watch, CTX.main_board, {}), judged()) == []     # nothing to lower to: no alert


def test_p1_from_a_named_industry_and_p2_for_watch_or_positive_unheld():
    assert [h.tier for h in LT.decide_llm(view(), CTX, judged(ids=(), inds=("房地产",)))] == ["P1"]
    assert LT.decide_llm(view(), CTX, judged(ids=(), inds=("银行",))) == []                       # an industry that holds none of our names
    both = view(ids=(HELD, WATCHED))
    assert [h.tier for h in LT.decide_llm(both, CTX, judged(ids=(HELD, WATCHED)))] == ["P0", "P2"]
    assert [h.tier for h in LT.decide_llm(both, CTX, judged("positive", (WATCHED,)))] == ["P2"]
    assert [h.tier for h in LT.decide_llm(view(ids=(HELD, OTHER)), CTX, judged("positive", (OTHER,)))] == ["P2"]    # positive on an unheld main-board name
    assert LT.decide_llm(view(ids=(HELD, OTHER)), CTX, judged("negative", (OTHER,))) == []                         # negative on a stranger: nothing
    assert LT.decide_llm(view(), CTX, judged("positive", (HELD,))) == []                                          # good news on a holding: no alert (INFO in the old system)
    assert LT.decide_llm(view(), CTX, judged("neutral")) == []
    assert LT.decide_llm(view(lag=3 * 3600), CTX, judged()) == []
    assert LT.decide_llm(view(ids=(OTHER,)), CTX, judged(ids=(HELD,))) == []                       # codes the claim never named are ignored


def test_llm_evidence_marks_the_alert():
    ev = LT.llm_evidence(judged(), T.TierHit("P0", "r"))
    assert ev["llm_judged"] is True and ev["llm_confidence"] == 0.7 and ev["judged_by"] == "llm" and ev["policy_version"].endswith("+llm")
    plain = T.evidence_body(T.TierHit("P0", "r"), view(), rule_id="x")
    assert "llm_judged" not in plain and T.evidence_body(T.TierHit("P0", "r"), view(), rule_id="x", extra=None) == plain


# ---- config and the client --------------------------------------------------------------------------------------------------------------
def test_cap_and_model_from_the_environment(tmp_path, monkeypatch):
    assert LS.cap_from_env({}) == 300 and LS.cap_from_env({"PULSE_ALERT_LLM_CAP": "7"}) == 7 and LS.cap_from_env({"PULSE_ALERT_LLM_CAP": "x"}) == 300
    assert LS.cap_from_env({"PULSE_ALERT_LLM_CAP": "-5"}) == 0
    key = tmp_path / "k.txt"
    key.write_bytes(f"﻿  {H.SECRET}  \nsecond\n".encode("utf-8"))
    c = AL.AlertLlmClient.from_env({"PULSE_OPENAI_KEY_FILE": str(key)})
    assert c is not None and c.model == "gpt-6-luna" and H.SECRET not in repr(c)               # the old default model; a BOM is tolerated
    assert AL.AlertLlmClient.from_env({"PULSE_OPENAI_KEY_FILE": str(key), "PULSE_ALERT_MODEL": "m2"}).model == "m2"
    assert AL.read_key_file(key) == H.SECRET


def test_off_without_a_readable_key_file(tmp_path):
    empty = tmp_path / "empty.key"
    empty.write_bytes(b"")
    switch = tmp_path / "on"
    switch.write_bytes(b"")
    for env in ({}, {"PULSE_OPENAI_KEY_FILE": ""}, {"PULSE_OPENAI_KEY_FILE": str(tmp_path / "missing.key")}, {"PULSE_OPENAI_KEY_FILE": str(empty)}):
        assert AL.AlertLlmClient.from_env(env) is None and LS.AlertFallback.from_env({**env, "PULSE_ALERT_LLM_SWITCH": str(switch)}) is None


def test_the_switch_file_is_the_second_key(tmp_path):
    key = tmp_path / "openai.key"
    key.write_bytes(H.SECRET.encode())
    lake = tmp_path / "var" / "lake"
    env = {"PULSE_OPENAI_KEY_FILE": str(key)}
    assert LS.switch_path(env, lake) == tmp_path / "var" / "alert_llm.enabled"                # next to the lake dir (C:/pulse3/var/alert_llm.enabled)
    assert LS.AlertFallback.from_env(env, lake_root=lake) is None                              # a key file alone does not switch it on
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "alert_llm.enabled").write_bytes(b"")
    assert isinstance(LS.AlertFallback.from_env(env, lake_root=lake), LS.AlertFallback)
    other = tmp_path / "elsewhere.on"
    env2 = {**env, "PULSE_ALERT_LLM_SWITCH": str(other)}                                       # the env override replaces the default path
    assert LS.switch_path(env2, lake) == other and LS.AlertFallback.from_env(env2, lake_root=lake) is None
    other.write_bytes(b"")
    assert LS.AlertFallback.from_env(env2, lake_root=lake) is not None
    assert LS.AlertFallback.from_env({"PULSE_ALERT_LLM_SWITCH": str(other)}, lake_root=lake) is None     # switch without a key: still off


def test_client_statuses_and_no_error_body(tmp_path):
    key = tmp_path / "k"
    key.write_bytes(H.SECRET.encode())

    def ask(handler):
        return AL.AlertLlmClient(key_file=key, transport=httpx.MockTransport(handler)).ask("sys", "prompt")

    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "hi "}, {"type": "output_text", "text": "there"}]}],
                                         "usage": {"total_tokens": 5}})
    r = ask(ok)
    assert (r.status, r.text, r.usage) == ("ok", "hi there", {"total_tokens": 5})
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/responses" and req.headers["authorization"] == f"Bearer {H.SECRET}"
    assert json.loads(req.content) == {"model": "gpt-6-luna", "instructions": "sys", "input": "prompt"}
    for code in (401, 403, 429, 500):
        r = ask(lambda req, code=code: httpx.Response(code, text=f"echo {H.SECRET}"))
        assert r.status == f"http_{code}" and r.text == "" and H.SECRET not in repr(r)
    def boom(req):
        raise httpx.ConnectTimeout("t", request=req)
    assert ask(boom).status == "error_ConnectTimeout"
    assert ask(lambda req: httpx.Response(200, text="<html>")).status.startswith("error_")
    assert ask(lambda req: httpx.Response(200, json=[1])).status == "error_not_an_object"
    key.unlink()
    assert ask(ok).status == "error_key_file"


def test_a_real_call_is_refused_under_pytest(tmp_path):
    key = tmp_path / "k"
    key.write_bytes(H.SECRET.encode())
    with pytest.raises(AL.LlmDisabled):
        AL.AlertLlmClient(key_file=key).ask("s", "p")


def test_day_bounds_are_the_shanghai_day():
    from datetime import datetime, timezone
    s, e = LS.day_bounds(datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc))          # 01:00 on the 24th in Shanghai
    assert s == datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc) and e == datetime(2026, 11, 24, 16, 0, tzinfo=timezone.utc)


def test_worker_wires_the_fallback_only_when_configured(monkeypatch, tmp_path):
    from pulse.application import alert_service
    from pulse.workers import alerts as W

    seen: list = []

    class FakeConn:
        def close(self):
            pass
    monkeypatch.setattr(W.db, "connect", lambda *a, **k: FakeConn())
    monkeypatch.delenv("PULSE_ALERT_LLM_SWITCH", raising=False)
    lake = tmp_path / "var" / "lake"
    monkeypatch.setattr(alert_service, "scan", lambda conn, **kw: seen.append(kw["llm"]) or {"claims_read": 0})
    monkeypatch.delenv("PULSE_OPENAI_KEY_FILE", raising=False)
    assert W.run(["--lake", str(lake), "scan"]) == 0 and seen == [None]                   # no key file: rules only, as before
    key = tmp_path / "openai.key"
    key.write_bytes(H.SECRET.encode())
    monkeypatch.setenv("PULSE_OPENAI_KEY_FILE", str(key))
    monkeypatch.setenv("PULSE_ALERT_LLM_CAP", "9")
    assert W.run(["--lake", str(lake), "scan"]) == 0 and seen[1] is None                  # key but no switch file: still rules only
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "alert_llm.enabled").write_bytes(b"")
    assert W.run(["--lake", str(lake), "scan"]) == 0 and isinstance(seen[2], LS.AlertFallback) and seen[2].cap == 9
    assert W.run(["--lake", str(lake), "scan", "--no-llm"]) == 0 and seen[3] is None
