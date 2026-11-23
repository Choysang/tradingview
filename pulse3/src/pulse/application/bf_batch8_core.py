"""E099 shared steps of the forward recorder and the historical run: windows, seat flags, the limit-up-close sample, both D+1 -> D+5 legs and their matched baselines.

Why: ``bf_batch8_forward`` (append-only rows) and ``bf_batch8_history`` (report only) must produce the same numbers from the same definitions, so the definitions live here once.
Invariants:
  * sample (section 2): in-scope main-board names (not ST, listed >= 60 sessions, traded, limit flags classified) whose close on D equals the exact limit-up price (``lu`` of the batch 7 exact
    panel, i.e. ``refdata_core.limit_flags_raw``; unclassified stock-days are out). First board = the session before D has a bar that is classified and not a limit-up close (or has no bar:
    a suspended name did not close at the limit); consecutive = that session closed at the limit-up price; ``unknown_prev`` = that session's flags are unknown (reported, part of the whole).
  * two legs per name, both D+1 open -> D+5 close with the batch 6 sell deferral: ``gated`` (batch 6 fill rule: an open at the limit-up price cannot be bought, used by H8b) and ``held``
    (the same without the buy rule, used by H8a: the position is already held). A D+1 stock-day whose flags are unknown is out in both.
  * matched baseline (E062) per leg kind: the pool of a day is every in-scope name with a filled leg of that kind that day.
  * flags use the single-day seat reports of D and the seat history up to D (point in time, ``domain.batch8.seats``); seat history is read from 2023-01-01 on, every call.
  * seat reports are read point in time by a batch 8 loader (``load_seat_history``; batch 6's ``load_seats`` is untouched): ONE revision per report (``source_id``, ``source_report_id``), the
    latest revision whose ``available_at`` is at or before the report's cut; the cut of a report with window end >= 2026-11-24 is the earlier of the run's ``as_of`` and the next open
    day's 09:30 (Asia/Shanghai) after the window end (a report published later could not have been used at the D+1 open, so it does not exist for the flags); a report of an earlier window
    (the bulk import, whose ``available_at`` is the import time) is cut at ``as_of`` only.
Not covered: rows, registry, reads (other ``bf_batch8_*`` modules), the statistics (``domain.batch8.stats``).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import UUID
from typing import Any

import polars as pl
import psycopg

from pulse.application import bf_batch6_history as H6
from pulse.application import bf_batch7_forward as FW7
from pulse.application import bf_batch7_inputs as IN7
from pulse.application.bf_batch6_inputs import HistoryInputs
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import rules as R6
from pulse.domain.batch8 import rules as R
from pulse.domain.batch8 import seats as SE
from pulse.domain.batch8.stats import NameRec
from pulse.domain.research import baseline as BL

SEAT_HISTORY_START = date(2023, 1, 1)
CALENDAR_TAIL_DAYS = 20
OPEN_CUT = time(9, 30)
SEAT_CHUNK = 4000
SEAT_SCHEMA = {"code": pl.String, "date": pl.Date, "side": pl.String, "seat": pl.String, "amount": pl.Float64}
NO_FLAG = {"lhasa": 0, "trip_seats": 0, "l1": False, "l3": False, "t1": False, "flag": False}


class Windows(FW7.Windows):
    """Batch 7 raw-bar windows (native first, legacy fallback) prepared WITHOUT the seat load: E099 loads the whole seat history itself."""

    def prepared(self, win: IN7.RawWindow) -> tuple[HistoryInputs, dict]:
        if win.source not in self._prep:
            self._prep[win.source] = IN7.prepare(self.conn, self.lake, win, as_of=self.as_of, with_seats=False)
        return self._prep[win.source]


def calendar_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if FW7.open_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def report_cut(window_end: date, as_of: datetime) -> datetime:
    """The latest ``available_at`` a seat report of ``window_end`` may have to be used (see the module docstring)."""
    if window_end < R.FORWARD_START:
        return as_of
    return min(as_of, datetime.combine(FW7.next_open(window_end), OPEN_CUT, FW7.CST))


def choose_reports(metas: list[dict], as_of: datetime) -> tuple[set, set[tuple[UUID, date]]]:
    """(report_revision_ids to read, (instrument, window end) pairs with any usable report): one revision per report, the latest usable one; single-day windows only for the rows."""
    best: dict[tuple[str, str], dict] = {}
    present: set[tuple[UUID, date]] = set()
    for m in metas:
        if m["available_at"] > report_cut(m["window_end"], as_of):
            continue
        present.add((m["instrument_id"], m["window_end"]))
        if m["window_start"] != m["window_end"]:
            continue
        k = (m["source_id"], m["source_report_id"])
        if k not in best or m["revision"] > best[k]["revision"]:
            best[k] = m
    return {m["report_revision_id"] for m in best.values()}, present


def load_seat_history(conn: psycopg.Connection, code_of: dict[str, str], as_of: datetime) -> tuple[pl.DataFrame, set[tuple[str, date]]]:
    """Point-in-time seat rows (code, date, side, seat, amount) of the single-day reports and the set of listed stock-days, as of ``as_of``."""
    ids = [UUID(i) for i in code_of]
    code_by = {UUID(k): v for k, v in code_of.items()}
    metas = conn.execute("SELECT report_revision_id, instrument_id, window_start, window_end, source_id, source_report_id, revision, available_at FROM intelligence.seat_report"
                         " WHERE window_end >= %s AND instrument_id = ANY(%s)", (SEAT_HISTORY_START, ids), prepare=False).fetchall()
    chosen, present = choose_reports(metas, as_of)
    where = {m["report_revision_id"]: (code_by[m["instrument_id"]], m["window_end"]) for m in metas if m["report_revision_id"] in chosen and m["instrument_id"] in code_by}
    rows: list[tuple] = []
    keys = list(where)
    for i in range(0, len(keys), SEAT_CHUNK):
        for r in conn.execute("SELECT report_revision_id, side, raw_seat_name, canonical_amount_cny FROM intelligence.seat_report_row WHERE report_revision_id = ANY(%s)",
                              (keys[i:i + SEAT_CHUNK],), prepare=False).fetchall():
            code, day = where[r["report_revision_id"]]
            rows.append((code, day, r["side"], r["raw_seat_name"], None if r["canonical_amount_cny"] is None else float(r["canonical_amount_cny"])))
    return pl.DataFrame(rows, schema=SEAT_SCHEMA, orient="row"), {(code_by[i], d) for i, d in present if i in code_by}


def seat_status(conn: psycopg.Connection, day: date, as_of: datetime) -> tuple[int, datetime | None, int]:
    """(instruments with a report of ``day`` usable at its cut, latest ``available_at`` among them, reports of the day that arrived after the cut and are ignored)."""
    cut = report_cut(day, as_of)
    r = conn.execute("SELECT count(DISTINCT instrument_id) FILTER (WHERE available_at <= %s) n, max(available_at) FILTER (WHERE available_at <= %s) at,"
                     " count(*) FILTER (WHERE available_at > %s) late FROM intelligence.seat_report WHERE window_end=%s", (cut, cut, cut, day)).fetchone()
    return int(r["n"]), r["at"], int(r["late"])


class SeatFlags:
    """Seat history -> flag table. ``load`` is the only place that touches the database (tests replace it)."""

    def __init__(self, table: pl.DataFrame, lhb_days: set[tuple[str, date]], first_day: date | None, rows: int):
        self.table, self.lhb_days, self.first_day, self.rows = table, lhb_days, first_day, rows

    @classmethod
    def build(cls, seats: pl.DataFrame, lhb_days: set[tuple[str, date]], through: date) -> "SeatFlags":
        first = seats["date"].min() if seats.height else None
        cal = calendar_days(min(SEAT_HISTORY_START, first or SEAT_HISTORY_START), through + timedelta(days=CALENDAR_TAIL_DAYS))
        return cls(SE.flag_table(seats, cal), lhb_days, first, seats.height)

    @classmethod
    def load(cls, conn: psycopg.Connection, code_of: dict[str, str], through: date, as_of: datetime) -> "SeatFlags":
        seats, lhb = load_seat_history(conn, code_of, as_of)
        return cls.build(seats, lhb, through)

    def of_day(self, day: date) -> dict[str, dict]:
        return dict(SE.flags_of_day(self.table, day))

    def listed(self, code: str, day: date) -> bool:
        return (code, day) in self.lhb_days


def limit_up_samples(inp: HistoryInputs, day: date, flags: SeatFlags) -> list[dict]:
    """Section 2 sample of ``day``: one dict per limit-up-close name with its group and flags."""
    sess = inp.sessions
    i = sess.index(day)
    cur = inp.panel.filter((pl.col("date") == day) & pl.col("in_scope") & pl.col("lu")).select("code", "streak").sort("code")
    prev: dict[str, tuple[bool, bool]] = {}
    if i > 0:
        prev = {c: (bool(fok), bool(lu)) for c, fok, lu in inp.panel.filter(pl.col("date") == sess[i - 1]).select("code", "flag_ok", "lu").iter_rows()}
    fl = flags.of_day(day)
    out = []
    for code, streak in cur.iter_rows():
        p = prev.get(code)
        group = "first" if p is None else "unknown_prev" if not p[0] else "consecutive" if p[1] else "first"
        out.append({"code": code, "group": group, "streak": int(streak), "listed": flags.listed(code, day), **fl.get(code, NO_FLAG)})
    return out


def legs(inp: HistoryInputs) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(gated, held) forward legs of every (code, D): the batch 6 legs, and the same without the buy rule."""
    n = len(inp.sessions)
    gated = PN.forward(inp.panel, R.HOLD, n)
    ungated = inp.panel.with_columns(pl.lit(None, dtype=inp.panel.schema["up_c"]).alias("up_c"))
    return gated, PN.forward(ungated, R.HOLD, n)


def _leg_body(leg: dict | None, means: dict, qret: dict, code: str) -> dict[str, Any]:
    status = "no_bar" if leg is None else leg["status"]
    if status == "pending_exit":
        status = "exit_unresolved"
    body: dict[str, Any] = {"status": status}
    if status != "filled":
        return body
    body.update({"entry_c": leg["entry_c"], "exit_c": leg["exit_c"], "exit_si": leg["exit_si"], "ret_gross": leg["ret"], "ret_net": leg["ret"] - float(R.COST)})
    if code in means and "sv" in means[code]:
        body.update({"base_sv": float(means[code]["sv"]), "ex_sv": float(qret[code] - means[code]["sv"])})
    else:
        body.update({"base_sv": None, "ex_sv": None, "note": "no matched baseline (cap or volatility missing)"})
    return body


def _pool(inp: HistoryInputs, fwd: pl.DataFrame, day: date) -> tuple[dict, dict]:
    tables = H6.day_tables(inp.panel, fwd, {day})
    if day not in tables:
        return {}, {}
    pool = EV.build_pool(list(tables[day].iter_rows()))
    return BL.matched_means(pool), {p.key: p.ret for p in pool}


def outcome_bodies(day: date, inp: HistoryInputs, gated: pl.DataFrame, held: pl.DataFrame, samples: list[dict], source: str) -> tuple[list[tuple[str, dict]], dict] | None:
    """``([(code, body)], day summary)`` of one decision day, or None while some exit is not observable yet (the batch 7 waiting rule)."""
    n = len(inp.sessions)
    i = inp.sessions.index(day)
    codes = [s["code"] for s in samples]
    lg = FW7.leg_dict(gated, day, codes)
    lh = FW7.leg_dict(held, day, codes)
    pending = ("pending_entry", "pending_exit")
    waiting = [c for c in codes if lg.get(c, {}).get("status") in pending or lh.get(c, {}).get("status") in pending]
    if waiting and (n - 1) < i + R.HOLD + R6.MAX_DEFER_SESSIONS:
        return None
    mg, qg = _pool(inp, gated, day)
    mh, qh = _pool(inp, held, day)
    out = []
    for s in samples:
        c = s["code"]
        out.append((c, {"group": s["group"], "streak": s["streak"], "listed": s["listed"], "lhasa": s["lhasa"], "trip_seats": s["trip_seats"], "l1": s["l1"], "l3": s["l3"], "t1": s["t1"],
                        "flag": s["flag"], "source": source, "gated": _leg_body(lg.get(c), mg, qg, c), "held": _leg_body(lh.get(c), mh, qh, c)}))
    flag = [b for _, b in out if b["flag"]]
    summary = {"source": source, "n_signals": len(out), "n_flag": len(flag), "n_l3": sum(1 for _, b in out if b["l3"]),
               "flag_matched": sum(1 for b in flag if b["held"].get("ex_sv") is not None), "gated_filled": sum(1 for _, b in out if b["gated"]["status"] == "filled"),
               "n_pool_gated": len(qg), "n_pool_held": len(qh)}
    return out, summary


def rec_of(day: str, code: str, b: dict) -> NameRec:
    """A ``NameRec`` from an outcome body."""
    g, h = b["gated"], b["held"]
    return NameRec(day, code, b["group"], bool(b["l1"]), bool(b["l3"]), bool(b["t1"]), bool(b["flag"]), h.get("ex_sv"), h.get("ret_gross"), g["status"], g.get("ex_sv"), g.get("ret_gross"))


def code_of(conn: psycopg.Connection, as_of: datetime) -> dict[str, str]:
    """iid -> code of the main-board instruments (through the batch 7 module attribute, so a test patches one place)."""
    return IN7.load_scope(conn, as_of)[0]
