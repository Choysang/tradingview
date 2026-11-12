"""Claim confirmation: supported / contradicted / insufficient (/ disputed) from independent origins (spec FINAL §5.1).

Why: "三家转载不能算三个独立证人". Status is computed from evidence links by code, never by a model vote.

Invariants: an official disclosure by the subject issuer supports the claim that the issuer disclosed it
(one origin); otherwise ``supported`` needs >= ``MIN_INDEPENDENT_ORIGINS`` confirmed independent origins;
republications and unknown origins never add votes; a confirmed refutation without confirmed support is
``contradicted``; confirmed support AND confirmed refutation is ``disputed`` (both kept); forecasts are
``not_falsifiable_yet``; withdrawn/deleted evidence is ignored (the claim is recomputed without it).
``insufficient`` maps to the stored claim_status ``unverified``.
Not covered: truth of the content of an official disclosure; semantic contradiction detection.
"""
from __future__ import annotations

from dataclasses import dataclass

from .origin import CONFIRMED, INACTIVE_DOCUMENT_STATUS, OriginItem, summarize

POLICY_VERSION = "multi-origin-v1"
MIN_INDEPENDENT_ORIGINS = 2
CLAIM_STATUS = {"supported": "supported", "contradicted": "contradicted", "disputed": "disputed", "insufficient": "unverified",
                "not_falsifiable_yet": "not_falsifiable_yet"}


@dataclass(frozen=True)
class EvidenceItem:
    document_revision_id: str
    document_id: str
    source_id: str
    origin: OriginItem
    official: bool = False  # the document is the subject issuer's/regulator's own disclosure


@dataclass(frozen=True)
class Verification:
    confirmation: str  # supported | contradicted | insufficient | disputed | not_falsifiable_yet
    claim_status: str
    support_origins: int
    refute_origins: int
    official_support: bool
    official_refute: bool
    attributed_chains: tuple[str, ...]
    unknown_origin_documents: int
    reason: str

    def to_payload(self) -> dict:
        return {"policy_version": POLICY_VERSION, "confirmation": self.confirmation, "independent_support_origins": self.support_origins,
                "independent_refute_origins": self.refute_origins, "official_support": self.official_support,
                "official_refute": self.official_refute, "attributed_chains": list(self.attributed_chains),
                "unknown_origin_documents": self.unknown_origin_documents, "reason": self.reason}


def verify(items: list[EvidenceItem], assertion_type: str) -> Verification:
    live = [i for i in items if i.origin.document_status not in INACTIVE_DOCUMENT_STATUS]
    sup = summarize([i.origin for i in live], "supports")
    ref = summarize([i.origin for i in live], "refutes")
    off_sup = any(i.official and i.origin.relation == "supports" and i.origin.origin_status in CONFIRMED for i in live)
    off_ref = any(i.official and i.origin.relation == "refutes" and i.origin.origin_status in CONFIRMED for i in live)

    def out(conf: str, reason: str) -> Verification:
        return Verification(conf, CLAIM_STATUS[conf], sup.independent_origins, ref.independent_origins, off_sup, off_ref,
                            sup.attributed_chains, sup.unknown_origin_documents, reason)

    if assertion_type == "forecast":
        return out("not_falsifiable_yet", "forecast: no outcome observable yet")
    has_sup = off_sup or sup.independent_origins >= MIN_INDEPENDENT_ORIGINS
    has_ref = off_ref or ref.independent_origins >= 1
    if has_sup and has_ref:
        return out("disputed", "confirmed support and confirmed refutation both present")
    if has_ref:
        return out("contradicted", "confirmed refutation without confirmed support")
    if has_sup:
        return out("supported", "official disclosure" if off_sup else f">= {MIN_INDEPENDENT_ORIGINS} independent origins")
    if sup.independent_origins == 1:
        return out("insufficient", "one independent origin; at least two (or an official disclosure) are required")
    return out("insufficient", "no confirmed origin (reposts and unknown origins add no votes)")
