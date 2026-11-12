"""Model capability port: policy, request, provider protocol, hashes and output guard (spec FINAL §7, §5.1, RECON §8).

Why: language understanding is a replaceable capability behind a stable contract. A run is bound to an input
manifest, an ``analysis_as_of`` (nothing after it may be in the manifest), a model policy version (provider +
model locator + prompt digest + retrieval policy + response schema), and ends with an immutable structured
output plus its hash. Replay reads the stored output; it never re-calls a provider.

Invariants: the model's output is ENUMS + EVIDENCE SPANS ONLY - the response schemas forbid free text, and
``guard_output`` rejects any float (scores, probabilities and rankings are computed by code); every document
reference in the output must be a manifest entry; the manifest must not contain material available after
``analysis_as_of``. Providers never see credentials outside their own constructor.
Not covered: scheduling, retries, cost accounting beyond what the provider reports (unknown stays unknown).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pulse.domain.intelligence import schemas


class ProviderError(RuntimeError):
    """Provider failure. The message never contains credentials."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class OutputRejected(ValueError):
    """The structured output violates the response schema or the enums-and-refs-only rule."""


@dataclass(frozen=True)
class ManifestRef:
    kind: str  # document_revision | feature | snapshot | claim_revision | seat_report ...
    ref: str
    content_hash: str
    available_at: datetime

    def to_doc(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "content_hash": self.content_hash, "available_at": self.available_at.isoformat()}


@dataclass(frozen=True)
class ModelPolicy:
    model_policy_id: str
    version: int
    task_kind: str
    provider_id: str
    model_locator: dict
    response_contract_id: str
    response_schema_version: str
    prompt_digest: str
    retrieval_policy_version: str
    capability_requirements: dict = field(default_factory=dict)
    timeout_seconds: int = 60
    token_budget: int = 20000
    rollout_state: str = "active"


@dataclass(frozen=True)
class ModelRequest:
    policy: ModelPolicy
    manifest: tuple[ManifestRef, ...]
    analysis_as_of: datetime
    inputs: dict[str, Any] = field(default_factory=dict)  # ref -> in-memory material (not hashed; the manifest hashes cover it)

    def __post_init__(self) -> None:
        late = [m.ref for m in self.manifest if m.available_at > self.analysis_as_of]
        if late:
            raise ValueError(f"manifest holds material available after analysis_as_of: {late[:3]}")


@dataclass(frozen=True)
class ProviderResult:
    output: dict
    usage: dict


class ModelProvider(Protocol):
    provider_id: str

    def run(self, request: ModelRequest) -> ProviderResult: ...


def canonical_bytes(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def input_context_hash(req: ModelRequest) -> str:
    p = req.policy
    doc = {"policy": [p.model_policy_id, p.version, p.provider_id, p.model_locator, p.prompt_digest, p.retrieval_policy_version,
                      p.response_contract_id, p.response_schema_version],
           "analysis_as_of": req.analysis_as_of.isoformat(), "manifest": sorted((m.to_doc() for m in req.manifest), key=lambda d: (d["kind"], d["ref"]))}
    return sha256_hex(canonical_bytes(doc))


def _no_floats(x: Any, path: str = "") -> None:
    if isinstance(x, float):
        raise OutputRejected(f"float at {path or '/'}: scores and probabilities are computed by code, not emitted by a model")
    if isinstance(x, dict):
        for k, v in x.items():
            _no_floats(v, f"{path}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            _no_floats(v, f"{path}/{i}")


def guard_output(req: ModelRequest, output: dict) -> dict:
    """Schema + enums-and-refs-only guard. Raises OutputRejected."""
    p = req.policy
    try:
        schemas.validate(p.response_contract_id, p.response_schema_version, output)
    except schemas.SchemaViolation as exc:
        raise OutputRejected(str(exc)) from exc
    _no_floats(output)
    refs = {m.ref for m in req.manifest}
    for ex in output.get("extractions", []):
        if ex["document_ref"] not in refs:
            raise OutputRejected(f"document_ref {ex['document_ref']!r} is not in the input manifest")
    return output


def prompt_digest(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


CLAIM_EXTRACTION_PROMPT = (
    "You label short Chinese financial news items. For each document return ONLY the JSON object required by the schema: "
    "enum values (category, assertion_type, polarity_hint, attribution_kind), boolean denial, subject_refs copied verbatim from the "
    "document's stock_refs, and evidence_spans as character offsets into the document's title or body. Do not write sentences, "
    "numbers, scores or probabilities. If unsure use category 'none' and polarity_hint 'none'."
)


def claim_extraction_policy(provider_id: str, locator: dict, *, version: int = 1, rollout_state: str = "active") -> ModelPolicy:
    pid = {"rules": "news-claim-extraction-rules"}.get(provider_id, f"news-claim-extraction-{provider_id}")
    return ModelPolicy(pid, version, "claim_extraction", provider_id, locator, "pulse.intel.claim_extraction", "1",
                       prompt_digest(CLAIM_EXTRACTION_PROMPT), "single-document-no-retrieval-v1",
                       {"structured_output": True, "tools": False}, 60, 20000, rollout_state)
