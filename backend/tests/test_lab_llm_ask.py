"""E092: guards of the paid web-search LLM calls and the dry-run switch in the daily pipeline."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import inspect
import json
import os
from pathlib import Path
import sys

import httpx
import pytest

LAB = Path(__file__).resolve().parents[2] / "scripts" / "lab"
sys.path.insert(0, str(LAB))

import daily_forward as D  # noqa: E402
import llm_ask as A  # noqa: E402
import llm_eval as V  # noqa: E402

KEY = "test-key-not-real"
GOOD = '{"action": "avoid", "p_outperform": 40, "expected_excess_pct": -1.5, "reason": "财报不及预期", "news": []}'
BEFORE = datetime(2026, 11, 17, 20, 40, tzinfo=A.CST)
NOT_AFTER = datetime(2026, 11, 18, 9, 25, tzinfo=A.CST)


def write_jobs(path: Path, n: int = 3, day: str = "2026-11-17") -> Path:
    jobs = [{"id": f"{day}_60000{i}", "code": f"60000{i}", "group": "top", "score": 1.0, "prompt": f"股票 {i}"} for i in range(n)]
    path.write_text(json.dumps({"decision_date": day, "not_after": NOT_AFTER.isoformat(), "model": A.LIVE_MODEL, "effort": A.LIVE_EFFORT,
                                "system": "s", "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
    return path


class Fake:
    """Stands in for `llm.ask`: records the calls and answers from a script of replies (the last one repeats)."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies) or [GOOD]
        self.calls: list[dict] = []

    def __call__(self, prompt, **kw) -> str:
        self.calls.append({"prompt": prompt, **kw})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def live(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PULSE_LLM_DIR", str(tmp_path / "live" / "llm"))
    return tmp_path / "live" / "llm" / "decisions.jsonl"


def go(path: Path, ask: Fake, key: str | None = KEY, now: datetime = BEFORE, **kw) -> dict:
    return A.run(path, ask=ask, key_loader=lambda: key, now=lambda: now, sleep=lambda _: None, **kw)


@pytest.mark.parametrize(("day", "expected"), [
    (date(2026, 11, 16), datetime(2026, 11, 17, 9, 25, tzinfo=A.CST)),  # Monday to Tuesday
    (date(2026, 11, 13), datetime(2026, 11, 16, 9, 25, tzinfo=A.CST)),  # Friday to Monday
])
def test_the_deadline_is_the_next_weekday_before_the_open(day, expected):
    assert A.deadline_after(day) == expected


def test_a_run_writes_one_row_per_job_with_our_own_fields(tmp_path, live):
    ask = Fake('前言 {"action": "buy", "p_outperform": 70, "code": "999999", "group": "x", "id": "fake"} 后记')
    result = go(write_jobs(tmp_path / "j.json"), ask)
    rows = [json.loads(x) for x in live.read_text(encoding="utf-8").splitlines()]
    assert result["status"] == "done" and result["asked"] == 3 and result["calls"] == 3 and result["failed"] == 0
    assert sorted(r["code"] for r in rows) == ["600000", "600001", "600002"]  # the model cannot overwrite the record's identity
    assert {r["group"] for r in rows} == {"top"} and rows[0]["not_after"] == NOT_AFTER.isoformat() and rows[0]["model"] == A.LIVE_MODEL
    assert rows[0]["asked_at"] == BEFORE.isoformat(timespec="seconds")
    assert all(c["key"] == KEY and c["extra"] == A.TOOLS and c["system"] == "s" and c["timeout"] == A.CALL_TIMEOUT for c in ask.calls)
    assert result["logged"] == 3


@pytest.mark.parametrize(("now", "key", "status"), [
    (NOT_AFTER, KEY, "late"),
    (NOT_AFTER + timedelta(hours=3), KEY, "late"),
    (BEFORE, None, "no_key"),
    (BEFORE, "", "no_key"),
    (BEFORE, "﻿" + KEY, "bad_key"),  # what a BOM from PowerShell's Set-Content leaves in the key
])
def test_a_refused_run_makes_no_call(tmp_path, live, now, key, status):
    ask = Fake()
    result = go(write_jobs(tmp_path / "j.json"), ask, key=key, now=now)
    assert result["status"] == status and result["asked"] == 0 and not ask.calls and not live.exists()


def test_the_twenty_day_limit_stops_a_new_day_but_finishes_a_started_one(tmp_path, live):
    live.parent.mkdir(parents=True)
    old = [{"id": f"d{i}", "decision_date": f"2026-10-{i + 1:02d}"} for i in range(A.MAX_DECISION_DAYS)]
    live.write_text("".join(json.dumps(r) + "\n" for r in old), encoding="utf-8")
    A.calls_path().write_text("".join(json.dumps(r) + "\n" for r in old), encoding="utf-8")
    ask = Fake()
    assert go(write_jobs(tmp_path / "j.json"), ask)["status"] == "limit" and not ask.calls
    result = go(write_jobs(tmp_path / "k.json", day="2026-10-01"), ask)
    assert result["status"] == "done" and result["asked"] == 3


def test_a_day_of_only_failures_still_counts_toward_the_limit(tmp_path, live):
    ask = Fake("no json")
    for i in range(A.MAX_DECISION_DAYS):
        assert go(write_jobs(tmp_path / "j.json", n=1, day=f"2026-10-{i + 1:02d}"), ask)["status"] == "failed"
    spent = len(ask.calls)
    result = go(write_jobs(tmp_path / "j.json", n=1, day="2026-11-17"), ask)
    assert result["status"] == "limit" and len(ask.calls) == spent == A.MAX_DECISION_DAYS * A.ATTEMPTS
    assert not live.exists()  # no verdict at all: the limit counts requests, not rows


def test_the_day_budget_holds_across_reruns(tmp_path, live):
    ask = Fake("no json")
    path = write_jobs(tmp_path / "j.json", n=A.MAX_JOBS)
    assert go(path, ask)["stop"] == "budget"
    again = go(path, ask)
    assert again["status"] == "budget" and again["calls"] == 0
    assert len(ask.calls) == A.MAX_CALLS == A.spent_calls(A.calls_path())["2026-11-17"]


def test_a_request_is_on_the_ledger_before_it_is_sent(tmp_path, live):
    seen = []

    class Spy(Fake):
        def __call__(self, prompt, **kw):
            seen.append(sum(A.spent_calls(A.calls_path()).values()))
            return super().__call__(prompt, **kw)

    go(write_jobs(tmp_path / "j.json", n=1), Spy())
    assert seen == [1]


def test_rows_without_the_ledger_are_refused(tmp_path, live):
    live.parent.mkdir(parents=True)
    live.write_text('{"id": "a", "decision_date": "2026-11-16"}\n', encoding="utf-8")
    ask = Fake()
    assert go(write_jobs(tmp_path / "j.json"), ask)["status"] == "ledger_lost" and not ask.calls


def test_each_request_counts_what_is_on_the_ledger_and_keeps_the_lock_alive(tmp_path, live):
    """A second process that took over a stale lock adds to the same ledger; the budget still holds."""
    stamps = []

    class Other(Fake):
        def __call__(self, prompt, **kw):
            stamps.append(A.lock_path().stat().st_mtime)
            if len(self.calls) == 0:  # another process spends almost the whole day while this run is going
                entry = json.dumps({"decision_date": "2026-11-17", "id": "other"}) + "\n"
                with A.calls_path().open("a", encoding="utf-8") as fh:
                    fh.write(entry * (A.MAX_CALLS - 2))
            return super().__call__(prompt, **kw)

    old = A.clock.time() - 3600
    real_single_run = A.single_run

    @A.contextmanager
    def aged(path):
        with real_single_run(path) as held:
            os.utime(path, (old, old))
            yield held

    import unittest.mock as mock
    with mock.patch.object(A, "single_run", aged), mock.patch.object(A, "WORKERS", 1):
        result = go(write_jobs(tmp_path / "j.json", n=10), Other())
    assert result["status"] == "stopped" and result["stop"] == "budget" and result["calls"] == 2
    assert A.spent_calls(A.calls_path())["2026-11-17"] == A.MAX_CALLS
    assert all(s > old + 60 for s in stamps)


def test_only_one_run_at_a_time_and_a_dead_lock_expires(tmp_path, live):
    path = write_jobs(tmp_path / "j.json", n=1)
    ask = Fake()
    A.lock_path().parent.mkdir(parents=True)
    A.lock_path().touch()
    result = go(path, ask)
    assert result["status"] == "busy" and not ask.calls and A.lock_path().exists()
    old = A.clock.time() - A.LOCK_STALE_S - 60
    os.utime(A.lock_path(), (old, old))
    assert go(path, ask)["status"] == "done" and len(ask.calls) == 1
    assert not A.lock_path().exists()


def test_a_line_cut_inside_a_character_does_not_block_the_log(tmp_path, live):
    live.parent.mkdir(parents=True)
    whole = json.dumps({"id": "a", "reason": "财报"}, ensure_ascii=False).encode("utf-8")
    live.write_bytes(whole + b"\n" + whole[:-5])
    A.calls_path().write_text('{"decision_date": "2026-11-16", "id": "a"}\n', encoding="utf-8")
    assert [r["id"] for r in A.read_rows(live)] == ["a"]
    assert go(write_jobs(tmp_path / "j.json", n=1), Fake())["status"] == "done"
    assert sorted(r["id"] for r in A.read_rows(live)) == ["2026-11-17_600000", "a"]


def test_a_rerun_only_asks_what_is_missing_and_survives_a_half_line(tmp_path, live):
    live.parent.mkdir(parents=True)
    done = {"id": "2026-11-17_600000", "decision_date": "2026-11-17"}
    live.write_text(json.dumps(done) + '\n{"id": "2026-11-17_600001", "deci', encoding="utf-8")
    A.calls_path().write_text(json.dumps(done) + "\n", encoding="utf-8")
    ask = Fake()
    result = go(write_jobs(tmp_path / "j.json"), ask)
    rows = A.read_rows(live)
    assert result["asked"] == 2 and len(ask.calls) == 2
    assert sorted(r["id"] for r in rows) == ["2026-11-17_600000", "2026-11-17_600001", "2026-11-17_600002"]


@pytest.mark.parametrize("status", ["401", "403", "429"])
def test_a_hard_refusal_stops_at_once_and_logs_only_the_status(tmp_path, live, status, capsys):
    secret_body = "Incorrect API key provided: sk-abc***xyz"
    ask = Fake(RuntimeError(f"{status}: {secret_body}"))
    result = A.run(write_jobs(tmp_path / "j.json", n=20), ask=ask, key_loader=lambda: KEY, now=lambda: BEFORE, sleep=lambda _: None)
    assert result["status"] == "stopped" and result["stop"] == status and result["asked"] == 0
    assert len(ask.calls) <= A.WORKERS  # only calls already in flight
    assert secret_body not in json.dumps(result) and "sk-abc" not in capsys.readouterr().out
    assert set(result["errors"]) == {status}


def test_a_bad_answer_is_retried_then_counted_as_failed(tmp_path, live):
    ask = Fake('{"action": "maybe", "p_outperform": 40}', '{"action": "buy", "p_outperform": 140}', "no json")
    result = go(write_jobs(tmp_path / "j.json", n=1), ask)
    assert result["status"] == "failed" and result["asked"] == 0 and result["failed"] == 1 and result["calls"] == A.ATTEMPTS
    assert not A.read_rows(live)


def test_some_answers_failing_is_reported_as_partial(tmp_path, live):
    ask = Fake(GOOD, "no json")
    with_one_worker = A.WORKERS
    A.WORKERS = 1
    try:
        result = go(write_jobs(tmp_path / "j.json", n=2), ask)
    finally:
        A.WORKERS = with_one_worker
    assert result["status"] == "partial" and result["asked"] == 1 and result["failed"] == 1


def test_a_server_error_is_retried_and_can_recover(tmp_path, live):
    ask = Fake(RuntimeError("500: oops"), GOOD)
    result = go(write_jobs(tmp_path / "j.json", n=1), ask)
    assert result["asked"] == 1 and result["calls"] == 2 and result["failed"] == 0


def test_the_budget_caps_paid_calls_even_when_every_answer_is_bad(tmp_path, live):
    ask = Fake("no json")
    result = go(write_jobs(tmp_path / "j.json", n=A.MAX_JOBS), ask)
    assert result["status"] == "stopped" and result["stop"] == "budget"
    assert result["calls"] == len(ask.calls) == A.MAX_CALLS


def test_the_deadline_stops_a_run_that_has_started(tmp_path, live):
    state = {"now": BEFORE}

    class Slow(Fake):
        def __call__(self, prompt, **kw):
            state["now"] = NOT_AFTER  # the first answer lands after the deadline
            return super().__call__(prompt, **kw)

    result = A.run(write_jobs(tmp_path / "j.json", n=20), ask=Slow(), key_loader=lambda: KEY, now=lambda: state["now"], sleep=lambda _: None)
    assert result["stop"] == "late" and result["status"] == "stopped" and result["calls"] <= A.WORKERS
    # what was already in flight is logged with its true time, so `llm_live.py score` leaves it out
    assert all(r["asked_at"] >= r["not_after"] for r in A.read_rows(live))


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("not_after"),
    lambda p: p.update(not_after="2026-11-18T09:25:00"),
    lambda p: p.update(jobs=[]),
    lambda p: p.update(jobs=p["jobs"] * 2),
    lambda p: p["jobs"][0].pop("prompt"),
    lambda p: p.update(model="a-dearer-model"),
    lambda p: p.update(effort="max"),
    lambda p: p.update(jobs=[{**p["jobs"][0], "id": f"{i}"} for i in range(A.MAX_JOBS + 1)]),
])
def test_a_broken_jobs_file_is_rejected_before_any_call(tmp_path, live, mutate):
    path = write_jobs(tmp_path / "j.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    ask = Fake()
    with pytest.raises(ValueError):
        go(path, ask)
    assert not ask.calls


def test_check_reports_without_any_network_call(tmp_path, live, monkeypatch):
    def no_network(*a, **k):
        pytest.fail("check must not touch the network")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    path = write_jobs(tmp_path / "j.json")
    result = A.check(path, key_loader=lambda: None, now=lambda: BEFORE)
    assert result == {"status": "check", "decision_date": "2026-11-17", "jobs": 3, "refusal": "no_key", "key": False,
                      "todo": 3, "not_after": NOT_AFTER.isoformat(), "spent_today": 0, "days": 0, "logged": 0}
    assert A.check(path, key_loader=lambda: KEY, now=lambda: BEFORE)["refusal"] is None
    assert A.check(path, key_loader=lambda: KEY, now=lambda: NOT_AFTER)["refusal"] == "late"
    assert not live.exists()


def test_the_default_key_path_reads_the_server_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PULSE_SECRETS_DIR", str(tmp_path))
    assert A._server_key() is None
    (tmp_path / "openai.key").write_text(KEY, encoding="utf-8")
    assert A._server_key() == KEY


def test_ping_never_reports_the_key():
    class R:
        status_code = 200

        def json(self):
            return {"data": [{"id": A.LIVE_MODEL}]}

    seen = {}

    def get(url, headers, timeout):
        seen.update(url=url, auth=headers["Authorization"])
        return R()

    result = A.ping(key_loader=lambda: KEY, get=get)
    assert result == {"status": "ok", "http": 200, "chars": len(KEY), "models": {A.ALERT_MODEL: False, A.LIVE_MODEL: True}}
    assert seen == {"url": A.MODELS_URL, "auth": f"Bearer {KEY}"} and KEY not in json.dumps(result)


def test_ping_outcomes_without_a_usable_key_or_network():
    def down(*a, **k):
        raise httpx.ConnectTimeout("no route")

    def must_not_send(*a, **k):
        pytest.fail("a broken key must not be sent")

    class Refused:
        status_code = 401

    assert A.ping(key_loader=lambda: None, get=must_not_send) == {"status": "no_key"}
    assert A.ping(key_loader=lambda: "﻿abc", get=must_not_send) == {"status": "bad_key", "chars": 4}
    assert A.ping(key_loader=lambda: KEY, get=down) == {"status": "network", "error": "ConnectTimeout"}
    assert A.ping(key_loader=lambda: KEY, get=lambda *a, **k: Refused())["status"] == "refused"


# --- the pre-registered verdict ---------------------------------------------------------------------


def verdict_rows(days: int, ic_sign: float = 1.0, other: int = 1, gap: float = 0.0) -> list[dict]:
    rows = []
    for k in range(days):
        for i in range(V.MIN_NAMES):
            action = "watch" if i < other else "avoid"
            rows.append({"decision_date": f"d{k:03d}", "group": "top", "action": action, "p_outperform": 60 - i,
                         "ex5": ic_sign * (5 - i) + (gap if action == "watch" else 0.0)})
    return rows


def test_a_late_or_repeated_verdict_is_not_scored():
    deadline = NOT_AFTER.isoformat()
    rows = [{"id": "a", "asked_at": "2026-11-17T20:41:00+08:00", "not_after": deadline, "action": "avoid"},
            {"id": "a", "asked_at": "2026-11-17T20:40:00+08:00", "not_after": deadline, "action": "watch"},
            {"id": "b", "asked_at": deadline, "not_after": deadline},
            {"id": "c", "asked_at": "2026-11-18T09:40:00+08:00", "not_after": deadline},
            {"id": "old", "asked_at": "2026-11-01T10:00:00"}]
    kept = {r["id"]: r for r in V.scorable(rows)}
    assert set(kept) == {"a", "old"} and kept["a"]["action"] == "watch"


def test_the_verdict_needs_twenty_days_and_both_conditions():
    good = V.evaluate(verdict_rows(20, other=4, gap=1.0))
    assert good["days"] == 20 and good["mean_ic"] == pytest.approx(1.0) and good["p_boot"] == 0.0
    assert good["passed"] is True and good["verdict"] == "passed"
    early = V.evaluate(verdict_rows(19, other=4, gap=1.0))
    assert early["ready"] is False and early["passed"] is False and early["verdict"] == "inconclusive"
    wrong_way = V.evaluate(verdict_rows(20, ic_sign=-1.0, other=4))
    assert wrong_way["ic_pass"] is False and wrong_way["passed"] is False and wrong_way["verdict"] == "fails"


def test_too_few_names_outside_avoid_cannot_pass():
    rows = verdict_rows(20, other=0, gap=1.0)
    for k in range(19):
        rows[k * V.MIN_NAMES]["action"] = "watch"
    r = V.evaluate(rows)
    assert r["n_other"] == 19 and r["spread_pass"] is None and r["passed"] is False and r["verdict"] == "inconclusive"
    r = V.evaluate(verdict_rows(20, other=1, gap=1.0))
    assert r["n_other"] == 20 and r["spread_pass"] is True and r["passed"] is True


def test_the_spread_must_beat_the_round_trip_cost():
    rows = [{"decision_date": f"d{k:03d}", "group": "top", "action": "watch" if i < 10 else "avoid", "p_outperform": 60 - i,
             "ex5": 0.1 if i < 10 else 0.0} for k in range(20) for i in range(V.MIN_NAMES)]
    r = V.evaluate(rows)
    assert r["ic_pass"] is True and r["spread_pct"] == pytest.approx(0.1) and r["spread_pass"] is False
    assert r["passed"] is False and r["verdict"] == "fails"


def test_days_without_an_ic_and_nan_labels_do_not_poison_the_mean():
    rows = verdict_rows(3)
    rows += [{"decision_date": "two_names", "group": "top", "action": "avoid", "p_outperform": 60 - i, "ex5": float(i)} for i in range(2)]
    rows += [{"decision_date": "flat", "group": "top", "action": "avoid", "p_outperform": 50, "ex5": float(i)} for i in range(V.MIN_NAMES)]
    rows += [{"decision_date": "d000", "group": "top", "action": "avoid", "p_outperform": 40, "ex5": float("nan")}]
    r = V.evaluate(rows)
    assert r["days"] == 3 and r["days_without_ic"] == 2 and r["mean_ic"] == pytest.approx(1.0)
    assert V.evaluate([])["mean_ic"] is None and V.evaluate([])["passed"] is False


def test_controls_are_reported_and_never_decide():
    rows = verdict_rows(2) + [{**r, "group": "control", "ex5": -r["ex5"]} for r in verdict_rows(2)]
    r = V.evaluate(rows)
    assert r["mean_ic"] == pytest.approx(1.0) and r["control_mean_ic"] == pytest.approx(-1.0)


# --- step 10 of the daily pipeline ------------------------------------------------------------------


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    state = {"reply": '{"status": "check", "todo": 40}'}
    jobs = tmp_path / "jobs_2026-11-17.json"

    def fake_run(cmd, env=None, cwd=None, timeout=7200):
        calls.append(cmd)
        name = Path(cmd[0]).name
        if "llm_live.py" in cmd:
            assert env == {"LAB_UNIVERSE": "main", "LIVE_SCORE": D.SCORE + ".pt"}
            return f"{jobs}\n"
        if name == "ssh.exe":
            return state["reply"] + "\n"
        if name == "scp.exe" and str(cmd[-1]).endswith(".part"):
            Path(cmd[-1]).write_text("pulled", encoding="utf-8")
        return ""

    monkeypatch.setattr(D, "run", fake_run)
    monkeypatch.setattr(D, "HOST", "host")
    monkeypatch.setattr(D, "LOG", tmp_path / "log.txt")
    monkeypatch.setattr(D, "LLM_SWITCH", tmp_path / "llm_live.enabled")
    monkeypatch.setattr(D, "LLM_DECISIONS", tmp_path / "main" / "decisions.jsonl")
    return {"calls": calls, "state": state, "tmp": tmp_path}


def remote_commands(calls: list[list[str]]) -> list[str]:
    import base64
    return [base64.b64decode(c[-1].rsplit(" ", 1)[-1]).decode("utf-16-le") for c in calls if Path(c[0]).name == "ssh.exe"]


def test_without_the_switch_the_pipeline_only_dry_runs(pipeline):
    D.run_llm_live()
    last = remote_commands(pipeline["calls"])[-1]
    assert "llm_ask.py check" in last and "llm_ask.py run" not in last
    assert not (pipeline["tmp"] / "main" / "decisions.jsonl").exists()
    assert "llm_live check" in (pipeline["tmp"] / "log.txt").read_text(encoding="utf-8")


def test_with_the_switch_the_server_calls_and_the_verdicts_come_back(pipeline):
    (pipeline["tmp"] / "llm_live.enabled").write_text("", encoding="utf-8")
    pipeline["state"]["reply"] = '{"status": "done", "asked": 40, "logged": 40}'
    D.run_llm_live()
    assert "llm_ask.py run" in remote_commands(pipeline["calls"])[-1]
    assert (pipeline["tmp"] / "main" / "decisions.jsonl").read_text(encoding="utf-8") == "pulled"


def test_nothing_on_the_server_means_nothing_to_pull(pipeline):
    (pipeline["tmp"] / "llm_live.enabled").write_text("", encoding="utf-8")
    pipeline["state"]["reply"] = '{"status": "late", "asked": 0, "logged": 0}'
    D.run_llm_live()
    assert not (pipeline["tmp"] / "main" / "decisions.jsonl").exists()
    assert not any(str(c[-1]).endswith(".part") for c in pipeline["calls"])
    assert "llm_live run FAILED" in (pipeline["tmp"] / "log.txt").read_text(encoding="utf-8")


def test_a_later_limit_day_still_pulls_what_an_earlier_day_could_not(pipeline):
    (pipeline["tmp"] / "llm_live.enabled").write_text("", encoding="utf-8")
    pipeline["state"]["reply"] = '{"status": "limit", "asked": 0, "logged": 800}'
    D.run_llm_live()
    assert (pipeline["tmp"] / "main" / "decisions.jsonl").read_text(encoding="utf-8") == "pulled"
    assert "FAILED" not in (pipeline["tmp"] / "log.txt").read_text(encoding="utf-8")


def test_a_server_file_that_lost_rows_is_merged_with_the_local_one(pipeline):
    (pipeline["tmp"] / "llm_live.enabled").write_text("", encoding="utf-8")
    pipeline["state"]["reply"] = '{"status": "done", "asked": 1, "logged": 1}'
    local = pipeline["tmp"] / "main" / "decisions.jsonl"
    local.parent.mkdir()
    local.write_text('{"id": "kept"}\n', encoding="utf-8")
    D.run_llm_live()
    assert local.read_text(encoding="utf-8") == '{"id": "kept"}\n' and not local.with_suffix(".part").exists()
    assert D.REMOTE_LLM == "C:/pulse/data/llm" and "PULSE_LLM_DIR" in remote_commands(pipeline["calls"])[-1]
    assert "lack 1 ids" in (pipeline["tmp"] / "log.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(("mode", "result", "flag"), [
    ("run", {"status": "done"}, ""),
    ("run", {"status": "limit"}, ""),
    ("run", {"status": "failed"}, " FAILED"),
    ("run", {"status": "stopped"}, " FAILED"),
    ("run", {"status": "no_key"}, " FAILED"),
    ("run", {"status": "busy"}, " FAILED"),
    ("run", {"status": "partial"}, " FAILED"),
    ("run", {"status": "ledger_lost"}, " FAILED"),
    ("check", {"status": "check", "refusal": "no_key"}, ""),
    ("check", {"status": "check", "refusal": None}, ""),
    ("check", {"status": "check", "refusal": "bad_key"}, " FAILED"),
])
def test_what_stands_out_in_the_log(mode, result, flag):
    assert D.llm_flag(mode, result) == flag


def test_step_ten_runs_after_the_books_and_the_holdings():
    source = inspect.getsource(D.main)
    assert source.index('"forward_book.py"') < source.index("push_holdings()") < source.index("run_llm_live()")
    assert source.count("run_llm_live()") == 1  # not on the data-revision path, which has no new decision day


def test_a_failure_is_logged_and_does_not_fail_the_run(pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("prepare exit 1")

    monkeypatch.setattr(D, "run", boom)
    D.run_llm_live()
    assert "llm_live FAILED (books unaffected): RuntimeError" in (pipeline["tmp"] / "log.txt").read_text(encoding="utf-8")
