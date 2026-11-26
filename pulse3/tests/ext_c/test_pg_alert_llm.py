"""Alert LLM fallback (legacy row #134) on real PostgreSQL with a mocked transport and the old system's news fixtures:
no-key path unchanged, who is asked, once per message, daily cap, stop on 401/403/429, rule precedence, digests, storage."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from pulse.application import alert_llm_service as LS
from pulse.application import alert_service as AS
from pulse.application import portfolio_service as PS
from pulse.domain.alerts import llm_tier as LT

from . import llm_support as H
from .llm_support import allow_test_models  # noqa: F401 - autouse fixture
from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc
UNDECIDED = H.msg("u1", "星河制药召开投资者交流会", ["600513"])                 # a holding, no direction in the words
UNDECIDED2 = H.msg("u2", "云峰地产董事长出席行业论坛", ["600001"])
UNDECIDED3 = H.msg("u3", "北辰养殖举办开放日活动", ["002100"])
UNDECIDED4 = H.msg("u4", "银杏航空参加航空展", ["000501"])
UNDECIDED5 = H.msg("u5", "星河制药发布新品包装", ["600513"])


def day(n: int) -> datetime:
    """A Shanghai day of its own per test: the cap and the stop-for-the-day state are per day and the database is shared."""
    return datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=n)


def world(conn, n: int) -> H.World:
    return H.World(conn, uuid.uuid4(), day(n))


def judgements(conn, w: H.World, phase: str = "result") -> list[dict]:
    s, e = LS.day_bounds(w.now)
    return conn.execute("SELECT * FROM notification.alert_llm_judgement WHERE phase=%s AND asked_at >= %s AND asked_at < %s ORDER BY seq", (phase, s, e)).fetchall()


def test_claims_from_the_old_fixtures_have_the_expected_rule_shape():
    """What the pulse3 claim rules say about the old fixture messages: only these are candidates for the fallback."""
    by_id = {m["item_id"]: H.rule_view(m).polarity_hint for m in H.fixture_messages()}
    assert by_id["107"] == "-" and by_id["103"] == "-" and by_id["102"] == "+" and by_id["106"] == "none"      # decided / nothing to ask about
    assert H.book()["holdings"] == ["600513", "600001", "002100", "000501"] and "600513" in H.securities()


def test_without_a_key_nothing_changes(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("PULSE_OPENAI_KEY_FILE", raising=False)
    assert LS.AlertFallback.from_env() is None
    w = world(conn, 31)
    for m in (*H.fixture_messages(), UNDECIDED):
        w.claim(m)
    s = w.scan(llm=LS.AlertFallback.from_env())                                    # what the worker passes when no key file is set
    assert "llm" not in s and "llm_intents" not in s and s["intents"] >= 2
    tiers = [(i["tier"], i["body"]["headline"]) for i in w.intents()]
    assert ("P0", "某公司减持计划已完成") in tiers and ("P2", "金桥建设中标重大项目") in tiers and ("P2", "海川电子披露业绩预亏公告") in tiers
    assert not any(h == UNDECIDED["title"] for _, h in tiers)                       # an undecided holding message simply has no alert
    assert all("llm_judged" not in i["body"] and i["dedup_key"].startswith("news-tier-v1:") for i in w.intents())      # bodies and keys as they always were
    assert judgements(conn, w) == []


def test_rules_win_and_only_the_undecided_message_of_a_fixture_day_is_asked(conn, tmp_path):
    """The old fixture day plus one undecided holding message: every claim the rules decide (or that touches nothing) keeps exactly its
    rule-only intent, and the model is asked about the one message the rules left open."""
    a, b = world(conn, 32), world(conn, 33)
    for w in (a, b):
        for m in (*H.fixture_messages(), UNDECIDED):
            w.claim(m)
    t = H.Transport(H.ANSWER)
    sa, sb = a.scan(llm=None), b.scan(llm=H.fallback(tmp_path, t))
    assert len(t.calls) == 1 and "星河制药召开投资者交流会" in t.bodies()[0]["input"] and sb["llm"]["calls"] == 1
    assert sb["intents"] == sa["intents"] + 1 and sb["llm_intents"] == 1 and "llm_intents" not in sa and "llm" not in sa
    plain = sorted((i["tier"], i["body"]["headline"]) for i in a.intents())
    from_rules = sorted((i["tier"], i["body"]["headline"]) for i in b.intents() if not i["body"].get("llm_judged"))
    assert from_rules == plain and [i["body"]["headline"] for i in b.intents() if i["body"].get("llm_judged")] == ["星河制药召开投资者交流会"]


def test_an_undecided_holding_claim_is_asked_once_and_the_alert_says_llm(conn, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = world(conn, 34)
    w.claim(UNDECIDED)
    w.claim(H.msg("n1", "今日天气晴朗", [], "市场整体平稳运行"))                    # touches nothing
    t = H.Transport(H.ANSWER)
    fb = H.fallback(tmp_path, t)
    s = w.scan(llm=fb)
    assert len(t.calls) == 1 and s["intents"] == 1 and s["llm_intents"] == 1
    req = t.calls[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.openai.com/v1/responses" and req.headers["authorization"] == f"Bearer {H.SECRET}"
    assert body["model"] == "gpt-test-model" and "JSON" in body["instructions"] and "星河制药召开投资者交流会" in body["input"]
    assert "600513 星河制药" in body["input"] and "600001" not in body["input"] and "002451" not in body["input"]      # the claim's own names only, not the book
    [(row)] = judgements(conn, w)
    assert row["status"] == "ok" and row["model"] == "gpt-test-model" and row["raw_text"] == json.dumps(H.ANSWER, ensure_ascii=False)
    assert row["usage"]["total_tokens"] == 120 and len(row["prompt_hash"]) == 64 and row["prompt_version"] == LT.PROMPT_VERSION
    assert row["judgement"]["direction"] == "negative" and row["judgement"]["confidence"] == 0.8 and row["judgement"]["instrument_ids"] == [str(w.ids["600513"])]
    assert H.SECRET not in json.dumps(row, default=str) and H.SECRET not in caplog.text
    [i] = w.intents()                                                                # old behaviour kept: a negative answer on a held code is P0
    assert i["tier"] == "P0" and i["dedup_key"].startswith("news-tier-v1+llm:P0:")
    ev = i["body"]
    assert ev["llm_judged"] is True and ev["judged_by"] == "llm" and ev["llm_confidence"] == 0.8 and ev["llm_model"] == "gpt-test-model"
    assert ev["llm_judgement_id"] == str(row["judgement_id"]) and ev["direction"] == "negative" and ev["instruments"][0]["name"] == "星河制药"
    assert H.SECRET not in json.dumps(ev, default=str)
    # the same message is never asked twice: a re-scan, a second scan object, another day's cap state - none of it calls again
    assert w.scan(llm=fb)["intents"] == 0 and w.scan(llm=H.fallback(tmp_path, t))["intents"] == 0 and len(t.calls) == 1
    assert fb.stats["reused"] >= 1 and fb.summary(conn, w.now)["calls"] == 1


def test_one_answer_serves_every_user(conn, tmp_path):
    w1 = world(conn, 35)
    w1.claim(UNDECIDED)
    t = H.Transport(H.ANSWER)
    fb = H.fallback(tmp_path, t)
    w1.scan(llm=fb)
    assert len(t.calls) == 1
    # a second user who holds the SAME instrument gets the stored answer (and the alert) without a call
    p2 = uuid.uuid4()
    acct = S.open_account(conn, positions={str(w1.ids["600513"]): {"quantity": 500, "cost": "5000"}})
    PS.grant(conn, p2, acct, "read", w1.now - timedelta(days=2))
    AS.init_default_rules(conn, p2, w1.now - timedelta(days=1))
    s = AS.scan(conn, w1.now, principals=[p2], since=w1.now - timedelta(hours=1), until=w1.now, llm=fb)
    assert len(t.calls) == 1 and fb.stats["reused"] >= 1 and s["llm"]["calls"] == 1 and s["llm_intents"] == 1
    assert [r["tier"] for r in conn.execute("SELECT tier FROM notification.alert_intent WHERE principal_id=%s", (p2,)).fetchall()] == ["P0"]


def test_mixed_polarity_is_undecided_too_and_neutral_means_no_alert(conn, tmp_path):
    w = world(conn, 36)
    w.claim(H.msg("m1", "星河制药高管增持与另一股东减持同日披露", ["600513"]))
    assert H.rule_view(H.msg("m1", "星河制药高管增持与另一股东减持同日披露", ["600513"])).polarity_hint == "mixed"
    t = H.Transport({"direction": "neutral", "magnitude": 1, "codes": [], "industries": []})
    fb = H.fallback(tmp_path, t)
    s = w.scan(llm=fb)
    assert len(t.calls) == 1 and s["intents"] == 0 and w.intents() == []
    [row] = judgements(conn, w)
    assert row["status"] == "ok" and row["judgement"]["direction"] == "neutral" and row["judgement"]["confidence"] is None       # no confidence given: stays NULL
    assert w.scan(llm=fb)["intents"] == 0 and len(t.calls) == 1                         # a neutral answer is an answer: not asked again


def test_digest_is_never_sent(conn, tmp_path):
    w = world(conn, 37)
    for n, title in enumerate(["A股早报：星河制药等精选个股一览", "每日盘点 星河制药 云峰地产 汇总", "收评：北辰养殖等个股复盘"], 1):
        w.claim(H.msg(f"d{n}", title, ["600513", "600001", "002100"]))
    t = H.Transport()
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert t.calls == [] and s["llm"]["calls"] == 0 and s["intents"] == 0 and judgements(conn, w) == []


def test_rule_decided_claims_never_reach_the_model_and_keep_their_tier(conn, tmp_path):
    w = world(conn, 38)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))                    # negative words: the rules give P0
    w.claim(H.msg("r2", "海川电子披露业绩预亏公告", ["002451"], "预计全年亏损"))          # watch list: P2 by the rules
    w.claim(H.msg("r3", "云峰地产中标大额订单", ["600001"]))                           # positive on a holding: decided, no tier
    t = H.Transport({"direction": "positive", "magnitude": 3, "codes": ["600513"], "industries": [], "confidence": 0.99})   # would flip r1 if the model were asked
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert t.calls == [] and s["intents"] == 2
    assert [(i["tier"], i["body"]["headline"]) for i in w.intents()] == [("P0", "星河制药收到证监会立案告知书"), ("P2", "海川电子披露业绩预亏公告")]
    assert all("llm_judged" not in i["body"] for i in w.intents())


def test_cap_pace_and_the_state_survives_a_new_process(conn, tmp_path):
    w = world(conn, 39)
    for m in (UNDECIDED, UNDECIDED2, UNDECIDED3, UNDECIDED4):
        w.claim(m)
    t = H.Transport({"direction": "positive", "magnitude": 2, "codes": ["600513"], "confidence": 0.5})
    sleeps: list[float] = []
    fb = H.fallback(tmp_path, t, cap=2, pace=LS.PACE_SECONDS, sleeps=sleeps)
    s = w.scan(llm=fb)
    assert len(t.calls) == 2 and s["llm"]["calls"] == 2 and s["llm"]["skipped"] == {"cap": 2} and s["llm"]["stopped"] == "cap"
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= LS.PACE_SECONDS                      # one call per 2 s
    assert len(judgements(conn, w)) == 2
    fresh = H.fallback(tmp_path, t, cap=2)                                              # a restart: the day's count comes from the table
    w.claim(UNDECIDED5)
    assert w.scan(llm=fresh)["llm"]["skipped"] == {"cap": 3} and len(t.calls) == 2      # u3, u4, u5 (u1, u2 have answers)
    nxt = w.now + timedelta(days=1)                                                     # the next Shanghai day starts a new count
    w.claim(H.msg("u6", "北辰养殖召开股东座谈会", ["002100"]), minutes_ago=-1430)
    s = AS.scan(conn, nxt, principals=[w.principal], since=nxt - timedelta(hours=1), until=nxt, llm=fresh)
    assert len(t.calls) == 3 and s["llm"]["calls_today"] == 1


@pytest.mark.parametrize("status, n", [(401, 41), (403, 43), (429, 45)])
def test_a_refusal_stops_the_fallback_for_the_day(conn, tmp_path, status, n, caplog):
    caplog.set_level(logging.DEBUG)
    w = world(conn, n)
    for m in (UNDECIDED, UNDECIDED2, UNDECIDED3):
        w.claim(m)
    t = H.Transport(status=status)                                                      # the error body echoes the key
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert len(t.calls) == 1 and s["intents"] == 0 and s["llm"]["skipped"] == {"stopped_http": 2} and s["llm"]["stopped"] == "stopped_http"
    [row] = judgements(conn, w)
    assert row["status"] == f"http_{status}" and row["raw_text"] is None and row["judgement"] is None
    assert H.SECRET not in json.dumps(row, default=str) and H.SECRET not in caplog.text
    again = H.fallback(tmp_path, H.Transport())                                         # restart, same day: still stopped (state is in the table)
    w.claim(UNDECIDED4)
    assert w.scan(llm=again)["llm"]["skipped"] == {"stopped_http": 3}        # u2, u3, u4; u1 has its (failed) row
    # the refused message is not asked again, but a new day is a new chance for new messages
    nxt = w.now + timedelta(days=1)
    t2 = H.Transport(H.ANSWER)
    w.claim(UNDECIDED5, minutes_ago=-1430)
    s = AS.scan(conn, nxt, principals=[w.principal], since=nxt - timedelta(hours=1), until=nxt, llm=H.fallback(tmp_path, t2))
    assert len(t2.calls) == 1 and s["llm"]["stopped"] is None


def test_five_failures_in_a_row_open_the_breaker(conn, tmp_path):
    w = world(conn, 47)
    msgs = [H.msg(f"b{n}", f"星河制药第{n}次开放日", ["600513"]) for n in range(7)]
    for m in msgs:
        w.claim(m)
    t = H.Transport(status=500)
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert len(t.calls) == 5 and s["llm"]["skipped"] == {"breaker": 2} and s["llm"]["failed"] == 5
    assert [r["status"] for r in judgements(conn, w)] == ["http_500"] * 5


def test_an_unparsable_answer_is_a_stored_failure_and_never_retried(conn, tmp_path):
    w = world(conn, 48)
    w.claim(UNDECIDED)
    t = H.Transport("sorry, I cannot answer that")
    fb = H.fallback(tmp_path, t)
    s = w.scan(llm=fb)
    assert s["intents"] == 0 and s["llm"]["failed"] == 1
    [row] = judgements(conn, w)
    assert row["status"] == "unparsed" and row["raw_text"] == "sorry, I cannot answer that" and row["judgement"] is None
    assert w.scan(llm=fb)["intents"] == 0 and len(t.calls) == 1


def test_p1_from_an_industry_and_the_p0_switch(conn, tmp_path, monkeypatch):
    w = world(conn, 49)
    node = conn.execute("SELECT taxonomy_version, node_id FROM core.taxonomy_node WHERE taxonomy_id='legacy_boards_industry' AND display_name='房地产'").fetchone()
    if node is None:
        conn.execute("INSERT INTO core.taxonomy(taxonomy_id,taxonomy_version,taxonomy_kind,definition,available_at) VALUES ('legacy_boards_industry',1,'sector','{}',now()) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO core.taxonomy_node(taxonomy_id,taxonomy_version,node_id,parent_node_id,display_name,definition) VALUES ('legacy_boards_industry',1,'new_fdc',NULL,'房地产','{}') ON CONFLICT DO NOTHING")
        node = {"taxonomy_version": 1, "node_id": "new_fdc"}
    conn.execute("INSERT INTO core.instrument_tag(assignment_id,instrument_id,taxonomy_id,taxonomy_version,node_id,effective_from,available_at,assertion_kind,evidence_ref)"
                 " VALUES (%s,%s,'legacy_boards_industry',%s,%s,'2020-01-01Z','2020-01-01Z','observed','t')", (uuid.uuid4(), w.ids["600001"], node["taxonomy_version"], node["node_id"]))
    answer = {"direction": "negative", "magnitude": 2, "confidence": 0.6, "codes": ["600001"], "industries": ["房地产", "没有这个行业"], "reason": "policy"}
    w.claim(UNDECIDED2)
    t = H.Transport(answer)
    w.scan(llm=H.fallback(tmp_path, t))
    assert "房地产" in t.bodies()[0]["input"]                                          # the industry list is the taxonomy's, not the user's holdings
    by_tier = {i["tier"]: i["body"] for i in w.intents()}
    assert set(by_tier) == {"P0", "P1"} and by_tier["P1"]["industries"] == ["房地产"] and by_tier["P1"]["llm_judged"] is True and by_tier["P1"]["llm_confidence"] == 0.6
    # the same answer with P0 switched off is LOWERED to P1 (never raised)
    w2 = H.World(conn, uuid.uuid4(), w.now)
    w2.claim(H.msg("u7", "云峰地产召开业绩说明会前沟通", ["600001"]))
    monkeypatch.setattr(LT, "LLM_MAY_RAISE_P0", False)
    w2.scan(llm=H.fallback(tmp_path, H.Transport(answer)))
    assert {i["tier"] for i in w2.intents()} <= {"P1", "P2"} and "P0" not in {i["tier"] for i in w2.intents()}


def test_answers_are_append_only(conn, tmp_path):
    w = world(conn, 50)
    w.claim(UNDECIDED)
    w.scan(llm=H.fallback(tmp_path, H.Transport(H.ANSWER)))
    assert len(judgements(conn, w, "pending")) == 1 and len(judgements(conn, w)) == 1          # ledger: a pending row and its result row
    for sql in ("UPDATE notification.alert_llm_judgement SET model='x'", "DELETE FROM notification.alert_llm_judgement"):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute(sql)
    for phase in ("pending", "result"):                                                          # one row per message and phase, whatever the process
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO notification.alert_llm_judgement(judgement_id,call_id,phase,message_key,claim_id,claim_revision_id,asked_at,model,prompt_version,prompt_hash,status,usage)"
                         " SELECT gen_random_uuid(),call_id,phase,message_key,claim_id,claim_revision_id,asked_at,model,prompt_version,prompt_hash,"
                         " CASE phase WHEN 'pending' THEN 'pending' ELSE 'http_500' END,'{}' FROM notification.alert_llm_judgement WHERE phase=%s LIMIT 1", (phase,))


def test_the_key_never_reaches_the_database(conn, tmp_path):
    w = world(conn, 51)
    for m in (UNDECIDED, UNDECIDED2):
        w.claim(m)
    t = H.Transport(status=500, body_text=f"upstream failure for key {H.SECRET}")
    w.scan(llm=H.fallback(tmp_path, t))
    for table, col in (("notification.alert_llm_judgement", "t::text"), ("ops.ext_artifact", "t::text"), ("notification.alert_intent", "t::text")):
        assert conn.execute(f"SELECT count(*) n FROM {table} t WHERE {col} LIKE %s", (f"%{H.SECRET}%",)).fetchone()["n"] == 0


# ---- review fixes ---------------------------------------------------------------------------------------------------------------------
def rule_alerts_exist(w: H.World) -> list[tuple[str, str]]:
    return sorted((i["tier"], i["body"]["headline"]) for i in w.intents() if not i["body"].get("llm_judged"))


@pytest.mark.parametrize("n, answer", [
    (60, '{"direction":"negative","magnitude":1e999,"codes":["600513"],"industries":[]}'),                  # inf: json.loads accepts it, int(inf) is an OverflowError
    (61, '{"direction":"negative","magnitude":2,"confidence":1' + "0" * 400 + ',"codes":["600513"]}'),     # an int no float can hold
], ids=["magnitude-1e999", "confidence-10e400"])
def test_an_odd_number_in_the_answer_never_stalls_the_scan(conn, tmp_path, n, answer):
    w = world(conn, n)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))                                      # a rule alert must survive whatever the model says
    w.claim(UNDECIDED)
    t = H.Transport(answer)
    fb = H.fallback(tmp_path, t)
    s = w.scan(llm=fb)                                                                           # no exception
    assert rule_alerts_exist(w) == [("P0", "星河制药收到证监会立案告知书")] and s["intents"] == 1 and len(t.calls) == 1
    [row] = judgements(conn, w)
    assert row["status"] == "unparsed" and row["judgement"] is None and row["raw_text"] == answer
    assert w.scan(llm=fb)["intents"] == 0 and w.scan(llm=H.fallback(tmp_path, t))["intents"] == 0 and len(t.calls) == 1    # never asked again, not at the next scan


def test_an_exception_after_the_call_is_stored_as_error_class(conn, tmp_path, monkeypatch):
    w = world(conn, 62)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))
    w.claim(UNDECIDED)

    def boom(*a, **k):
        raise OverflowError("int too large to convert to float")
    monkeypatch.setattr(LT, "parse_answer", boom)
    t = H.Transport(H.ANSWER)
    fb = H.fallback(tmp_path, t)
    s = w.scan(llm=fb)
    assert s["intents"] == 1 and rule_alerts_exist(w) == [("P0", "星河制药收到证监会立案告知书")]
    [row] = judgements(conn, w)
    assert row["status"] == "error_OverflowError" and row["judgement"] is None and row["raw_text"] is None
    assert len(judgements(conn, w, "pending")) == 1 and s["llm"]["failed"] == 1
    monkeypatch.undo()
    assert w.scan(llm=fb)["intents"] == 0 and len(t.calls) == 1                                  # the paid call is not repeated


def test_nul_characters_in_the_answer_are_stripped_not_fatal(conn, tmp_path):
    w = world(conn, 63)
    w.claim(UNDECIDED)
    text = '{"direction":"negative","magnitude":2,"confidence":0.5,"codes":["600513"],"industries":[],"reason":"a\\u0000b"}' + "\u0000 trailing"
    t = H.Transport(text)
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert s["intents"] == 1
    [row] = judgements(conn, w)
    assert row["status"] == "ok" and "\u0000" not in row["raw_text"] and row["raw_text"].endswith(" trailing") and row["judgement"]["reason"] == "ab"
    [i] = w.intents()
    assert i["body"]["llm_reason"] == "ab"


def test_a_storage_error_after_the_call_leaves_an_error_row(conn, tmp_path, monkeypatch):
    w = world(conn, 64)
    w.claim(UNDECIDED)
    orig = LS.AlertFallback._append

    def flaky(self, conn, **kw):
        if kw["phase"] == "result" and kw["status"] == "ok":
            raise psycopg.errors.DataError("unsupported Unicode escape sequence")
        return orig(self, conn, **kw)
    monkeypatch.setattr(LS.AlertFallback, "_append", flaky)
    t = H.Transport(H.ANSWER)
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert s["intents"] == 0 and s["llm"]["calls"] == 1 and s["llm"]["lost"] == 0
    [row] = judgements(conn, w)
    assert row["status"] == "error_DataError" and row["judgement"] is None
    # and when even the error row cannot be written, the pending row still stops a second paid call for the message
    w2 = world(conn, 65)
    w2.claim(UNDECIDED)

    def no_results(self, conn, **kw):
        if kw["phase"] == "result":
            raise RuntimeError("db gone")
        return orig(self, conn, **kw)
    monkeypatch.setattr(LS.AlertFallback, "_append", no_results)
    s = w2.scan(llm=H.fallback(tmp_path, t))
    assert s["llm"]["lost"] == 1 and len(judgements(conn, w2, "pending")) == 1 and judgements(conn, w2) == []
    monkeypatch.undo()
    monkeypatch.setenv("PULSE_LLM_ALLOWED_MODELS", "gpt-6-luna,gpt-test-model,m2")        # undo() also removed the autouse allow-list of the made-up test model
    assert w2.scan(llm=H.fallback(tmp_path, t))["intents"] == 0 and len(t.calls) == 2


class Crash(BaseException):
    """A kill between the paid call and the result row (not an Exception: nothing may swallow it)."""


def with_handler(fb: LS.AlertFallback, handler) -> None:
    import httpx
    fb.client._transport = httpx.MockTransport(handler)


def test_the_pending_row_is_written_before_the_call_and_a_crash_never_repeats_it(conn, tmp_path):
    w = world(conn, 66)
    w.claim(UNDECIDED)
    seen: list[int] = []

    def handler(req):
        seen.append(len(judgements(conn, w, "pending")))                                         # the ledger row is already committed when the call goes out
        raise Crash()
    fb = H.fallback(tmp_path, H.Transport())
    with_handler(fb, handler)
    with pytest.raises(Crash):
        w.scan(llm=fb)
    assert seen == [1] and len(judgements(conn, w, "pending")) == 1 and judgements(conn, w) == []
    t2 = H.Transport(H.ANSWER)
    again = H.fallback(tmp_path, t2)
    s = w.scan(llm=again)                                                                        # the next scan: not asked again, the call still counts
    assert t2.calls == [] and s["llm"]["calls_today"] == 1 and s["intents"] == 0 and again.stats["reused"] == 1


def test_a_failing_fallback_never_breaks_the_rule_path(conn, tmp_path, monkeypatch):
    w = world(conn, 67)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))
    w.claim(UNDECIDED)
    w.claim(UNDECIDED2)
    fb = H.fallback(tmp_path, H.Transport())

    def broken(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(fb, "judge", broken)
    s = w.scan(llm=fb)
    assert s["intents"] == 1 and s["llm_errors"] == 2 and rule_alerts_exist(w) == [("P0", "星河制药收到证监会立案告知书")]


def test_rules_are_committed_before_the_first_paid_call_and_no_lock_is_held_during_it(conn, pg_dsn, tmp_path):
    import httpx
    w = world(conn, 68)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))
    w.claim(H.msg("r2", "海川电子披露业绩预亏公告", ["002451"], "预计全年亏损"))
    for m in (UNDECIDED, UNDECIDED2, UNDECIDED3):
        w.claim(m)
    seen: list[dict] = []
    other = psycopg.connect(pg_dsn, autocommit=True)

    def handler(req):
        locked = other.execute("SELECT pg_try_advisory_lock(%s)", (LS.LOCK_KEY,)).fetchone()[0]
        if locked:
            other.execute("SELECT pg_advisory_unlock(%s)", (LS.LOCK_KEY,))
        visible = other.execute("SELECT count(*) FROM notification.alert_intent WHERE principal_id=%s", (w.principal,)).fetchone()[0]
        seen.append({"lock_free": locked, "rule_intents_visible": visible})
        return httpx.Response(200, json={"output_text": json.dumps({"direction": "neutral", "magnitude": 1})})
    fb = H.fallback(tmp_path, H.Transport())
    with_handler(fb, handler)
    try:
        s = w.scan(llm=fb)
    finally:
        other.close()
    assert len(seen) == 3 and all(x["lock_free"] and x["rule_intents_visible"] == 2 for x in seen)      # both rule alerts were already visible to everyone
    assert s["intents"] == 2


def test_a_pass_that_runs_out_of_time_leaves_the_rest_for_the_next_scan(conn, tmp_path):
    w = world(conn, 69)
    w.claim(H.msg("r1", "星河制药收到证监会立案告知书", ["600513"]))
    w.claim(UNDECIDED)
    t = H.Transport(H.ANSWER)
    s = w.scan(llm=H.fallback(tmp_path, t, budget=0.0))
    assert t.calls == [] and s["intents"] == 1 and rule_alerts_exist(w) == [("P0", "星河制药收到证监会立案告知书")]     # rules first, always
    assert datetime.fromisoformat(s["llm_resume_at"]) == w.now - timedelta(minutes=3)                                                # the cursor stops before the unasked claim
    s2 = w.scan(llm=H.fallback(tmp_path, t))
    assert len(t.calls) == 1 and s2["llm_intents"] == 1 and "llm_resume_at" not in s2


def instrument_rule(conn, w: H.World, iid):
    inbox = AS.destination_id(w.principal, "in_app")
    body = {"scope": {"scope_kind": "instrument", "scope_key": str(iid), "membership_snapshot_id": None},
            "condition": {"node": "compare", "clock": {"kind": "event_available"}, "operator": "eq", "left": {"kind": "constant", "value": "1", "unit": "count"},
                          "right": {"kind": "constant", "value": "1", "unit": "count"}},
            "policy_version": "alert-rule-v1", "annotation_id": None, "annotation_version": None, "destination_ids": [str(inbox)], "enabled": True}
    AS.save_rule(conn, w.principal, {"rule_id": uuid.uuid4(), "expected_rule_version": 0, "body": body}, w.now - timedelta(days=1))


def test_a_message_that_already_has_a_rule_alert_gets_no_llm_alert_and_no_paid_call(conn, tmp_path):
    w = world(conn, 70)
    instrument_rule(conn, w, w.ids["600513"])                                                    # the user's own instrument rule on a holding
    w.claim(UNDECIDED)                                                                           # undecided for the tier rules, but the user rule fires
    t = H.Transport(H.ANSWER)
    s = w.scan(llm=H.fallback(tmp_path, t))
    assert [i["tier"] for i in w.intents()] == ["RULE"] and t.calls == [] and s["llm"]["calls"] == 0 and "llm_intents" not in s
    # a user without that rule still gets the model's view of the same message (it is asked, once)
    p2 = uuid.uuid4()
    acct = S.open_account(conn, positions={str(w.ids["600513"]): {"quantity": 500, "cost": "5000"}})
    PS.grant(conn, p2, acct, "read", w.now - timedelta(days=2))
    AS.init_default_rules(conn, p2, w.now - timedelta(days=1))
    fb = H.fallback(tmp_path, t)
    s2 = AS.scan(conn, w.now, principals=[w.principal, p2], since=w.now - timedelta(hours=1), until=w.now, llm=fb)
    assert len(t.calls) == 1 and s2["llm_intents"] == 1
    assert [r["tier"] for r in conn.execute("SELECT tier FROM notification.alert_intent WHERE principal_id=%s", (p2,)).fetchall()] == ["P0"]
    assert [i["tier"] for i in w.intents()] == ["RULE"]                                          # the first user still has only the rule alert


def test_the_api_role_cannot_read_the_ledger(conn):
    def priv(role, kind="SELECT"):
        return conn.execute("SELECT has_table_privilege(%s, 'notification.alert_llm_judgement', %s) AS ok", (role, kind)).fetchone()["ok"]
    assert priv("pulse_api") is False and priv("pulse_worker") is True and priv("pulse_worker", "INSERT") is True and priv("pulse_worker", "UPDATE") is False
    assert conn.execute("SELECT has_column_privilege('pulse_api', 'notification.alert_llm_judgement', 'raw_text', 'SELECT') AS ok").fetchone()["ok"] is False
