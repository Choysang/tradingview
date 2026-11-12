"""Contract wire builders for the strategy registry (``Strategy`` / ``StrategyList``).

Why: the API serves registry rows through the one v3 contract; this keeps the mapping
(DefinitionSeed/InstanceSeed/TagSeed -> Strategy.instances/tags) in one tested place.
Invariants: ``specification`` is the stored object verbatim; tags are TaxonomyNodeRefs.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from pulse.contracts import to_wire
from pulse.domain.strategy.seed import DefinitionSeed, InstanceSeed, TagSeed


def strategy_wire(d: DefinitionSeed, instances: Sequence[InstanceSeed], tags: Sequence[TagSeed]) -> dict[str, Any]:
    return to_wire(
        {
            "strategy_id": d.strategy_id, "strategy_version": d.strategy_version, "display_name": d.display_name,
            "role": d.role, "method_family": d.method_family, "forecast_target": d.forecast_target,
            "horizon_id": d.horizon_id, "definition_hash": d.definition_hash, "specification": dict(d.specification),
            "tags": [
                {"taxonomy_id": t.taxonomy_id, "taxonomy_version": t.taxonomy_version, "node_id": t.node_id}
                for t in tags if t.strategy_id == d.strategy_id and t.strategy_version == d.strategy_version
            ],
            "instances": [
                {"instance_id": i.instance_id, "strategy_id": i.strategy_id, "strategy_version": i.strategy_version,
                 "parameter_hash": i.parameter_hash, "universe_policy_version": i.universe_policy_version,
                 "lifecycle": i.lifecycle, "implementation_status": i.implementation_status,
                 "reproduction_status": i.reproduction_status, "health": i.health}
                for i in instances if i.strategy_id == d.strategy_id and i.strategy_version == d.strategy_version
            ],
        }
    )


def strategy_list_wire(seed_defs: Sequence[DefinitionSeed], insts: Sequence[InstanceSeed], tags: Sequence[TagSeed]) -> dict[str, Any]:
    by_inst: dict[Any, list[InstanceSeed]] = defaultdict(list)
    for i in insts:
        by_inst[(i.strategy_id, i.strategy_version)].append(i)
    return {"items": [strategy_wire(d, by_inst[(d.strategy_id, d.strategy_version)], tags) for d in seed_defs]}
