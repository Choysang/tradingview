"""Pure logic of the native reference-data producers: frames, revisions, refresh planning, reconcile comparison (Step 2, lane D).

Why: everything that decides WHAT is published (which rows are new revisions, which codes need a refetch, how a native
dataset differs from the legacy-imported one) is kept free of PostgreSQL and HTTP so it is tested on small synthetic
frames. ``refdata_service.py`` wires it to the lake / PG; ``workers/refdata.py`` is the CLI.

Invariants: frames carry EXACTLY the legacy schemas (``bars_legacy_fwd_adjusted_1d``, ``index_bars_legacy_1d``,
``sector_membership_legacy_boards_industry``) so a consumer switch is a dataset-id change; a revision is appended only
when a value changes (or a bar becomes final) and nothing is ever overwritten; ``available_at`` is the fetch time; the
reconcile functions only read frames and return plain dicts.
Not covered: persistence, HTTP, scheduling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Mapping, Sequence

import polars as pl

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
SESSION_DONE = time(15, 5)           # bars dated on/after "today" are unfinished before this time (old session_cut)
READJUST_TOLERANCE = 0.005           # stored close vs published previous close: more than this means an ex-right day
RECENT_DAYS = 45                     # stored tail used to detect an ex-right day (about 30 trading bars)
REL_TOL = 1e-6

ADJ_DATASET = "bars_native_fwd_adjusted_1d"
INDEX_DATASET = "index_bars_native_1d"
MEMBERSHIP_DATASETS = {"industry": "sector_membership_native_boards_industry", "concept": "sector_membership_native_boards_concept"}
LISTING_DATASET = "instrument_listing_native"
LEGACY_ADJ_DATASET = "bars_legacy_fwd_adjusted_1d"
LEGACY_INDEX_DATASET = "index_bars_legacy_1d"
LEGACY_INDUSTRY_DATASET = "sector_membership_legacy_boards_industry"

_DEC = pl.Decimal(38, 12)
ADJ_SCHEMA = {"instrument_id": pl.String, "trade_date": pl.Date, "timeframe": pl.String, "bar_open_time": pl.Datetime("us", "UTC"),
              "bar_close_time": pl.Datetime("us", "UTC"), "open_price": _DEC, "high_price": _DEC, "low_price": _DEC, "close_price": _DEC,
              "volume_shares": pl.Int64, "turnover_amount_cny": _DEC, "adjustment_mode": pl.String, "adjustment_version": pl.String,
              "is_final": pl.Boolean, "available_at": pl.Datetime("us", "UTC"), "quality_status": pl.String, "revision": pl.Int32}
INDEX_SCHEMA = {"instrument_id": pl.String, "symbol": pl.String, "date": pl.Date, "timeframe": pl.String, "open": pl.Float64,
                "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64, "price_basis": pl.String,
                "ingested_at": pl.Datetime("us", "UTC")}
MEMBERSHIP_SCHEMA = {"instrument_id": pl.String, "node_id": pl.String, "effective_from": pl.Datetime("us", "UTC"),
                     "available_at": pl.Datetime("us", "UTC")}
LISTING_SCHEMA = {"instrument_id": pl.String, "code": pl.String, "listed_on": pl.Date, "exchange": pl.String,
                  "available_at": pl.Datetime("us", "UTC")}
BAR_KEY = ["instrument_id", "trade_date"]
BAR_VALUES = ["open_price", "high_price", "low_price", "close_price", "volume_shares", "turnover_amount_cny"]
INDEX_VALUES = ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------------------------------------------
# session finality
# ---------------------------------------------------------------------------------------------------------------
def session_cut(now: datetime) -> date | None:
    """Bars dated on or after this day are unfinished and must not be stored: today during a weekday before 15:05 CST."""
    local = now.astimezone(CST)
    return local.date() if local.weekday() < 5 and local.time() < SESSION_DONE else None


def complete_rows(rows: Iterable[dict], cut: date | None, *, start: date | None = None, end: date | None = None) -> list[dict]:
    return [r for r in rows if (cut is None or r["date"] < cut) and (start is None or r["date"] >= start) and (end is None or r["date"] <= end)]


# ---------------------------------------------------------------------------------------------------------------
# revisions (adjusted bars, index bars)
# ---------------------------------------------------------------------------------------------------------------
def empty_bars() -> pl.DataFrame:
    return pl.DataFrame(schema=ADJ_SCHEMA)


def next_revisions(existing: pl.DataFrame | None, fresh: pl.DataFrame) -> pl.DataFrame:
    """Rows of ``fresh`` (revision column ignored) that are new facts against the latest stored revision per key.

    A key never seen -> revision 1. A key whose OHLC / volume / turnover differ (a new ex-right event rewrote the past)
    -> stored latest revision + 1. A bar that was not final and now is -> revision + 1 with unchanged values (the
    finality evidence is part of the fact). Identical re-fetches produce nothing."""
    if fresh.is_empty():
        return empty_bars()
    fresh = fresh.select(list(ADJ_SCHEMA)).with_columns(pl.col("revision").cast(pl.Int32))
    if existing is None or existing.is_empty():
        return fresh.with_columns(pl.lit(1, dtype=pl.Int32).alias("revision"))
    latest = (existing.sort("revision").unique(BAR_KEY, keep="last")
              .select(BAR_KEY + BAR_VALUES + ["is_final", "revision"])
              .rename({c: f"{c}_old" for c in BAR_VALUES + ["is_final", "revision"]}))
    joined = fresh.join(latest, on=BAR_KEY, how="left")
    differs = pl.any_horizontal(*[pl.col(c).ne_missing(pl.col(f"{c}_old")) for c in BAR_VALUES])
    promoted = pl.col("is_final") & ~pl.col("is_final_old")
    out = joined.filter(pl.col("revision_old").is_null() | differs | promoted)
    return (out.with_columns((pl.col("revision_old").fill_null(0) + 1).cast(pl.Int32).alias("revision"))
            .select(list(ADJ_SCHEMA)))


def next_index_versions(existing: pl.DataFrame | None, fresh: pl.DataFrame) -> pl.DataFrame:
    """Index rows that are new versions: never seen, or a value changed against the latest stored version of (symbol, date)."""
    if fresh.is_empty():
        return pl.DataFrame(schema=INDEX_SCHEMA)
    fresh = fresh.select(list(INDEX_SCHEMA))
    if existing is None or existing.is_empty():
        return fresh
    latest = (existing.sort("ingested_at").unique(["symbol", "date"], keep="last").select(["symbol", "date"] + INDEX_VALUES)
              .rename({c: f"{c}_old" for c in INDEX_VALUES}))
    joined = fresh.join(latest, on=["symbol", "date"], how="left")
    seen = pl.col("close_old").is_not_null() | pl.col("open_old").is_not_null()
    differs = pl.any_horizontal(*[pl.col(c).ne_missing(pl.col(f"{c}_old")) for c in INDEX_VALUES])
    return joined.filter(~seen | differs).select(list(INDEX_SCHEMA))


# ---------------------------------------------------------------------------------------------------------------
# refresh planning (port of the old plan_daily, over the latest stored closes)
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RefreshPlan:
    append: dict[str, dict] = field(default_factory=dict)      # code -> quote row to append as today's bar
    refetch: dict[str, str] = field(default_factory=dict)      # code -> "gap" | "readjusted" | "new"
    skipped_suspended: int = 0
    up_to_date: int = 0


def plan_refresh(quotes: Sequence[dict], tail: Mapping[str, Mapping[date, float]], codes: Iterable[str],
                 cut: date | None, known: Iterable[str] | None = None) -> RefreshPlan:
    """Split codes by what today's closing quote tells us.

    ``quotes`` rows carry ``code``, ``date`` (quote day), ``volume`` (shares), ``prev_close`` (the previous close as
    PUBLISHED: on an ex-right day this is the exchange's reference price) and OHLC; ``tail`` is the stored latest-revision
    close per (code, date). A code is appended when it traded, the quote day is a finished session not yet stored and the
    published previous close matches the stored close of the last market day before it (within ``READJUST_TOLERANCE``);
    when the previous close moved the code is "readjusted", when the stored tail does not reach the previous session it is
    a "gap", a code without any stored bar (not in ``known``, default: codes with a non-empty tail) is "new" (its whole
    history is fetched). Suspended codes (volume 0) are skipped."""
    known_set = set(known) if known is not None else {c for c, t in tail.items() if t}
    market_days = sorted({d for closes in tail.values() for d in closes})
    by_code = {q["code"]: q for q in quotes}
    append: dict[str, dict] = {}
    refetch: dict[str, str] = {}
    suspended = fresh = 0
    for code in codes:
        if code not in known_set:
            refetch[code] = "new"
            continue
        stored = tail.get(code) or {}
        q = by_code.get(code)
        if q is None:
            continue
        if not q["volume"] or q["volume"] <= 0:
            suspended += 1
            continue
        before = [d for d in market_days if d < q["date"]]
        last = stored.get(before[-1]) if before else None
        if last is None or not q.get("prev_close"):
            refetch[code] = "gap"
        elif abs(last / q["prev_close"] - 1) > READJUST_TOLERANCE:
            refetch[code] = "readjusted"
        elif (stored and max(stored) >= q["date"]) or (cut is not None and q["date"] >= cut):
            fresh += 1                                   # already stored, or the quote day is still unfinished
        else:
            append[code] = q
    return RefreshPlan(append, refetch, suspended, fresh)


# ---------------------------------------------------------------------------------------------------------------
# reconcile (read only)
# ---------------------------------------------------------------------------------------------------------------
def latest_bars(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.sort("revision").unique(BAR_KEY, keep="last")


def _rel(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return (a.cast(pl.Float64) - b.cast(pl.Float64)).abs() / b.cast(pl.Float64).abs().clip(lower_bound=1e-12)


def compare_bars(native: pl.DataFrame, legacy: pl.DataFrame, *, rel_tol: float = REL_TOL, examples: int = 5) -> dict:
    """Latest revision of each dataset over their common date window: row counts and cells that differ beyond ``rel_tol``."""
    n, g = latest_bars(native), latest_bars(legacy)
    if n.is_empty() or g.is_empty():
        return {"native_rows": n.height, "legacy_rows": g.height, "overlap_rows": 0, "note": "one side is empty"}
    lo, hi = max(n["trade_date"].min(), g["trade_date"].min()), min(n["trade_date"].max(), g["trade_date"].max())
    window = (pl.col("trade_date") >= lo) & (pl.col("trade_date") <= hi)
    n_w, g_w = n.filter(window), g.filter(window)
    joined = n_w.join(g_w, on=BAR_KEY, how="inner", suffix="_legacy")
    codes_n, codes_g = set(n_w["instrument_id"].to_list()), set(g_w["instrument_id"].to_list())
    out: dict = {"window": f"{lo}..{hi}", "native_rows": n.height, "legacy_rows": g.height, "native_rows_in_window": n_w.height,
                 "legacy_rows_in_window": g_w.height, "overlap_rows": joined.height,
                 "native_only_rows": n_w.join(g_w, on=BAR_KEY, how="anti").height,
                 "legacy_only_rows": g_w.join(n_w, on=BAR_KEY, how="anti").height,
                 "native_only_instruments": len(codes_n - codes_g), "legacy_only_instruments": len(codes_g - codes_n),
                 "rel_tol": rel_tol, "columns": {}}
    for c in BAR_VALUES:
        a, b = pl.col(c), pl.col(f"{c}_legacy")
        both = joined.filter(a.is_not_null() & b.is_not_null())
        null_mismatch = joined.filter(a.is_null() != b.is_null()).height
        diff = both.filter(_rel(a, b) > rel_tol)
        out["columns"][c] = {
            "compared": both.height, "differ": diff.height, "null_mismatch": null_mismatch,
            "max_rel_diff": float(both.select(_rel(a, b).max()).item() or 0.0) if both.height else 0.0}
    bad = joined.filter(_rel(pl.col("close_price"), pl.col("close_price_legacy")) > rel_tol).sort(BAR_KEY)
    out["examples"] = [{"instrument_id": r["instrument_id"], "trade_date": r["trade_date"].isoformat(), "native_close": float(r["close_price"]),
                        "legacy_close": float(r["close_price_legacy"])} for r in bad.head(examples).iter_rows(named=True)]
    out["instruments_with_close_diff"] = bad["instrument_id"].n_unique() if bad.height else 0
    return out


def compare_index(native: pl.DataFrame, legacy: pl.DataFrame, *, rel_tol: float = REL_TOL, examples: int = 5) -> dict:
    n = native.sort("ingested_at").unique(["symbol", "date"], keep="last")
    g = legacy.sort("ingested_at").unique(["symbol", "date"], keep="last")
    if n.is_empty() or g.is_empty():
        return {"native_rows": n.height, "legacy_rows": g.height, "overlap_rows": 0, "note": "one side is empty"}
    lo, hi = max(n["date"].min(), g["date"].min()), min(n["date"].max(), g["date"].max())
    n_w, g_w = n.filter(pl.col("date").is_between(lo, hi)), g.filter(pl.col("date").is_between(lo, hi))
    joined = n_w.join(g_w, on=["symbol", "date"], how="inner", suffix="_legacy")
    out: dict = {"window": f"{lo}..{hi}", "native_rows": n.height, "legacy_rows": g.height, "overlap_rows": joined.height,
                 "native_only_rows": n_w.join(g_w, on=["symbol", "date"], how="anti").height,
                 "legacy_only_rows": g_w.join(n_w, on=["symbol", "date"], how="anti").height,
                 "native_symbols": sorted(n["symbol"].unique().to_list()), "legacy_symbols": sorted(g["symbol"].unique().to_list()),
                 "rel_tol": rel_tol, "columns": {}}
    for c in INDEX_VALUES:
        a, b = pl.col(c), pl.col(f"{c}_legacy")
        both = joined.filter(a.is_not_null() & b.is_not_null())
        out["columns"][c] = {"compared": both.height, "differ": both.filter(_rel(a, b) > rel_tol).height,
                             "max_rel_diff": float(both.select(_rel(a, b).max()).item() or 0.0) if both.height else 0.0}
    bad = joined.filter(_rel(pl.col("close"), pl.col("close_legacy")) > rel_tol).sort(["symbol", "date"])
    out["examples"] = [{"symbol": r["symbol"], "date": r["date"].isoformat(), "native_close": r["close"], "legacy_close": r["close_legacy"]}
                       for r in bad.head(examples).iter_rows(named=True)]
    return out


def compare_membership(native: pl.DataFrame, legacy: pl.DataFrame, *, examples: int = 5) -> dict:
    """Membership diffs on (instrument_id, node_id) pairs, per node and per stock (the fusion lane takes ONE node per stock)."""
    n = native.select("instrument_id", "node_id").unique()
    g = legacy.select("instrument_id", "node_id").unique()
    only_n, only_g = n.join(g, on=["instrument_id", "node_id"], how="anti"), g.join(n, on=["instrument_id", "node_id"], how="anti")
    nodes_n, nodes_g = set(n["node_id"].to_list()), set(g["node_id"].to_list())
    per_node = (n.group_by("node_id").agg(pl.len().alias("native")).join(g.group_by("node_id").agg(pl.len().alias("legacy")),
                                                                        on="node_id", how="full", coalesce=True)
                .with_columns(pl.col("native").fill_null(0), pl.col("legacy").fill_null(0))
                .with_columns((pl.col("native") - pl.col("legacy")).alias("delta")))
    moved = per_node.filter(pl.col("delta") != 0).sort(pl.col("delta").abs(), descending=True)

    def primary(frame: pl.DataFrame) -> dict[str, str]:       # the fusion rule: the lowest node_id of a stock
        first = frame.sort(["instrument_id", "node_id"]).unique("instrument_id", keep="first")
        return dict(zip(first["instrument_id"].to_list(), first["node_id"].to_list()))

    pn, pg = primary(n), primary(g)
    common = set(pn) & set(pg)
    return {"native_pairs": n.height, "legacy_pairs": g.height, "native_nodes": len(nodes_n), "legacy_nodes": len(nodes_g),
            "nodes_only_native": sorted(nodes_n - nodes_g)[:examples], "nodes_only_legacy": sorted(nodes_g - nodes_n)[:examples],
            "pairs_only_native": only_n.height, "pairs_only_legacy": only_g.height,
            "instruments_native": n["instrument_id"].n_unique(), "instruments_legacy": g["instrument_id"].n_unique(),
            "instruments_primary_node_differs": sum(1 for k in common if pn[k] != pg[k]), "instruments_in_both": len(common),
            "nodes_with_size_change": moved.height,
            "largest_node_changes": [{"node_id": r["node_id"], "native": r["native"], "legacy": r["legacy"]}
                                     for r in moved.head(examples).iter_rows(named=True)]}


def compare_listings(native: pl.DataFrame, legacy: Mapping[str, date | None], *, examples: int = 5) -> dict:
    """``legacy``: code -> core.instrument.listed_on. Differences are by code."""
    nat = {r["code"]: r["listed_on"] for r in native.iter_rows(named=True)}
    both = set(nat) & set(legacy)
    differ = sorted(c for c in both if legacy[c] is not None and nat[c] is not None and nat[c] != legacy[c])
    return {"native_codes": len(nat), "legacy_codes_with_date": sum(1 for v in legacy.values() if v is not None),
            "native_only": len(set(nat) - set(legacy)), "legacy_only": len([c for c, v in legacy.items() if v is not None and c not in nat]),
            "overlap": len(both), "date_differs": len(differ),
            "native_null_legacy_set": sum(1 for c in both if nat[c] is None and legacy[c] is not None),
            "examples": [{"code": c, "native": nat[c].isoformat(), "legacy": legacy[c].isoformat()} for c in differ[:examples]]}


def render_report(sections: Mapping[str, dict]) -> str:
    """Plain-text report of the compare_* dicts (nested dicts are indented, lists kept inline)."""
    lines: list[str] = []

    def put(value: object, indent: int) -> None:
        pad = "  " * indent
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, dict):
                    lines.append(f"{pad}{k}:")
                    put(v, indent + 1)
                else:
                    lines.append(f"{pad}{k}: {v}")
        else:
            lines.append(f"{pad}{value}")

    for name, body in sections.items():
        lines.append(f"== reconcile {name} ==")
        put(body, 1)
    return "\n".join(lines)


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), max(1, size)):
        yield items[i:i + max(1, size)]
