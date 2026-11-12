"""Validate the contract fixture, not a trading engine or complete application."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError


ROOT = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_links(f: dict) -> None:
    """Small fixture-level invariants. This is deliberately not PlanCompiler."""
    ext = {
        (e["object_type"], e["object_id"], str(e["version"])): e
        for e in f["external_references"]
    }
    require(len(ext) == len(f["external_references"]), "duplicate external revision")
    strategies = {(s["strategy_id"], s["version"]): s for s in f["strategies"]}
    manifests = {r["run_id"]: r for r in f["run_manifests"]}
    models = {m["model_run_id"]: m for m in f["model_runs"]}
    proposals = {m["structured_output"]["proposal_id"]: m["structured_output"] for m in f["model_runs"]}
    plans = {(p["plan_id"], p["version"]): p for p in f["trade_plans"]}
    decisions = {d["snapshot_id"]: d for d in f["decision_snapshots"]}
    objects = [*f["strategies"], *f["run_manifests"], *f["model_runs"],
               *proposals.values(), *f["trade_plans"], *f["decision_snapshots"], *f["events"]]
    require(len({o["object_id"] for o in objects}) == len(objects), "duplicate object_id")
    require(len({o["idempotency_key"] for o in objects}) == len(objects), "duplicate logical write key")

    def external(kind: str, oid: str, version: str | int, cutoff: str) -> dict:
        key = kind, oid, str(version)
        require(key in ext, f"missing external reference {key}")
        e = ext[key]
        require(time(e["available_at"]) <= time(cutoff), f"future dependency {key}")
        return e

    def version_ref(value: dict, kind: str, cutoff: str) -> None:
        if kind == "strategy":
            key = value["object_id"], value["version"]
            require(key in strategies, f"missing strategy {key}")
            require(time(strategies[key]["created_at"]) <= time(cutoff), "future strategy")
        else:
            external(kind, value["object_id"], value["version"], cutoff)

    def evidence(ids: list[str], cutoff: str) -> None:
        for oid in ids:
            matches = [e for e in ext.values() if e["object_type"] == "evidence_revision" and e["object_id"] == oid]
            require(any(time(e["available_at"]) <= time(cutoff) for e in matches), f"unknown or future evidence {oid}")

    def condition(node: dict, registry: dict) -> None:
        if node["node"] in {"all", "any"}:
            for child in node["children"]:
                condition(child, registry)
        elif node["node"] == "not":
            condition(node["child"], registry)
        elif node["node"] in {"compare", "crosses"}:
            require(node["left"]["unit"] == node["right"]["unit"], "operand unit mismatch")
            for operand in (node["left"], node["right"]):
                if operand["kind"] == "feature":
                    key = operand["feature_id"], operand["definition_version"]
                    require(registry.get(key) == operand["unit"], f"undeclared feature {key}")
                    require(operand["instrument_id"] == "SAMPLE_MAIN_001", "unexpected fixture instrument")

    for r in manifests.values():
        for reference in r["strategy_refs"]:
            version_ref(reference, "strategy", r["as_of"])
        for reference in r["model_policy_refs"]:
            version_ref(reference, "model_policy", r["as_of"])
        for kind in ("universe_policy", "market_rules", "cost_profile", "execution_policy"):
            version_ref(r[kind + "_ref"], kind, r["as_of"])
        for oid in r["input_manifest_ids"]:
            matches = [e for e in ext.values() if e["object_id"] == oid and e["object_type"].endswith("_manifest")]
            require(any(time(e["available_at"]) <= time(r["as_of"]) for e in matches), f"missing/future input manifest {oid}")
        for mid in r["input_model_run_ids"]:
            require(mid in models, f"unknown input ModelRun {mid}")
            require(time(models[mid]["completed_at"]) <= time(r["as_of"]), "future model output")

    for m in models.values():
        require(m["run_manifest_id"] in manifests, "ModelRun missing RunManifest")
        r = manifests[m["run_manifest_id"]]
        p = m["structured_output"]
        require(p["model_run_id"] == m["model_run_id"], "proposal points to a different ModelRun")
        require(m["input_manifest_id"] in r["input_manifest_ids"], "model input not in RunManifest")
        require(r["as_of"] == p["analysis_as_of"] == m["as_of"], "model input cutoff mismatch")
        require(time(m["as_of"]) <= time(m["started_at"]) <= time(m["completed_at"]), "model time order")
        require(time(p["created_at"]) <= time(m["completed_at"]) < time(p["expires_at"]), "proposal expiry/order")
        version_ref(m["model_policy_ref"], "model_policy", m["as_of"])
        version_ref(p["strategy_ref"], "strategy", p["analysis_as_of"])
        require(p["strategy_ref"] in r["strategy_refs"], "strategy absent from ModelRun manifest")
        evidence(m["evidence_ids"], m["as_of"])
        evidence(p["evidence_ids"], p["analysis_as_of"])
        for dep in p["hard_dependencies"]:
            if dep["kind"] == "strategy":
                version_ref({"object_id": dep["object_id"], "version": dep["expected_version"]}, "strategy", p["analysis_as_of"])
            elif dep["kind"] == "plan":
                require((dep["object_id"], dep["expected_version"]) in plans, "missing hard plan dependency")
            else:
                external(dep["kind"], dep["object_id"], dep["expected_version"], p["analysis_as_of"])
        for dep in p["soft_dependencies"]:
            external(dep["kind"], dep["object_id"], dep["expected_version"], p["analysis_as_of"])
        s = strategies[p["strategy_ref"]["object_id"], p["strategy_ref"]["version"]]
        registry = {(x["feature_id"], x["definition_version"]): x["unit"] for x in s["required_features"]}
        for field in ("entry_conditions", "exit_conditions", "invalidation"):
            condition(p[field], registry)

    for p in plans.values():
        require(p["instrument_id"] == "SAMPLE_MAIN_001", "fixture must use a hypothetical instrument")
        require(p["proposal_id"] in proposals, "plan missing proposal")
        proposal = proposals[p["proposal_id"]]
        require(p["version"] == proposal["expected_plan_version"] + 1, "plan version is not expected + 1")
        require(p["plan_id"] == proposal["proposed_plan_id"], "plan identity changed")
        require(p["strategy_ref"] == proposal["strategy_ref"], "plan strategy differs from proposal")
        require(p["validated_hard_dependencies"] == proposal["hard_dependencies"], "fixture hard dependency versions differ")
        require(p["run_manifest_id"] in manifests, "plan missing compile RunManifest")
        require(p["decision_snapshot_id"] in decisions, "plan missing DecisionSnapshot")
        require(p["model_run_id"] in models, "plan missing ModelRun")
        require(time(models[p["model_run_id"]]["completed_at"]) <= time(p["compiled_at"]) <= time(p["valid_from"]), "plan precedes model result")
        require(time(p["valid_from"]) < time(p["expires_at"]), "plan already expired")
        require(Decimal(p["entry_price_zone"]["lower"]) > 0, "non-positive price")
        require(Decimal(p["entry_price_zone"]["lower"]) <= Decimal(p["entry_price_zone"]["upper"]), "reversed price zone")
        old_quotes = {d["object_id"]: d for d in proposal["soft_dependencies"] if d["kind"] == "quote"}
        for dep in p["revalidated_soft_dependencies"]:
            external(dep["kind"], dep["object_id"], dep["expected_version"], p["compiled_at"])
            if dep["kind"] == "quote":
                require(dep["object_id"] in old_quotes, "unknown quote to revalidate")
                old = old_quotes[dep["object_id"]]
                deviation = abs(Decimal(dep["reference_price"]) / Decimal(old["reference_price"]) - 1) * 10000
                require(deviation <= proposal["max_price_deviation_bps"], "quote outside allowed deviation")

    for d in decisions.values():
        require(d["run_manifest_id"] in manifests, "decision missing RunManifest")
        evidence(d["evidence_ids"], d["decision_at"])
        for kind in ("universe_policy", "market_state", "bundle"):
            version_ref(d[kind + "_ref"], kind, d["decision_at"])
        for mid in d["model_run_ids"]:
            require(mid in models and time(models[mid]["completed_at"]) <= time(d["decision_at"]), "decision uses missing/future model")
        for r in d["plan_refs"]:
            require((r["plan_id"], r["version"]) in plans, "decision references missing plan version")
            p = plans[r["plan_id"], r["version"]]
            require(p["decision_snapshot_id"] == d["snapshot_id"], "plan points to different snapshot")
            require(time(p["compiled_at"]) <= time(d["decision_at"]) < time(p["expires_at"]), "decision/plan time mismatch")
        for candidate in d["candidates"]:
            require(candidate["plan_ref"] in d["plan_refs"], "candidate plan absent from snapshot")
            require(candidate["calibrated_probability"] is None, "hypothetical rank score must not claim calibrated probability")

    for e in f["events"]:
        require(time(e["occurred_at"]) <= time(e["recorded_at"]), "event recording order")
        require(e["payload"]["snapshot_id"] in decisions, "event references missing snapshot")
        if e["event_type"] == "decision.published":
            require(e["aggregate_id"] == e["payload"]["snapshot_id"], "decision aggregate mismatch")


def main() -> None:
    schema = json.loads((ROOT / "definitions.schema.json").read_text(encoding="utf-8"))
    fixture = json.loads((ROOT / "fixture.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(fixture)
    check_links(fixture)

    def reject(label: str, mutate) -> None:
        broken = copy.deepcopy(fixture)
        mutate(broken)
        try:
            validator.validate(broken)
            check_links(broken)
        except (ValueError, ValidationError):
            print("PASS negative:", label)
            return
        raise ValueError("negative fixture was accepted: " + label)

    reject("no eval field in AST", lambda x: x["model_runs"][0]["structured_output"]["entry_conditions"].update(eval="price > 0"))
    reject("decimal operands are typed strings", lambda x: x["model_runs"][0]["structured_output"]["entry_conditions"]["children"][0]["right"].update(value=10.05))
    reject("bar_close clock needs timeframe", lambda x: x["model_runs"][0]["structured_output"]["entry_conditions"].update(clock={"kind": "bar_close"}))
    reject("timestamp format checked", lambda x: x["events"][0].update(recorded_at="not-a-time"))
    reject("future evidence unavailable", lambda x: next(e for e in x["external_references"] if e["object_type"] == "evidence_revision").update(available_at="2026-11-10T10:02:00+08:00"))
    reject("expected plan version enforced in fixture", lambda x: x["model_runs"][0]["structured_output"].update(expected_plan_version=1))
    reject("snapshot must reference existing plan", lambda x: x["decision_snapshots"][0]["plan_refs"][0].update(plan_id="missing-plan"))
    reject("soft quote tolerance checked", lambda x: x["trade_plans"][0]["revalidated_soft_dependencies"][0].update(reference_price="10.80"))
    print("PASS Draft 2020-12 schema meta-validation")
    print("PASS hypothetical fixture schema and selected link/time/unit checks")
    print("PASS 8 negative contract examples")
    print("Python:", sys.version.split()[0], "jsonschema:", importlib.metadata.version("jsonschema"))
    print("SCOPE: contracts only; no feed, LLM call, PlanCompiler, matching, ledger or complete system executed.")


if __name__ == "__main__":
    main()
