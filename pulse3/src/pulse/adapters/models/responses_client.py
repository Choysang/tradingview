"""OpenAI Responses API client with web search, for the E092 live judgement (port of ``scripts/lab/llm.py`` + ``llm_ask.py``).

Why: the experiment needs one paid, web-searching call per name and a record of exactly what came back (text, response id, usage). The key is the most sensitive thing here.
Invariants: a 429 carries a ``detail`` (quota vs rate limit, numeric headers) so the stop row can tell our own pressure from other users of the key; the key comes from the file named by ``PULSE_OPENAI_KEY_FILE`` (plain UTF-8, no BOM, first line) and lives only in the request header and a private attribute; it is never
printed, logged, stored, put in a URL, repr'd or placed in an error. An error carries only a label (an HTTP status or an exception class name), never the vendor's body (a 401 body quotes
part of the key). 401, 403, 429 and a model refusal raise ``CallStop`` (the caller stops the day); any other failure raises ``CallFailed``; a timeout is ``CallFailed("timeout")`` so the
caller can ask once more. The transport is injectable (tests use ``httpx.MockTransport`` and never touch the network).
Not covered: pacing between calls, retries (the caller decides), pricing (``domain.llm_live.rules``).
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ENV_KEY_FILE = "PULSE_OPENAI_KEY_FILE"
URL = "https://api.openai.com/v1/responses"
TOOLS = [{"type": "web_search"}]
CALL_TIMEOUT_S = 300.0
STOP_STATUS = (401, 403, 429)


class KeyProblem(RuntimeError):
    """The key cannot be used; ``code`` is ``no_key`` or ``bad_key`` (never the key)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CallStop(RuntimeError):
    """The vendor refused in a way that must stop the day: ``reason`` is ``http_401`` / ``http_403`` / ``http_429`` / ``refusal``."""

    def __init__(self, reason: str, reply: "Reply | None" = None, detail: dict | None = None):
        super().__init__(reason)
        self.reason, self.reply, self.detail = reason, reply, detail or {}


class CallFailed(RuntimeError):
    """One call failed; ``label`` is an HTTP status label, ``timeout`` or an exception class name. ``reply`` is set when a reply arrived but was unusable (its usage is still a cost)."""

    def __init__(self, label: str, reply: "Reply | None" = None):
        super().__init__(label)
        self.label, self.reply = label, reply


@dataclass(frozen=True)
class Reply:
    text: str
    response_id: str | None
    request_id: str | None
    usage: dict = field(default_factory=dict)
    searches: int = 0
    status: str | None = None
    seconds: float = 0.0


def read_key(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> str:
    """The key from the key file (first line, stripped). ``no_key``: not configured, missing or empty. ``bad_key``: not ASCII (a byte order mark from a PowerShell write would only fail at the vendor)."""
    env = os.environ if env is None else env
    p = path or env.get(ENV_KEY_FILE)
    if not p:
        raise KeyProblem("no_key")
    try:
        lines = Path(p).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        raise KeyProblem("no_key") from None
    key = lines[0].strip() if lines else ""
    if not key:
        raise KeyProblem("no_key")
    if not key.isascii() or any(ch.isspace() for ch in key):
        raise KeyProblem("bad_key")
    return key


def _parse(data: dict, seconds: float, request_id: str | None) -> Reply:
    """The reply of a 200 response. A structure that is not what the API documents (non-dict items, a non-list output) is a ``malformed_reply`` failure that still carries the usage."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    output = data.get("output")
    texts, searches, refused, malformed = [], 0, False, False
    if output is not None and not isinstance(output, list):
        malformed = True
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            malformed = True
            continue
        kind = item.get("type")
        if kind == "web_search_call":
            searches += 1
        elif kind == "message":
            parts = item.get("content")
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    malformed = True
                elif part.get("type") == "refusal":
                    refused = True
                elif part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            if parts is not None and not isinstance(parts, list):
                malformed = True
    top = data.get("output_text")
    text = "".join(texts) or (top if isinstance(top, str) else "")
    status = data.get("status") if isinstance(data.get("status"), str) else None
    rid = data.get("id") if isinstance(data.get("id"), str) else None
    reply = Reply(text, rid, request_id, usage, searches, status, round(seconds, 1))
    details = data.get("incomplete_details")
    if refused or (isinstance(details, dict) and details.get("reason") == "content_filter"):
        raise CallStop("refusal", reply)
    if malformed:
        raise CallFailed("malformed_reply", reply)
    if status not in (None, "completed"):
        raise CallFailed(f"status_{status}"[:40], reply)
    return reply


_SAFE = re.compile(r"[A-Za-z0-9_.-]{1,48}")


def rate_limit_detail(response: httpx.Response) -> dict:
    """What a 429 says about its cause, without keeping the body (it may quote the key): ``kind`` is ``quota`` (billing / insufficient quota), ``rate_limit`` (requests or tokens per minute)
    or ``unknown``; plus the numeric ``retry_after`` / remaining-limit headers and the short machine ``error_type`` / ``error_code`` when they look like identifiers."""
    detail: dict = {}
    headers = response.headers
    for key, name in (("retry_after", "retry-after"), ("remaining_requests", "x-ratelimit-remaining-requests"), ("remaining_tokens", "x-ratelimit-remaining-tokens")):
        raw = headers.get(name)
        try:
            if raw is not None:
                detail[key] = float(raw)
        except ValueError:
            pass
    etype = ecode = ""
    text = ""
    try:
        err = response.json().get("error")
        if isinstance(err, dict):
            etype, ecode, text = str(err.get("type") or ""), str(err.get("code") or ""), str(err.get("message") or "")
    except (ValueError, AttributeError):
        pass
    for key, val in (("error_type", etype), ("error_code", ecode)):
        if val and _SAFE.fullmatch(val) and "sk-" not in val:
            detail[key] = val
    blob = f"{etype} {ecode} {text}".lower()
    if "quota" in blob or "billing" in blob or "credit" in blob:
        detail["kind"] = "quota"
    elif "rate_limit" in blob or "rate limit" in blob or detail.get("remaining_requests") == 0 or detail.get("remaining_tokens") == 0 or "retry_after" in detail:
        detail["kind"] = "rate_limit"
    else:
        detail["kind"] = "unknown"
    return detail


class ResponsesClient:
    def __init__(self, key: str, *, url: str = URL, transport: httpx.BaseTransport | None = None, timeout: float = CALL_TIMEOUT_S):
        self.__key = key
        self._url, self._transport, self._timeout = url, transport, timeout

    def __repr__(self) -> str:
        return f"ResponsesClient(url={self._url!r})"

    def ask(self, prompt: str, *, model: str, effort: str, system: str, max_output_tokens: int | None = None) -> Reply:
        body = {"model": model, "input": prompt, "instructions": system, "reasoning": {"effort": effort}, "tools": TOOLS}
        if max_output_tokens is not None:                              # counts reasoning tokens too: a bound on what one call can cost
            body["max_output_tokens"] = max_output_tokens
        t0 = time.monotonic()
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as c:
                r = c.post(self._url, json=body, headers={"Authorization": f"Bearer {self.__key}"})
        except httpx.TimeoutException:
            raise CallFailed("timeout") from None
        except httpx.HTTPError as exc:                               # the message of a transport error may quote the URL or headers: the class name only
            raise CallFailed(type(exc).__name__) from None
        if r.status_code in STOP_STATUS:
            raise CallStop(f"http_{r.status_code}", detail=rate_limit_detail(r) if r.status_code == 429 else None)
        if r.status_code != 200:
            raise CallFailed(f"http_{r.status_code}")
        try:
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise CallFailed("malformed_reply") from None
        return _parse(data, time.monotonic() - t0, r.headers.get("x-request-id"))
