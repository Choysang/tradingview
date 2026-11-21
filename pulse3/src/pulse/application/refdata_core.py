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

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

import polars as pl

from pulse.domain.instruments import identity as ident
from pulse.domain.market_rules import Board, Exchange, InstrumentSpec, PriceLimitRegistry, RuleNotVerified

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
                  "available_at": pl.Datetime("us", "UTC"), "source": pl.String}      # source: sse | szse | eastmoney (which list gave the date)
_DEC16 = pl.Decimal(38, 16)
#: ``bars_native_raw_1d``: the frame schema of ``bars_raw_1d`` (``DataService._bars_frame``) plus ``revision``; one row per revision.
RAW_SCHEMA = {"instrument_id": pl.String, "trade_date": pl.Date, "timeframe": pl.String, "bar_open_time": pl.Datetime("us", "UTC"),
              "bar_close_time": pl.Datetime("us", "UTC"), "open_price_raw": _DEC, "high_price_raw": _DEC, "low_price_raw": _DEC,
              "close_price_raw": _DEC, "volume_shares": pl.Int64, "turnover_amount_cny": _DEC, "is_final": pl.Boolean,
              "available_at": pl.Datetime("us", "UTC"), "quality_status": pl.String, "revision": pl.Int32}
#: ``adj_factor_native``: Sina's qfq factor rows exactly as served (adjusted = raw / factor from ``effective_from`` on).
FACTOR_SCHEMA = {"instrument_id": pl.String, "effective_from": pl.Date, "factor": _DEC16, "available_at": pl.Datetime("us", "UTC"),
                 "revision": pl.Int32}
RAW_DATASET = "bars_native_raw_1d"
FACTOR_DATASET = "adj_factor_native"
RAW_VALUES = ["open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw", "volume_shares", "turnover_amount_cny"]
FACTOR_KEY = ["instrument_id", "effective_from"]
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


def next_revisions(existing: pl.DataFrame | None, fresh: pl.DataFrame, *, schema: dict | None = None, key: list[str] | None = None,
                   values: list[str] | None = None, promote: str | None = "is_final") -> pl.DataFrame:
    """Rows of ``fresh`` (revision column ignored) that are new facts against the latest stored revision per key.

    A key never seen -> revision 1. A key whose values differ (a new ex-right event rewrote the past: OHLC / volume / turnover of the
    adjusted bars, the factors, the raw rows when the vendor corrects one) -> stored latest revision + 1. A bar that was not final and
    now is (``promote`` column) -> revision + 1 with unchanged values (the finality evidence is part of the fact). Identical
    re-fetches produce nothing. Defaults are the adjusted-bar dataset; the raw and factor datasets pass their own schema/key/values."""
    schema, key, values = schema or ADJ_SCHEMA, key or BAR_KEY, values or BAR_VALUES
    if fresh.is_empty():
        return pl.DataFrame(schema=schema)
    fresh = fresh.select(list(schema)).with_columns(pl.col("revision").cast(pl.Int32))
    if existing is None or existing.is_empty():
        return fresh.with_columns(pl.lit(1, dtype=pl.Int32).alias("revision"))
    carried = values + ([promote] if promote else []) + ["revision"]
    latest = (existing.sort("revision").unique(key, keep="last").select(key + carried).rename({c: f"{c}_old" for c in carried}))
    joined = fresh.join(latest, on=key, how="left")
    differs = pl.any_horizontal(*[pl.col(c).ne_missing(pl.col(f"{c}_old")) for c in values])
    changed = pl.col("revision_old").is_null() | differs
    if promote:
        changed = changed | (pl.col(promote) & ~pl.col(f"{promote}_old"))
    out = joined.filter(changed)
    return (out.with_columns((pl.col("revision_old").fill_null(0) + 1).cast(pl.Int32).alias("revision")).select(list(schema)))


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
    by_source = dict(native["source"].value_counts().iter_rows()) if "source" in native.columns else {}
    return {"native_codes": len(nat), "native_sources": by_source, "legacy_codes_with_date": sum(1 for v in legacy.values() if v is not None),
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


# ---------------------------------------------------------------------------------------------------------------
# exact limit flags from RAW prices
# ---------------------------------------------------------------------------------------------------------------
LIMIT_FLAG_COLUMNS = ["prev_close_raw", "ref_price", "ref_method", "ref_uncertain", "limit_ratio", "limit_up_price", "limit_down_price",
                      "rule_status", "is_limit_up_close", "open_at_limit_up", "touched_limit_up", "is_limit_down", "limit_up_streak"]
_TIE_BAND = Decimal("0.01")          # a derived reference within this fraction of a half tick may round either way: ``ref_uncertain``
_EXCHANGE = {"XSHG": Exchange.SSE, "XSHE": Exchange.SZSE, "XBSE": Exchange.BSE, "SSE": Exchange.SSE, "SZSE": Exchange.SZSE, "BSE": Exchange.BSE}
_BOARD = {"main": Board.MAIN, "chinext": Board.CHINEXT, "star": Board.STAR, "bse": Board.BSE}


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _pick(frame: pl.DataFrame, *names: str) -> str:
    for n in names:
        if n in frame.columns:
            return n
    raise ValueError(f"limit_flags_raw needs one of the columns {names}")


def factor_in_force(factors: pl.DataFrame | None) -> dict[str, tuple[list[date], list[Decimal]]]:
    """{instrument_id: (effective dates ascending, factors)} from the latest revision of each factor row."""
    out: dict[str, tuple[list[date], list[Decimal]]] = {}
    if factors is None or factors.is_empty():
        return out
    latest = factors.sort("revision").unique(FACTOR_KEY, keep="last").sort(FACTOR_KEY)
    for iid, d, f in latest.select("instrument_id", "effective_from", "factor").iter_rows():
        ds, fs = out.setdefault(iid, ([], []))
        ds.append(d)
        fs.append(_dec(f))
    return out


def _factor_at(table: tuple[list[date], list[Decimal]] | None, day: date) -> Decimal | None:
    """The factor in force on ``day`` (backward as-of; a day before the first row uses the earliest factor, as the adjustment does)."""
    if table is None:
        return None
    i = bisect_right(table[0], day) - 1
    return table[1][max(i, 0)]


def limit_flags_raw(frame: pl.DataFrame, factors: pl.DataFrame | None = None, *, registry: PriceLimitRegistry | None = None) -> pl.DataFrame:
    """Exact limit-up / limit-down flags from RAW daily prices (never from adjusted ones). Returns ``frame`` plus ``LIMIT_FLAG_COLUMNS``.

    Input columns: ``instrument_id``, ``trade_date``, raw ``open/high/low/close`` (or ``*_price_raw``), ``is_st`` (Boolean; NULL = unknown),
    ``exchange`` + ``board`` (or a 6-digit ``code`` they are derived from), optional ``exchange_ref_price`` (the exchange's reference price
    of the day when a source gives it), optional ``in_ipo_window`` (True = the no-limit listing window is not modelled). A ``revision`` column
    means the latest revision per (instrument, date) is used. ``factors`` is the ``adj_factor_native`` frame (latest revision per row);
    without it an ex-right day cannot be detected.

    Reference price of day t: the exchange reference if given (``exchange_reference``); else, when the factor in force changed between
    the previous session and t (an ex-right day), ``prev_close_raw * f(t) / f(t-1)`` rounded half up to the tick (``factor_derived``:
    ratio adjustment makes adjusted(t-1) == ref(t) / f(t), so ref(t) = raw(t-1) * f(t)/f(t-1); on 000001 2026-06-12 this gives 10.94,
    two decimals exactly, while the opposite ratio would RAISE the reference on a dividend day); else the raw previous close
    (``prev_close_raw``, or ``prev_close_raw_no_factor_file`` when the code has no factor rows, where an ex-right day cannot be seen).
    The band is the registry's (``PriceLimitRegistry.band``: previous close x (1 +/- ratio), rounded half up to the tick), so ST and
    board ratios and their effective dates come from ONE rule source; an unverified rule (e.g. SZSE main-board ST) or an unknown ST
    state gives NULL flags and says why in ``rule_status``. ``ref_uncertain`` marks a derived reference within 0.01 of a half tick.
    The previous close is the previous ROW of the same instrument (suspensions keep the chain; a missing session is not detected).
    Flags: ``is_limit_up_close`` (close == limit up), ``open_at_limit_up``, ``touched_limit_up`` (high == limit up), ``is_limit_down``
    (close == limit down); ``limit_up_streak`` counts consecutive ``is_limit_up_close`` rows (NULL on an unknown row, which restarts the count)."""
    registry = registry or PriceLimitRegistry()
    cols = {k: _pick(frame, f"{k}_price_raw", k) for k in ("open", "high", "low", "close")}
    if "is_st" not in frame.columns:
        raise ValueError("limit_flags_raw needs an is_st column (NULL where unknown): the band of a risk-warning share is not the board's")
    have_ex, have_bd, have_code = "exchange" in frame.columns, "board" in frame.columns, "code" in frame.columns
    if not ((have_ex and have_bd) or have_code):
        raise ValueError("limit_flags_raw needs exchange + board columns or a 6-digit code column")
    work = frame
    if "revision" in work.columns:
        work = work.sort("revision").unique(["instrument_id", "trade_date"], keep="last")
    work = work.sort(["instrument_id", "trade_date"], maintain_order=True)
    n = work.height
    table = factor_in_force(factors)
    ref_given = work["exchange_ref_price"].to_list() if "exchange_ref_price" in work.columns else [None] * n
    ipo = work["in_ipo_window"].to_list() if "in_ipo_window" in work.columns else [False] * n
    rows = zip(work["instrument_id"].to_list(), work["trade_date"].to_list(), work[cols["open"]].to_list(), work[cols["high"]].to_list(),
               work[cols["close"]].to_list(), work["is_st"].to_list(), work["exchange"].to_list() if have_ex else [None] * n,
               work["board"].to_list() if have_bd else [None] * n, work["code"].to_list() if have_code else [None] * n)
    out: dict[str, list] = {c: [] for c in LIMIT_FLAG_COLUMNS}
    prev_iid, prev_close, prev_day, streak = None, None, None, 0
    specs: dict[tuple, InstrumentSpec] = {}
    tick = Decimal("0.01")
    for idx, (iid, day, o, h, c, st, ex, bd, code) in enumerate(rows):
        if iid != prev_iid:
            prev_iid, prev_close, prev_day, streak = iid, None, None, 0
        o, h, c = _dec(o), _dec(h), _dec(c)
        row = dict.fromkeys(LIMIT_FLAG_COLUMNS)
        row["prev_close_raw"], row["ref_uncertain"] = prev_close, False
        status, ref, method = None, None, None
        if ipo[idx]:
            status = "ipo_no_limit_window_not_modelled"
        elif st is None:
            status = "st_unknown"
        elif ref_given[idx] is not None:
            ref, method = _dec(ref_given[idx]), "exchange_reference"
        elif prev_close is None:
            status = "no_previous_close"
        else:
            f_t, f_p = _factor_at(table.get(iid), day), _factor_at(table.get(iid), prev_day)
            if f_t is not None and f_p is not None and f_t != f_p:
                raw_ref = prev_close * f_t / f_p
                ref, method = (raw_ref / tick).to_integral_value(ROUND_HALF_UP) * tick, "factor_derived"
                row["ref_uncertain"] = abs((raw_ref / tick) % 1 - Decimal("0.5")) <= _TIE_BAND
            else:
                ref, method = prev_close, ("prev_close_raw" if f_t is not None else "prev_close_raw_no_factor_file")
        if status is None:
            try:
                key = (ex, bd, code, bool(st))
                if key not in specs:
                    e = _EXCHANGE[ex] if ex is not None else (Exchange.SSE if code[0] == "6" else Exchange.BSE if code[0] in "489" else Exchange.SZSE)
                    b = _BOARD[bd] if bd is not None else _BOARD[ident.board_for(ident.venue_for_code(code), code)]
                    specs[key] = InstrumentSpec(iid, e, b, bool(st))
                band = registry.band(specs[key], day, ref)
                row.update(ref_price=ref, ref_method=method, limit_ratio=band.ratio, limit_up_price=band.limit_up, limit_down_price=band.limit_down)
                status = "verified"
                row["is_limit_up_close"], row["open_at_limit_up"] = c == band.limit_up, o == band.limit_up
                row["touched_limit_up"], row["is_limit_down"] = h == band.limit_up, c == band.limit_down
            except RuleNotVerified as exc:
                status = f"unverified:{exc}"
                row.update(ref_price=ref, ref_method=method)
        row["rule_status"] = status
        if row["is_limit_up_close"] is None:
            streak = 0
        else:
            streak = streak + 1 if row["is_limit_up_close"] else 0
            row["limit_up_streak"] = streak
        for k in LIMIT_FLAG_COLUMNS:
            out[k].append(row[k])
        prev_close, prev_day = c, day
    dec_t = pl.Decimal(38, 12)
    typed = {"prev_close_raw": dec_t, "ref_price": dec_t, "ref_method": pl.String, "ref_uncertain": pl.Boolean, "limit_ratio": pl.Decimal(10, 4),
             "limit_up_price": dec_t, "limit_down_price": dec_t, "rule_status": pl.String, "is_limit_up_close": pl.Boolean,
             "open_at_limit_up": pl.Boolean, "touched_limit_up": pl.Boolean, "is_limit_down": pl.Boolean, "limit_up_streak": pl.Int32}
    flags = pl.DataFrame({k: pl.Series(k, out[k], dtype=typed[k]) for k in LIMIT_FLAG_COLUMNS})
    return pl.concat([work, flags], how="horizontal")


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), max(1, size)):
        yield items[i:i + max(1, size)]
