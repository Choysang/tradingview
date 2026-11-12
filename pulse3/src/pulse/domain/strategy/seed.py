"""Turn the package catalogs into registry seed rows (candidates only).

Why: SPEC §1.1 / §9 and the catalog READMEs: 61 strategy candidates and 105 factor
candidates start the registry. They are *candidates*: lifecycle ``discovered``,
implementation ``not_implemented``, reproduction ``not_started``, health ``unavailable``.
Nothing here can mark a row ``active`` - promotion is a separate evidence-gated step.
Invariants: ids are deterministic UUIDv5 under the catalog's own namespace (the strategy
catalog already carries ``strategy_id`` - it is used as-is, and cross-checked against the
documented derivation); the full catalog entry is stored as the definition specification so
no catalog fact is lost; tags come from the entry's ``taxonomy_tags`` plus its family.
Not covered: DB writes (strategy_repo.seed_catalogs), horizon/target registration per
instance (``exact_horizon_must_be_registered_per_instance`` stays true).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid5

from pulse.domain.strategy.canonical import canonical_hash

TAXONOMY_VERSION = 1


@dataclass(frozen=True)
class DefinitionSeed:
    catalog_id: str
    strategy_id: UUID
    strategy_version: int
    display_name: str
    role: str
    method_family: str
    forecast_target: str
    horizon_id: str
    specification: Mapping[str, Any]
    definition_hash: str


@dataclass(frozen=True)
class InstanceSeed:
    instance_id: UUID
    strategy_id: UUID
    strategy_version: int
    parameters: Mapping[str, Any]
    parameter_hash: str
    universe_policy_version: str
    lifecycle: str = "discovered"
    implementation_status: str = "not_implemented"
    reproduction_status: str = "not_started"
    health: str = "unavailable"


@dataclass(frozen=True)
class TagSeed:
    strategy_id: UUID
    strategy_version: int
    taxonomy_id: str
    node_id: str
    reason_ref: str
    taxonomy_version: int = TAXONOMY_VERSION


@dataclass(frozen=True)
class CatalogSeed:
    definitions: tuple[DefinitionSeed, ...]
    instances: tuple[InstanceSeed, ...]
    tags: tuple[TagSeed, ...]
    taxonomy_nodes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # taxonomy_id -> node ids


def _namespace(strategy_catalog: Mapping[str, Any]) -> UUID:
    ns = UUID(strategy_catalog["identity_policy"]["namespace_uuid"])
    return ns


def seed_strategies(catalog: Mapping[str, Any]) -> CatalogSeed:
    ns = _namespace(catalog)
    defs: list[DefinitionSeed] = []
    insts: list[InstanceSeed] = []
    tags: list[TagSeed] = []
    for e in catalog["entries"]:
        sid = UUID(e["strategy_id"])
        if sid != uuid5(ns, f"strategy/{e['catalog_id']}"):
            raise ValueError(f"{e['catalog_id']}: strategy_id does not match the documented UUIDv5 derivation")
        if e["lifecycle_state"] != "discovered" or e["implementation_status"] != "not_implemented":
            raise ValueError(f"{e['catalog_id']}: catalog candidate must start discovered/not_implemented")
        ver = int(e["strategy_version"])
        horizon = e["horizon_contract"].get("runtime_horizon_id") or e["horizon_contract"]["primary_tag"]
        defs.append(DefinitionSeed(
            catalog_id=e["catalog_id"], strategy_id=sid, strategy_version=ver, display_name=e["name"],
            role=e["primary_role"], method_family=e["method_family"],
            forecast_target=f"candidate_target_unregistered:{e['primary_role']}", horizon_id=horizon,
            specification=e, definition_hash=canonical_hash(e),
        ))
        params = {"parameter_candidates": e.get("parameter_candidates", {}), "parameter_status": e.get("parameter_status")}
        insts.append(InstanceSeed(
            instance_id=uuid5(ns, f"instance/{e['catalog_id']}/v{ver}/candidate"), strategy_id=sid, strategy_version=ver,
            parameters=params, parameter_hash=canonical_hash(params),
            universe_policy_version=f"{e['universe_policy_ref']}/v1",
        ))
        facets: dict[str, list[str]] = {k: list(v) for k, v in e.get("taxonomy_tags", {}).items()}
        facets.setdefault("strategy.family", []).append(e["family_id"])
        for facet, values in sorted(facets.items()):
            for v in sorted(set(values)):
                tags.append(TagSeed(sid, ver, facet, v, f"catalog:{e['catalog_id']}"))
    return CatalogSeed(tuple(defs), tuple(insts), tuple(tags), _nodes(tags))


def seed_factors(factor_catalog: Mapping[str, Any], strategy_catalog: Mapping[str, Any]) -> CatalogSeed:
    ns = _namespace(strategy_catalog)
    defs: list[DefinitionSeed] = []
    insts: list[InstanceSeed] = []
    tags: list[TagSeed] = []
    for e in factor_catalog["entries"]:
        if e["implementation_status"] != "not_implemented":
            raise ValueError(f"{e['factor_id']}: factor candidate must be not_implemented")
        sid = uuid5(ns, f"factor/{e['factor_id']}")
        defs.append(DefinitionSeed(
            catalog_id=e["factor_id"], strategy_id=sid, strategy_version=1, display_name=e["name"],
            role="selection", method_family="factor_candidate",
            forecast_target="factor_candidate_unregistered", horizon_id="unspecified",
            specification=e, definition_hash=canonical_hash(e),
        ))
        insts.append(InstanceSeed(
            instance_id=uuid5(ns, f"instance/{e['factor_id']}/v1/candidate"), strategy_id=sid, strategy_version=1,
            parameters={}, parameter_hash=canonical_hash({}), universe_policy_version="unregistered",
        ))
        tags.append(TagSeed(sid, 1, "factor.family", e["family_id"], f"catalog:{e['factor_id']}"))
        for s in sorted(set(e.get("scope", []))):
            tags.append(TagSeed(sid, 1, "factor.scope", s, f"catalog:{e['factor_id']}"))
        if e.get("output_unit") is not None:  # a missing unit stays missing, it is not tagged
            tags.append(TagSeed(sid, 1, "factor.output_unit", e["output_unit"], f"catalog:{e['factor_id']}"))
    return CatalogSeed(tuple(defs), tuple(insts), tuple(tags), _nodes(tags))


def _nodes(tags: list[TagSeed]) -> dict[str, tuple[str, ...]]:
    nodes: dict[str, set[str]] = {}
    for t in tags:
        nodes.setdefault(t.taxonomy_id, set()).add(t.node_id)
    return {k: tuple(sorted(v)) for k, v in sorted(nodes.items())}
