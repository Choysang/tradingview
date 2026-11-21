"""Dragon-Tiger list (龙虎榜) comparison: the old warehouse's seats against pulse3's own Eastmoney seat reports, READ ONLY.

Why: the old task PulseLhbDaily and pulse3 ``intel seats --days 2`` crawl the same Eastmoney pages every evening. The old task can
be retired once both sides agree for 10 sessions (docs/m6/LEGACY_RETIREMENT.md). This module loads both sides for a date range,
compares them per session and returns a verdict per session with the reasons; it decides nothing and writes nothing.

Sides:
* old: the parquet tables ``lhb_stocks`` (code, date, reason) and ``lhb_seats`` (code, date, side, reason, seat, rank, buy, sell) of the
  old warehouse directory (``<warehouse>/lhb_stocks/*.parquet``). Files are read as the old store reads them: retired files are skipped,
  duplicates resolve to the newest ``ingested_at``. Amounts are float 亿元; they are converted to exact yuan through the shortest
  decimal repr (``legacy_yi_to_wan``), never by float arithmetic. Pure parquet reading: nothing is imported from ``backend/``.
* new: ``intelligence.seat_report`` / ``seat_report_row`` rows of source ``eastmoney_lhb_disclosure`` (newest revision of each
  report), amounts in ``canonical_amount_cny``. Reports carry the instrument id, not the code; the code comes from the
  ``legacy_warehouse`` alias of ``core.instrument_alias`` (or any six-digit alias).

Invariants: both sides are cut to the codes both pipelines can store (old regex AND ``is_equity_code``); seat names are compared
whitespace-stripped but otherwise verbatim; a seat row without an amount is dropped from the comparison on its side and counted
(the new pipeline never stores such rows). Rows are matched as a multiset per (code, reason, side, name): several institutions share the
name 机构专用, so their amounts are paired in descending order. Two amounts agree when they differ by at most 1 yuan or by at most 0.5%.
A session is SAME when no stock is listed on one side only, no stock has a different reason set, and no seat row is missing or
off-amount; otherwise DIFF with the reasons. A session present on only one side is DIFF.
Not covered: stock-level fields of the list (close, net amounts, ratios); only the seat tables and the listing itself are compared.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from pulse.domain.intelligence.seats import is_equity_code, legacy_yi_to_wan

NEW_SOURCE_ID = "eastmoney_lhb_disclosure"
ABS_TOL_CNY = Decimal(1)
REL_TOL = Decimal("0.005")
RETIRE_READY_SESSIONS = 10
INSTITUTION_PREFIX = "机构专用"
LHASA = "拉萨"
_OLD_CODE = re.compile(r"(00|30|60|68|4|8|92)\d*")

Rows = list[tuple[str, str, Decimal]]  # (side, seat name, amount in yuan)
Side = dict[date, dict[str, dict[str, Rows]]]  # session -> code -> reason -> seat rows


class LhbCompareError(RuntimeError):
    """An input is unusable (missing warehouse table, bad range); the comparison was not run."""


def in_scope(code: str) -> bool:
    """Codes both pipelines can hold: the old A-share regex and the new equity numbering."""
    return len(code) == 6 and code.isdigit() and _OLD_CODE.fullmatch(code) is not None and is_equity_code(code)


def amounts_agree(a: Decimal, b: Decimal) -> bool:
    diff = abs(a - b)
    return diff <= ABS_TOL_CNY or diff <= REL_TOL * max(abs(a), abs(b))


# ---- loading ------------------------------------------------------------------------------------------------------------------
def _table_files(warehouse: Path, table: str) -> list[Path]:
    folder = warehouse / table
    if not folder.is_dir():
        return []
    try:
        retired = json.loads((folder / ".retired.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        retired = {}
    return sorted(p for p in folder.glob("*.parquet") if p.name not in retired)


def _newest(files: list[Path], keys: list[str], since: date, until: date) -> pl.DataFrame:
    lazy = pl.scan_parquet([str(p) for p in files]).filter(pl.col("date").is_between(since, until))
    if "ingested_at" in lazy.collect_schema().names():
        lazy = lazy.sort("ingested_at")
    return lazy.unique(subset=keys, keep="last", maintain_order=True).collect()


def load_old(warehouse: Path, since: date, until: date) -> tuple[Side, dict[str, int]]:
    """The old warehouse's list and seats for [since, until]; also counts of the rows dropped on the way."""
    stock_files, seat_files = _table_files(warehouse, "lhb_stocks"), _table_files(warehouse, "lhb_seats")
    if not stock_files:
        raise LhbCompareError(f"no lhb_stocks parquet files under {warehouse}")
    dropped = {"old_rows_without_amount": 0, "old_rows_without_name": 0}
    side: Side = {}
    stocks = _newest(stock_files, ["code", "date", "reason"], since, until)
    for code, day, reason in stocks.select("code", "date", "reason").iter_rows():
        if in_scope(code):
            side.setdefault(day, {}).setdefault(code, {}).setdefault(reason or "", [])
    if seat_files:
        seats = _newest(seat_files, ["code", "date", "side", "reason", "seat", "rank"], since, until)
        for r in seats.iter_rows(named=True):
            if not in_scope(r["code"]):
                continue
            name = (r["seat"] or "").strip()
            raw = r["buy"] if r["side"] == "buy" else r["sell"]
            if not name:
                dropped["old_rows_without_name"] += 1
                continue
            if raw is None:
                dropped["old_rows_without_amount"] += 1
                continue
            yuan = legacy_yi_to_wan(raw) * 10000
            reasons = side.setdefault(r["date"], {}).setdefault(r["code"], {})
            reasons.setdefault(r["reason"] or "", []).append((r["side"], name, yuan))
    return side, dropped


def load_new(conn, since: date, until: date, source_id: str = NEW_SOURCE_ID) -> tuple[Side, set[str], dict[str, int]]:
    """pulse3's seat reports (newest revision each) for [since, until]; also the codes that are registered instruments and the
    number of reports left out because no six-digit code alias maps their instrument (loud, never silent)."""
    reports = conn.execute(
        "SELECT DISTINCT ON (source_report_id) report_revision_id, instrument_id, window_end, disclosure_reason FROM intelligence.seat_report"
        " WHERE source_id=%s AND window_end BETWEEN %s AND %s ORDER BY source_report_id, revision DESC", (source_id, since, until)).fetchall()
    if not reports:
        return {}, set(), {"new_reports_without_code": 0}
    codes = _codes_of(conn, [r["instrument_id"] for r in reports])
    side: Side = {}
    unmapped = 0
    index: dict[Any, tuple[date, str, str]] = {}
    for r in reports:
        code = codes.get(r["instrument_id"])
        if code is None:
            unmapped += 1
            continue
        if not in_scope(code):
            continue
        index[r["report_revision_id"]] = (r["window_end"], code, r["disclosure_reason"] or "")
        side.setdefault(r["window_end"], {}).setdefault(code, {}).setdefault(r["disclosure_reason"] or "", [])
    for row in conn.execute(
            "SELECT report_revision_id, side, raw_seat_name, canonical_amount_cny FROM intelligence.seat_report_row WHERE report_revision_id = ANY(%s)",
            (list(index),)).fetchall():
        day, code, reason = index[row["report_revision_id"]]
        side[day][code][reason].append((row["side"], row["raw_seat_name"].strip(), Decimal(row["canonical_amount_cny"])))
    return side, registered_codes(conn, {c for by_code in side.values() for c in by_code}), {"new_reports_without_code": unmapped}


def _codes_of(conn, instrument_ids: list) -> dict[Any, str]:
    out: dict[Any, str] = {}
    for r in conn.execute("SELECT instrument_id, source_id, source_symbol FROM core.instrument_alias WHERE instrument_id = ANY(%s)",
                          (list(set(instrument_ids)),)).fetchall():
        sym = r["source_symbol"]
        if len(sym) == 6 and sym.isdigit() and (r["instrument_id"] not in out or r["source_id"] == "legacy_warehouse"):
            out[r["instrument_id"]] = sym
    return out


def registered_codes(conn, codes: set[str]) -> set[str]:
    """Which of ``codes`` are registered instruments in pulse3 (an unregistered code is skipped by the seat ingest)."""
    if not codes:
        return set()
    rows = conn.execute("SELECT DISTINCT source_symbol FROM core.instrument_alias WHERE source_id='legacy_warehouse' AND source_symbol = ANY(%s)",
                        (sorted(codes),)).fetchall()
    return {r["source_symbol"] for r in rows}


# ---- comparison ---------------------------------------------------------------------------------------------------------------
@dataclass
class SideCounts:
    stocks: int = 0
    reports: int = 0  # (code, reason) lists
    seat_rows: int = 0
    institution_rows: int = 0
    lhasa_rows: int = 0


@dataclass
class StockDiff:
    code: str
    reason: str | None  # None: the difference is about the stock as a whole
    kind: str  # only_old | only_new | reason_only_old | reason_only_new | seat_only_old | seat_only_new | amount
    side: str | None = None
    seat: str | None = None
    old: str | None = None
    new: str | None = None
    note: str | None = None


@dataclass
class SessionResult:
    day: str
    verdict: str  # SAME | DIFF
    reasons: list[str]
    old: SideCounts
    new: SideCounts
    stocks_only_old: list[str] = field(default_factory=list)
    stocks_only_new: list[str] = field(default_factory=list)
    diffs: list[StockDiff] = field(default_factory=list)


def _counts(by_code: dict[str, dict[str, Rows]]) -> SideCounts:
    c = SideCounts(stocks=len(by_code))
    for reasons in by_code.values():
        c.reports += len(reasons)
        for rows in reasons.values():
            c.seat_rows += len(rows)
            c.institution_rows += sum(1 for _, n, _ in rows if n.startswith(INSTITUTION_PREFIX))
            c.lhasa_rows += sum(1 for _, n, _ in rows if LHASA in n)
    return c


def _group(rows: Rows) -> dict[tuple[str, str], list[Decimal]]:
    out: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for side, name, amount in rows:
        out[(side, name)].append(amount)
    return {k: sorted(v, reverse=True) for k, v in out.items()}


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _compare_rows(code: str, reason: str, old: Rows, new: Rows) -> list[StockDiff]:
    go, gn = _group(old), _group(new)
    diffs: list[StockDiff] = []
    for key in sorted(set(go) | set(gn)):
        a, b = go.get(key, []), gn.get(key, [])
        side, name = key
        for x, y in zip(a, b):
            if not amounts_agree(x, y):
                diffs.append(StockDiff(code, reason, "amount", side, name, _fmt(x), _fmt(y)))
        for x in a[len(b):]:
            diffs.append(StockDiff(code, reason, "seat_only_old", side, name, _fmt(x), None))
        for y in b[len(a):]:
            diffs.append(StockDiff(code, reason, "seat_only_new", side, name, None, _fmt(y)))
    return diffs


def compare_session(day: date, old: dict[str, dict[str, Rows]], new: dict[str, dict[str, Rows]], new_registered: set[str] | None = None) -> SessionResult:
    """Compare one session. ``new_registered`` (codes known as instruments in pulse3) annotates stocks missing on the new side."""
    only_old, only_new = sorted(set(old) - set(new)), sorted(set(new) - set(old))
    diffs: list[StockDiff] = []
    for code in only_old:
        note = None if new_registered is None or code in new_registered else "instrument not registered in pulse3 (seat ingest skips it)"
        diffs.append(StockDiff(code, None, "only_old", note=note))
    diffs += [StockDiff(code, None, "only_new") for code in only_new]
    for code in sorted(set(old) & set(new)):
        ro, rn = old[code], new[code]
        diffs += [StockDiff(code, r, "reason_only_old") for r in sorted(set(ro) - set(rn))]
        diffs += [StockDiff(code, r, "reason_only_new") for r in sorted(set(rn) - set(ro))]
        for r in sorted(set(ro) & set(rn)):
            diffs += _compare_rows(code, r, ro[r], rn[r])
    reasons = _verdict_reasons(only_old, only_new, diffs, bool(old), bool(new))
    return SessionResult(day.isoformat(), "DIFF" if reasons else "SAME", reasons, _counts(old), _counts(new), only_old, only_new, diffs)


def _verdict_reasons(only_old: list[str], only_new: list[str], diffs: list[StockDiff], has_old: bool, has_new: bool) -> list[str]:
    if has_old != has_new:
        return ["session has data on the old side only" if has_old else "session has data on the new side only"]
    n = defaultdict(int)
    for d in diffs:
        n[d.kind] += 1
    labels = [("only_old", "stocks listed only in old"), ("only_new", "stocks listed only in new"),
              ("reason_only_old", "listing reasons only in old"), ("reason_only_new", "listing reasons only in new"),
              ("seat_only_old", "seat rows only in old"), ("seat_only_new", "seat rows only in new"), ("amount", "seat amounts differ")]
    return [f"{label}: {n[kind]}" for kind, label in labels if n[kind]]


@dataclass
class Comparison:
    since: str
    until: str
    sessions: list[SessionResult]
    dropped: dict[str, int]
    same_tail: int  # newest consecutive SAME sessions
    ready_to_retire: bool  # same_tail >= RETIRE_READY_SESSIONS
    tolerance: str = f"{ABS_TOL_CNY} yuan or {REL_TOL * 100}%"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def all_same(self) -> bool:
        return bool(self.sessions) and all(s.verdict == "SAME" for s in self.sessions)


def compare(old: Side, new: Side, since: date, until: date, dropped: dict[str, int] | None = None,
            new_registered: set[str] | None = None) -> Comparison:
    days = sorted(d for d in set(old) | set(new) if since <= d <= until)
    sessions = [compare_session(d, old.get(d, {}), new.get(d, {}), new_registered) for d in days]
    tail = 0
    for s in reversed(sessions):
        if s.verdict != "SAME":
            break
        tail += 1
    return Comparison(since.isoformat(), until.isoformat(), sessions, dropped or {}, tail, tail >= RETIRE_READY_SESSIONS)


def run_compare(conn, warehouse: Path, since: date, until: date, source_id: str = NEW_SOURCE_ID) -> Comparison:
    if since > until:
        raise LhbCompareError(f"--since {since} is after --until {until}")
    old, dropped = load_old(warehouse, since, until)
    new, registered, new_dropped = load_new(conn, since, until, source_id)
    return compare(old, new, since, until, {**dropped, **new_dropped}, registered)


# ---- rendering ----------------------------------------------------------------------------------------------------------------
def render(c: Comparison, max_lines: int = 30) -> str:
    out = [f"LHB seats old warehouse vs pulse3, {c.since} .. {c.until}; tolerance {c.tolerance}"]
    if not c.sessions:
        out.append("no session with data on either side in the range")
    for s in c.sessions:
        o, n = s.old, s.new
        out.append(f"{s.day} {s.verdict}  stocks {o.stocks}/{n.stocks}  seat rows {o.seat_rows}/{n.seat_rows}  "
                   f"institution {o.institution_rows}/{n.institution_rows}  lhasa {o.lhasa_rows}/{n.lhasa_rows}  (old/new)")
        if s.verdict == "SAME":
            continue
        out.append("  reasons: " + "; ".join(s.reasons))
        for d in s.diffs[:max_lines]:
            where = d.code if d.reason is None else f"{d.code} [{d.reason}]"
            detail = d.kind if d.seat is None else f"{d.kind} {d.side} {d.seat}"
            amount = "" if d.old is None and d.new is None else f"  old={d.old} new={d.new}"
            out.append(f"  - {where}: {detail}{amount}" + (f"  ({d.note})" if d.note else ""))
        if len(s.diffs) > max_lines:
            out.append(f"  ... {len(s.diffs) - max_lines} more (use --json)")
    if c.dropped and any(c.dropped.values()):
        out.append("dropped old rows: " + ", ".join(f"{k}={v}" for k, v in c.dropped.items() if v))
    verdict = "READY to retire PulseLhbDaily" if c.ready_to_retire else "not ready"
    out.append(f"newest consecutive SAME sessions: {c.same_tail} (need {RETIRE_READY_SESSIONS}) -> {verdict}")
    return "\n".join(out)
