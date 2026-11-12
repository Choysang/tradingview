"""Mechanical v3 catalog checks; no strategy/factor computation or return claims."""
from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROLES = {"selection", "allocation", "entry", "holding", "execution", "exit"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--taxonomy", type=Path)
    args = parser.parse_args()
    taxonomy_path = args.taxonomy
    if taxonomy_path is None:
        taxonomy_path = next((p for p in (BASE / "taxonomy.json", BASE.parent / "sources_sectors" / "taxonomy.json") if p.exists()), None)
    if taxonomy_path is None:
        raise ValueError("Supply --taxonomy pointing to the shared taxonomy.json")
    strategies = json.loads((BASE / "strategy_catalog_v3.json").read_text())
    factors = json.loads((BASE / "factor_catalog_v3.json").read_text())
    taxonomy = json.loads(taxonomy_path.read_text())
    facets = {f["facet_id"]: f for f in taxonomy["facets"]}
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append({"check": name, "passed": True})

    se, fe = strategies["entries"], factors["entries"]
    check("61 strategy candidates retain S identities", {e["catalog_id"] for e in se} == {f"S{i:03}" for i in range(1, 62)} and len(se) == 61)
    check("105 factor candidates retain F identities", {e["factor_id"] for e in fe} == {f"F{i:03}" for i in range(1, 106)} and len(fe) == 105)
    namespace = uuid.UUID(strategies["identity_policy"]["namespace_uuid"])
    check("strategy UUIDs are deterministic and unique", len({e["strategy_id"] for e in se}) == 61 and all(str(uuid.uuid5(namespace, "strategy/" + e["catalog_id"])) == e["strategy_id"] for e in se))
    check("only six formal roles across registry and taxonomy", set(strategies["role_contracts"]) == ROLES == set(facets["strategy.role"]["allowed_values"]))
    check("primary and supporting roles remain distinct and formal", all(e["primary_role"] in ROLES and set(e["supporting_roles"]) <= ROLES and e["primary_role"] not in e["supporting_roles"] for e in se))
    check("all component/delegated/account roles use the same enum", all(set(e["composition_contract"]["delegated_roles"]) <= ROLES and set(e["data_requires"]["account_snapshot_required_for_roles"]) <= ROLES and {c["role"] for c in e["composition_contract"]["components"]} == {e["primary_role"], *e["supporting_roles"]} for e in se))
    check("all strategy facet tags resolve to controlled shared values", all(key in facets and set(values) <= set(facets[key]["allowed_values"]) for e in se for key, values in e["taxonomy_tags"].items()))
    check("primary method is represented in method tags", all(e["method_family"] in e["method_family_tags"] for e in se))
    check("all strategy factor references resolve", all(set(e["data_requires"]["factor_catalog_ids"]) <= {f["factor_id"] for f in fe} for e in se))
    check("all candidates remain unimplemented and production routing disabled", all(e["implementation_status"] == "not_implemented" and e["validation_status"] == "unvalidated" and e["lifecycle_state"] == "discovered" and not e["routing"]["production_route_enabled"] for e in se))
    check("all factors remain unimplemented and not connected", all(e["implementation_status"] == "not_implemented" and e["availability_status"] == "not_connected" and e["validation_status"] == "unvalidated" for e in fe))
    check("no simulated profitability or training runs invented", all(not e["evaluation_status"]["profitability_claim"] and not e["evaluation_status"]["backtest_run_ids"] and not e["evaluation_status"]["training_run_ids"] and not e["evaluation_status"]["forward_run_ids"] for e in se))
    check("factor missing data separates value field quality and condition", all(e["missing_handling"]["value"] is None and e["missing_handling"]["field_quality"]["status"] == "missing" and e["missing_handling"]["quality_status"] == "partial" and e["missing_handling"]["condition_value"] == "UNKNOWN" and not e["missing_handling"]["missing_is_zero"] for e in fe))
    check("quality_status never uses unknown", all(set(e["result_contract"]["quality_status_enum"]) == {"valid", "partial", "disputed", "invalid"} and "quality_state" not in e["missing_handling"] for e in fe))
    units = set(factors["unit_registry"])
    check("units are controlled or require explicit binding", all(e["output_unit"] in units if e["output_unit"] is not None else e["unit_contract"]["binding_required_before_calculation"] for e in fe))
    check("unknown is not a unit and vectors have component units", all(e["output_unit"] != "ratio_or_unknown" and (e["output_kind"] != "feature_vector" or e["unit_contract"]["component_units_required"]) for e in fe))
    policy = strategies["universe_policies"]["CN_MAINBOARD_DEFAULT"]
    check("circulating market cap basis and threshold retained", "float_market_cap" not in policy and "free_float_market_cap" not in policy and policy["circulating_market_cap"]["basis"] == "circulating_market_cap" and policy["circulating_market_cap"]["value"] == "10000000000")
    by_id = {e["catalog_id"]: e for e in se}
    check("T overlays are holding forms and execution RL stays execution", all(by_id[c]["primary_role"] == "holding" and by_id[c]["strategy_form"] == "holding_overlay" for c in ("S058", "S059")) and by_id["S053"]["primary_role"] == "execution")
    check("mixed raw score averaging is not an enabled route", all(not e["routing"]["score_only_routing_allowed"] and e["routing"]["exact_target_and_horizon_match_required"] for e in se))
    check("actual availability and reconstructed availability are distinct", all("policy_usable_at" in e["data_requires"]["availability_contract"] for e in se) and all("policy_usable_at" in e["clock"]["as_of_rule"] for e in fe))

    result = {"checked_at": "2026-11-11", "passed_checks": len(checks), "checks": checks,
              "strategy_count": len(se), "factor_count": len(fe),
              "role_counts": dict(Counter(e["primary_role"] for e in se)),
              "production_routes_enabled": 0, "live_data_or_strategy_returns_tested": False}
    (BASE / "catalog_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(checks)} catalog consistency checks passed; 61 strategies and 105 factors remain candidates.")


if __name__ == "__main__":
    main()
