"""Generate the product-extension contract from the package's extension cards (W01 contract-owner tool).

Why: REFERENCE_RECONCILIATION_V32 §12 and review_v32/product_extension_cards.json require the 41 proposed
product operations (PEX01-PEX16) to enter ONE contract before implementation. This tool turns the cards'
typed field lists into:
  src/pulse/contracts/data/extension.schema.json   ($id urn:pulse:contracts:ext:v1; $defs for PE types,
                                                    <op>Request and <op>Response per operation)
  src/pulse/contracts/data/extension.openapi.json  (paths /v1/x/<kebab-op>; GET for queries, POST for commands)
The v3.0.0 public contract stays byte-identical; extension types reference it by $ref.
Rules kept from the cards: required vs nullable are separate; Decimal stays the v3 decimal string;
finite_float is a JSON number (only for statistics the cards declare as floats).
Run: uv run python tools/build_extension_contract.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "spec_package" / "review_v32" / "product_extension_cards.json"
OUT = ROOT / "src" / "pulse" / "contracts" / "data"
EXT_ID = "urn:pulse:contracts:ext:v1"
V3 = "urn:pulse:contracts:v3"
SUP = "urn:pulse:contracts:supplemental:v31"
EXT_VERSION = "3.0.0+ext.1"


def type_schema(t: str) -> dict:
    if t.endswith("[]"):
        return {"type": "array", "items": type_schema(t[:-2])}
    if t == "string":
        return {"type": "string"}
    if t == "boolean":
        return {"type": "boolean"}
    if t == "finite_float":
        return {"type": "number"}
    ns, _, name = t.partition(".")
    if ns == "v3":
        return {"$ref": f"{V3}#/$defs/{name}"}
    if ns == "supplemental":
        return {"$ref": f"{SUP}#/$defs/{name}"}
    if ns == "PE":
        return {"$ref": f"#/$defs/{name}"}
    raise ValueError(f"unknown field type {t!r}")


def record(fields: list[dict], description: str | None = None) -> dict:
    props, required = {}, []
    for f in fields:
        s = type_schema(f["type"])
        if f.get("nullable"):
            s = {"anyOf": [s, {"type": "null"}]}
        if f.get("meaning"):
            s = {**s, "description": f["meaning"]} if "$ref" not in s else {"allOf": [s], "description": f["meaning"]}
        props[f["name"]] = s
        if f.get("required"):
            required.append(f["name"])
    out = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    if description:
        out["description"] = description
    return out


def kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def main() -> None:
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    defs: dict = {}
    for name, t in cards["types"].items():
        if t["kind"] == "enum":
            defs[name] = {"type": "string", "enum": t["values"]}
        else:
            defs[name] = record(t["fields"], "; ".join(t.get("semantic_constraints") or []) or None)
    paths: dict = {}
    for card in cards["cards"]:
        for op in card["proposed_operations"]:
            oid = op["proposed_operation_id"]
            kind = op["operation_kind"]
            defs[f"{oid}Request"] = record(op.get("input_fields", []), f"{card['card_id']} {card['resource']}")
            defs[f"{oid}Data"] = record(op.get("output_fields", []))
            defs[f"{oid}Response"] = {
                "type": "object", "additionalProperties": False, "required": ["meta", "data"],
                "properties": {"meta": {"$ref": f"{V3}#/$defs/ResponseMeta"}, "data": {"$ref": f"#/$defs/{oid}Data"}}}
            method = "get" if kind in ("query", "pure_query", "transport_adapter") else "post"
            entry = {
                "operationId": oid, "summary": f"{card['card_id']} {card['resource']}",
                "x-card": card["card_id"], "x-operation-kind": kind,
                "x-authorization": op.get("authorization"), "x-idempotency": op.get("idempotency"),
                "x-acceptance": op.get("acceptance", []),
                "responses": {"200": {"description": "typed response", "content": {"application/json": {
                    "schema": {"$ref": f"{EXT_ID}#/$defs/{oid}Response"}}}}},
            }
            if method == "post":
                entry["requestBody"] = {"required": True, "content": {"application/json": {
                    "schema": {"$ref": f"{EXT_ID}#/$defs/{oid}Request"}}}}
                entry["parameters"] = [{"name": "Idempotency-Key", "in": "header", "required": True,
                                        "schema": {"type": "string", "minLength": 1}}]
            else:
                entry["x-query-schema"] = f"{EXT_ID}#/$defs/{oid}Request"
            paths[f"/v1/x/{kebab(oid)}"] = {method: entry}
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": EXT_ID,
              "title": "Pulse product extension contract (PEX01-PEX16)", "x-contract-version": EXT_VERSION,
              "description": "Generated by tools/build_extension_contract.py from the package extension cards; "
                             "v3.0.0 stays unchanged and is referenced, never copied.",
              "$defs": defs}
    api = {"openapi": "3.1.0", "info": {"title": "Pulse extension operations", "version": EXT_VERSION},
           "paths": paths}
    (OUT / "extension.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "extension.openapi.json").write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(paths)} operations, {len(defs)} defs")


if __name__ == "__main__":
    main()
