"""Single public contract (v3.0.0) and its validators.

Why this module exists: every API response, internal payload and fixture must be
checked against the one contract in ``data/`` (copied verbatim from the v3.2
package). No other module may define a parallel wire shape.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_VERSION = "3.0.0"
V3_ID = "urn:pulse:contracts:v3"
SUPPLEMENTAL_ID = "urn:pulse:contracts:supplemental:v31"
EXT_ID = "urn:pulse:contracts:ext:v1"  # product extension cards PEX01-PEX16 (tools/build_extension_contract.py)


def _load(name: str) -> dict[str, Any]:
    text = resources.files(__package__).joinpath("data", name).read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def v3_schema() -> dict[str, Any]:
    return _load("v3.schema.json")


@lru_cache(maxsize=1)
def openapi() -> dict[str, Any]:
    return _load("openapi.json")


@lru_cache(maxsize=1)
def api_bindings() -> dict[str, Any]:
    return _load("api_bindings.json")


@lru_cache(maxsize=1)
def _registry() -> Registry:
    resources_ = [
        (V3_ID, Resource.from_contents(v3_schema())),
        (SUPPLEMENTAL_ID, Resource.from_contents(_load("supplemental.schema.json"))),
        (EXT_ID, Resource.from_contents(_load("extension.schema.json"))),
    ]
    return Registry().with_resources(resources_)


@lru_cache(maxsize=512)
def validator(definition: str, schema_id: str = V3_ID) -> Draft202012Validator:
    """Validator for one ``$defs`` entry of a registered contract."""
    root = {V3_ID: v3_schema, SUPPLEMENTAL_ID: lambda: _load("supplemental.schema.json"),
            EXT_ID: lambda: _load("extension.schema.json")}[schema_id]()
    if definition not in root["$defs"]:
        raise KeyError(f"unknown contract definition {definition!r} in {schema_id}")
    ref_schema = {"$ref": f"{schema_id}#/$defs/{definition}"}
    return Draft202012Validator(ref_schema, registry=_registry(), format_checker=FormatChecker())


class ContractViolation(ValueError):
    def __init__(self, definition: str, errors: list[str]):
        self.definition = definition
        self.errors = errors
        super().__init__(f"{definition}: {len(errors)} contract error(s): " + "; ".join(errors[:5]))


def check(definition: str, payload: Any, schema_id: str = V3_ID) -> Any:
    """Raise ContractViolation unless ``payload`` (already wire-encoded) matches."""
    errs = sorted(validator(definition, schema_id).iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errs:
        raise ContractViolation(definition, [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errs])
    return payload


def decimal_str(value: Decimal | int | str | None) -> str | None:
    """Wire form of a decimal: plain string, no exponent, signed zero as "0"."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d.is_zero():
        return "0"
    text = format(d.normalize(), "f")
    return text


def utc_z(value: datetime | None) -> str | None:
    """Wire form of an instant: UTC with trailing Z. Naive datetimes are rejected."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime cannot cross the contract boundary")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_wire(obj: Any) -> Any:
    """Recursively encode Decimal/datetime/date/UUID into the contract wire form."""
    if isinstance(obj, Decimal):
        return decimal_str(obj)
    if isinstance(obj, datetime):
        return utc_z(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: to_wire(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    return obj
