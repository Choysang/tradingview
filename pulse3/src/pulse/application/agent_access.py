"""Agent tool catalogue: the read-only MCP tools, derived from the same manifest that serves ``/v1/x/bf/ops/manifest`` and llms.txt.

Why: legacy triage #65 (MCP server). One source of truth: ``bf_ops_service.build_manifest`` lists every registered operation
(core ``core.api_binding`` rows + extension + mounted backfill routes). This module does not list operations of its own; it
holds only a *policy*, ``MCP_TOOL_IDS``: which manifest operations an outside agent may call as a tool. A tool is exactly one
manifest operation: its name is the ``operation_id``, its method/path/auth are the manifest's, its input schema comes from the
operation's own request definition (extension ``<op>Request`` or the OpenAPI parameters of the mounted route).
Invariants:
* read-only: only ``GET`` operations can be tools; an id in the policy that the manifest lists with another method is dropped
  and reported by ``unresolved`` (the tests fail on it), never silently turned into a write;
* nothing user-private by default: accounts, layouts, watchlists, annotations, alert rules, workspace, saved screens, screen
  runs, custom fillr runs and export artifacts are
  not in the policy (a later decision, with the write tools); the routes still enforce their own principal rules when called;
* the policy never invents an operation: an id that is not in the manifest is not a tool.
Not covered: transport and dispatch (``pulse.api.routes_bf_mcp``), write tools, streams (SSE / CSV are not JSON tools).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pulse import contracts

PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_REF_DEPTH = 8

#: Tool groups -> manifest operation ids. Order is the order of ``tools/list``.
MCP_TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "market": ("queryMarketToday", "queryMarketSentiment", "querySectorDetails", "getSectorOverview", "getSectorRanking",
               "getCandidatePool", "queryEventClusters", "queryCalendarEvents",
               "theme_map_v1_x_bf_board_theme_map_get", "crowd_v1_x_bf_fillm_crowd_get", "seal_clock_v1_x_bf_live_seal_clock_get",
               "getNexusHubOverview", "getNexusSectorDeepDive"),
    "rankings": ("getCurrentRelease", "getRelease", "getMarketRanking", "queryRankingHistory", "queryRankingTrackRecord",
                 "getTradePlan", "queryPlanVersions", "watch_cards_v1_x_bf_board_watch_cards_get"),
    "stock": ("instrument_search_v1_x_bf_board_instrument_search_get", "getStockWorkspace", "getStockQuote", "getStockBars", "getStockEvidence", "getStockCommunity", "getStockFundFlow",
              "getStockSeats", "queryAnalogCases", "queryIntervalStatistics", "queryCommunityOpinions",
              "querySeatDirectory", "querySeatProfile", "querySeatReports",
              "get_indicators_v1_x_bf_stock_indicators_get", "get_stats_v1_x_bf_stock_stats_get",
              "stock_stats_plus_v1_x_bf_fillm_stock_stats_plus_get", "seal_facts_v1_x_bf_live_seal_facts_get",
              "getNexusStockProAnalysis"),
    # Saved screens, screen runs and custom fillr runs are per-user (2026-11-23 review M6): left out with the other private data.
    "playbooks_screens": ("getFillrPlaybookBoard", "getFillrMonthlyReturns", "getFillrFactorSeries", "getFillrAnalogStudy",
                          "parseScreenText"),
    "research_ledger": ("getLabLedger", "getLabLedgerImports", "getLabLedgerEntry", "getLabRetiredTools", "getLabTearSheet",
                        "listLabBatchCompare", "getLabBatchCompare", "listStrategies", "queryStrategyVersion", "listPapers",
                        "getEvaluation", "getLeague", "playbook_replay_v1_x_bf_research_playbook_replay_get",
                        "regime_heatmap_v1_x_bf_research_regime_heatmap_get", "experiments_v1_x_bf_research_experiments_get",
                        "getNexusArenaOverview"),
    "forward_logs": ("getForwardMonitor", "getFusionStatus", "getFusionScores", "getFusionBooks", "getFusionExperiments",
                     "experiments_v1_x_bf_fwdlog_experiments_get",
                     "rows_v1_x_bf_fwdlog_experiments__key__rows_get", "paper_fills_v1_x_bf_fwdlog_paper__session_date__fills_get"),
    "data_health": ("getDataStatus", "queryDatasetStatus", "queryJobRuns", "getBatch6Overview", "getNexusIntegrityAudit"),
}
MCP_TOOL_IDS: tuple[str, ...] = tuple(oid for group in MCP_TOOL_GROUPS.values() for oid in group)
TOOL_GROUP: dict[str, str] = {oid: g for g, ids in MCP_TOOL_GROUPS.items() for oid in ids}


@dataclass(frozen=True)
class Tool:
    """One read-only tool = one manifest operation."""

    name: str
    description: str
    method: str
    path: str
    auth: str
    source: str
    contract_id: str | None
    group: str
    input_schema: dict[str, Any]
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    json_params: bool = False                    # extension requests: object/array values travel as JSON text

    def wire(self) -> dict[str, Any]:
        """The ``tools/list`` entry."""
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema,
                "annotations": {"title": self.name, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                "openWorldHint": False},
                "_meta": {"pulse": {"operation_id": self.name, "method": self.method, "path": self.path, "auth": self.auth,
                                    "source": self.source, "contract_id": self.contract_id, "group": self.group}}}


@dataclass(frozen=True)
class Catalogue:
    tools: tuple[Tool, ...]
    unresolved: tuple[str, ...] = field(default=())     # policy ids the manifest does not list as GET operations

    def by_name(self) -> dict[str, Tool]:
        return {t.name: t for t in self.tools}


# ---- schema helpers ------------------------------------------------------------------------------------------------------------
def _resolve_ref(ref: str, own: dict[str, Any]) -> dict[str, Any] | None:
    """``#/$defs/X`` (own document) or ``urn:pulse:contracts:v3#/$defs/X`` / ``...:ext:v1#/$defs/X``; anything else -> unknown."""
    base, _, frag = ref.partition("#")
    if base == "":
        doc = own
    elif base == contracts.V3_ID:
        doc = contracts.v3_schema()
    elif base == contracts.EXT_ID:
        doc = contracts._load("extension.schema.json")
    else:
        return None
    node: Any = doc
    for part in frag.lstrip("/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _inline(node: Any, own: dict[str, Any], seen: tuple[str, ...] = (), depth: int = 0) -> Any:
    """Copy of a JSON schema with every ``$ref`` replaced by its definition (cycles and unknown refs become ``{}``)."""
    if isinstance(node, list):
        return [_inline(x, own, seen, depth) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _resolve_ref(ref, own)
        if target is None or ref in seen or depth >= MAX_REF_DEPTH:
            return {}
        return _inline(target, own, seen + (ref,), depth + 1)
    return {k: _inline(v, own, seen, depth) for k, v in node.items() if k not in ("$id", "$schema", "title")}


def _ext_input(op_id: str) -> dict[str, Any] | None:
    doc = contracts._load("extension.schema.json")
    request = doc["$defs"].get(f"{op_id}Request")
    if request is None:
        return None
    props = {name: _inline(schema, doc) for name, schema in request.get("properties", {}).items()}
    return {"type": "object", "properties": props, "required": list(request.get("required", [])), "additionalProperties": False}


def _openapi_input(operation: dict[str, Any], path_params: tuple[str, ...]) -> tuple[dict[str, Any], tuple[str, ...]]:
    props: dict[str, Any] = {}
    required: list[str] = []
    queries: list[str] = []
    for p in operation.get("parameters", []):
        if p.get("in") not in ("path", "query"):
            continue
        schema = {k: v for k, v in p.get("schema", {}).items() if k != "title"}
        if p.get("description"):
            schema["description"] = p["description"]
        props[p["name"]] = schema
        if p.get("required") or p["name"] in path_params:
            required.append(p["name"])
        if p["in"] == "query":
            queries.append(p["name"])
    for name in path_params:                       # a template variable the route did not declare is still required
        props.setdefault(name, {"type": "string"})
        if name not in required:
            required.append(name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}, tuple(queries)


def _description(op: dict[str, Any], group: str) -> str:
    summary = (op.get("summary") or "").replace("\n", " ").strip()
    head = f"{op['operation_id']}" + (f": {summary}" if summary else "")
    return f"{head}. Read-only ({group}). REST GET {op['path']} [auth: {op['auth']}]; returns the same {{meta, data}} envelope."


def build_catalogue(manifest: dict[str, Any], openapi_paths: dict[str, Any]) -> Catalogue:
    """Tools = the policy ids that the manifest lists as GET operations, in policy order."""
    listed = {o["operation_id"]: o for o in manifest["operations"] if o["method"] == "GET"}
    tools: list[Tool] = []
    unresolved: list[str] = []
    for oid in MCP_TOOL_IDS:
        op = listed.get(oid)
        if op is None:
            unresolved.append(oid)
            continue
        path_params = tuple(PATH_PARAM.findall(op["path"]))
        schema = _ext_input(oid) if op["source"] == "ext" else None
        if schema is not None:
            queries, json_params = tuple(schema["properties"]), True
        else:
            declared = openapi_paths.get(op["path"], {}).get("get")
            if declared is None:
                unresolved.append(oid)
                continue
            schema, queries = _openapi_input(declared, path_params)
            json_params = False
        tools.append(Tool(name=oid, description=_description(op, TOOL_GROUP[oid]), method="GET", path=op["path"], auth=op["auth"],
                          source=op["source"], contract_id=op.get("contract_id"), group=TOOL_GROUP[oid], input_schema=schema,
                          path_params=path_params, query_params=queries, json_params=json_params))
    return Catalogue(tuple(tools), tuple(unresolved))
