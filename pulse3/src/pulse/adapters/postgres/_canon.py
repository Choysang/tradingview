"""Canonical JSON hashing shared by the W02 repositories.

Why: payload_hash, request_hash and release_hash must be reproducible by any producer, so they hash one
canonical text: contract wire form (Decimal/UTC/UUID encoded), sorted keys, no whitespace, UTF-8.
Invariant: equal logical documents hash equal regardless of key order or Python numeric types.
Not covered: JSON numbers beyond the safe-integer range (callers pass such values as strings).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pulse.contracts import to_wire


def canonical_json(value: Any) -> str:
    return json.dumps(to_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
