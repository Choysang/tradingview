"""Static v3.1 integration checks. No business algorithms or live services run.

Run from any cwd after installing requirements-validation.txt. Checks real SQL
AST columns, public response paths, schema resolution, and unchanged byte hashes.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urldefrag
import json
import re
import sys

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec
from pglast import ast, parse_sql
from referencing import Registry, Resource

PACKAGE = Path(__file__).resolve().parents[1]
EXPECTED_DEFINITIONS = {
    "CommunityForecastPayload", "CommunityOutcomePayload", "ConvertibleRelationPayload",
    "ChartLayerDescriptor", "MacdProfile",
}
EXPECTED_INPUT_HASH = "098815d8f096b69bb2497881083bbc4cd4618332623947c308c686b2aa15ec5e"
EXPECTED_BASELINE = {
    "contracts/v3.schema.json": "7f0e55a4dd430232dd5b97325b804326e1056c8a38e6130e822e0cc5ca63309b",
    "contracts/openapi.json": "8ed3c090c7b10a36998a0f3773c81c1509d772bb3ca5f7d1ffcd81ffd94713da",
    "contracts/api_bindings.json": "dfaa2ab0886cd0eabe31422e1580ac1f99097f57fbe21eb9d5809b6535525d87",
    "database/core_schema.sql": "7066cc83650ae07602a8585a91757eb448daae1bb5e123f983a02d77474274f2",
    "execution/ledger_schema.sql": "0639b4fa05274633691961e407911418cd5f8e3bad611bd26812e32411cd8e85",
    "sources_sectors/sources_sectors_schema.sql": "71f36e99075a7ccd26fcc1ec06afc2c256157ec36e336d3178705c1052e2b354",
    "integration/integration_schema.sql": "ce2c2189f8197416b0bdbb6f179b0e7d095907e5495f59973a8300be9df0c758",
    "execution/unified_ledger.py": "fe5f086a84ed61c9d0eb0cfded50d25416c94f5621ec54927b1710fe40b320dc",
}
SQL_FILES = [
    "database/core_schema.sql", "execution/ledger_schema.sql",
    "sources_sectors/sources_sectors_schema.sql", "integration/integration_schema.sql",
]
checks = []


def read(relative):
    return json.loads((PACKAGE / relative).read_text(encoding="utf-8"))


def check(name, condition, detail=None):
    row = {"check": name, "passed": bool(condition)}
    if detail is not None:
        row["detail"] = detail
    checks.append(row)


def attempt(name, callback):
    try:
        detail = callback()
    except Exception as exc:
        check(name, False, type(exc).__name__ + ": " + str(exc).splitlines()[0])
    else:
        check(name, True, detail)


def refs_in(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from refs_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from refs_in(child)


def pointer(root, fragment):
    current = root
    if fragment == "":
        return current
    if not fragment.startswith("/"):
        raise ValueError("Only JSON Pointer fragments are allowed here")
    for segment in fragment[1:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        current = current[int(segment)] if isinstance(current, list) else current[segment]
    return current


def dereference(node, document, base, depth=0):
    if depth > 100:
        raise ValueError("Unexpected cyclic property traversal")
    if "$ref" not in node:
        return node, document
    location, fragment = urldefrag(node["$ref"])
    if location in {base["$id"], "v3.schema.json"}:
        document = base
    elif location:
        raise ValueError("Undeclared external API reference: " + location)
    return dereference(pointer(document, fragment), document, base, depth + 1)


def normalized_reference_locations(value, base):
    """The unchanged OpenAPI generator copies $defs under components/schemas."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == "$ref" and child.startswith("#/components/schemas/"):
                result[key] = base["$id"] + "#/$defs/" + child[len("#/components/schemas/"):]
            elif key == "$ref" and child.startswith("#/$defs/"):
                result[key] = base["$id"] + child
            else:
                result[key] = normalized_reference_locations(child, base)
        return result
    if isinstance(value, list):
        return [normalized_reference_locations(child, base) for child in value]
    return value


def public_binding(binding, operations, openapi, base):
    segments = binding["public_path"].split(".")
    current = operations[segments[0]]["responses"]["200"]["content"]["application/json"]["schema"]
    document = openapi
    last_property = None
    for segment in segments[1:]:
        current, document = dereference(current, document, base)
        is_array = segment.endswith("[]")
        name = segment[:-2] if is_array else segment
        current = current["properties"][name]
        last_property = current
        if is_array:
            current, document = dereference(current, document, base)
            if current.get("type") != "array":
                raise ValueError("[] path segment is not an array: " + segment)
            current = current["items"]
    expected = base["$defs"][binding["base_definition"]]["properties"][binding["base_property"]]
    # Compare an actual response leaf with its declared base slot, not just names.
    got, _ = dereference(last_property, document, base)
    want, _ = dereference(expected, base, base)
    if normalized_reference_locations(got, base) != normalized_reference_locations(want, base):
        raise ValueError("Declared base slot differs from public response leaf")
    return {"public_path": binding["public_path"],
            "base_slot": binding["base_definition"] + "." + binding["base_property"]}


def fixtures():
    i = "11111111-1111-4111-8111-111111111111"
    d = "22222222-2222-4222-8222-222222222222"
    c = "33333333-3333-4333-8333-333333333333"
    h = "a" * 64
    artifact = {"ref": "fixture://static-schema-only", "content_hash": h,
                "contract_id": "fixture.binary_target", "schema_version": "1"}
    t0, t1, t2 = "2026-11-11T01:00:00Z", "2026-11-11T07:00:00Z", "2026-11-11T07:01:00Z"
    positives = {
        "CommunityForecastPayload": {
            "instrument_id": i, "target_spec_ref": artifact, "forecast_as_of": t0,
            "horizon_start_at": t0, "horizon_end_at": t1, "session_id": "fixture-session",
            "prediction_probability": "0.75", "probability_origin": "source_explicit",
            "source_document_revision_id": d, "source_evidence_ordinal": 1,
            "invalidation_condition": "Defined by the fixture target",
        },
        "CommunityOutcomePayload": {
            "claim_revision_id": c, "forecast_payload_hash": h, "target_spec_hash": h,
            "evaluation_snapshot_id": d, "scoring_policy_version": "brier_binary_decimal12_v1",
            "horizon_end_at": t1, "computed_at": t2, "label_state": "resolved",
            "label_available_at": t1, "label": 1, "brier_error": "0.0625", "reason": None,
        },
        "ConvertibleRelationPayload": {
            "underlying_instrument_id": i, "effective_from": t0, "effective_to": None,
        },
        "ChartLayerDescriptor": {
            "layer_id": "fixture-volume-profile", "layer_kind": "volume_profile",
            "required_capabilities": ["minute_bars"], "input_artifacts": [artifact],
            "data_status": "available", "derivation_kind": "estimated", "price_basis": "raw",
            "window_start_at": t0, "window_end_at": t1, "available_at": t2,
            "assumptions": ["Synthetic schema fixture, no real data"], "reason": None,
        },
        "MacdProfile": {
            "profile_version": "fixture", "timeframe": "1m", "fast_period": 12, "slow_period": 26,
            "signal_period": 9, "ema_seed": "sma_seed", "hist_multiplier": "2",
            "min_completed_bars": 100, "closed_bars_only": True, "pivot_left_bars": 3,
            "pivot_right_bars": 3, "pivot_available_rule": "right_closed_confirmation_only",
        },
    }
    rows = []
    for name, payload in positives.items():
        rows.append((name + "/valid", name, payload, True))
        extra = deepcopy(payload)
        extra["undeclared_extra_field"] = "must reject"
        rows.append((name + "/extra-field", name, extra, False))
    mutations = {
        "CommunityForecastPayload": ("instrument_id", "SH.600519"),
        "CommunityOutcomePayload": ("target_spec_hash", "not-a-sha256"),
        "ConvertibleRelationPayload": ("effective_from", "2026-02-30T00:00:00Z"),
        "ChartLayerDescriptor": ("available_at", "2026-11-11T15:01:00+08:00"),
        "MacdProfile": ("timeframe", "1M"),
    }
    for name, (key, bad_value) in mutations.items():
        bad = deepcopy(positives[name])
        bad[key] = bad_value
        rows.append((name + "/base-primitive", name, bad, False))
    return rows


def main():
    base = read("contracts/v3.schema.json")
    supplemental = read("contracts/supplemental.schema.json")
    bindings = read("contracts/extension_bindings.json")
    openapi = read("contracts/openapi.json")
    authority = read("authority_manifest.json")
    attempt("supplemental_schema_meta_valid", lambda: Draft202012Validator.check_schema(supplemental))
    check("exactly_five_named_payload_definitions", set(supplemental["$defs"]) == EXPECTED_DEFINITIONS)
    registry = Registry().with_resource(base["$id"], Resource.from_contents(base)).with_resource(
        supplemental["$id"], Resource.from_contents(supplemental))
    for name, definition in supplemental["$defs"].items():
        attempt("payload_meta/" + name, lambda definition=definition: Draft202012Validator.check_schema(definition))
        check("payload_closed_shape/" + name, definition.get("additionalProperties") is False)
    for ref in sorted(set(refs_in(supplemental))):
        attempt("reference_resolves/" + ref, lambda ref=ref: registry.resolver(supplemental["$id"]).lookup(ref) and None)
    for fixture_id, name, payload, expected in fixtures():
        validator = Draft202012Validator({"$ref": supplemental["$id"] + "#/$defs/" + name},
                                         registry=registry, format_checker=FormatChecker())
        errors = list(validator.iter_errors(payload))
        check("schema_fixture/" + fixture_id, (not errors) == expected,
              {"expected_valid": expected, "actual_valid": not errors,
               "rejection_path": list(errors[0].absolute_path) if errors else None})
    attempt("unchanged_openapi_meta_valid", lambda: validate_spec(openapi))
    operations = {body["operationId"]: body for path in openapi["paths"].values()
                  for body in path.values() if isinstance(body, dict) and "operationId" in body}
    check("public_operations_remain_31", len(operations) == 31)
    check("public_definitions_remain_142", len(base["$defs"]) == 142)
    tables = {}
    for file in SQL_FILES:
        try:
            statements = parse_sql((PACKAGE / file).read_text())
            for statement in statements:
                if isinstance(statement.stmt, ast.CreateStmt):
                    table = statement.stmt
                    identity = table.relation.schemaname + "." + table.relation.relname
                    if identity in tables:
                        raise ValueError("Duplicate CREATE TABLE " + identity)
                    tables[identity] = {column.colname for column in table.tableElts or ()
                                        if isinstance(column, ast.ColumnDef)}
            check("sql_ast_parses/" + file, True, {"statement_count": len(statements)})
        except Exception as exc:
            check("sql_ast_parses/" + file, False, str(exc))
    check("sql_table_count_remains_80", len(tables) == 80)
    check("sql_column_count_remains_883", sum(map(len, tables.values())) == 883)
    check("bindings_exactly_cover_five_payloads",
          {b["definition"] for b in bindings["bindings"]} == EXPECTED_DEFINITIONS and len(bindings["bindings"]) == 5)
    for binding in bindings["bindings"]:
        name = binding["definition"]
        actual_columns = tables.get(binding["storage_table"], set())
        check("binding_sql_slot/" + name, {binding["storage_column"], binding["version_column"]} <= actual_columns,
              {"table": binding["storage_table"], "columns": [binding["storage_column"], binding["version_column"]]})
        attempt("binding_public_path/" + name, lambda binding=binding: public_binding(binding, operations, openapi, base))
    decisions = read("traceability/proposal_decisions_v31.json")
    rows = decisions["decisions"]
    check("decisions_unique_complete_50",
          len(rows) == 50 and {r["decision_id"] for r in rows} == {f"P{i:02}" for i in range(1, 51)})
    check("decision_disposition_totals_match", dict(Counter(r["disposition"] for r in rows)) == decisions["counts"])
    document = (PACKAGE / "spec/PROPOSAL_DECISIONS_V31.md").read_text()
    for row in rows:
        path = (PACKAGE / row["baseline_or_target_path"]).resolve()
        anchor = re.search(r"^## " + re.escape(row["decision_id"]) + r"\s", document, re.M)
        check("decision_anchor/" + row["decision_id"],
              path.is_relative_to(PACKAGE) and path.is_file() and anchor is not None,
              row["baseline_or_target_path"])
    check("decisions_keep_design_and_efficacy_distinct",
          all(not r["production_implemented"] and not r["profitability_validated"]
              and r["rationale"].strip() and r["acceptance"].strip() for r in rows))
    strategies, factors = read("catalogs/strategy_catalog_v3.json"), read("catalogs/factor_catalog_v3.json")
    se, fe = strategies["entries"], factors["entries"]
    check("strategy_candidates_61",
          strategies["count"] == len(se) == 61 and {e["catalog_id"] for e in se} == {f"S{i:03}" for i in range(1, 62)})
    check("factor_candidates_105",
          factors["count"] == len(fe) == 105 and {e["factor_id"] for e in fe} == {f"F{i:03}" for i in range(1, 106)})
    check("strategy_uuid_and_factor_slugs_unique",
          len({e["strategy_id"] for e in se}) == 61 and len({e["slug"] for e in fe}) == 105)
    additions = [e for e in fe if int(e["factor_id"][1:]) >= 97]
    new_strategies = [e for e in se if e["catalog_id"] == "S061"]
    check("all_added_factors_are_unimplemented_unvalidated_unconnected",
          all(e["implementation_status"] == "not_implemented" and e["validation_status"] == "unvalidated"
              and e["availability_status"] == "not_connected" for e in additions))
    check("new_strategy_is_unimplemented_and_not_production_routed",
          all(e["implementation_status"] == "not_implemented" and e["validation_status"] == "unvalidated"
              and not e["routing"]["production_route_enabled"] for e in new_strategies))
    source_index = read("research/source_index.json")
    source_ids = {entry["id"] for entry in source_index}
    check("source_ids_unique", len(source_ids) == len(source_index))
    new_refs = sorted({ref for e in additions + new_strategies for ref in e.get("source_refs", [])})
    check("new_candidate_source_refs_resolve", set(new_refs) <= source_ids,
          {"source_refs": new_refs, "unresolved": sorted(set(new_refs) - source_ids)})
    factor_ids = {e["factor_id"] for e in fe}
    check("new_strategy_factor_dependencies_resolve",
          all(set(e["data_requires"]["factor_catalog_ids"]) <= factor_ids for e in new_strategies))
    baseline = read("traceability/baseline_v3_hashes.json")
    check("baseline_manifest_matches_reviewed_eight_files", baseline["files"] == EXPECTED_BASELINE)
    for path, digest in EXPECTED_BASELINE.items():
        check("baseline_bytes_unchanged/" + path, sha256((PACKAGE / path).read_bytes()).hexdigest() == digest)
    raw_input = (PACKAGE / "reference_input/user_proposal_20261111.txt").read_bytes()
    input_manifest = read("reference_input/input_manifest.json")
    check("user_input_bytes_and_hash_preserved",
          sha256(raw_input).hexdigest() == input_manifest["input_sha256"] == EXPECTED_INPUT_HASH
          and len(raw_input) == input_manifest["bytes"] == 48482)
    papers = read("research/papers_registry.json")
    methods = read("research/supplemental_method_review.json")
    check("core_paper_registry_remains_46",
          papers["core_papers"] == len(papers["papers"]) == 46
          and len({p["paper_id"] for p in papers["papers"]}) == 46)
    check("supplement_names_are_10_not_added_as_10_core_papers",
          methods["named_items"] == len(methods["items"]) == 10 and methods["core_registry_count_change"] == 0)
    status_counts = Counter(item["verification_status"] for item in methods["items"])
    check("supplement_name_identity_9_confirmed_1_unresolved",
          status_counts == {"identified": 9, "unresolved": 1}
          and methods["identified_works"] == 9 and methods["unresolved_items"] == 1)
    check("supplement_method_source_refs_resolve",
          all(set(item["source_ids"]) <= source_ids for item in methods["items"]))
    check("supplement_methods_do_not_claim_reproduction_or_profitability",
          all(not item["author_training_reproduced"] and not item["ashare_profitability_validated"]
              and not item["production_enabled"] for item in methods["items"]))
    ec = authority["evidence_counts"]
    check("authority_research_counts_match_files",
          ec["core_papers"] == 46 and ec["supplement_named_method_items"] == 10
          and ec["identified_supplement_works"] == 9 and ec["unresolved_supplement_items"] == 1
          and ec["source_records"] == len(source_index)
          and ec["unique_source_urls"] == len({x["url"] for x in source_index}))
    failures = [row for row in checks if not row["passed"]]
    report = {
        "checked_at": "2026-11-11",
        "scope": "Static integration and structural fixtures only; not repository, database execution, data feed, HTTP server or strategy validation.",
        "checks_total": len(checks), "checks_passed": len(checks) - len(failures),
        "checks_failed": len(failures), "passed": not failures,
        "schema_fixture_count": len(fixtures()), "decision_anchor_count": len(rows),
        "sql_ast_tables": len(tables), "sql_ast_columns": sum(map(len, tables.values())),
        "base_http_operations": len(operations), "public_schema_definitions": len(base["$defs"]),
        "dependencies": {p: version(p) for p in ("jsonschema", "pglast", "openapi-spec-validator", "referencing")},
        "checks": checks,
        "not_implemented_repository_requirements": [
            {"id": "X05", "status": "not_implemented",
             "requirement": "Bind capabilities to the exact input artifact and instrument/scope, not a caller-global capability set."},
            {"id": "X08", "status": "not_implemented",
             "requirement": "Verify actual dataset window completeness and availability against the requested window and replay cutoff."},
            {"id": "X09", "status": "not_implemented",
             "requirement": "Resolve and verify the full artifact identity: ref, content hash, contract_id and schema_version."},
        ],
        "repository_gap_count_in_passed_checks": 0,
        "reviewed_sha256": {path: sha256((PACKAGE / path).read_bytes()).hexdigest() for path in [
            "contracts/supplemental.schema.json", "contracts/extension_bindings.json",
            "traceability/proposal_decisions_v31.json", "catalogs/strategy_catalog_v3.json",
            "catalogs/factor_catalog_v3.json", "research/source_index.json",
            "research/papers_registry.json", "research/supplemental_method_review.json",
        ]},
    }
    (PACKAGE / "verification/supplement_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{report['checks_passed']}/{len(checks)} static checks passed; {len(failures)} failed. "
          "Three Repository requirements remain unimplemented and are excluded from pass counts.")
    for failed in failures:
        print(json.dumps(failed, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
