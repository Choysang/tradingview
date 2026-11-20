"""Inputs of the batch 6 lane: daily panel, scope, caps, 申万 level-2 map and 龙虎榜 seats, all point in time and read only.

Why: E093 / E094 are historical runs on the pulse3 daily bars, the frozen Shenwan level-2 map with join dates (fwdlog lane data), the circulating-cap snapshot and the imported
seat reports. Nothing is written here.
Invariants: bars are read as visible at ``as_of`` (``market_today_service.load_bars``, final bars only); scope = main board (00 / 60), not ST, listed at least 60 sessions, traded on the day;
float cap of a day is ESTIMATED from the latest cap snapshot: cap(D) = cap(snapshot) x close(D) / close(snapshot day), i.e. a constant share count (the repository has no cap history);
turnover is ESTIMATED as shares x close (the daily bars carry no amount); the seat reports used are the single-day ones (window start == window end), seat rows de-duplicated by
(side, seat name, amount) across the reports of one stock-day; "拉萨" = name contains it, "机构专用" = name equals it (a broker branch whose name merely contains the words is not an institution seat).
Bars source (``prepare_history(source=...)``): ``adjusted`` (default) = the forward-adjusted research bars the fusion lane reads (``bindings.load_research_inputs``: latest READY
``bars_legacy_fwd_adjusted_1d`` snapshot, latest revision visible at the cutoff; about three years), ``raw`` = the raw daily bars (about 13 months). Adjusted returns are true returns, adjusted
prices are not traded prices (see ``domain.batch6.panel``). Everything else (scope, caps, sw2, seats) is the same for both.
Not covered: ST history (the ST flag is the current instrument state), cap history, delisted names without bars.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_fwdlog_inputs as FI
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import rules as R

CST = timezone(timedelta(hours=8))
WARMUP_SESSIONS = 10                     # streaks start counting at the first observed session: the first sessions are not decision days


def cents(x: Decimal | None) -> int | None:
    return None if x is None else int((x * 100).to_integral_value())


@dataclass(frozen=True)
class Scope:
    name: Mapping[str, str]
    st: frozenset[str]
    ok_from: Mapping[str, date]          # first session with >= 60 sessions since listing (absent = unknown listing date: out of scope)


@dataclass
class HistoryInputs:
    as_of: datetime
    sessions: list[date]
    panel: pl.DataFrame                  # panel columns + sw2, in_scope, cap
    seats: pl.DataFrame                  # code, date, side, seat, amount
    lhb_days: set[tuple[str, date]]      # every stock-day with any seat report (single or multi-day window)
    sw: pl.DataFrame
    cap_note: dict
    notes: list[str]
    source: str = "raw"                  # bars source of the run


def observed_full_sessions(counts: Mapping[date, int]) -> list[date]:
    """Sessions with at least half of the median instrument count (a thin day is not a session)."""
    if not counts:
        return []
    med = sorted(counts.values())[len(counts) // 2]
    return sorted(d for d, n in counts.items() if n >= 0.5 * med)


def load_bars_frame(conn: psycopg.Connection, ids: list[str], as_of: datetime, since: date | None, code_of: Mapping[str, str]) -> pl.DataFrame:
    rows: list[tuple] = []
    for i in range(0, len(ids), 400):
        series = MT.load_bars(conn, ids[i:i + 400], as_of, B.AVAILABILITY_POLICY, since=since)
        for iid, bars in series.items():
            code = code_of[iid]
            for b in bars:
                if b.c is None or b.o is None or b.h is None or b.l is None:
                    continue
                rows.append((code, b.d, cents(b.o), cents(b.h), cents(b.l), cents(b.c), int(b.v or 0)))
    schema = {"code": pl.String, "date": pl.Date, "o": pl.Int64, "h": pl.Int64, "l": pl.Int64, "c": pl.Int64, "v": pl.Int64}
    return pl.DataFrame(rows, schema=schema, orient="row")


def load_scope(conn: psycopg.Connection, as_of: datetime) -> tuple[dict[str, str], dict[str, str], Scope, dict[str, date | None]]:
    """(iid -> code, code -> iid, scope pieces, code -> listed_on) of the main-board equities."""
    metas, listed = B.load_instrument_meta(conn, as_of, B.AVAILABILITY_POLICY)
    code_of: dict[str, str] = {}
    iid_of: dict[str, str] = {}
    name: dict[str, str] = {}
    st: set[str] = set()
    listed_on: dict[str, date | None] = {}
    for iid, m in metas.items():
        if m.board_code != "main" or not m.code or not R.main_board(m.code):
            continue
        code_of[str(iid)] = m.code
        iid_of[m.code] = str(iid)
        name[m.code] = m.display_name
        if R.is_st(m.display_name, m.is_risk_warning):
            st.add(m.code)
        listed_on[m.code] = listed.get(iid)
    return code_of, iid_of, Scope(name, frozenset(st), {}), listed_on


def load_caps_frame(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, iid_of: Mapping[str, str]) -> tuple[pl.DataFrame, dict]:
    """code, cap_ref (float cap at the snapshot), ref_date (the snapshot's as-of day)."""
    man = B._ready_manifest(conn, B.CAP_DATASET, as_of)
    if man is None:
        return pl.DataFrame(schema={"code": pl.String, "cap_ref": pl.Float64, "ref_date": pl.Date}), {"present": False}
    df = lake.read_snapshot_frames(man["content_hash"]).filter(pl.col("available_at") <= as_of)
    df = df.filter(pl.col("float_cap_cny").is_not_null()).sort(["as_of", "available_at"]).unique("instrument_id", keep="last")
    rev = {v: k for k, v in iid_of.items()}
    rows = [(rev[i], float(c), date.fromisoformat(a)) for i, c, a in zip(df["instrument_id"].to_list(), df["float_cap_cny"].to_list(), df["as_of"].to_list()) if i in rev]
    out = pl.DataFrame(rows, schema={"code": pl.String, "cap_ref": pl.Float64, "ref_date": pl.Date}, orient="row")
    return out, {"present": True, "manifest_version": man["manifest_version"], "rows": out.height, "estimated": True,
                 "note": "cap(D) = snapshot cap x close(D) / close(snapshot day): constant share count"}


def load_seats(conn: psycopg.Connection, code_of: Mapping[str, str], since: date) -> tuple[pl.DataFrame, set[tuple[str, date]]]:
    ids = [__import__("uuid").UUID(i) for i in code_of]
    rows = conn.execute(
        "SELECT DISTINCT r.instrument_id, r.window_end, w.side, w.raw_seat_name, w.canonical_amount_cny FROM intelligence.seat_report r"
        " JOIN intelligence.seat_report_row w USING (report_revision_id) WHERE r.window_start = r.window_end AND r.window_end >= %s AND r.instrument_id = ANY(%s)",
        (since, ids), prepare=False).fetchall()
    code_by = {__import__("uuid").UUID(k): v for k, v in code_of.items()}
    seat_rows = [(code_by[r["instrument_id"]], r["window_end"], r["side"], r["raw_seat_name"], float(r["canonical_amount_cny"]) if r["canonical_amount_cny"] is not None else None)
                 for r in rows if r["instrument_id"] in code_by]
    seats = pl.DataFrame(seat_rows, schema={"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}, orient="row")
    anyrep = conn.execute("SELECT DISTINCT instrument_id, window_end FROM intelligence.seat_report WHERE window_end >= %s AND instrument_id = ANY(%s)", (since, ids), prepare=False).fetchall()
    lhb = {(code_by[r["instrument_id"]], r["window_end"]) for r in anyrep if r["instrument_id"] in code_by}
    return seats, lhb


def load_adjusted_frame(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, since: date | None, code_of: Mapping[str, str]) -> tuple[pl.DataFrame, list[date], dict]:
    """Forward-adjusted float bars of the in-scope codes from the fusion lane's research snapshot, and its observed sessions."""
    ri = B.load_research_inputs(conn, lake, as_of)
    rev = {k: v for k, v in code_of.items()}
    df = ri.bars.with_columns(pl.col("instrument_id").replace_strict(rev, default=None, return_dtype=pl.String).alias("code")).filter(pl.col("code").is_not_null())
    if since is not None:
        df = df.filter(pl.col("trade_date") >= since)
    bars = df.select("code", pl.col("trade_date").alias("date"), pl.col("open").alias("o"), pl.col("high").alias("h"), pl.col("low").alias("l"), pl.col("close").alias("c"),
                     pl.col("volume").alias("v"))
    sessions = [d for d in ri.sessions if since is None or d >= since]
    return bars, sessions, {"research_snapshot": str(ri.manifest["snapshot_id"]), "research_manifest_version": ri.manifest["manifest_version"], "policy": ri.availability_policy}


def attach_scope(panel: pl.DataFrame, sessions: list[date], listed_on: Mapping[str, date | None], st: frozenset[str], conn: psycopg.Connection, lake: LakeStore,
                 as_of: datetime, iid_of: Mapping[str, str], *, require: str | None = None) -> tuple[pl.DataFrame, list[str], dict, pl.DataFrame]:
    """Add ``in_scope`` (main board, not ST, listed >= 60 sessions, plus the boolean column ``require`` when given), ``cap`` (estimated) and ``sw2`` (as of the row's date) to a panel
    with columns code, date, c (price in any unit): the scope of every batch 6 / batch 7 run."""
    notes: list[str] = []
    ok_from = {c: v for c, lo in listed_on.items() if (v := R.listed_ok_from(lo, sessions)) is not None}
    unknown = sum(1 for lo in listed_on.values() if lo is None)
    if unknown:
        notes.append(f"{unknown} instruments without listing date are out of scope")
    caps, cap_note = load_caps_frame(conn, lake, as_of, iid_of)
    ref_close = (panel.join(caps.select("code", "ref_date"), on="code").filter(pl.col("date") <= pl.col("ref_date")).sort("date").group_by("code").agg(pl.col("c").last().alias("c_ref")))
    caps = caps.join(ref_close, on="code", how="left")
    sw = FI.load_frozen().sw
    sw_map = dict(sw.select("code", "sw2").iter_rows())
    joined = dict(sw.select("code", "joined").iter_rows())
    okf = pl.DataFrame({"code": list(ok_from), "ok_from": list(ok_from.values())}, schema={"code": pl.String, "ok_from": pl.Date})
    scope = pl.col("ok_from").is_not_null() & (pl.col("date") >= pl.col("ok_from")) & ~pl.col("code").is_in(list(st))
    if require:
        scope = scope & pl.col(require)
    panel = (panel.join(okf, on="code", how="left").join(caps.select("code", "cap_ref", "c_ref"), on="code", how="left")
             .with_columns((pl.col("cap_ref") * pl.col("c") / pl.col("c_ref")).alias("cap"), scope.alias("in_scope"))
             .with_columns(pl.col("code").replace_strict(sw_map, default=None, return_dtype=pl.String).alias("sw2"),
                           pl.col("code").replace_strict(joined, default=None, return_dtype=pl.String).alias("sw_joined"))
             .with_columns(pl.when(pl.col("sw_joined").is_null() | (pl.col("sw_joined") <= pl.col("date").dt.strftime("%Y-%m-%d"))).then(pl.col("sw2")).otherwise(None).alias("sw2"))
             .drop("sw_joined", "ok_from", "c_ref", "cap_ref"))
    return panel, notes, cap_note, sw


def prepare_history(conn: psycopg.Connection, lake: LakeStore, *, as_of: datetime, since: date | None = None, source: str = "adjusted") -> HistoryInputs:
    if source not in ("adjusted", "raw"):
        raise ValueError("source must be 'adjusted' or 'raw'")
    code_of, iid_of, scope0, listed_on = load_scope(conn, as_of)
    if source == "raw":
        bars = load_bars_frame(conn, list(code_of), as_of, since, code_of)
        counts = {d: n for d, n in bars.group_by("date").len().iter_rows()}
        sessions = observed_full_sessions(counts)
        panel = PN.build_panel(bars, sessions)
        ref = {}
    else:
        bars, sessions, ref = load_adjusted_frame(conn, lake, as_of, since, code_of)
        panel = PN.build_panel_adjusted(bars, sessions)
    notes: list[str] = [f"bars source {source}: {bars.height} rows, {len(sessions)} sessions {sessions[0] if sessions else None}..{sessions[-1] if sessions else None}"
                        + (f" (research snapshot v{ref['research_manifest_version']})" if ref else "")]
    panel, scope_notes, cap_note, sw = attach_scope(panel, sessions, listed_on, scope0.st, conn, lake, as_of, iid_of)
    notes += scope_notes
    seats_since = sessions[0] if sessions else date(2023, 1, 1)
    seats, lhb = load_seats(conn, code_of, seats_since)
    return HistoryInputs(as_of, sessions, panel, seats, lhb, sw, cap_note, notes, source)
