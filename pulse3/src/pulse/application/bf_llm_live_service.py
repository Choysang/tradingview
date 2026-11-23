"""E092 live web-search LLM judgement: the daily ``decide`` step, the 5-day ``track`` step, the pre-registered ``judge`` and ``status``.

Why: pre-registration 2026-11-09 asks for a forward-only record: after the close of decision day D the day's 30 best main-board fusion names and 10 random controls are each judged by a
web-searching model, the answer is stored with everything needed to audit it, and 5 sessions later it is scored against the matched baseline. One class holds the loop so the caps
(60 requests per decision day, a USD cap per day, 20 decision days in all) live in one place and are tested without a network.
Invariants: the ledger row of a request is durable BEFORE the request is sent (if it cannot be written the request is not made); an answered name is never asked again, a stored row is never
rewritten; a day stops at the first 401/403/429, refusal, deadline, request cap, spend cap or 5 failures in a row, and a hard-stopped day is not resumed without an explicit flag; a timeout
is asked once more, nothing else is retried; the 21st decision day with a request is refused; an answer finished at or after the day's deadline (next session 09:25 CST) is stored but never scored;
the key never enters a row or a message (``adapters.models.responses_client``). The spend figure is an estimate (``rules.estimate_cost``).
Not covered: the data behind the universe and the labels (``bf_llm_live_inputs``), the registry rows (``bf_llm_live_registry``), scheduling.
"""
from __future__ import annotations

import threading
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pulse.adapters.models.responses_client import CallFailed, CallStop, KeyProblem, Reply
from pulse.application import frozen_specs as FS
from pulse.application.bf_llm_live_store import LogStore, make_row
from pulse.domain.llm_live import judge as J
from pulse.domain.llm_live import rules as R

WORKERS = 4
TRACK_HORIZON = 6                    # decision day D buys at the D+1 open and sells at the D+6 open: the window is closed once the D+6 bar exists


PreregistrationChanged = FS.FrozenSpecError          # kept for callers of the first version


class ContextMissing(RuntimeError):
    """The inputs of the decision day are not ready; ``code`` is the stable reason (scores_not_ready, bars_not_ready, market_line_missing ...)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class DayPlan:
    """The universe of one decision day with its prompts, as the context loader built them."""
    day: date
    jobs: list[dict]                 # id, code, name, group, score, prompt
    market: str
    refs: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


ContextLoader = Callable[[date], DayPlan]
LabelLoader = Callable[[Sequence[date]], dict[date, dict[str, tuple[float | None, float | None]]]]   # day -> code -> (ret5, ex5) for days whose window is closed
KeyLoader = Callable[[], str]


SPEC_FILE = "2026-11-09-e092-llm-live-preregistration.md"


def check_document(specs_dir: Path | None = None, repo_root: Path | None = None) -> str:
    """Verify the frozen pre-registration shipped with the app (``pulse/research_specs``); raises ``FrozenSpecError`` when it is missing or differs. Never returns None."""
    return FS.verify(SPEC_FILE, R.SPEC_SHA256, R.frozen_hash, repo_path=R.SPEC_PATH, specs_dir=specs_dir, repo_root=repo_root)


# ---------------------------------------------------------------------------------------------------------------
# state read from the log
# ---------------------------------------------------------------------------------------------------------------
def _of(rows: Sequence[Mapping], kind: str, session: str | None = None) -> list[Mapping]:
    return [r for r in rows if r["kind"] == kind and (session is None or r["session"] == session)]


def call_days(rows: Sequence[Mapping]) -> set[str]:
    """Decision days that have at least one request on the ledger: the days the 20-day limit counts."""
    return {r["session"] for r in rows if r["kind"] == "call"}


def hard_stop(rows: Sequence[Mapping], day: str) -> str | None:
    hits = [r["body"]["reason"] for r in _of(rows, "stop", day) if r["body"]["reason"] in R.HARD_STOPS]
    return hits[-1] if hits else None


def answered_ids(rows: Sequence[Mapping], day: str) -> set[str]:
    return {r["key"] for r in _of(rows, "answer", day)}


# ---------------------------------------------------------------------------------------------------------------
# the day loop's gate
# ---------------------------------------------------------------------------------------------------------------
class _Gate:
    """What may still start. Shared by the worker threads; every ledger and result write happens under one lock."""

    def __init__(self, svc: "LlmLive", plan: DayPlan, not_after: datetime, run_id: str):
        self.s, self.day, self.not_after, self.run_id = svc, plan.day.isoformat(), not_after, run_id
        self.lock = threading.Lock()
        self.stop: str | None = None
        self.calls = 0
        self.consecutive = 0
        self.asked_at: dict[int, datetime] = {}
        self.errors: Counter[str] = Counter()
        self.answers = 0
        self.unsaved = 0

    def _row(self, kind: str, key: str, body: dict) -> dict:
        return make_row(self.day, kind, key, self.run_id, body, at=self.s.clock())

    def _halt(self, reason: str, detail: dict | None = None) -> None:
        if self.stop is None:
            self.stop = reason
            try:
                body = {"reason": reason, "hard": reason in R.HARD_STOPS, **({"detail": detail} if detail else {})}
                self.s.store.append([self._row("stop", self.day, body)])
            except Exception:                                    # noqa: BLE001 - the stop is still in force for this run and in the summary
                pass

    def halt(self, reason: str, detail: dict | None = None) -> None:
        with self.lock:
            self._halt(reason, detail)

    def reserve(self, job: dict, attempt: int) -> int | None:
        """Write the ledger row of the next request and return its number, or None when the day may not ask any more."""
        with self.lock:
            if self.stop is not None:
                return None
            rows = self.s.store.rows()
            used = sum(1 for r in rows if r["kind"] == "call" and r["session"] == self.day)
            if self.s.clock() >= self.not_after:
                self._halt("late")
            elif used >= R.MAX_CALLS:
                self._halt("budget")
            elif not R.allows_next_call(rows, self.day, self.s.prices, self.s.cap_usd):
                self._halt("cost_cap")
            elif self.consecutive >= R.MAX_CONSECUTIVE_FAILURES:
                self._halt("failures")
            if self.stop is not None:
                return None
            n = used + 1
            try:
                self.s.store.append([self._row("call", job["id"], {"n": n, "id": job["id"], "attempt": attempt, "prompt_sha256": job["prompt_sha256"],
                                                                  "model": R.LIVE_MODEL, "effort": R.LIVE_EFFORT,
                                                                  "max_output_tokens": self.s.max_output_tokens})])
            except Exception:                                     # noqa: BLE001 - no ledger row, no request
                self._halt("ledger_error")
                return None
            self.asked_at[n] = self.s.clock()
            self.calls += 1
            return n

    def _cost(self, reply: Reply | None) -> float | None:
        return None if reply is None else R.estimate_cost(reply.usage, reply.searches, self.s.prices)

    def answer(self, n: int, job: dict, reply: Reply, verdict: dict) -> None:
        with self.lock:
            done = self.s.clock()
            body = {"id": job["id"], "code": job["code"], "group": job["group"], "score": job["score"], "call_n": n, "model": R.LIVE_MODEL, "effort": R.LIVE_EFFORT,
                    "prompt_sha256": job["prompt_sha256"], "response_id": reply.response_id, "request_id": reply.request_id, "usage": reply.usage, "searches": reply.searches,
                    "cost_usd": self._cost(reply), "prices": self.s.prices.__dict__, "status": reply.status, "seconds": reply.seconds, "raw_text": reply.text,
                    "action": verdict["action"], "p_outperform": verdict["p_outperform"], "expected_excess_pct": verdict.get("expected_excess_pct"),
                    "reason": verdict.get("reason"), "news": verdict.get("news"), "asked_at": self.asked_at[n].isoformat(timespec="seconds"),
                    "available_at": done.isoformat(timespec="seconds"), "not_after": self.not_after.isoformat(), "valid": done < self.not_after}
            try:
                self.s.store.append([self._row("answer", job["id"], body)])
            except Exception:                                     # noqa: BLE001 - a paid answer that cannot be stored: stop the day and say so in the summary
                self.unsaved += 1
                self._halt("ledger_error")
                return
            self.consecutive = 0
            self.answers += 1

    def fail(self, n: int, job: dict, label: str, reply: Reply | None = None) -> None:
        with self.lock:
            # usage when the reply reported it; a refusal before any work (401/403/429, refusal) costs 0; every other failure after the request was sent counts at the unit cost (cost None)
            cost = R.failure_cost(label, self._cost(reply))
            body = {"id": job["id"], "call_n": n, "label": label, "cost_usd": cost, "at": self.s.clock().isoformat(timespec="seconds")}
            if reply is not None:                                 # the reply was paid for: keep its text and ids
                body.update(raw_text=reply.text, response_id=reply.response_id, request_id=reply.request_id, usage=reply.usage, searches=reply.searches)
            try:
                self.s.store.append([self._row("error", job["id"], body)])
            except Exception:                                     # noqa: BLE001 - the call stays unresolved on the ledger and counts as the reserve
                self._halt("ledger_error")
            self.consecutive += 1
            self.errors[label] += 1


# ---------------------------------------------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------------------------------------------
class LlmLive:
    def __init__(self, store: LogStore, *, context: ContextLoader, labels: LabelLoader, key_loader: KeyLoader, client_factory: Callable[[str], Any],
                 prices: R.Prices | None = None, cap_usd: float = R.DEFAULT_DAILY_CAP_USD, clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] | None = None, switch_on: Callable[[], bool] = lambda: True, register: Callable[[datetime], None] | None = None,
                 workers: int = WORKERS, max_output_tokens: int = R.DEFAULT_MAX_OUTPUT_TOKENS):
        self.store, self.context, self.labels = store, context, labels
        self.key_loader, self.client_factory = key_loader, client_factory
        self.prices = prices or R.Prices()
        self.cap_usd = cap_usd
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.switch_on, self.register, self.workers = switch_on, register, workers
        self.max_output_tokens = max_output_tokens
        self.run_id = uuid.uuid4().hex[:12]

    def today(self) -> date:
        return self.clock().astimezone(R.CST).date()

    # -- decide ----------------------------------------------------------------------------------------------------
    def decide(self, day: date | None = None, *, check_only: bool = False, resume_after_stop: bool = False) -> dict:
        day = day or self.today()
        iso = day.isoformat()
        not_after = R.deadline_after(day)

        def out(status: str, **kw: Any) -> dict:
            return {"status": status, "decision_date": iso, "not_after": not_after.isoformat(), **kw}

        if self.clock() >= not_after:
            return out("late")
        if self.cap_usd <= 0:
            return out("cost_cap", spent_usd=0.0)
        try:
            key = self.key_loader()
        except KeyProblem as exc:
            return out(exc.code)
        with self.store.exclusive() as held:
            if not held:
                return out("busy")
            self.store.refresh()
            rows = self.store.rows()
            days = call_days(rows)
            if iso not in days and len(days) >= R.MAX_DECISION_DAYS:
                return out("limit", decision_days=len(days))
            stopped = hard_stop(rows, iso)
            if stopped and not resume_after_stop:
                return out("stopped_today", stop=stopped)
            used = sum(1 for r in _of(rows, "call", iso))
            spent, unresolved = R.day_spend(rows, iso, self.prices)
            if used >= R.MAX_CALLS:
                return out("budget", calls=used)
            if not R.allows_next_call(rows, iso, self.prices, self.cap_usd):
                return out("cost_cap", spent_usd=round(spent, 4), cap_usd=self.cap_usd)
            stored = _of(rows, "day", iso)
            if stored:
                plan = self._plan_from_row(day, stored[0]["body"])
            else:
                try:
                    plan = self.context(day)
                except ContextMissing as exc:
                    return out("not_ready", code=exc.code, detail=exc.detail)
            if not plan.jobs or len(plan.jobs) > R.MAX_JOBS:
                return out("bad_universe", jobs=len(plan.jobs))
            done = answered_ids(rows, iso)
            todo = [j for j in plan.jobs if j["id"] not in done]
            info = {"jobs": len(plan.jobs), "todo": len(todo), "calls": used, "spent_usd": round(spent, 4), "cap_usd": self.cap_usd, "decision_days": len(days)}
            if not todo:
                return out("complete", **info)
            if check_only or not self.switch_on():
                return out("check" if check_only else "switch_off", **info)
            if self.register is not None:
                self.register(self.clock())
            if not stored:
                self.store.append([make_row(iso, "day", iso, self.run_id, self._day_body(plan, not_after), at=self.clock())])
            gate = _Gate(self, plan, not_after, self.run_id)
            client = self.client_factory(key)
            self._ask_all(gate, client, plan, todo)
            status = "stopped" if gate.stop else "done" if not gate.errors else "partial" if gate.answers else "failed"
            spent_after, _ = R.day_spend(self.store.rows(), iso, self.prices)
            summary = out(status, asked=gate.answers, failed=sum(gate.errors.values()), calls=gate.calls, stop=gate.stop, errors=dict(gate.errors), unsaved=gate.unsaved,
                          spent_usd=round(spent_after, 4), cap_usd=self.cap_usd, decision_days=len(call_days(self.store.rows())))
            try:
                self.store.append([make_row(iso, "run", self.run_id, self.run_id, {"cmd": "decide", **{k: summary[k] for k in ("status", "asked", "failed", "calls", "stop", "errors")}},
                                            at=self.clock())])
            except Exception:                                     # noqa: BLE001 - the run row is a convenience; the ledger and answers are already stored
                summary["run_row"] = "not written"
            return summary

    @staticmethod
    def _day_body(plan: DayPlan, not_after: datetime) -> dict:
        system = R.system_text(plan.day)
        jobs = [{**j, "prompt_sha256": R.prompt_sha256(system, j["prompt"])} for j in plan.jobs]
        return {"decision_date": plan.day.isoformat(), "not_after": not_after.isoformat(), "model": R.LIVE_MODEL, "effort": R.LIVE_EFFORT, "system": system,
                "market": plan.market, "jobs": jobs, "refs": plan.refs, "notes": plan.notes, "spec_sha256": R.SPEC_SHA256}

    @staticmethod
    def _plan_from_row(day: date, body: dict) -> DayPlan:
        return DayPlan(day, body["jobs"], body["market"], body.get("refs", {}), body.get("notes", []))

    def _ask_all(self, gate: _Gate, client: Any, plan: DayPlan, todo: list[dict]) -> None:
        system = R.system_text(plan.day)
        jobs = [{**j, "prompt_sha256": j.get("prompt_sha256") or R.prompt_sha256(system, j["prompt"])} for j in todo]

        def one(job: dict) -> None:
            for attempt in range(1 + R.MAX_TIMEOUT_RETRIES):
                n = gate.reserve(job, attempt)
                if n is None:
                    return
                try:
                    reply = client.ask(job["prompt"], model=R.LIVE_MODEL, effort=R.LIVE_EFFORT, system=system, max_output_tokens=self.max_output_tokens)
                except CallStop as exc:
                    gate.halt(exc.reason, exc.detail)
                    gate.fail(n, job, exc.reason, exc.reply)
                    return
                except CallFailed as exc:
                    gate.fail(n, job, exc.label, exc.reply)
                    if exc.label == "timeout" and attempt < R.MAX_TIMEOUT_RETRIES:
                        continue
                    return
                except Exception as exc:                          # noqa: BLE001 - recorded by class name only; its message may quote a header
                    gate.fail(n, job, type(exc).__name__)
                    return
                try:
                    verdict = R.parse_verdict(reply.text)
                except ValueError:
                    gate.fail(n, job, "bad_verdict", reply)
                    return
                gate.answer(n, job, reply, verdict)
                return

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(one, jobs))

    # -- track -----------------------------------------------------------------------------------------------------
    def track(self, today: date | None = None) -> dict:
        today = today or self.today()
        with self.store.exclusive() as held:
            if not held:
                return {"status": "busy"}
            self.store.refresh()
            rows = self.store.rows()
            have = {r["key"] for r in _of(rows, "outcome")}
            pending = [r for r in _of(rows, "answer") if r["key"] not in have]
            due = sorted({r["session"] for r in pending if R.sessions_after(date.fromisoformat(r["session"]), TRACK_HORIZON) <= today})
            if not due:
                return {"status": "nothing_due", "pending": len(pending)}
            labels = self.labels([date.fromisoformat(d) for d in due])
            new = []
            for r in pending:
                d = date.fromisoformat(r["session"])
                if d not in labels:
                    continue                                      # the window is not closed in the data yet
                ret5, ex5 = labels[d].get(r["body"]["code"], (None, None))
                finite = ex5 is not None and ex5 == ex5 and abs(ex5) != float("inf")
                status = "late_answer" if not r["body"]["valid"] else "scored" if finite else "no_label"
                b = r["body"]
                new.append(make_row(r["session"], "outcome", r["key"], self.run_id,
                                    {"id": r["key"], "code": b["code"], "group": b["group"], "action": b["action"], "p_outperform": b["p_outperform"], "score": b["score"],
                                     "valid": b["valid"], "ret5": ret5 if finite else None, "ex5": ex5 if finite else None, "status": status}, at=self.clock()))
            if new:
                self.store.append(new)
            return {"status": "tracked" if new else "waiting_for_data", "written": len(new), "by_status": dict(Counter(r["body"]["status"] for r in new)),
                    "pending_after": len(pending) - len(new)}

    # -- judge and status ------------------------------------------------------------------------------------------
    def _eval_rows(self, rows: Sequence[Mapping]) -> list[dict]:
        return [{"decision_date": r["session"], "group": r["body"]["group"], "action": r["body"]["action"], "p_outperform": r["body"]["p_outperform"],
                 "ex5": r["body"]["ex5"], "code": r["body"]["code"]} for r in _of(rows, "outcome") if r["body"]["status"] == "scored"]

    def progress(self, rows: Sequence[Mapping]) -> dict:
        days = call_days(rows)
        outcomes = {r["key"] for r in _of(rows, "outcome")}
        unresolved = sorted(d for d in days if any(a["key"] not in outcomes for a in _of(rows, "answer", d)))
        return {"decision_days": len(days), "days_limit": R.MAX_DECISION_DAYS, "days_unresolved": unresolved, "answers": len(_of(rows, "answer")),
                "errors": len(_of(rows, "error")), "outcomes": len(outcomes), "scored": len(self._eval_rows(rows)), "calls": len(_of(rows, "call"))}

    def judge(self) -> dict:
        """The pre-registered verdict. Written once, when 20 days have a rank correlation or the 20-day limit is reached and every one of those days is scored; otherwise progress only."""
        with self.store.exclusive() as held:
            if not held:
                return {"status": "busy"}
            self.store.refresh()
            rows = self.store.rows()
            prog = self.progress(rows)
            ev = J.evaluate(self._eval_rows(rows))
            stored = _of(rows, "judgement")
            if stored:
                return {"status": "judged", "stored": True, "evaluation": stored[0]["body"], "progress": prog}
            limit_reached = prog["decision_days"] >= R.MAX_DECISION_DAYS and not prog["days_unresolved"]
            if not (ev["ready"] or limit_reached):
                return {"status": "progress", "evaluation": ev, "progress": prog,
                        "reason": f"{ev['days']}/{J.MIN_DAYS} days with a rank correlation; {prog['decision_days']}/{R.MAX_DECISION_DAYS} decision days started"}
            body = {**ev, "decision_days": prog["decision_days"], "spec_sha256": R.SPEC_SHA256, "decided_at": self.clock().isoformat(timespec="seconds")}
            self.store.append([make_row(R.EXPERIMENT, "judgement", R.EXPERIMENT, self.run_id, body, at=self.clock())])
            return {"status": "judged", "stored": False, "evaluation": body, "progress": prog}

    def status(self) -> dict:
        self.store.refresh()
        rows = self.store.rows()
        today = self.today().isoformat()
        spent, unresolved = R.day_spend(rows, today, self.prices)
        stops = {r["session"]: r["body"]["reason"] for r in _of(rows, "stop")}
        return {"spec_sha256": R.SPEC_SHA256, "model": R.LIVE_MODEL, "effort": R.LIVE_EFFORT, "today": today, "switch_on": self.switch_on(), "cap_usd": self.cap_usd,
                "calls_today": len(_of(rows, "call", today)), "calls_cap": R.MAX_CALLS, "spent_today_usd": round(spent, 4), "unresolved_calls_today": unresolved,
                "stops": stops, "judgement": (_of(rows, "judgement") or [{"body": None}])[0]["body"], **self.progress(rows)}
