"""The live web-search LLM experiments that share this lane: E092 (closed) and E100 (open), each with its own frozen document, model and log.

Why: E092 (``gpt-6.1-terra``) cost about 0.27-0.30 USD a call and was closed by the user after one decision day (2026-11-23); E100 repeats it with ``gpt-6-luna`` (pre-registration
2026-11-23: sections 2-3 of E092 unchanged except the model, the 5 USD daily cap and a cost probe). Everything that differs between them is data here; the prompt, the universe rules, the
caps, the deadline and the judge are the same code for both (``rules``, ``judge``).
Invariants: an experiment's log is its own dataset (counts, caps, the 20-day stop, the judge and the status never mix); a closed experiment never asks (``decide`` and ``probe`` refuse; its
existing rows can still be tracked, judged and read); the frozen text is the part of the packaged document before the experiment's section heading, hashed with the experiment's own rule.
Not covered: running anything (``application.bf_llm_live_service``).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from . import rules as R

DEFAULT_EXPERIMENT = "E100"


def _hash_before(marker: bytes) -> Callable[[bytes], str]:
    """sha256 of the document's bytes (LF newlines) before the first ``marker`` (the whole document while the marker is absent)."""
    def frozen_hash(raw: bytes) -> str:
        b = raw.replace(b"\r\n", b"\n")
        at = b.find(marker)
        return hashlib.sha256(b if at < 0 else b[:at]).hexdigest()
    return frozen_hash


@dataclass(frozen=True)
class Experiment:
    key: str
    model: str
    effort: str
    service_tier: str | None            # None = the vendor's default tier (no ``service_tier`` in the request)
    dataset: str                        # the experiment's own log
    spec_path: str                      # repository path of the pre-registration
    spec_file: str                      # its name under ``pulse/research_specs``
    spec_sha256: str
    frozen_hash: Callable[[bytes], str]
    title: str
    closed: str | None = None           # why no further request may be made (None = open)


E092 = Experiment(
    key="E092", model="gpt-6.1-terra", effort="high", service_tier=None, dataset="llm_live_log", spec_path=R.SPEC_PATH, spec_file="2026-11-09-e092-llm-live-preregistration.md",
    spec_sha256=R.SPEC_SHA256, frozen_hash=R.frozen_hash, title="Live web-search LLM judgement of the fusion top 30 (main board)",
    closed="closed by the user 2026-11-23 (one decision day, too expensive); its rows can still be tracked, judged and read")

E100 = Experiment(
    key="E100", model="gpt-6-luna", effort="high", service_tier=None, dataset="llm_live_log_e100", spec_path="docs/research/2026-11-23-e100-llm-live-luna-preregistration.md",
    spec_file="2026-11-23-e100-llm-live-luna-preregistration.md", spec_sha256="b8eba28e6ed09a5cba59a34348ccd8536d2759f1c02c242779277ae855bc9139",
    frozen_hash=_hash_before("## 5. 冻结".encode("utf-8")), title="Live web-search LLM judgement of the fusion top 30 with gpt-6-luna (main board)")

EXPERIMENTS: dict[str, Experiment] = {e.key: e for e in (E092, E100)}


def get(key: str) -> Experiment:
    try:
        return EXPERIMENTS[key]
    except KeyError:
        raise ValueError(f"unknown experiment {key!r}; expected one of {', '.join(EXPERIMENTS)}") from None
