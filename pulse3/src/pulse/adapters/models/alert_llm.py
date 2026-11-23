"""Responses-API client for the news-alert LLM fallback (legacy row #134; old ``news_alerts.LLMGate``).

Why: when the alert rules cannot decide a message that touches a holding or a watch-list name, one short question goes to a
model. This module is only the HTTP call; caps, dedupe, storage and tier mapping live in ``application.alert_llm_service``.
Invariants:
* off unless ``PULSE_OPENAI_KEY_FILE`` names a readable, non-empty file (``from_env`` returns ``None``); the file is plain
  UTF-8, first line = key (a BOM is tolerated);
* the key is read at call time, lives only in the request header, and is never stored, logged, repr'd or put in an error;
* the answer's status is a short label (``ok``, ``http_429``, ``error_ConnectTimeout``); an error response body is never
  returned (it can echo the key), only a 200 body's text and its ``usage`` object are;
* nothing is sent under pytest unless a ``transport`` is injected (``LlmDisabled``), like the webhook adapter.
Not covered: streaming, retries, pacing and caps (the service does those).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import httpx

from .openai_compat import DEFAULT_BASE_URL, ENV_BASE_URL, ENV_KEY_FILE

ENV_MODEL = "PULSE_ALERT_MODEL"
DEFAULT_MODEL = "gpt-6-luna"       # the old system's default (news_alerts.LLM_DEFAULT_MODEL)
TIMEOUT_SECONDS = 30.0


class LlmDisabled(RuntimeError):
    """A real call was attempted where it must never happen (tests without an injected transport)."""


@dataclass(frozen=True)
class LlmReply:
    status: str                                  # "ok" or a short failure label
    text: str = ""                               # the model's answer text (only for a 200 answer)
    usage: dict = field(default_factory=dict)    # as reported by the API, else empty


def read_key_file(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, ValueError):
        return None
    return (lines[0].strip() or None) if lines else None


class AlertLlmClient:
    def __init__(self, *, key_file: Path, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL,
                 transport: httpx.BaseTransport | None = None):
        self._key_file = Path(key_file)
        self.model = model
        self._url = base_url.rstrip("/") + "/responses"
        self._transport = transport

    def __repr__(self) -> str:                   # never the key file content
        return f"AlertLlmClient(model={self.model!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None) -> "AlertLlmClient | None":
        env = os.environ if env is None else env
        path = (env.get(ENV_KEY_FILE) or "").strip()
        if not path or read_key_file(Path(path)) is None:
            return None
        return cls(key_file=Path(path), model=(env.get(ENV_MODEL) or "").strip() or DEFAULT_MODEL,
                   base_url=(env.get(ENV_BASE_URL) or "").strip() or DEFAULT_BASE_URL, transport=transport)

    def ask(self, instructions: str, prompt: str) -> LlmReply:
        """One Responses-API call. Never raises for provider or network trouble; raises ``LlmDisabled`` for a forbidden call."""
        if self._transport is None and os.environ.get("PYTEST_CURRENT_TEST"):
            raise LlmDisabled("real LLM calls are disabled under pytest")
        key = read_key_file(self._key_file)
        if key is None:
            return LlmReply("error_key_file")
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT_SECONDS) as c:
                r = c.post(self._url, headers={"Authorization": f"Bearer {key}"},
                           json={"model": self.model, "instructions": instructions, "input": prompt})
            if r.status_code != 200:
                return LlmReply(f"http_{r.status_code}")
            data = r.json()
            if not isinstance(data, dict):
                return LlmReply("error_not_an_object")
            text = data.get("output_text") or "".join(
                p.get("text", "") for item in data.get("output") or [] if isinstance(item, dict) and item.get("type") == "message"
                for p in item.get("content") or [] if isinstance(p, dict) and p.get("type") in ("output_text", "text"))
            usage = data.get("usage")
            return LlmReply("ok", text if isinstance(text, str) else "", usage if isinstance(usage, dict) else {})
        except (httpx.HTTPError, ValueError) as exc:     # ValueError: the 200 body was not JSON
            return LlmReply(f"error_{type(exc).__name__}")
