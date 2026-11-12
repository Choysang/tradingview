"""ScreenAst: build, validate and evaluate (three-valued) the whitelisted screening condition tree.

Why: PEX10 ScreenAst = the v3 ``Condition`` subset (all / any / not / compare) + sort; "禁止任意SQL/脚本；最大深度、节点、
窗口与计算预算". Evaluation reuses ``pulse.domain.plans.conditions.evaluate`` (Kleene TRUE/FALSE/UNKNOWN, units must match,
a missing input is UNKNOWN - never 0), so a screen and a plan trigger can never disagree about a condition.
Invariants: only the four node kinds and the five compare operators are accepted; every feature operand names a catalog
field with exactly that field's unit; constants are decimal strings in the same unit; depth <= MAX_DEPTH, nodes <=
MAX_NODES; ``instrument_id`` of an operand is the subject placeholder (each candidate). The sort always ends with the
instrument id (stable tie-break); a missing sort value is never treated as 0 (``nulls`` first/last is explicit).
Not covered: crosses / within_session / event_state nodes (rejected: ``SCREEN_NODE_UNSUPPORTED``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from typing import Any

from pulse.contracts import decimal_str
from pulse.domain.plans import conditions as C

from . import catalog as K

MAX_DEPTH = 6
MAX_NODES = 64
OPERATORS = ("lt", "lte", "gt", "gte", "eq")
CLOCK = C.bar_close_clock("1d", K.SESSION_POLICY)
DEFAULT_SORT = [{"field_id": "pct_chg", "direction": "desc", "nulls": "last"}]


class ScreenAstError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- builders ------------------------------------------------------------------------------------------------
def operand(field_id: str) -> dict[str, Any]:
    f = K.BY_ID[field_id]
    return C.feature_operand(feature_id=field_id, instrument_id=K.SUBJECT_ID, timeframe="1d", field_path="value", unit=f.unit,
                             definition_version=K.FIELD_VERSION, resolution_policy_version=K.RESOLUTION_POLICY)


def cmp(field_id: str, op: str, value: float | int | str) -> dict[str, Any]:
    f = K.BY_ID[field_id]
    return C.compare(op, operand(field_id), C.constant(Decimal(str(value)), f.unit), CLOCK)


def all_of(*children: dict) -> dict:
    return children[0] if len(children) == 1 else C.all_of(CLOCK, *children)


def any_of(*children: dict) -> dict:
    return children[0] if len(children) == 1 else C.any_of(CLOCK, *children)


def make_ast(condition: dict, sort: list[dict] | None = None) -> dict[str, Any]:
    return {"condition": condition, "allowed_field_catalog_version": K.CATALOG_VERSION, "sort": list(sort or DEFAULT_SORT)}


# ---- validation ----------------------------------------------------------------------------------------------
def validate_ast(ast: dict[str, Any], extra: Mapping[str, str] | None = None) -> None:
    """``extra`` = additional {field id: unit} the caller authorises (signal.<playbook> of the chosen strategy instances)."""
    if ast.get("allowed_field_catalog_version") != K.CATALOG_VERSION:
        raise ScreenAstError("SCREEN_CATALOG_MISMATCH", f"字段目录版本应为 {K.CATALOG_VERSION}")
    count = [0]

    def walk(node: dict, depth: int) -> None:
        count[0] += 1
        if depth > MAX_DEPTH or count[0] > MAX_NODES:
            raise ScreenAstError("SCREEN_BUDGET_EXCEEDED", f"条件树超出预算（深度 ≤ {MAX_DEPTH}，节点 ≤ {MAX_NODES}）")
        kind = node.get("node")
        if kind in ("all", "any"):
            for ch in node.get("children", []):
                walk(ch, depth + 1)
        elif kind == "not":
            walk(node["child"], depth + 1)
        elif kind == "compare":
            if node["operator"] not in OPERATORS:
                raise ScreenAstError("SCREEN_OPERATOR_UNSUPPORTED", f"不支持的比较符 {node['operator']}")
            for side in ("left", "right"):
                op = node[side]
                if op["kind"] == "feature":
                    f = K.BY_ID.get(op["feature_id"])
                    if f is None and extra and op["feature_id"] in extra:
                        f = K.Field(op["feature_id"], op["feature_id"], extra[op["feature_id"]], "strategy signal", "signal")
                    if f is None:
                        raise ScreenAstError("SCREEN_FIELD_UNKNOWN", f"字段不在目录：{op['feature_id']}")
                    if op["unit"] != f.unit:
                        raise ScreenAstError("SCREEN_UNIT_MISMATCH", f"{f.id} 的单位应为 {f.unit}，收到 {op['unit']}")
                else:
                    try:
                        Decimal(op["value"])
                    except (InvalidOperation, TypeError) as e:
                        raise ScreenAstError("SCREEN_CONSTANT_INVALID", "常量不是十进制数") from e
            if node["left"]["unit"] != node["right"]["unit"]:
                raise ScreenAstError("SCREEN_UNIT_MISMATCH", "比较两侧单位不一致，不做隐式换算")
        else:
            raise ScreenAstError("SCREEN_NODE_UNSUPPORTED", f"筛选只支持 all/any/not/compare，收到 {kind!r}")

    if not ast.get("condition"):
        raise ScreenAstError("SCREEN_EMPTY", "条件为空")
    walk(ast["condition"], 1)
    for s in ast.get("sort", []):
        if s["field_id"] not in K.BY_ID and not (extra and s["field_id"] in extra):
            raise ScreenAstError("SCREEN_FIELD_UNKNOWN", f"排序字段不在目录：{s['field_id']}")
        if s["direction"] not in ("asc", "desc") or s["nulls"] not in ("first", "last"):
            raise ScreenAstError("SCREEN_SORT_INVALID", "排序方向/空值位置无效")


def ast_hash(ast: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(ast, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def used_fields(ast: dict[str, Any]) -> list[str]:
    seen: list[str] = []

    def walk(n: dict) -> None:
        if n["node"] in ("all", "any"):
            for c in n["children"]:
                walk(c)
        elif n["node"] == "not":
            walk(n["child"])
        elif n["node"] == "compare":
            for side in ("left", "right"):
                if n[side]["kind"] == "feature" and n[side]["feature_id"] not in seen:
                    seen.append(n[side]["feature_id"])

    walk(ast["condition"])
    return seen


# ---- evaluation ----------------------------------------------------------------------------------------------
@dataclass
class RowContext:
    """EvalContext of ONE candidate: operands resolve to that instrument's catalog field values."""
    now: datetime
    values: dict[str, float | None]

    def current(self, op: dict[str, Any]) -> C.Observation | None:
        v = self.values.get(op["feature_id"])
        if v is None:
            return None
        return C.Observation(Decimal(repr(round(float(v), 6))), self.now, True)

    def previous(self, op):  # not used: crosses is rejected by validate_ast
        return None

    def session_kind(self):
        return None

    def event(self, event_id):
        return None


def evaluate_row(ast: dict[str, Any], values: dict[str, float | None], now: datetime | None = None) -> C.Result:
    return C.evaluate(ast["condition"], RowContext(now or datetime.now(timezone.utc), values))


def _sort_key(spec: dict, row_values: dict[str, float | None], iid: str):
    out = []
    for s in spec:
        v = row_values.get(s["field_id"])
        missing = v is None
        rank = (1 if missing else 0) if s["nulls"] == "last" else (0 if missing else 1)
        num = 0.0 if missing else (-v if s["direction"] == "desc" else v)
        out.append((rank, num))
    out.append((0, iid))
    return tuple(out)


def order_rows(spec: list[dict], rows: list[tuple[str, dict[str, float | None]]]) -> list[str]:
    """Instrument ids ordered by the sort spec; ties and missing handled explicitly, ends with the instrument id."""
    return [iid for iid, _ in sorted(rows, key=lambda r: _sort_key(spec, r[1], r[0]))]


def wire_value(v: float | None) -> str | None:
    return None if v is None else decimal_str(Decimal(repr(round(float(v), 6))))
