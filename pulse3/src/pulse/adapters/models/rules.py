"""Deterministic rules provider (always available), spec FINAL §7.

Why: the platform must produce claims without any external model. This provider runs the keyword rules of
``domain.intelligence.claims`` and returns the same enums-and-spans structure an LLM provider must return,
so the downstream code path (guard -> derive -> verify) is identical for both.

Invariants: no network, no randomness, no clock; identical inputs give identical output; materials arrive as
``NewsRecord`` objects in ``request.inputs``. Not covered: free-text understanding beyond keyword rules.
"""
from __future__ import annotations

from pulse.domain.intelligence import claims

from .port import ModelRequest, ProviderResult

LOCATOR = {"provider": "rules", "implementation": claims.EXTRACTION_VERSION}


class RulesProvider:
    provider_id = "rules"

    def run(self, request: ModelRequest) -> ProviderResult:
        out = []
        for m in request.manifest:
            rec = request.inputs[m.ref]
            out.append({"document_ref": m.ref, "claims": [c.to_structured() for c in claims.extract_rules(rec)]})
        return ProviderResult({"extractions": out}, {"tokens": "not_applicable", "cost": None, "implementation": claims.EXTRACTION_VERSION})
