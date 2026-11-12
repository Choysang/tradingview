"""Registered JSON Schemas for intelligence payloads (SS-G10: jsonb_typeof alone is not validation).

Why: claim ``object_payload``, event ``analysis_payload``, community ``analysis_payload`` and the structured
model output are free JSON in the DDL. Each is validated against a named, versioned schema before it is
written; the schemas are also registered in ``core.contract_version`` so provenance names the exact version.

Invariants: ``additionalProperties: false`` on model-facing output (enums + evidence spans only, no free
numeric score); every schema has a stable ``(contract_id, schema_version)``. Not covered: the v3 package
contracts (checked with ``pulse.contracts.check``).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from .claims import ASSERTION_TYPES, ATTRIBUTION_KINDS, CATEGORY_IDS, POLARITY

_SPAN = {"type": "object", "additionalProperties": False, "required": ["field", "start", "end"],
         "properties": {"field": {"enum": ["title", "body"]}, "start": {"type": "integer", "minimum": 0}, "end": {"type": "integer", "minimum": 0}}}
_CLAIM = {"type": "object", "additionalProperties": False,
          "required": ["category", "assertion_type", "polarity_hint", "subject_refs", "attribution_kind", "denial", "evidence_spans"],
          "properties": {"category": {"enum": list(CATEGORY_IDS) + ["none"]}, "assertion_type": {"enum": list(ASSERTION_TYPES)},
                         "polarity_hint": {"enum": list(POLARITY)}, "subject_refs": {"type": "array", "items": {"type": "string"}},
                         "attribution_kind": {"enum": list(ATTRIBUTION_KINDS)}, "denial": {"type": "boolean"},
                         "evidence_spans": {"type": "array", "minItems": 1, "items": _SPAN}}}

SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("pulse.intel.claim_extraction", "1"): {
        "type": "object", "additionalProperties": False, "required": ["extractions"],
        "properties": {"extractions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["document_ref", "claims"],
            "properties": {"document_ref": {"type": "string"}, "claims": {"type": "array", "items": _CLAIM}}}}}},
    ("pulse.intel.news_claim", "1"): {
        "type": "object", "required": ["headline", "category", "subject_key", "predicate", "numbers", "numbers_key", "sketch", "cluster", "verification"],
        "properties": {"headline": {"type": "string"}, "category": {"type": ["string", "null"]}, "subject_key": {"type": ["string", "null"]},
                       "predicate": {"type": "string"}, "numbers": {"type": "array"}, "numbers_key": {"type": "string"},
                       "sketch": {"type": "array", "items": {"type": "integer"}}, "cluster": {"type": "object"}, "verification": {"type": "object"}}},
    ("pulse.intel.impact_mapping", "1"): {
        "type": "object", "additionalProperties": False,
        "required": ["policy_version", "fact", "business_variables", "exposure_status", "exposed_instruments", "sector_hints", "horizon",
                     "falsifier", "competing_explanations", "calibration"],
        "properties": {
            "policy_version": {"type": "string"}, "fact": {"type": "object"},
            "business_variables": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["variable", "direction_prior", "direction_status"],
                                  "properties": {"direction_prior": {"enum": ["+", "-", "±", "unknown"]}, "direction_status": {"const": "uncalibrated_prior"}}}},
            "exposure_status": {"enum": ["named_subject", "sector_hypothesis_unmapped", "none"]},
            "exposed_instruments": {"type": "array"}, "sector_hints": {"type": "array"},
            "horizon": {"type": "object", "required": ["unit", "from", "to", "basis"]},
            "falsifier": {"type": "object", "required": ["observable"]},
            "competing_explanations": {"type": "array", "minItems": 1},
            "calibration": {"type": "object", "required": ["status", "probability"], "properties": {"status": {"const": "uncalibrated"}, "probability": {"type": "null"}}}}},
    ("pulse.intel.community_post", "1"): {
        "type": "object", "required": ["instrument_id", "subject_match", "bar_code", "title_hash", "duplicate_cluster", "metrics_basis"],
        "properties": {"instrument_id": {"type": "string"}, "subject_match": {"enum": ["own_stockbar", "other_stockbar"]}, "bar_code": {"type": "string"},
                       "title_hash": {"type": "string"}, "duplicate_cluster": {"type": "string"}, "metrics_basis": {"const": "attention_proxy_not_trading_flow"}}},
    ("pulse.intel.seat_style_evaluation", "1"): {
        "type": "object", "required": ["hypothesis_key", "as_of", "conditions", "n", "outcome"],
        "properties": {"hypothesis_key": {"type": "string"}, "as_of": {"type": "string"}, "conditions": {"type": "object"}, "n": {"type": "integer"}, "outcome": {"type": "object"}}},
}


class SchemaViolation(ValueError):
    def __init__(self, name: str, errors: list[str]):
        super().__init__(f"{name}: " + "; ".join(errors[:5]))
        self.errors = errors


@lru_cache(maxsize=None)
def _validator(contract_id: str, version: str) -> Draft202012Validator:
    return Draft202012Validator(SCHEMAS[(contract_id, version)])


def validate(contract_id: str, version: str, payload: Any) -> Any:
    errs = sorted(_validator(contract_id, version).iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errs:
        raise SchemaViolation(f"{contract_id}@{version}", [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errs])
    return payload
