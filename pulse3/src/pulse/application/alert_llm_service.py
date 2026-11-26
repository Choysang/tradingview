"""LLM fallback for undecided news alerts: one question per message, a daily cap, stop-for-the-day, an append-only ledger (row #134).

Why: ``alert_service.scan`` decides tiers from rules. For a claim the rules cannot decide that touches a holding or a watch-list
name (``domain.alerts.llm_tier.is_undecided``) this service asks a model once and stores the answer; the scan turns the stored
answer into P0/P1/P2 intents flagged as LLM-judged with the model's confidence. The user approved the API spend on 2026-11-23.
Invariants:
* OFF unless BOTH the switch file exists (``PULSE_ALERT_LLM_SWITCH``, default ``<lake parent>/alert_llm.enabled``, like the E092
  switch) AND ``PULSE_OPENAI_KEY_FILE`` names a readable key file (``from_env`` returns ``None`` otherwise; the scan then behaves exactly
  as without this module). The model comes from ``PULSE_ALERT_MODEL`` (default ``gpt-6-luna``), the daily cap from
  ``PULSE_ALERT_LLM_CAP`` (default 300 calls per Asia/Shanghai day, an unreadable value falls back to the default);
* LEDGER FIRST: a ``pending`` row of ``notification.alert_llm_judgement`` is committed before the paid call and the outcome is appended
  as a ``result`` row afterwards (append-only: model, prompt version + hash, status, usage as reported, the answer text as received
  minus NULs, the validated judgement). Whatever goes wrong after the call (an unparsable answer, an odd number, a storage error) ends in
  a result row with status ``error_<Class>`` / ``unparsed``; if even that cannot be written the pending row stays. Either way the message
  is never asked twice and the call counts against the cap;
* the day's state is derived from those rows, so it survives restarts and two workers: ``cap`` (calls today >= cap),
  ``stopped_http`` (a 401/403/429 today stops the fallback until the next day) and ``breaker`` (the last 5 calls today all failed or
  lost their outcome);
* one advisory lock serializes only the short check-and-reserve step (and the result insert needs none): it is NOT held during the call;
* the key, the prompt text and error response bodies are never stored or logged; calls are paced (``PACE_SECONDS``).
Not covered: scheduling (the scan runs ``pulse.workers.alerts scan``), the message body (claims carry the headline).
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from pulse import config
from pulse.adapters.models.alert_llm import AlertLlmClient
from pulse.adapters.models.model_guard import ModelNotAllowed
from pulse.domain.alerts import llm_tier as LT
from pulse.domain.alerts import tiers as T

LOG = logging.getLogger("pulse.alert_llm")
ENV_CAP = "PULSE_ALERT_LLM_CAP"
ENV_SWITCH = "PULSE_ALERT_LLM_SWITCH"
SWITCH_NAME = "alert_llm.enabled"
DEFAULT_CAP = 300                      # old news_alerts.LLM_DEFAULT_CAP
PACE_SECONDS = 2.0                     # old news_alerts.LLM_PACE: one call per 2 s
MAX_CONSECUTIVE_FAILURES = 5           # old news_alerts.LLM_MAX_CONSECUTIVE_FAILURES
PASS_BUDGET_SECONDS = 240.0            # wall time of one scan's LLM pass; the rest is picked up by the next scan
STOP_STATUSES = ("http_401", "http_403", "http_429")
RAW_TEXT_LIMIT = 8000
LOCK_KEY = 71340134                    # pg_advisory_xact_lock key of the fallback's check-and-reserve step
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def cap_from_env(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        return max(0, int(env.get(ENV_CAP, "")))
    except ValueError:
        return DEFAULT_CAP


def switch_path(env: Mapping[str, str] | None = None, lake_root: Path | None = None) -> Path:
    """``PULSE_ALERT_LLM_SWITCH``, else ``alert_llm.enabled`` next to the lake directory (``C:\\pulse3\\var\\alert_llm.enabled``)."""
    env = os.environ if env is None else env
    given = (env.get(ENV_SWITCH) or "").strip()
    return Path(given) if given else (lake_root or config.load().lake_root).parent / SWITCH_NAME


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The Asia/Shanghai day containing ``now`` as UTC instants."""
    start = datetime.combine(now.astimezone(CST).date(), dtime.min, CST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


class AlertFallback:
    def __init__(self, client: AlertLlmClient, *, cap: int | None = None, pace: float = PACE_SECONDS,
                 sleep: Callable[[float], None] = time.sleep, max_consecutive: int = MAX_CONSECUTIVE_FAILURES,
                 budget_seconds: float = PASS_BUDGET_SECONDS):
        self.client = client
        self.cap = DEFAULT_CAP if cap is None else cap
        self.budget_seconds = budget_seconds
        self._pace, self._sleep, self._max_consecutive = pace, sleep, max_consecutive
        self._last_call: float | None = None
        self._industries: list[str] | None = None
        self.stats: Counter = Counter()
        self.skipped: Counter = Counter()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, lake_root: Path | None = None) -> "AlertFallback | None":
        """``None`` (rules only) unless the switch file exists and the key file is readable."""
        if not switch_path(env, lake_root).is_file():
            return None
        try:
            client = AlertLlmClient.from_env(env, transport)
        except ModelNotAllowed as exc:                                # the configured model is not in PULSE_LLM_ALLOWED_MODELS: rules only, and say why
            LOG.error("alert LLM fallback off: %s", exc)
            return None
        return None if client is None else cls(client, cap=cap_from_env(env), sleep=sleep)

    # ---- the day's state, from the rows ----------------------------------------------------------------------------------
    def _day_calls(self, conn: psycopg.Connection, now: datetime) -> list[dict]:
        """One entry per call of today (newest first): its outcome status, or ``pending`` when no result row exists."""
        s, e = day_bounds(now)
        return conn.execute(
            "SELECT coalesce(r.status, 'pending') AS status FROM notification.alert_llm_judgement p"
            " LEFT JOIN notification.alert_llm_judgement r ON r.message_key = p.message_key AND r.phase = 'result'"
            " WHERE p.phase = 'pending' AND p.asked_at >= %s AND p.asked_at < %s ORDER BY p.seq DESC", (s, e)).fetchall()

    def stop_reason(self, conn: psycopg.Connection, now: datetime) -> str | None:
        calls = self._day_calls(conn, now)
        if len(calls) >= self.cap:
            return "cap"
        if any(c["status"] in STOP_STATUSES for c in calls):
            return "stopped_http"
        last = calls[:self._max_consecutive]
        if len(last) == self._max_consecutive and all(c["status"] != "ok" for c in last):
            return "breaker"
        return None

    def day_status(self, conn: psycopg.Connection, now: datetime) -> dict:
        return {"model": self.client.model, "cap": self.cap, "calls_today": len(self._day_calls(conn, now)), "stopped": self.stop_reason(conn, now)}

    def summary(self, conn: psycopg.Connection, now: datetime) -> dict:
        return {**self.day_status(conn, now), "calls": self.stats["calls"], "failed": self.stats["failed"], "reused": self.stats["reused"],
                "judged": self.stats["judged"], "lost": self.stats["lost"], "skipped": dict(self.skipped)}

    # ---- one message ---------------------------------------------------------------------------------------------------------
    def industries(self, conn: psycopg.Connection) -> list[str]:
        if self._industries is None:
            self._industries = [r["display_name"] for r in conn.execute(
                "SELECT DISTINCT display_name FROM core.taxonomy_node WHERE taxonomy_id=%s ORDER BY display_name", ("legacy_boards_industry",))]
        return self._industries

    def _append(self, conn: psycopg.Connection, *, call_id: uuid.UUID, phase: str, key: str, claim: T.ClaimView, now: datetime, prompt_hash: str,
                status: str, raw_text: str | None = None, usage: dict | None = None, judgement: dict | None = None) -> uuid.UUID:
        jid = uuid.uuid4()
        conn.execute(
            "INSERT INTO notification.alert_llm_judgement(judgement_id, call_id, phase, message_key, claim_id, claim_revision_id, asked_at, model,"
            " prompt_version, prompt_hash, status, raw_text, usage, judgement) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (jid, call_id, phase, key, uuid.UUID(claim.claim_id), uuid.UUID(claim.claim_revision_id), now, self.client.model, LT.PROMPT_VERSION,
             prompt_hash, status, raw_text, Jsonb(usage or {}), Jsonb(judgement) if judgement is not None else None))
        return jid

    def judge(self, conn: psycopg.Connection, claim: T.ClaimView, names: dict[str, dict], now: datetime) -> LT.Judgement | None:
        """The stored or freshly asked judgement of this message; ``None`` = none exists (skipped, failed, unparsable or lost)."""
        key = LT.message_key(claim)
        try:
            self.client.check_model()                                 # before the pending row: a model outside the allow-list leaves no trace and no call
        except ModelNotAllowed:
            self.skipped["model_not_allowed"] += 1
            return None
        with conn.transaction():                                      # short: check, then reserve the call with a pending row
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            rows = conn.execute("SELECT phase, judgement_id, model, judgement FROM notification.alert_llm_judgement WHERE message_key=%s", (key,)).fetchall()
            if rows:                                                  # asked before (a pending row without a result is an asked message too)
                self.stats["reused"] += 1
                done = next((r for r in rows if r["phase"] == "result" and r["judgement"] is not None), None)
                return None if done is None else LT.Judgement.from_json(done["judgement"], judgement_id=str(done["judgement_id"]), model=done["model"])
            reason = self.stop_reason(conn, now)
            if reason is not None:
                self.skipped[reason] += 1
                return None
            candidates = LT.candidates_of(claim, names)
            industries = self.industries(conn)
            prompt = LT.build_prompt(claim, candidates, industries)
            phash = LT.prompt_hash(prompt)
            call_id = uuid.uuid4()
            self._append(conn, call_id=call_id, phase="pending", key=key, claim=claim, now=now, prompt_hash=phash, status="pending")
        # ---- the paid call: no transaction, no lock ---------------------------------------------------------------------------
        if self._last_call is not None and self._pace > 0:
            wait = self._pace - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self.stats["calls"] += 1
        status, raw_text, usage, parsed = "error_unknown", None, {}, None
        try:
            reply = self.client.ask(LT.SYSTEM_PROMPT, prompt)
            self._last_call = time.monotonic()
            status = reply.status
            if status == "ok":
                raw_text = LT.clean_text(reply.text)[:RAW_TEXT_LIMIT]
                usage = LT.clean_json(reply.usage)
                parsed = LT.parse_answer(reply.text, candidates, set(industries))
                if parsed is None:
                    status = "unparsed"
        except Exception as exc:                                      # anything after (or in) the call: keep the row, never propagate
            self._last_call = time.monotonic()
            status, raw_text, usage, parsed = f"error_{type(exc).__name__}", None, {}, None
        if status != "ok":
            self.stats["failed"] += 1
        stored = self._store_result(conn, call_id, key, claim, now, phash, status, raw_text, usage, parsed)
        if stored is None or stored[0] != "ok" or parsed is None:
            return None
        self.stats["judged"] += 1
        return LT.Judgement.from_json(parsed.to_json(), judgement_id=stored[1], model=self.client.model)

    def _store_result(self, conn, call_id, key, claim, now, phash, status, raw_text, usage, parsed) -> tuple[str, str] | None:
        """Append the result row (status, row id). If that fails, append a bare ``error_<Class>`` row; if that fails too, log and
        return ``None``: the pending row stays, the message is not asked again and the call still counts."""
        attempt = (status, raw_text, usage, parsed.to_json() if parsed is not None else None)
        for _ in range(2):
            try:
                with conn.transaction():
                    jid = self._append(conn, call_id=call_id, phase="result", key=key, claim=claim, now=now, prompt_hash=phash,
                                       status=attempt[0], raw_text=attempt[1], usage=attempt[2], judgement=attempt[3])
                return attempt[0], str(jid)
            except Exception as exc:
                LOG.warning("alert llm: result row not written (%s)", type(exc).__name__)
                attempt = (f"error_{type(exc).__name__}", None, {}, None)
        self.stats["lost"] += 1
        return None
