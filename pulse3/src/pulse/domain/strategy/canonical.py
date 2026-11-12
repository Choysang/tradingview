"""Canonical hashing and deterministic identities shared by the decision domains.

Why: contexts, plan bodies, definitions and explanations are content-addressed so
that a re-run is provably the same thing (SPEC §9, STRATEGY_LEAGUE_SPEC §3).
Invariants: same value -> same bytes -> same sha256; ``Decimal``/``UUID``/``datetime``
are rendered through the contract wire form first, never through ``repr``.
Not covered: RFC 8785 canonicalisation (the project policy is sorted keys,
compact separators, UTF-8, no NaN, as in reference_extensions/semantic_validation).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any
from uuid import UUID, uuid5

from pulse.contracts import to_wire

#: Namespace for ids minted by the decision domains (distinct from the catalog's own).
DECISION_NAMESPACE = UUID("6f0c2d1e-3b8a-5c7d-9e41-2a5b7c9d1e3f")

D = Decimal
_CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)


def dec_ctx() -> Context:
    """Project Decimal context (precision 50, ROUND_HALF_EVEN)."""
    return Context(prec=50, rounding=ROUND_HALF_EVEN)


def quantize12(value: Decimal) -> Decimal:
    """Wire precision of the contract ``Decimal`` type (12 fractional digits)."""
    with localcontext(dec_ctx()):
        return value.quantize(Decimal("0.000000000001"))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        to_wire(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_uuid(kind: str, *parts: object) -> UUID:
    """Deterministic UUID5 so redelivery of the same business fact reuses its id."""
    return uuid5(DECISION_NAMESPACE, kind + "|" + "|".join(str(p) for p in parts))


def sqrt_dec(value: Decimal) -> Decimal:
    with localcontext(dec_ctx()):
        return value.sqrt()


def with_ctx(fn: Callable[[], Any]) -> Any:
    with localcontext(dec_ctx()):
        return fn()
