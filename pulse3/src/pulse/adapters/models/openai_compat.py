"""Optional OpenAI-compatible chat provider, enabled only by ``PULSE_OPENAI_KEY_FILE`` (spec FINAL §7).

Why: a language model can label documents better than keyword rules, but only through the same guarded
contract. The key is read from a file whose path is in the environment; it is read at call time, held only in
the request headers and never logged, stored, repr'd or placed in an error message.

Invariants: ``from_env`` returns ``None`` unless the variable names a readable non-empty file (so the system
runs without it); the transport is injectable (tests use a fake transport and never touch the network); the
reply must be JSON conforming to the policy's response schema (``port.guard_output`` rejects anything else);
usage is recorded as reported or ``unknown``. Not covered: streaming, tool calls, retries, rate pacing.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Mapping

import httpx

from pulse.domain.intelligence import schemas

from .port import CLAIM_EXTRACTION_PROMPT, ModelRequest, ProviderError, ProviderResult

ENV_KEY_FILE = "PULSE_OPENAI_KEY_FILE"
ENV_BASE_URL = "PULSE_OPENAI_BASE_URL"
ENV_MODEL = "PULSE_OPENAI_MODEL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatProvider:
    provider_id = "openai_compat"

    def __init__(self, *, key_file: Path, base_url: str = DEFAULT_BASE_URL, model: str, transport: httpx.BaseTransport | None = None):
        self._key_file = Path(key_file)
        self._base = base_url.rstrip("/")
        self._model = model
        self._transport = transport

    def __repr__(self) -> str:  # never include the key file content
        return f"OpenAICompatProvider(base_url={self._base!r}, model={self._model!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None) -> "OpenAICompatProvider | None":
        env = os.environ if env is None else env
        path = env.get(ENV_KEY_FILE)
        if not path:
            return None
        p = Path(path)
        try:
            if not p.is_file() or not p.read_text(encoding="utf-8").strip():
                return None
        except OSError:
            return None
        return cls(key_file=p, base_url=env.get(ENV_BASE_URL, DEFAULT_BASE_URL), model=env.get(ENV_MODEL, "unset-model-name"), transport=transport)

    @property
    def locator(self) -> dict:
        return {"provider": self.provider_id, "base_url": self._base, "model": self._model}

    def _key(self) -> str:
        try:
            key = self._key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderError("key_file_unreadable") from exc
        if not key:
            raise ProviderError("key_file_empty")
        return key

    @staticmethod
    def _documents(request: ModelRequest) -> list[dict]:
        docs = []
        for m in request.manifest:
            rec = request.inputs[m.ref]
            docs.append({"ref": m.ref, "title": rec.title, "body": rec.body, "stock_refs": list(rec.stock_refs)})
        return docs

    def run(self, request: ModelRequest) -> ProviderResult:
        schema = schemas.SCHEMAS[(request.policy.response_contract_id, request.policy.response_schema_version)]
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": CLAIM_EXTRACTION_PROMPT},
                         {"role": "user", "content": json.dumps({"documents": self._documents(request)}, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "claim_extraction", "schema": schema, "strict": True}},
            "max_tokens": min(request.policy.token_budget, 4000),
            "temperature": 0,
        }
        t0 = time.monotonic()
        try:
            with httpx.Client(transport=self._transport, timeout=request.policy.timeout_seconds) as c:
                r = c.post(f"{self._base}/chat/completions", json=payload, headers={"Authorization": f"Bearer {self._key()}"})
        except httpx.HTTPError as exc:
            raise ProviderError("transport_error", type(exc).__name__) from exc
        if r.status_code >= 400:
            raise ProviderError(f"http_{r.status_code}")
        try:
            body = r.json()
            content = body["choices"][0]["message"]["content"]
            output = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("malformed_reply") from exc
        usage = body.get("usage") or {}
        return ProviderResult(output, {"latency_ms": int((time.monotonic() - t0) * 1000), "tokens": usage if usage else "unknown",
                                       "cost": None, "model": self._model})
