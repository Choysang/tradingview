"""A small, safe factor language: JSON trees compiled to Polars expressions (no eval).

node := {"col": NAME} | {"const": NUMBER} | {"op": OP, "args": [node, ...], "n": INT}
Time-series ops run per stock over past bars only (shift n >= 1, rolling windows ending today).
"""
from __future__ import annotations

import polars as pl

from backend.app.playbooks.library import CODE
from backend.app.playbooks.library_sentiment import float_cap_pit

c = pl.col
EPS = 1e-9
COLUMNS = {
    "open": c("open"), "high": c("high"), "low": c("low"), "close": c("close"), "prev_close": c("prev_close"),
    "volume": c("volume"), "amount": c("amount"), "pct_chg": c("pct_chg"),
    "turn": c("amount") / float_cap_pit() * 100, "float_cap": float_cap_pit(),
    "ma5": c("ma5"), "ma20": c("ma20"), "ma60": c("ma60"), "rsi_14": c("rsi_14"), "volatility_20": c("volatility_20"),
    "is_limit_up": c("is_limit_up").cast(pl.Float64), "mkt_ret": c("pct_chg").mean().over("date"),
}
UNARY = {
    "neg": lambda a: -a, "abs": lambda a: a.abs(), "sign": lambda a: a.sign(),
    "log": lambda a: (a.abs() + EPS).log(), "sqrt": lambda a: a.abs().sqrt(),
    "rank": lambda a: a.rank().over("date") / a.count().over("date"),
    "demean": lambda a: a - a.mean().over("date"),
}
BINARY = {
    "add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
    "div": lambda a, b: pl.when(b.abs() > EPS).then(a / b), "max2": lambda a, b: pl.max_horizontal(a, b),
    "min2": lambda a, b: pl.min_horizontal(a, b),
}
TS = {
    "mean": lambda a, n: a.rolling_mean(n).over(CODE), "std": lambda a, n: a.rolling_std(n).over(CODE),
    "max": lambda a, n: a.rolling_max(n).over(CODE), "min": lambda a, n: a.rolling_min(n).over(CODE),
    "sum": lambda a, n: a.rolling_sum(n).over(CODE), "shift": lambda a, n: a.shift(n).over(CODE),
    "delta": lambda a, n: (a - a.shift(n)).over(CODE),
    "pct": lambda a, n: pl.when(a.shift(n).abs() > EPS).then(a / a.shift(n) - 1).over(CODE),
    "zscore": lambda a, n: ((a - a.rolling_mean(n)) / (a.rolling_std(n) + EPS)).over(CODE),
    "skew": lambda a, n: a.rolling_skew(n).over(CODE),
}
TS2 = {"corr": lambda a, b, n: pl.rolling_corr(a, b, window_size=n).over(CODE)}
MAX_NODES, MAX_DEPTH, MAX_N = 30, 7, 250


class DSLError(ValueError):
    pass


def compile_node(node: dict, depth: int = 0, count: list | None = None) -> pl.Expr:
    count = count if count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES or depth > MAX_DEPTH:
        raise DSLError("expression too large")
    if not isinstance(node, dict):
        raise DSLError(f"node must be an object: {node!r}")
    if "col" in node:
        if node["col"] not in COLUMNS:
            raise DSLError(f"unknown column {node['col']}")
        return COLUMNS[node["col"]]
    if "const" in node:
        value = node["const"]
        if not isinstance(value, (int, float)):
            raise DSLError("const must be a number")
        return pl.lit(float(value))
    op, args = node.get("op"), node.get("args", [])
    if not isinstance(args, list):
        raise DSLError("args must be a list")
    sub = [compile_node(a, depth + 1, count) for a in args]
    if op in UNARY and len(sub) == 1:
        return UNARY[op](sub[0])
    if op in BINARY and len(sub) == 2:
        return BINARY[op](*sub)
    n = node.get("n")
    if op in TS or op in TS2:
        if not isinstance(n, int) or not 1 <= n <= MAX_N:
            raise DSLError(f"{op} needs an integer n in 1..{MAX_N}")
        if op in TS and len(sub) == 1:
            if op != "shift" and n < 2 and op in ("std", "skew", "zscore"):
                raise DSLError(f"{op} needs n >= 2")
            return TS[op](sub[0], n)
        if op in TS2 and len(sub) == 2:
            if n < 3:
                raise DSLError("corr needs n >= 3")
            return TS2[op](sub[0], sub[1], n)
    raise DSLError(f"bad op {op!r} with {len(sub)} args")


SPEC = """Factor language (JSON). A node is one of:
  {"col": NAME}            NAME in: open, high, low, close, prev_close, volume, amount (亿元), pct_chg (%),
                           turn (turnover %, amount / float cap), float_cap (亿元), ma5, ma20, ma60, rsi_14,
                           volatility_20, is_limit_up (0/1), mkt_ret (same-day mean pct_chg of all stocks)
  {"const": NUMBER}
  {"op": OP, "args": [...], "n": INT}
Unary (1 arg): neg, abs, sign, log (of |x|), sqrt (of |x|), rank (cross-sectional percentile that day), demean (minus that day's mean)
Binary (2 args): add, sub, mul, div (null when the divisor is ~0), max2, min2
Per-stock time series over past bars only (1 arg + n, 1 <= n <= 250): mean, std, max, min, sum, shift, delta (x - x n bars ago),
  pct (x / x n bars ago - 1), zscore (vs its own n-bar mean/std), skew
Per-stock rolling correlation (2 args + n): corr
At most 30 nodes and depth 7. Values are known at the day's close; trades happen at the next open."""
