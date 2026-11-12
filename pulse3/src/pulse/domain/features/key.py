"""FeatureKey: the 19-dimension computation identity (contract v3 FeatureKey).

Why: a bare indicator name cannot distinguish stock, timeframe, adjustment, input
snapshot or implementation. Every consumer builds the same key so results are
shared, never recomputed under another name.

Invariants: no implicit defaults (every dimension is an explicit non-empty
value); ``decision_as_of`` is timezone-aware and normalized to UTC (UTC and
UTC+8 spellings of one instant give one key); ``instrument_ids`` keeps its
declared order; the hash covers all 19 dimensions. A key does NOT enforce PIT
filtering of inputs - the data query must.
Not covered: persistence (market_repo.save_feature_result) and scheduling.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any


def parameter_digest(parameters: dict[str, Any]) -> str:
    """Hash canonical JSON parameters; reject non-string keys and non-JSON/non-finite values."""
    def check(value: Any) -> None:
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                raise TypeError("parameter object keys must be strings")
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise TypeError("parameters must contain JSON values only")

    if not isinstance(parameters, dict):
        raise TypeError("parameters must be a JSON object")
    check(parameters)
    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureKey:
    feature_id: str
    definition_version: str
    implementation_digest: str
    parameter_digest: str
    instrument_ids: tuple[str, ...]
    timeframe: str
    adjustment_mode: str
    snapshot_id: str
    decision_as_of: datetime
    availability_policy_version: str
    session_policy_version: str
    input_revision: str
    input_schema_version: str
    dependency_digest: str
    universe_policy_version: str
    calendar_version: str
    adjustment_version: str
    market_rules_version: str
    numeric_policy_version: str

    def __post_init__(self) -> None:
        for f in fields(self):
            name, value = f.name, getattr(self, f.name)
            if name == "decision_as_of":
                if not isinstance(value, datetime) or value.utcoffset() is None:
                    raise ValueError("decision_as_of must be a timezone-aware datetime")
                object.__setattr__(self, name, value.astimezone(timezone.utc))
            elif name == "instrument_ids":
                if not isinstance(value, tuple) or not value:
                    raise ValueError("instrument_ids must be a nonempty tuple")
                if any(not isinstance(x, str) or not x.strip() for x in value):
                    raise ValueError("instrument identifiers must be nonempty strings")
                if len(set(value)) != len(value):
                    raise ValueError("duplicate instrument_ids are not allowed")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be an explicit nonempty string")

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "decision_as_of":
                v = v.isoformat().replace("+00:00", "Z")
            elif f.name == "instrument_ids":
                v = list(v)
            out[f.name] = v
        return out


def key_hash(key: FeatureKey) -> str:
    """feature_key_hash: sha256 over the canonical wire form of all 19 dimensions."""
    text = json.dumps(key.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
