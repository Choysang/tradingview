"""E101 forward recording (append-only) and the pre-registered judgement.

Why: pre-registration 2026-11-25 sections 2-4: confirmation uses forward samples only (events whose D0 is on or after 2026-11-26). One call (``step``) records every session's
market row, every event once its D0 has passed, the D0 outcome (H1 return, H3 legs) once the D0 exit is observable, and the R outcome (H2 gap, H4 legs) once the R+2 exit is.
Dataset ``e101_forward`` (columns experiment, hypothesis, session, kind, code, body, body_sha256), experiment ``E101``:
  ``market``      one per session from 2026-11-26: equal-weight return, gap, intraday move, names;
  ``event``       one per forward event (session = D0, code = class): release time, R, the calendar names, when pulse3 first knew it, known before the D0 open or not;
  ``d0_outcome``  one per event: status ``decided`` | ``no_data``; the two groups at the session before D0 and their D0 open -> D0 close legs with matched excess (H3);
  ``r_outcome``   one per event: R gap and intraday move, the consensus and surprise (``null`` until a consensus source exists, with the reason), the two groups at the session before R
                  and their R open -> R+2 close legs (H4: dovish events decide, the others are reported);
  ``context_event`` one per classified event outside the sample whose D0 or R is in the forward window (e.g. the 2026-11-25 core PCE: its R 2026-11-26 is no
                  control day for H1);
  ``event_revised`` one per stored event whose calendar row later points to another D0 (postponed): the stored rows stay, the event is left out of the judgement;
  ``judgement_h1h3`` / ``judgement_h2h4``  written once each.
Timing: a run on day T records sessions BEFORE T only (their bars are complete by then; rows are never rewritten). The calendar's NEWEST visible revision decides.
A consensus source decided later is appended as its own row kind; the stored ``r_outcome`` rows are not rewritten.
Consensus: Eastmoney's calendar (``RPT_CPH_FECALENDAR``) and its economic-value report (``RPT_ECONOMICVALUE_USANEW``: actual and previous values only) carry NO expected value,
and the latter has no core PCE (checked 2026-11-25). Section 2 needs a consensus recorded before the release, so H2 / H4 have no sample; the user decided on 2026-11-25 to leave them empty (inconclusive after 2027-12-31). Nothing is guessed.
Not covered: the plan reports (``bf_e101_plan``), the history run (``bf_e101_service``).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any, Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_forward as FW7
from pulse.application import bf_batch8_core as CORE8
from pulse.application import bf_e101_core as C
from pulse.application import bindings as B
from pulse.contracts import to_wire
from pulse.domain.e101 import rules as R
from pulse.domain.e101 import stats as ST

DATASET = "e101_forward"
CONTRACT = "pulse.analysis_artifact"
METHOD = "e101-forward-v1"
HYPOTHESIS = "H1+H2+H3+H4"
WINDOW_DAYS = 150
WAIT_SESSIONS = 2
EVENT_KINDS = ("event", "context_event", "event_revised", "d0_outcome", "r_outcome", "plan_d0", "plan_r")
NO_CONSENSUS = ("no consensus source: Eastmoney's calendar and economic-value reports carry no expected value (checked 2026-11-25); "
                "a source is the user's decision, nothing is guessed")


def _row(session: date | str, kind: str, code: str | None, body: Mapping) -> dict:
    text = json.dumps(to_wire(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    s = session if isinstance(session, str) else session.isoformat()
    return {"experiment": R.EXPERIMENT, "hypothesis": HYPOTHESIS, "session": s, "kind": kind, "code": code or "", "body": text, "body_sha256": sha256(text.encode("utf-8")).hexdigest()}


def key_of(r: Mapping) -> tuple[str, str, str, str]:
    return (r["experiment"], r["session"], r["kind"], r["code"])


def stored_rows(conn: psycopg.Connection, lake: LakeStore) -> list[dict]:
    rows: list[dict] = []
    for m in conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (DATASET,)).fetchall():
        rows += lake.read_snapshot_frames(m["content_hash"]).to_dicts()
    return rows


def publish(svc, rows: list[dict], *, as_of: datetime, stored_keys: set | None = None) -> dict:
    """Append rows as one immutable snapshot; a key already stored (or repeated) is refused, never overwritten."""
    keys = [key_of(r) for r in rows]
    clash = (set(keys) & (stored_keys or set())) | {k for k in keys if keys.count(k) > 1}
    if clash:
        raise RuntimeError(f"{DATASET}: rows {sorted(clash)[:5]} already exist; a row is never rewritten")
    frame = pl.DataFrame(rows).sort(["session", "kind", "code"])
    cov = {"dataset": DATASET, "method": METHOD, "spec_sha256": R.SPEC_SHA256, "rows": frame.height, "evaluated_at": as_of.isoformat(),
           "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)


def index(rows: list[dict]) -> dict[str, Any]:
    ix: dict[str, Any] = {k: {} for k in ("market",) + EVENT_KINDS}
    ix.update({"once": {}, "keys": {key_of(r) for r in rows}})
    for r in rows:
        k, body = r["kind"], json.loads(r["body"])
        if k == "market":
            ix["market"][r["session"]] = body
        elif k in EVENT_KINDS:
            ix[k][f"{r['session']}|{r['code']}"] = body
        else:
            ix["once"][k] = body
    return ix


# ---- step ----------------------------------------------------------------------------------------------------------------------------------
def _event_body(e: R.Event) -> dict:
    return {"cls": e.cls, "d0": e.d0.isoformat(), "r": None if e.r is None else e.r.isoformat(), "release_at": e.release_at.isoformat(), "names": list(e.names),
            "known_at": None if e.known_at is None else e.known_at.isoformat(), "known_before_d0_open": e.known_before_open(), "calendar_event_ids": list(e.ids)}


def event_from_body(b: Mapping) -> R.Event:
    return R.Event(b["cls"], date.fromisoformat(b["d0"]), None if b["r"] is None else date.fromisoformat(b["r"]), datetime.fromisoformat(b["release_at"]), tuple(b["names"]),
                   None if b["known_at"] is None else datetime.fromisoformat(b["known_at"]), tuple(b.get("calendar_event_ids") or ()))


def _trim(inp, T: date):
    """Only sessions BEFORE the run date: a session's bars are recorded the next run, when they are complete (rows are never rewritten)."""
    keep = [d for d in inp.sessions if d < T]
    return dataclasses.replace(inp, sessions=keep, panel=inp.panel.filter(pl.col("date") < T))


def event_rows(ix: dict, current: list[R.Event], T: date) -> list[dict]:
    """New ``event`` rows (forward events whose D0 is before T), ``context_event`` rows (classified events outside the sample whose D0 or R falls in the
    forward window: their days are not control days for H1) and ``event_revised`` rows (a stored event whose calendar row now points to another D0:
    it is left out of the judgement; the stored row is never rewritten)."""
    out = []
    stored_ids = {i: k for k, b in ix["event"].items() for i in b.get("calendar_event_ids") or ()}
    now_by_id = {i: e for e in current for i in e.ids}
    for e in current:
        if R.in_forward(e) and e.d0 < T and e.key not in ix["event"]:
            out.append(_row(e.d0, "event", e.cls, _event_body(e)))
        elif not R.in_forward(e) and e.key not in ix["context_event"] and any(R.FORWARD_START <= d < T for d in (e.d0, e.r) if d is not None):
            out.append(_row(e.d0, "context_event", e.cls, _event_body(e)))
    for i, k in stored_ids.items():
        e = now_by_id.get(i)
        if e is not None and e.key != k and k not in ix["event_revised"]:
            out.append(_row(k.split("|")[0], "event_revised", k.split("|")[1], {"calendar_event_id": i, "now": _event_body(e)}))
    return out


def step(conn: psycopg.Connection, lake: LakeStore, svc, T: date, *, as_of: datetime, loader=None, events: list[R.Event] | None = None) -> dict:
    stored = stored_rows(conn, lake)
    ix = index(stored)
    current = events if events is not None else C.events_of(C.calendar_rows(conn, as_of))
    new = event_rows(ix, current, T)
    known = {k: event_from_body(b) for k, b in ix["event"].items()}
    known.update({json.loads(r["body"])["d0"] + "|" + r["code"]: event_from_body(json.loads(r["body"])) for r in new if r["kind"] == "event"})
    todo_d0 = [e for k, e in known.items() if k not in ix["d0_outcome"]]
    todo_r = [e for k, e in known.items() if k not in ix["r_outcome"] and e.r is not None and e.r < T]
    market_from = R.FORWARD_START
    ws = CORE8.Windows(conn, lake, as_of, min([market_from] + [e.d0 for e in todo_d0 + todo_r]) - timedelta(days=WINDOW_DAYS), loader=loader)
    win = ws.best(None)
    notes: list[str] = []
    if win is None:
        notes.append("no raw daily bars window")
    else:
        inp, _ = ws.prepared(win)
        inp = _trim(dataclasses.replace(inp, panel=C.with_helpers(inp.panel)), T)
        mk = C.market_days(inp.panel)
        for d in inp.sessions:
            if market_from <= d and d.isoformat() not in ix["market"] and d in mk:
                new.append(_row(d, "market", None, {**mk[d], "source": win.source}))
        h3 = C.legs_for(inp, R.H3_HOLD)
        h4 = C.legs_for(inp, R.H4_HOLD)
        for e in todo_d0:
            body = _d0_outcome(inp, h3, mk, e, T, win.source)
            if body is None:
                notes.append(f"waiting D0 {e.key}")
            else:
                new.append(_row(e.d0, "d0_outcome", e.cls, body))
        for e in todo_r:
            body = _r_outcome(inp, h4, mk, e, T, win.source)
            if body is None:
                notes.append(f"waiting R {e.key}")
            else:
                new.append(_row(e.d0, "r_outcome", e.cls, body))
    if new:
        publish(svc, new, as_of=as_of, stored_keys=ix["keys"])
    kinds: dict[str, int] = {}
    for r in new:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {"through": T.isoformat(), "appended": bool(new), "rows": kinds, "notes": notes}


def _lost(day: date, T: date) -> bool:
    return FW7.open_days_between(day, T) >= WAIT_SESSIONS


def _d0_outcome(inp, legs: pl.DataFrame, mk: dict, e: R.Event, T: date, source: str) -> dict | None:
    if e.d0 not in inp.sessions:
        return {"status": "no_data", "reason": f"no bars of D0 {e.d0}"} if _lost(e.d0, T) else None
    p = C.previous_session(inp.sessions, e.d0)
    if p is None:
        return {"status": "no_data", "reason": "no session before D0 in the window"} if _lost(e.d0, T) else None
    lb = C.leg_bodies(inp, legs, p, C.groups_at(inp.panel, p), R.H3_HOLD)
    if lb is None:
        return None
    return {"status": "decided", "source": source, "market": mk.get(e.d0), "known_before_d0_open": e.known_before_open(), **lb}


def _r_outcome(inp, legs: pl.DataFrame, mk: dict, e: R.Event, T: date, source: str) -> dict | None:
    if e.r not in inp.sessions:
        return {"status": "no_data", "reason": f"no bars of R {e.r}"} if _lost(e.r, T) else None
    p = C.previous_session(inp.sessions, e.r)
    lb = C.leg_bodies(inp, legs, p, C.groups_at(inp.panel, p), R.H4_HOLD) if p is not None else None
    if lb is None:
        return None
    return {"status": "decided", "source": source, "market": mk.get(e.r), "consensus": None, "actual": None, "surprise": None, "direction": None,
            "consensus_note": NO_CONSENSUS, **lb}


# ---- judge ---------------------------------------------------------------------------------------------------------------------------------
def _resolved(ix: dict) -> list[tuple[R.Event, dict]]:
    out = []
    for k, b in ix["d0_outcome"].items():
        if b.get("status") == "decided" and k in ix["event"] and k not in ix["event_revised"]:
            out.append((event_from_body(ix["event"][k]), b))
    return sorted(out, key=lambda x: (x[0].d0, x[0].cls))


def h1h3(ix: dict, events: list[tuple[R.Event, dict]]) -> dict[str, Any]:
    """H1 and H3 on ``events`` (a prefix of resolved forward events); slots = market sessions up to the last D0 (and the decision days before them)."""
    if not events:
        return {"events": 0}
    cut = events[-1][0].d0.isoformat()
    market_r = {s: b["r"] for s, b in ix["market"].items() if s <= cut and b.get("r") is not None}
    allev = [event_from_body(b) for b in list(ix["event"].values()) + list(ix["context_event"].values())]
    event_days = {e.d0.isoformat() for e in allev} | {e.r.isoformat() for e in allev if e.r is not None}
    slots = sorted(market_r)
    e1 = ST.h1(market_r, [e for e, _ in events], slots, event_days)
    recs = [r for e, b in events if b.get("known_before_d0_open") for r in C.group_records(e.key, b)]
    h3_slots = sorted(set(slots) | {b["decision_day"] for _, b in events})
    groups, p3 = ST.two_groups(recs, h3_slots)
    return {"events": len(events), "cut": cut, "H1": e1.wire(), "H3": {g: v.wire() for g, v in groups.items()}, "H3_p": p3}


def overview(conn: psycopg.Connection, lake: LakeStore, today: date, rows: list[dict] | None = None) -> dict[str, Any]:
    rows = stored_rows(conn, lake) if rows is None else rows
    ix = index(rows)
    res = _resolved(ix)
    waiting = sorted(k for k in ix["event"] if k not in ix["d0_outcome"] and k not in ix["event_revised"])
    n = len(res)
    if n >= R.MIN_EVENTS:
        prefix = res[: R.MIN_EVENTS]
        cut_day = prefix[-1][0].d0.isoformat()
        blocked = [k for k in waiting if k.split("|")[0] <= cut_day]
        state = "refused" if blocked else "judge"
        reason = f"blocked: events up to {cut_day} without a D0 outcome: {blocked[:3]}" if blocked else f"first {R.MIN_EVENTS} resolved events, last D0 {cut_day}"
    else:
        prefix = res
        state = "inconclusive" if today > R.DEADLINE else "refused"
        reason = f"{n}/{R.MIN_EVENTS} resolved forward events" + (" at the deadline" if state == "inconclusive" else "")
    stats = h1h3(ix, prefix)
    verdicts = None
    if state == "judge":
        g = stats["H3"]
        verdicts = R.judge({"H1": ([stats["H1"]["effect"]], stats["H1"]["p_value"]), "H3": ([g["leader"]["effect"], g["core"]["effect"]], stats["H3_p"])})
    with_consensus = sum(1 for b in ix["r_outcome"].values() if b.get("surprise") is not None)
    return {"spec_sha256": R.SPEC_SHA256, "forward_start": R.FORWARD_START.isoformat(), "deadline": R.DEADLINE.isoformat(), "today": today.isoformat(),
            "events_recorded": len(ix["event"]), "events_resolved": n, "market_sessions": len(ix["market"]), "r_outcomes": len(ix["r_outcome"]),
            "h1h3": {"state": state, "reason": reason, "stats": stats, "verdicts": None if verdicts is None else {h: v.__dict__ for h, v in verdicts.items()}},
            "h2h4": {"state": "inconclusive" if today > R.DEADLINE and with_consensus < R.MIN_EVENTS else "refused",
                     "reason": f"{with_consensus}/{R.MIN_EVENTS} events with a consensus; {NO_CONSENSUS}; user decision 2026-11-25: H2 / H4 stay empty, "
                               f"inconclusive after {R.DEADLINE}"},
            "stored": ix["once"]}


def judge_and_store(svc, conn: psycopg.Connection, lake: LakeStore, today: date, as_of: datetime) -> dict:
    rows = stored_rows(conn, lake)
    ov = overview(conn, lake, today, rows)
    new = []
    if ov["h1h3"]["state"] in ("judge", "inconclusive") and "judgement_h1h3" not in ov["stored"]:
        new.append(_row(today, "judgement_h1h3", None, {**ov["h1h3"], "decided_at": as_of}))
    if ov["h2h4"]["state"] == "inconclusive" and "judgement_h2h4" not in ov["stored"]:
        new.append(_row(today, "judgement_h2h4", None, {**ov["h2h4"], "decided_at": as_of}))
    if new:
        publish(svc, new, as_of=as_of, stored_keys={key_of(r) for r in rows})
    ov["rows_written"] = [r["kind"] for r in new]
    return ov


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.3%}"


def describe(ov: dict) -> str:
    h = ov["h1h3"]
    s = h["stats"]
    lines = [f"E101 spec {ov['spec_sha256'][:12]}  forward from {ov['forward_start']}  events {ov['events_recorded']} recorded / {ov['events_resolved']} resolved  "
             f"market sessions {ov['market_sessions']}",
             f"H1/H3 {h['state']}: {h['reason']}"]
    if s.get("events"):
        lines.append(f"  H1 effect {_pct(s['H1']['effect'])} p {s['H1']['p_value']}; H3 leader {_pct(s['H3']['leader']['effect'])} (n {s['H3']['leader']['n']}), "
                     f"core {_pct(s['H3']['core']['effect'])} (n {s['H3']['core']['n']}), p {s['H3_p']}")
    for k, v in (h.get("verdicts") or {}).items():
        lines.append(f"  {k}: {'PASS' if v['passed'] else 'no pass'}: {v['reason']}")
    lines.append(f"H2/H4 {ov['h2h4']['state']}: {ov['h2h4']['reason']}")
    return "\n".join(lines)
