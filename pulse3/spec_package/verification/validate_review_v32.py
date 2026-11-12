"""Check v3.2 review provenance and cross-file links, not trading efficacy.

The uploaded files are evidence under review, never executable instructions.
The eight unchanged public/SQL/ledger hashes come from the existing v3.1
validator and baseline manifest. Proposed product cards remain design objects.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from pglast import ast as sql_ast, parse_sql


PACKAGE = Path(__file__).resolve().parents[1]


def split_clauses(text, delimiter="、"):
    """Split the uploaded named-item lists without splitting parentheses."""
    result, start, depth = [], 0, 0
    for position, character in enumerate(text):
        if character in "（(":
            depth += 1
        elif character in "）)":
            depth -= 1
        elif character == delimiter and depth == 0:
            result.append(text[start:position].strip())
            start = position + 1
    if text[start:].strip():
        result.append(text[start:].strip())
    return result


def uploaded_sections(lines):
    section = ""
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = line[3:]
        yield number, section, line


def table_cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def v31_constants(root):
    """Read existing literal constants without executing the old validator."""
    tree = ast.parse((root / "verification/validate_supplement_v31.py").read_text())
    wanted = {"EXPECTED_BASELINE", "SQL_FILES"}
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    result[target.id] = ast.literal_eval(node.value)
    if set(result) != wanted:
        raise ValueError("Existing v3.1 baseline constants are unavailable")
    return result


def validate(root=PACKAGE):
    root = Path(root).resolve()
    checks = []

    def read(relative):
        return json.loads((root / relative).read_text(encoding="utf-8"))

    def check(name, condition, detail=None):
        row = {"check": name, "passed": bool(condition)}
        if detail is not None:
            row["detail"] = detail
        checks.append(row)

    def no_errors(name, errors):
        check(name, not errors, {"error_count": len(errors), "errors": errors[:50]})

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def real_path(relative):
        if not text(relative):
            return False
        path = (root / relative).resolve()
        return path.is_relative_to(root) and path.is_file()

    constants = v31_constants(root)
    baseline = read("traceability/baseline_v3_hashes.json")["files"]
    check("eight_file_baseline_matches_existing_v31_validator",
          len(baseline) == 8 and baseline == constants["EXPECTED_BASELINE"])
    for relative, digest in constants["EXPECTED_BASELINE"].items():
        check("unchanged_baseline/" + relative,
              real_path(relative) and sha256((root / relative).read_bytes()).hexdigest() == digest)

    manifest = read("review_v32/input_manifest.json")
    units = read("review_v32/input_units.json")
    decisions = read("review_v32/decisions.json")["decisions"]
    coverage = read("review_v32/coverage.json")
    summary = read("review_v32/summary.json")
    unit_map = read("review_v32/unit_decision_map.json")
    files = {row["file"]: row for row in manifest["files"]}
    input_folder = root / "review_v32/reference_input"
    check("eleven_original_files_have_exact_manifest_membership",
          len(files) == len(manifest["files"]) == 11
          and set(files) == {p.name for p in input_folder.glob("*.md")})
    lines_by_file = {}
    for name, row in files.items():
        raw = (input_folder / name).read_bytes()
        lines = raw.decode("utf-8").splitlines()
        lines_by_file[name] = lines
        check("original_hash_bytes_and_lines/" + name,
              sha256(raw).hexdigest() == row["sha256"]
              and len(raw) == row["bytes"] and len(lines) == row["lines"])

    by_unit = {row["unit_id"]: row for row in units}
    substantive = {row["unit_id"] for row in units if row["requires_decision"] is True}
    check("unit_ids_unique_and_declared_counts_exact",
          len(by_unit) == len(units) == manifest["total_units"]
          and len(substantive) == manifest["substantive_units"] == 1524
          and sum(len(lines) for lines in lines_by_file.values()) == manifest["total_lines"])
    unit_errors = []
    for row in units:
        source = lines_by_file.get(row["file"], [])
        start, end = row["line_start"], row["line_end"]
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(source)
                and row["text"] == "\n".join(source[start - 1:end])
                and isinstance(row["requires_decision"], bool)):
            unit_errors.append(row["unit_id"])
    no_errors("every_unit_matches_its_exact_original_text_and_line_span", unit_errors)
    for name, row in files.items():
        own = [u for u in units if u["file"] == name]
        covered_lines = {n for u in own for n in range(u["line_start"], u["line_end"] + 1)}
        nonblank = {n for n, line in enumerate(lines_by_file[name], 1) if line.strip()}
        check("original_nonblank_coverage/" + name,
              len(own) == row["units"]
              and sum(u["requires_decision"] is True for u in own) == row["substantive_units"]
              and nonblank <= covered_lines
              and len(nonblank) == row["nonblank_lines"] == row["nonblank_lines_covered"])

    decision_ids = {row["decision_id"] for row in decisions}
    assigned = Counter(unit_id for row in decisions for unit_id in row["unit_ids"])
    check("590_unique_nonempty_decisions", len(decisions) == len(decision_ids) == 590
          and all(row["unit_ids"] for row in decisions))
    check("1524_substantive_units_assigned_exactly_once",
          assigned == Counter({unit_id: 1 for unit_id in substantive}),
          {"missing": sorted(substantive - assigned.keys()),
           "extra": sorted(assigned.keys() - substantive),
           "duplicates": sorted(key for key, count in assigned.items() if count != 1)})
    decision_errors = []
    for row in decisions:
        if not (all(text(row.get(field)) for field in ("summary", "reason", "implementation_action", "acceptance"))
                and isinstance(row.get("baseline_binding"), list) and row["baseline_binding"]
                and all(real_path(path) for path in row["baseline_binding"])
                and row["disposition"] in {"adopt", "revise", "covered", "reject", "unresolved"}):
            decision_errors.append(row["decision_id"])
    no_errors("every_decision_has_reason_action_acceptance_and_existing_bindings", decision_errors)
    expected_map = Counter((unit_id, by_unit[unit_id]["file"], by_unit[unit_id]["line_start"],
                            by_unit[unit_id]["line_end"], row["decision_id"])
                           for row in decisions for unit_id in row["unit_ids"] if unit_id in by_unit)
    check("unit_decision_map_exactly_matches_decisions_and_original_spans",
          Counter((r["unit_id"], r["file"], r["line_start"], r["line_end"], r["decision_id"])
                  for r in unit_map) == expected_map and len(unit_map) == 1524)
    actual_summary = {
        "input_files": len(files), "input_lines": sum(len(x) for x in lines_by_file.values()),
        "structural_units": len(units), "substantive_units": len(substantive),
        "decision_count": len(decisions), "covered_units": len(substantive & assigned.keys()),
        "uncovered_units": sorted(substantive - assigned.keys()),
        "duplicate_assignments": sorted(k for k, count in assigned.items() if count > 1),
        "dispositions": dict(Counter(row["disposition"] for row in decisions)),
    }
    check("coverage_and_summary_recomputed_counts_agree",
          all(coverage.get(key) == value == summary.get(key) for key, value in actual_summary.items()))
    expected_by_file = [{"file": name, "lines": row["lines"],
                         "substantive_units": row["substantive_units"],
                         "covered": sum(u["unit_id"] in assigned for u in units
                                        if u["file"] == name and u["requires_decision"]),
                         "sha256": row["sha256"]} for name, row in files.items()]
    check("per_file_coverage_matches_original_manifest",
          coverage["by_file"] == expected_by_file == summary["by_file"])

    sources = read("research/source_index.json")
    source_count, url_count = len(sources), len({row["url"] for row in sources})
    check("304_unique_source_records_and_288_distinct_urls",
          source_count == len({row["id"] for row in sources}) == 304 and url_count == 288)
    authority = read("authority_manifest.json")
    for name, count in (("source_records", source_count), ("unique_source_urls", url_count),
                        ("reference_v32_items", 55), ("review_v32_substantive_units", 1524),
                        ("review_v32_decisions", 590)):
        check("authority_count/" + name, authority["evidence_counts"].get(name) == count)

    evidence_lines = lines_by_file["09-evidence.md"]
    original_papers = {}
    for number, section, line in uploaded_sections(evidence_lines):
        cells = table_cells(line)
        if section.startswith("3. ") and line.startswith("|") and len(cells) == 4 and cells[1].startswith("https://"):
            original_papers[number] = cells
    paper_document = read("research/reference_papers_v32.json")
    papers = paper_document["items"]
    check("55_input_paper_rows_have_one_to_one_reference_cards",
          len(original_papers) == paper_document["count"] == len(papers) == 55
          and Counter(p["input_line"] for p in papers) == Counter({n: 1 for n in original_papers})
          and {p["reference_id"] for p in papers} == {f"R32-P{n:02}" for n in range(1, 56)})
    paper_errors = []
    for paper in papers:
        original = original_papers.get(paper["input_line"])
        supplied = [paper.get(k) for k in ("input_label", "input_url", "input_claim", "input_read_level_reported_by_other_author")]
        if not (supplied == original
                and all(text(paper.get(k)) for k in ("decision", "decision_reason", "actual_reading_level", "primary_url"))
                and paper["implementation_bindings"] and all(real_path(p) for p in paper["implementation_bindings"])
                and isinstance(paper["identity_confirmed"], bool)
                and isinstance(paper["claim_verified_to_reading_level"], bool)
                and paper["independently_reproduced"] is False
                and paper["production_efficacy_verified"] is False):
            paper_errors.append(paper["reference_id"])
    no_errors("paper_input_identity_reading_scope_bindings_and_unimplemented_status", paper_errors)
    check("paper_reading_summary_derived_from_actual_cards",
          summary["reference_reading_levels"] == dict(Counter(p["actual_reading_level"] for p in papers)))
    # Media entries are also inputs; identity_confirmed=False does not turn a
    # media report into an unidentified primary research paper.
    check("primary_unresolved_summary_matches_explicit_review_classification",
          summary["paper_primary_unresolved"] == [p["reference_id"] for p in papers if p["decision"] == "unresolved_primary"])

    appendix = read("review_v32/appendix_item_mapping.json")
    expected_factors, expected_sources, expected_events = [], [], []
    for number, section, line in uploaded_sections(evidence_lines):
        if section.startswith("附表 A"):
            match = re.fullmatch(r"- \*\*(.+)\*\*：(.+)。", line)
            if match:
                expected_factors.extend((number, match[1], item) for item in split_clauses(match[2]))
        elif section.startswith("附表 B") and line.startswith("|"):
            cells = table_cells(line)
            if len(cells) == 2 and cells[0] in {"T0", "T1", "T2", "海外", "T3", "聚合"}:
                expected_sources.extend((number, cells[0], item) for item in split_clauses(cells[1]))
        elif section.startswith("附表 C") and line.startswith("|"):
            cells = table_cells(line)
            if len(cells) == 2 and cells[0] not in {"组", "---"}:
                group, body = cells
                clauses = split_clauses(body.split("：")[0]) if group == "低可信补充" else split_clauses(body, "；")
                for clause in clauses:
                    match = re.fullmatch(r"(.+?)（(.+)）", clause)
                    expected_events.append((number, group, match[1] if match else clause))
    for name, id_field, group_field, expected, count, prefix in (
        ("factor_items", "candidate_reference_id", "group", expected_factors, 159, "RF"),
        ("source_items", "source_reference_id", "input_tier", expected_sources, 56, "RS"),
        ("event_items", "event_reference_id", "group", expected_events, 70, "RE"),
    ):
        rows = appendix[name]
        check("appendix_exact_named_clause_coverage/" + name,
              len(rows) == len(expected) == count
              and Counter((r["input_line"], r[group_field], r["input_named_item"]) for r in rows) == Counter(expected)
              and {r[id_field] for r in rows} == {f"{prefix}{i:03}" for i in range(1, count + 1)})
        check("appendix_each_item_has_a_reason/" + name,
              all(text(r.get("rationale", r.get("reason"))) and text(r.get("decision")) for r in rows))
    factor_ids = {r["factor_id"] for r in read("catalogs/factor_catalog_v3.json")["entries"]}
    check("appendix_candidates_do_not_claim_connected_validated_or_automatic_trading",
          all(set(r["related_factor_ids"]) <= factor_ids and r["data_status"] == "not_connected_in_this_review"
              and r["efficacy_status"] == "unvalidated" for r in appendix["factor_items"])
          and all(r["current_access_status"] == "not_verified_on_production_server" for r in appendix["source_items"])
          and all(r["automatic_polarity"] is False and r["predictive_route"] == "disabled_until_validated"
                  for r in appendix["event_items"]))
    check("appendix_count_labels_match_content",
          appendix["counts"] == {"factor_named_clauses": 159, "source_named_clauses": 56, "event_named_clauses": 70})

    openapi = read("contracts/openapi.json")
    operations = {body["operationId"] for path in openapi["paths"].values()
                  for method, body in path.items() if method in {"get", "post", "put", "patch", "delete", "head", "options"}}
    bindings = read("contracts/api_bindings.json")["operations"]
    check("current_operations_match_actual_public_api_and_binding_registry",
          operations == {r["operation_id"] for r in bindings}
          and len(operations) == len(bindings) == authority["compatibility"]["unchanged_http_operations"])
    table_sources, columns = {}, {}
    for relative in constants["SQL_FILES"]:
        for statement in parse_sql((root / relative).read_text()):
            if isinstance(statement.stmt, sql_ast.CreateStmt):
                table = statement.stmt
                name = table.relation.schemaname + "." + table.relation.relname
                if name in table_sources:
                    raise ValueError("Duplicate CREATE TABLE: " + name)
                table_sources[name] = relative
                columns[name] = {column.colname for column in table.tableElts or () if isinstance(column, sql_ast.ColumnDef)}
    check("actual_sql_table_and_column_counts_match_preserved_authority",
          len(table_sources) == authority["compatibility"]["unchanged_sql_tables"]
          and sum(len(v) for v in columns.values()) == authority["compatibility"]["unchanged_sql_columns"])
    owner_roles = {row["work_package_id"]: row["owner_role"] for row in read("traceability/work_packages.json")["work_packages"]}

    def valid_input(row):
        unit = by_unit.get(row["input_unit_id"], {})
        return (unit.get("file") == row["input_file"] and row["input_unit_id"] in substantive
                and unit.get("line_start", 0) <= row["input_line"] <= unit.get("line_end", -1))

    def valid_owners(rows):
        return bool(rows) and all(owner_roles.get(row["id"]) == row["role"] for row in rows)

    def valid_tables(names, sources):
        return bool(names) and set(names) <= table_sources.keys() and sources == {name: table_sources.get(name) for name in names}

    features = read("review_v32/product_subfeature_mapping.json")
    resources = read("review_v32/product_api_mapping.json")
    datasets = read("review_v32/dataset_job_page_mapping.json")
    for label, rows, key, prefix, count in (
        ("subfeatures", features, "feature_id", "PF", 21),
        ("resources", resources, "resource_id", "PA", 16),
        ("dataset_jobs", datasets, "mapping_id", "DJ", 20),
    ):
        check("mapping_count_and_identity/" + label,
              len(rows) == count and {r[key] for r in rows} == {f"{prefix}{i:02}" for i in range(1, count + 1)})
        check("mapping_input_spans_resolve/" + label, all(valid_input(r) for r in rows))
    resource_by_id = {r["resource_id"]: r for r in resources}
    check("subfeature_operations_owners_acceptance_and_extension_links_resolve",
          all(r["current_operations"] and set(r["current_operations"]) <= operations
              and valid_owners(r["work_package_owners"])
              and r["extension_card_resource_ids"] and set(r["extension_card_resource_ids"]) <= resource_by_id.keys()
              and all(text(r.get(k)) for k in ("reason", "implementation_action", "acceptance"))
              and r["status"] == "specified_not_claimed_implemented" for r in features))
    check("resource_operations_actual_tables_and_owners_resolve",
          all(r["current_operation_ids"] and set(r["current_operation_ids"]) <= operations
              and valid_tables(r["candidate_table_owners"], r["table_definition_sources"])
              and valid_owners(r["work_package_owners"]) and text(r["acceptance"])
              and r["resource"] == table_cells(lines_by_file[r["input_file"]][r["input_line"] - 1])[0]
              and r["contract_status"] == "existing_ops_verified_extensions_specified_not_in_public_openapi_3_0_0"
              for r in resources))
    check("dataset_job_page_operations_actual_tables_owners_and_failure_policy_resolve",
          all(r["consuming_operation_ids"] and set(r["consuming_operation_ids"]) <= operations
              and valid_tables(r["target_owner_tables"], r["table_definition_sources"])
              and r["work_package_owners"] and set(r["work_package_owners"]) <= owner_roles.keys()
              and r["owner_roles"] == {name: owner_roles.get(name) for name in r["work_package_owners"]}
              and all(text(r.get(k)) for k in ("normalized_job", "pages", "trigger_freshness_and_failure_policy"))
              and [r[k] for k in ("input_datasets", "input_job", "milestone", "pages")]
                  == table_cells(lines_by_file[r["input_file"]][r["input_line"] - 1])
              and r["status"] == "required_implementation_not_claimed_running" for r in datasets))

    extensions = read("review_v32/product_extension_cards.json")
    cards, types, persistence = extensions["cards"], extensions["types"], extensions["persistence_definitions"]
    public_types = read("contracts/v3.schema.json")["$defs"]
    supplemental_types = read("contracts/supplemental.schema.json")["$defs"]
    check("sixteen_cards_are_one_to_one_with_resources",
          len(cards) == 16 and {c["card_id"] for c in cards} == {r["extension_card_id"] for r in resources}
          and Counter(c["resource_id"] for c in cards) == Counter(resource_by_id.keys()))
    type_errors = []

    def field_errors(fields, location):
        if not fields or len({f["name"] for f in fields}) != len(fields):
            type_errors.append(location + ": empty or duplicate fields")
        for field in fields:
            declared = field.get("type", "")
            base = declared[:-2] if declared.endswith("[]") else declared
            if base.startswith("v3."):
                resolved = base[3:] in public_types
            elif base.startswith("supplemental."):
                resolved = base.split(".", 1)[1] in supplemental_types
            elif base.startswith("PE."):
                resolved = base[3:] in types
            else:
                resolved = base in {"string", "boolean", "finite_float"}
            if not (resolved and text(field.get("name")) and text(field.get("meaning"))
                    and isinstance(field.get("required"), bool) and isinstance(field.get("nullable"), bool)):
                type_errors.append(location + "/" + str(field.get("name")) + ": " + declared)

    for name, definition in types.items():
        if definition["kind"] == "record":
            field_errors(definition["fields"], "types/" + name)
        elif definition["kind"] != "enum" or not definition.get("values") or len(set(definition["values"])) != len(definition["values"]):
            type_errors.append("types/" + name + ": invalid kind or enum")
    for name, definition in persistence.items():
        field_errors(definition["fields"], "persistence/" + name)
    proposed = []
    card_errors = []
    used_persistence = set()
    for card in cards:
        resource = resource_by_id.get(card["resource_id"], {})
        storage = card["persistence"]
        if not (valid_input(card) and valid_owners(card["owner_roles"])
                and card["card_id"] == resource.get("extension_card_id")
                and card["input_unit_id"] == resource.get("input_unit_id")
                and set(card["current_verified_operation_ids"]) == set(resource.get("current_operation_ids", []))
                and set(card["current_verified_operation_ids"]) <= operations
                and valid_tables(storage["existing_tables"], storage["existing_table_definitions"])
                and set(storage["new_or_registration_required_object_ids"]) <= persistence.keys()
                and card["acceptance"] and card["status"] == "implementation_design_card_only"):
            card_errors.append(card["card_id"])
        used_persistence.update(storage["new_or_registration_required_object_ids"])
        for operation in card["proposed_operations"]:
            name = operation["proposed_operation_id"]
            proposed.append(name)
            field_errors(operation["input_fields"], name + "/input")
            field_errors(operation["output_fields"], name + "/output")
            if not (all(text(operation.get(k)) for k in ("authorization", "idempotency", "version_and_conflict"))
                    and operation["acceptance"] and operation["operation_kind"] in {"command", "query", "pure_query", "transport_adapter"}
                    and operation["public_status"] == "not_in_current_OpenAPI_3.0.0_not_implemented"):
                card_errors.append(card["card_id"] + "/" + name)
    no_errors("extension_type_references_fields_required_and_nullable_are_closed", type_errors)
    no_errors("extension_cards_have_valid_resources_owners_storage_permissions_and_acceptance", card_errors)
    check("proposed_operations_unique_and_absent_from_current_public_api",
          len(proposed) == len(set(proposed)) and not (set(proposed) & operations))
    check("proposed_types_and_cards_explicitly_remain_unimplemented_design",
          extensions["status"] == "proposed_application_contracts_not_registered_in_public_openapi"
          and extensions["base_public_contract_version"] == authority["compatibility"]["public_contract_version"]
          and all(t["status"] == "proposed_type_not_public_v3" for t in types.values()))
    check("proposed_persistence_is_linked_owned_and_not_an_existing_table",
          used_persistence == persistence.keys()
          and all(d["status"] == "requires_registration_or_migration_not_present_as_implemented_object"
                  and owner_roles.get(d["owner"]["work_package_id"]) == d["owner"]["role"]
                  and set(d["reuses_existing_tables"]) <= table_sources.keys()
                  and isinstance(d["new_sql_table"], bool)
                  and (not d["new_sql_table"] or d["name"] not in table_sources)
                  for d in persistence.values()))
    column_bindings = extensions["existing_column_bindings"]
    check("extension_existing_column_bindings_resolve_to_actual_sql_definitions",
          bool(column_bindings)
          and len({(r["table"], r["column"]) for r in column_bindings}) == len(column_bindings)
          and all(r["column"] in columns.get(r["table"], set())
                  and r["definition_path"] == table_sources.get(r["table"])
                  and text(r["meaning_and_limits"]) for r in column_bindings))
    check("extension_async_receipts_link_to_current_or_proposed_operations",
          all(name in proposed and text(binding["resource_id"])
              and binding["poll_operation_id"] in operations | set(proposed)
              for name, binding in extensions["async_receipt_bindings"].items()))
    extension_counts = {
        "resource_cards": len(cards), "proposed_operations": len(proposed), "proposed_types": len(types),
        "persistence_definitions": len(persistence),
        "proposed_new_sql_tables": sum(d["new_sql_table"] for d in persistence.values()),
        "existing_public_operation_count": len(operations), "existing_sql_table_count": len(table_sources),
        "verified_existing_column_bindings": len(column_bindings),
    }
    card_validation = read("review_v32/product_extension_cards_validation.json")
    check("extension_summary_counts_come_from_current_cards",
          all(extensions["verification"].get(k) == v == card_validation.get(k) for k, v in extension_counts.items()))
    contract_sql_hashes = {path: digest for path, digest in baseline.items() if path != "execution/unified_ledger.py"}
    check("extension_hash_summary_agrees_with_unchanged_public_sql_baseline",
          extensions["verification"]["unchanged_public_contract_and_sql_hashes"] == contract_sql_hashes
          == card_validation["unchanged_public_contract_and_sql_hashes"])
    summary_counts = {"factor_named_clauses": len(appendix["factor_items"]),
                      "source_named_clauses": len(appendix["source_items"]),
                      "event_named_clauses": len(appendix["event_items"]),
                      "product_subfeatures": len(features), "product_resource_mappings": len(resources),
                      "dataset_job_page_rows": len(datasets), "paper_reference_items": len(papers),
                      "source_records": source_count, "unique_source_urls": url_count}
    check("review_summary_matches_all_cross_file_counts", all(summary.get(k) == v for k, v in summary_counts.items()))
    failures = [row for row in checks if not row["passed"]]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Static original-byte, review coverage, citation identity and contract-link validation; no business algorithm or live service execution.",
        "checks_total": len(checks), "checks_passed": len(checks) - len(failures),
        "checks_failed": len(failures), "passed": not failures,
        **actual_summary, **summary_counts, "extension_counts": extension_counts,
        "unchanged_baseline_files": len(baseline), "checks": checks,
        "production_behavior_validated": False, "paper_results_reproduced": False,
        "limits": ["Coverage means each input unit has a decision, not that every decision is empirically true.",
                   "Named API/table/type bindings are checked against the shipped artifacts; proposed extensions are not implemented services.",
                   "No provider, PostgreSQL runtime, web server, training or investment return is validated here."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PACKAGE)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        report = validate(root)
    except Exception as exc:
        report = {"passed": False, "checks_failed": 1, "fatal_error": type(exc).__name__ + ": " + str(exc),
                  "production_behavior_validated": False}
    output = root / "verification/review_v32_validation.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "checks"}, ensure_ascii=False, indent=2))
    for failure in report.get("checks", []):
        if not failure["passed"]:
            print(json.dumps(failure, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
