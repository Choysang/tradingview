"""One allow-list of models for every paid OpenAI call (E092/E100 live judgement and the alert fallback).

Why: the user decided (2026-11-23) that every OpenAI API call uses ``gpt-6-luna``. A per-client default is not a guarantee (an environment variable such as ``PULSE_ALERT_MODEL`` can name
anything), so both clients ask this guard before they build a request.
Invariants: the allow-list is read from ``PULSE_LLM_ALLOWED_MODELS`` (comma separated, exact model ids, blanks ignored) at CALL time; unset or empty means ``gpt-6-luna`` only; a model outside the
list raises ``ModelNotAllowed`` before any network I/O and before any ledger row (callers check on construction and again before a request); the message names the model and the list, never a key.
Not covered: prices (``domain.llm_live.rules``), which experiment may run (``domain.llm_live.experiments``).
"""
from __future__ import annotations

import os
from collections.abc import Mapping

ENV_ALLOWED = "PULSE_LLM_ALLOWED_MODELS"
DEFAULT_ALLOWED = ("gpt-6-luna",)


class ModelNotAllowed(RuntimeError):
    """The model is not in ``PULSE_LLM_ALLOWED_MODELS``; nothing was sent and nothing was written."""

    def __init__(self, model: str, allowed: tuple[str, ...]):
        super().__init__(f"model {model!r} is not allowed (allowed: {', '.join(allowed)}; set {ENV_ALLOWED})")
        self.model, self.allowed = model, allowed


def allowed_models(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if env is None else env
    given = tuple(m.strip() for m in (env.get(ENV_ALLOWED) or "").split(",") if m.strip())
    return given or DEFAULT_ALLOWED


def require_allowed(model: str, env: Mapping[str, str] | None = None) -> str:
    """``model`` when it is in the allow-list, else ``ModelNotAllowed``."""
    allowed = allowed_models(env)
    if not isinstance(model, str) or model not in allowed:
        raise ModelNotAllowed(str(model), allowed)
    return model
