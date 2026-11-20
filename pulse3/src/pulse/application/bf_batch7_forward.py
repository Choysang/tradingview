"""E097 (H1 prime) and E098 (H4 prime) forward recording: decisions, fills, outcomes, matched baseline, append-only rows.

Why: pre-registration 2026-11-20 sections 2 and 3. One call (``step``) takes every decision day from 2026-11-23 up to the run date that has no ``day`` row yet and records it, then writes the
outcome of every decision day whose exit is observable. Nothing is rewritten: a row exists once.
Dataset ``batch7_forward`` (columns experiment, hypothesis, session, kind, code, body, body_sha256):
  ``day``          one per (experiment, decision day): status ``decided`` | ``no_data`` | ``no_seats``, the raw bars source used (native | legacy | legacy_no_factors), counts, rows not classified;
  ``event``        E097: one per leader break (industry, leader, previous streak, number of targets);
  ``signal``       E097: core / follower of a break; E098: institution net buyer (net, share of turnover); one per (day, code);
  ``control``      E098: LHB stock of the day without institution net buying in the same pct-change quintile as an event stock (reported, never judged);
  ``outcome``      one per signal (and ``control_outcome`` per control) once its exit is observable: status filled | blocked_buy | no_bar | flag_unknown | exit_unresolved, entry, exit,
                   gross and net return, matched baseline (E062) and excess;
  ``day_outcome``  one per (experiment, decision day) when its outcomes were written: filled count, the day's mean matched excess and its signal count (the bootstrap input), source;
  ``judgement``    written once by ``judge`` when section 2.3 / 3 says it may judge (never rewritten).
Data rules: the day's source is native raw bars when they cover the day, else the legacy raw bars, else the day is ``no_data`` (recorded only once two later sessions have passed, never guessed);
a decision uses data up to the decision day only; seats of day D need at least 20 reports with window end D (else ``no_seats``, same two-session wait); the E098 seat-import time is recorded.
Not covered: the verdict arithmetic (``bf_batch7_reads``), scheduling, the document's section 5 (a person appends it).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application import bf_batch6_history as H6
from pulse.application import bf_batch7_inputs as IN
from pulse.application.bf_batch6_inputs import HistoryInputs, load_scope
from pulse.contracts import to_wire
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import roles as RL
from pulse.domain.batch6 import rules as R6
from pulse.domain.batch7 import rules as R7
from pulse.domain.market_rules.holidays import is_open
from pulse.domain.research import baseline as BL

DATASET = "batch7_forward"
CONTRACT = "pulse.analysis_artifact"
METHOD = "batch7-forward-v1"
CST = timezone(timedelta(hours=8))
WINDOW_DAYS = 150                       # calendar days of raw bars read per run (about 100 sessions: exact streaks up to the window length, 20-day volatility, listing counts)
WAIT_SESSIONS = 2                       # a day without data is recorded as such only after this many open days have passed
MIN_SEAT_REPORTS = 20
EXP = {"E097": "H1p", "E098": "H4p"}


def open_day(d: date) -> bool:
    state = is_open(d)
    return d.weekday() < 5 if state is None else state


def open_days_between(a: date, b: date) -> int:
    """Open days in (a, b]."""
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        n += open_day(d)
        d += timedelta(days=1)
    return n


def next_open(d: date) -> date:
    d += timedelta(days=1)
    while not open_day(d):
        d += timedelta(days=1)
    return d


def candidate_days(start: date, through: date) -> list[date]:
    out, d = [], start
    while d <= through:
        if open_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _row(exp: str, session: date, kind: str, code: str | None, body: Mapping) -> dict:
    text = json.dumps(to_wire(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"experiment": exp, "hypothesis": EXP[exp], "session": session.isoformat(), "kind": kind, "code": code or "", "body": text,
            "body_sha256": sha256(text.encode("utf-8")).hexdigest()}


# ---- stored rows -------------------------------------------------------------------------------------------------------------------------
def stored_manifests(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (DATASET,)).fetchall()


def stored_rows(conn: psycopg.Connection, lake: LakeStore) -> list[dict]:
    rows: list[dict] = []
    for m in stored_manifests(conn):
        rows += lake.read_snapshot_frames(m["content_hash"]).to_dicts()
    return rows


def publish(svc, rows: list[dict], *, as_of: datetime) -> dict:
    frame = pl.DataFrame(rows).sort(["session", "experiment", "kind", "code"])
    cov = {"dataset": DATASET, "method": METHOD, "spec_sha256": R7.SPEC_SHA256, "rows": frame.height, "evaluated_at": as_of.isoformat(),
           "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)


def index(rows: list[dict]) -> dict[str, Any]:
    """Lookup of what is already stored: day rows, signal rows by (exp, session), outcome markers."""
    day, sig, ctl, done, judged = {}, defaultdict(list), defaultdict(list), set(), {}
    for r in rows:
        k = (r["experiment"], r["session"])
        if r["kind"] == "day":
            day[k] = json.loads(r["body"])
        elif r["kind"] == "signal":
            sig[k].append((r["code"], json.loads(r["body"])))
        elif r["kind"] == "control":
            ctl[k].append((r["code"], json.loads(r["body"])))
        elif r["kind"] == "day_outcome":
            done.add(k)
        elif r["kind"] == "judgement":
            judged[r["experiment"]] = json.loads(r["body"])
    return {"day": day, "signal": sig, "control": ctl, "done": done, "judged": judged}


# ---- windows -----------------------------------------------------------------------------------------------------------------------------
class Windows:
    """Raw-bar windows read once per run (native preferred, legacy fallback) with their panels."""

    def __init__(self, conn: psycopg.Connection, lake: LakeStore, as_of: datetime, since: date, loader=None):
        self.conn, self.lake, self.as_of, self.since = conn, lake, as_of, since
        self.loader = loader
        self.code_of = {} if loader is not None else load_scope(conn, as_of)[0]
        self._wins: dict[str, IN.RawWindow | None] = {}
        self._prep: dict[str, tuple[HistoryInputs, dict]] = {}

    def window(self, which: str) -> IN.RawWindow | None:
        if which not in self._wins:
            if self.loader is not None:
                self._wins[which] = self.loader(which, self.since)
            else:
                reader = {IN.NATIVE: IN.read_native, IN.LEGACY: IN.read_legacy}[which]
                self._wins[which] = reader(self.conn, self.lake, self.as_of, self.since, self.code_of)
        return self._wins[which]

    def best(self, need_day: date | None) -> IN.RawWindow | None:
        """Native when it covers ``need_day``, else legacy when it covers it, else (older days still processable) the one that reaches later."""
        cands = [w for w in (self.window(IN.NATIVE), self.window(IN.LEGACY)) if w is not None]
        if need_day is not None:
            for w in cands:
                if w.covers(need_day):
                    return w
        return max(cands, key=lambda w: (w.last_day() or date.min, w.source == IN.NATIVE), default=None)

    def prepared(self, win: IN.RawWindow) -> tuple[HistoryInputs, dict]:
        key = win.source
        if key not in self._prep:
            self._prep[key] = IN.prepare(self.conn, self.lake, win, as_of=self.as_of, with_seats=True, seats_since=self.since)
        return self._prep[key]


# ---- decisions ---------------------------------------------------------------------------------------------------------------------------
def e097_breaks(inp: HistoryInputs, day: date) -> list[dict]:
    """Leader breaks of ``day`` with their targets (roles) - the E093 H1 rule on the exact panel."""
    sess = inp.sessions
    i = sess.index(day)
    if i < 1:
        return []
    mem = H6.members_by_day(inp.panel.filter(pl.col("date").is_in([sess[i - 1], day])))
    out = []
    prev = {sw: l for sw, ms in mem.get(sess[i - 1], {}).items() if (l := RL.leader_of(ms)) is not None}
    for sw, pl_ in prev.items():
        today = mem.get(day, {}).get(sw, [])
        ev = RL.event_of(pl_, today)
        if ev is None or ev.kind != "break":
            continue
        out.append({"industry": sw, "leader": ev.leader, "leader_streak_prev": ev.leader_streak_prev, "targets": [{"code": c, "role": r} for c, r in RL.targets_with_roles(ev, today)]})
    return out


def seat_tables_for(inp: HistoryInputs, day: date) -> tuple[dict[str, float], set[str]]:
    """({code: institution net buy (CNY)} for the day, codes on the list that day)."""
    _, net = H6.seat_tables(inp)
    nets = {c: float(n) for c, d, n in net.filter(pl.col("date") == day).select("code", "date", "net").iter_rows()}
    listed = {c for c, d in inp.lhb_days if d == day}
    return nets, listed


def e098_decision(inp: HistoryInputs, day: date) -> tuple[list[dict], list[dict]]:
    """(events, controls) of the day. Event: on the list, institution net buy > 0 and >= 5 % of the day's turnover estimate. Control: on the list, in scope, institution net not > 0,
    in the same pct-change quintile (among the day's in-scope listed stocks) as an event stock."""
    nets, listed = seat_tables_for(inp, day)
    p = inp.panel.filter((pl.col("date") == day) & pl.col("in_scope")).select("code", "amt", "pct")
    scope = {c: (a, pc) for c, a, pc in p.iter_rows()}
    events = []
    for c, net in nets.items():
        if c in scope and net > 0 and net >= R7.INSTITUTION_SHARE * scope[c][0]:
            events.append({"code": c, "net": net, "amount_est": scope[c][0], "share": net / scope[c][0] if scope[c][0] else None})
    on_list = [c for c in sorted(listed) if c in scope and scope[c][1] is not None]
    quint = dict(zip(on_list, BL.quintiles([EV.dec(scope[c][1]) for c in on_list]))) if on_list else {}
    ev_q = {quint.get(e["code"]) for e in events} - {None}
    for e in events:
        e["quintile"] = quint.get(e["code"])
    controls = [{"code": c, "quintile": quint[c], "net": nets.get(c)} for c in on_list if not nets.get(c, 0) > 0 and quint.get(c) in ev_q]
    return events, controls


@dataclass
class StepReport:
    session: str
    decided: dict
    outcomes: dict
    no_data: list
    notes: list

    def wire(self) -> dict:
        return {"through": self.session, "decided": self.decided, "outcomes": self.outcomes, "no_data": self.no_data, "notes": self.notes}


def seat_status(conn: psycopg.Connection, day: date) -> tuple[int, datetime | None]:
    r = conn.execute("SELECT count(DISTINCT instrument_id) n, max(available_at) at FROM intelligence.seat_report WHERE window_end=%s", (day,)).fetchone()
    return int(r["n"]), r["at"]


def decide(win_set: Windows, conn: psycopg.Connection, T: date, ix: dict, as_of: datetime) -> tuple[list[dict], list[date], dict]:
    """Rows for every decision day <= T without a day row. Returns (rows, days still waiting for data, counts)."""
    rows: list[dict] = []
    waiting: list[date] = []
    counts = {"E097": 0, "E098": 0}
    pending = [d for d in candidate_days(R7.FORWARD_START, T) if ("E097", d.isoformat()) not in ix["day"] or ("E098", d.isoformat()) not in ix["day"]]
    if not pending:
        return rows, waiting, counts
    for d in pending:
        win = win_set.best(d)
        has = win is not None and win.covers(d)
        late = open_days_between(d, T) >= WAIT_SESSIONS
        if not has:
            if late:
                for exp in EXP:
                    if (exp, d.isoformat()) not in ix["day"]:
                        rows.append(_row(exp, d, "day", None, {"status": "no_data", "source": None, "reason": "no raw bars (native or legacy) cover the day"}))
            else:
                waiting.append(d)
            continue
        inp, info = win_set.prepared(win)
        day_info = {"source": win.source, "rows_not_classified": info["flag_not_ok"], "ref_uncertain": info["ref_uncertain"], "ref_method": info["ref_method"], "method": METHOD}
        if ("E097", d.isoformat()) not in ix["day"]:
            breaks = e097_breaks(inp, d)
            for b in breaks:
                rows.append(_row("E097", d, "event", None, {k: v for k, v in b.items() if k != "targets"} | {"n_targets": len(b["targets"])}))
                for t in b["targets"]:
                    rows.append(_row("E097", d, "signal", t["code"], {"role": t["role"], "industry": b["industry"], "leader": b["leader"]}))
            n_sig = sum(len(b["targets"]) for b in breaks)
            rows.append(_row("E097", d, "day", None, {"status": "decided", **day_info, "n_events": len(breaks), "n_signals": n_sig}))
            counts["E097"] += 1
        if ("E098", d.isoformat()) not in ix["day"]:
            n_rep, seat_at = seat_status(conn, d)
            if n_rep < MIN_SEAT_REPORTS:
                if late:
                    rows.append(_row("E098", d, "day", None, {"status": "no_seats", **day_info, "seat_reports": n_rep}))
                else:
                    waiting.append(d)
            else:
                events, controls = e098_decision(inp, d)
                nxt = next_open(d)
                after_open = seat_at is not None and seat_at.astimezone(CST) > datetime.combine(nxt, datetime.min.time(), CST) + timedelta(hours=9, minutes=30)
                for e in events:
                    rows.append(_row("E098", d, "signal", e["code"], {k: v for k, v in e.items() if k != "code"}))
                for c in controls:
                    rows.append(_row("E098", d, "control", c["code"], {k: v for k, v in c.items() if k != "code"}))
                rows.append(_row("E098", d, "day", None, {"status": "decided", **day_info, "n_signals": len(events), "n_controls": len(controls), "seat_reports": n_rep,
                                                          "seat_available_at": seat_at, "seats_imported_after_next_open": after_open}))
                counts["E098"] += 1
    return rows, waiting, counts


# ---- outcomes ----------------------------------------------------------------------------------------------------------------------------
def leg_dict(fwd: pl.DataFrame, day: date, codes: list[str]) -> dict[str, dict]:
    sub = fwd.filter((pl.col("date") == day) & pl.col("code").is_in(codes))
    return {r["code"]: r for r in sub.iter_rows(named=True)}


def outcome_rows(exp: str, day: date, inp: HistoryInputs, fwd: pl.DataFrame, hold: int, signals: list[tuple[str, dict]], controls: list[tuple[str, dict]], source: str,
                 n_sessions: int) -> list[dict] | None:
    """Outcome rows of one decision day, or None while some exit is not observable yet."""
    i = inp.sessions.index(day)
    codes = [c for c, _ in signals] + [c for c, _ in controls]
    legs = leg_dict(fwd, day, codes)
    waiting = [c for c in codes if legs.get(c, {}).get("status") in ("pending_entry", "pending_exit")]
    if waiting and (n_sessions - 1) < i + hold + R6.MAX_DEFER_SESSIONS:
        return None
    tables = H6.day_tables(inp.panel, fwd, {day})
    means: dict = {}
    qret: dict = {}
    if day in tables:
        pool = EV.build_pool(list(tables[day].iter_rows()))
        means = BL.matched_means(pool)
        qret = {p.key: p.ret for p in pool}
    rows: list[dict] = []

    def one(kind: str, code: str, meta: dict) -> tuple[dict, float | None]:
        leg = legs.get(code)
        status = "no_bar" if leg is None else leg["status"]
        if status == "pending_exit":
            status = "exit_unresolved"
        body: dict[str, Any] = {"status": status, "source": source, **{k: v for k, v in meta.items() if k in ("role", "net", "share", "quintile")}}
        ex = None
        if status == "filled" and code in means and "sv" in means[code]:
            m = means[code]
            ret = qret[code]
            ex = float(ret - m["sv"])
            body.update({"entry_c": leg["entry_c"], "exit_c": leg["exit_c"], "exit_si": leg["exit_si"], "ret_gross": leg["ret"], "ret_net": leg["ret"] - float(R7.COST), "base_all": float(m["all"]),
                         "base_size": float(m["size"]), "base_sv": float(m["sv"]), "base_svi": float(m["svi"]), "ex_sv": ex, "ex_all": float(ret - m["all"])})
        elif status == "filled":
            body.update({"entry_c": leg["entry_c"], "exit_c": leg["exit_c"], "ret_gross": leg["ret"], "ret_net": leg["ret"] - float(R7.COST), "ex_sv": None, "note": "no matched baseline (cap or volatility missing)"})
        return _row(exp, day, kind, code, body), ex

    ex_s, ex_c = [], []
    for c, meta in signals:
        r, ex = one("outcome", c, meta)
        rows.append(r)
        if ex is not None:
            ex_s.append(ex)
    for c, meta in controls:
        r, ex = one("control_outcome", c, meta)
        rows.append(r)
        if ex is not None:
            ex_c.append(ex)
    n_filled = sum(1 for r in rows if r["kind"] == "outcome" and json.loads(r["body"])["status"] == "filled")
    rows.append(_row(exp, day, "day_outcome", None, {"source": source, "n_signals": len(signals), "n_filled": n_filled, "k": len(ex_s), "ex_sv_day": (sum(ex_s) / len(ex_s)) if ex_s else None,
                                                     "n_controls": len(controls), "control_k": len(ex_c), "control_ex_sv_day": (sum(ex_c) / len(ex_c)) if ex_c else None, "n_pool": len(qret)}))
    return rows


def outcomes(win_set: Windows, ix: dict, decided_now: set[tuple[str, str]], rows_now: list[dict]) -> list[dict]:
    """Outcome rows for every decided day whose exit is observable (a day decided in this run counts)."""
    new_ix_day = dict(ix["day"])
    sig = {k: list(v) for k, v in ix["signal"].items()}
    ctl = {k: list(v) for k, v in ix["control"].items()}
    for r in rows_now:
        k = (r["experiment"], r["session"])
        if r["kind"] == "day":
            new_ix_day[k] = json.loads(r["body"])
        elif r["kind"] == "signal":
            sig.setdefault(k, []).append((r["code"], json.loads(r["body"])))
        elif r["kind"] == "control":
            ctl.setdefault(k, []).append((r["code"], json.loads(r["body"])))
    todo = sorted(((exp, s) for (exp, s), b in new_ix_day.items() if b.get("status") == "decided" and (exp, s) not in ix["done"]), key=lambda x: (x[1], x[0]))
    out: list[dict] = []
    if not todo:
        return out
    win = win_set.best(None)
    if win is None:
        return out
    inp, _ = win_set.prepared(win)
    n = len(inp.sessions)
    cache: dict[int, pl.DataFrame] = {}
    for exp, s in todo:
        d = date.fromisoformat(s)
        if d not in inp.sessions:
            continue
        hold = R7.HYPOTHESES[EXP[exp]][2]
        if hold not in cache:
            cache[hold] = PN.forward(inp.panel, hold, n)
        res = outcome_rows(exp, d, inp, cache[hold], hold, sig.get((exp, s), []), ctl.get((exp, s), []), win.source, n)
        if res is not None:
            out += res
    return out


def step(conn: psycopg.Connection, lake: LakeStore, svc, T: date, *, as_of: datetime, loader=None) -> StepReport:
    rows_stored = stored_rows(conn, lake)
    ix = index(rows_stored)
    pending_exists = any(("E097", d.isoformat()) not in ix["day"] or ("E098", d.isoformat()) not in ix["day"] for d in candidate_days(R7.FORWARD_START, T))
    decided_days = [date.fromisoformat(s) for (e, s), b in ix["day"].items() if b.get("status") == "decided" and (e, s) not in ix["done"]]
    if not pending_exists and not decided_days:
        return StepReport(T.isoformat(), {}, {}, [], ["nothing pending"])
    pending_days = [d for d in candidate_days(R7.FORWARD_START, T) if ("E097", d.isoformat()) not in ix["day"] or ("E098", d.isoformat()) not in ix["day"]]
    since = min(pending_days + decided_days) - timedelta(days=WINDOW_DAYS)
    ws = Windows(conn, lake, as_of, since, loader=loader)
    new_rows, waiting, counts = decide(ws, conn, T, ix, as_of)
    out_rows = outcomes(ws, ix, set(), new_rows)
    allrows = new_rows + out_rows
    notes = [f"waiting for data: {[d.isoformat() for d in waiting]}"] if waiting else []
    if allrows:
        publish(svc, allrows, as_of=as_of)
    n_out = sum(1 for r in out_rows if r["kind"] == "day_outcome")
    return StepReport(T.isoformat(), counts, {"days_resolved": n_out}, [r["session"] for r in new_rows if r["kind"] == "day" and json.loads(r["body"]).get("status") != "decided"], notes)
