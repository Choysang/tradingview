"""Shared builders of the E092 tests: a fake day plan, Responses API replies, a mock transport, a controllable clock. Nothing here touches the network or a key file."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from pulse.adapters.models.responses_client import ResponsesClient
from pulse.application.bf_llm_live_service import DayPlan, LlmLive
from pulse.application.bf_llm_live_store import LogStore, MemoryLogStore
from pulse.domain.llm_live import rules as R

DAY = date(2026, 11, 23)                                      # a Monday: the deadline is Tuesday 2026-11-24 09:25 CST
KEY = "sk-test-SECRET-0123456789"
EVENING = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)  # 20:00 CST on the decision day


class Clock:
    def __init__(self, now: datetime = EVENING):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def codes(n: int = 40) -> list[str]:
    return [f"60{i:04d}" for i in range(n)]


def plan(day: date = DAY, n_top: int = 30, n_ctl: int = 10) -> DayPlan:
    cs = codes(n_top + n_ctl)
    jobs = [{"id": R.job_id(day, c), "code": c, "name": f"N{c}", "group": R.GROUP_TOP if i < n_top else R.GROUP_CONTROL, "score": 1.0 - i / 100,
             "prompt": f"prompt of {c}"} for i, c in enumerate(cs)]
    return DayPlan(day, jobs, "market line", {"fusion": "x"}, [])


VERDICT = {"action": "watch", "p_outperform": 55, "expected_excess_pct": 0.4, "reason": "ok", "news": []}


def reply(verdict: dict | str | None = None, *, rid: str = "resp_1", tin: int = 2000, tout: int = 500, searches: int = 2, status: str = "completed", extra: dict | None = None) -> httpx.Response:
    text = verdict if isinstance(verdict, str) else json.dumps(verdict or VERDICT, ensure_ascii=False)
    body = {"id": rid, "status": status, "output": [{"type": "web_search_call"}] * searches + [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": tin, "output_tokens": tout}, **(extra or {})}
    return httpx.Response(200, json=body, headers={"x-request-id": f"req-{rid}"})


class Recorder:
    """A mock vendor: counts requests, keeps what it saw (without being able to leak the key: it only records header presence), delegates the answer to ``fn(n, body)``."""

    def __init__(self, fn: Callable[[int, dict], httpx.Response] | None = None):
        self.fn = fn or (lambda n, body: reply(rid=f"r{n}"))
        self.requests: list[dict] = []
        self.urls: list[str] = []
        self.auth_ok: list[bool] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.urls.append(str(request.url))
        self.auth_ok.append(request.headers.get("authorization") == f"Bearer {KEY}")
        return self.fn(len(self.requests), body)


def build(store: LogStore | None = None, *, vendor: Recorder | None = None, clock: Clock | None = None, plan_: DayPlan | None = None, prices: R.Prices | None = None,
          cap_usd: float = 5.0, workers: int = 1, switch: bool = True, key_loader=None, context=None, labels=None, register=None, experiment=None) -> tuple[LlmLive, MemoryLogStore, Recorder, Clock]:
    store = store or MemoryLogStore()
    vendor = vendor or Recorder()
    clock = clock or Clock()
    p = plan_ or plan()
    svc = LlmLive(store, context=context or (lambda d: p), labels=labels or (lambda days: {}), key_loader=key_loader or (lambda: KEY),
                  client_factory=lambda key: ResponsesClient(key, transport=httpx.MockTransport(vendor)), prices=prices or R.Prices(), cap_usd=cap_usd, clock=clock,
                  switch_on=lambda: switch, register=register, workers=workers, **({"experiment": experiment} if experiment is not None else {}))
    return svc, store, vendor, clock


def kinds(store: MemoryLogStore, kind: str, session: str | None = None) -> list[dict]:
    return [r for r in store.rows() if r["kind"] == kind and (session is None or r["session"] == session)]
