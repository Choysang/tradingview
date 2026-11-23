"""E099 forward recording: decisions (flags on the limit-up-close sample), outcomes (both D+5 legs with matched baselines) and the paper-replay overlay, append-only.

Why: pre-registration 2026-11-23 sections 2-5. One call (``step``) takes every decision day from 2026-11-24 up to the run date that has no ``day`` row yet and records it, then writes the
outcome of every decided day whose D+5 exit is observable and the overlay of every day whose next session is known. Nothing is rewritten: a row exists once.
Dataset ``batch8_forward`` (columns experiment, hypothesis, session, kind, code, body, body_sha256), experiment ``E099``:
  ``day``          one per decision day D: status ``decided`` | ``no_data`` | ``no_seats``, the raw-bar source, counts, seat reports found, when the seats were imported;
  ``signal``       one per limit-up-close name of D: group (first / consecutive / unknown_prev), Lhasa count, one-day-seat buyers, L1 / L3 / T1 / FLAG, listed on the list;
  ``outcome``      one per signal once its D+5 exit is observable: the ``gated`` leg (entry rule, H8b) and the ``held`` leg (H8a), each with status, entry, exit, returns, matched baseline, excess;
  ``day_outcome``  one per resolved decision day (the bootstrap slot): counts, FLAG samples with a matched H8a leg;
  ``overlay_day``  one per paper-replay day D (section 5): number of B1 / B3 fills, flags known or not;
  ``overlay``      one per filled B1 / B3 paper opportunity (key = its opportunity id: intent and queue scenario): L1 / L3 / T1 flags of night D; for a FLAG name also the counterfactual D+1-open sale next to every actual sell rule;
  ``overlay_summary`` / ``judgement``  written once each (``bf_batch8_reads``).
Data rules: a decision uses data up to D only; seats of D are read point in time (``bf_batch8_core``: usable at the D+1 open, latest revision) and need at least 20 reports with window end D (else ``no_seats`` after the same two-session wait as batch 7); a day without raw bars is
``no_data`` after two later sessions, never guessed; the number of D reports that arrived after the D+1 open (ignored) is recorded on the day row.
Not covered: statistics and the verdict (``bf_batch8_reads``), scheduling.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Callable, Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_forward as FW7
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_batch8_core as CORE
from pulse.application import bindings as B
from pulse.contracts import to_wire
from pulse.domain.batch8 import rules as R

DATASET = "batch8_forward"
CONTRACT = "pulse.analysis_artifact"
METHOD = "batch8-forward-v1"
HYPOTHESIS = "H8a+H8b"
PAPER_DAYS, PAPER_BUYS, PAPER_SELLS = "fwdlog_paper_days", "fwdlog_paper_buys", "fwdlog_paper_sells"
OVERLAY_RULES = ("B1", "B3")
FILLED_STATUS = ("FILLED", "PARTIALLY_FILLED")
open_day, open_days_between, next_open, candidate_days = FW7.open_day, FW7.open_days_between, FW7.next_open, FW7.candidate_days
WINDOW_DAYS, WAIT_SESSIONS, MIN_SEAT_REPORTS, CST = FW7.WINDOW_DAYS, FW7.WAIT_SESSIONS, FW7.MIN_SEAT_REPORTS, FW7.CST


def _row(session: date, kind: str, code: str | None, body: Mapping) -> dict:
    text = json.dumps(to_wire(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"experiment": R.EXPERIMENT, "hypothesis": HYPOTHESIS, "session": session.isoformat(), "kind": kind, "code": code or "", "body": text,
            "body_sha256": sha256(text.encode("utf-8")).hexdigest()}


def key_of(r: Mapping) -> tuple[str, str, str, str]:
    return (r["experiment"], r["session"], r["kind"], r["code"])


# ---- stored rows -------------------------------------------------------------------------------------------------------------------------
def stored_rows(conn: psycopg.Connection, lake: LakeStore) -> list[dict]:
    rows: list[dict] = []
    for m in conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (DATASET,)).fetchall():
        rows += lake.read_snapshot_frames(m["content_hash"]).to_dicts()
    return rows


def publish(svc, rows: list[dict], *, as_of: datetime, stored_keys: set | None = None) -> dict:
    """Append rows as one immutable snapshot. A row key that is already stored (or repeated in ``rows``) is a bug: refuse, never overwrite."""
    keys = [key_of(r) for r in rows]
    clash = (set(keys) & (stored_keys or set())) | {k for k in keys if keys.count(k) > 1}
    if clash:
        raise RuntimeError(f"{DATASET}: rows {sorted(clash)[:5]} already exist; a row is never rewritten")
    frame = pl.DataFrame(rows).sort(["session", "kind", "code"])
    cov = {"dataset": DATASET, "method": METHOD, "spec_sha256": R.SPEC_SHA256, "rows": frame.height, "evaluated_at": as_of.isoformat(),
           "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)


def index(rows: list[dict]) -> dict[str, Any]:
    day: dict[str, dict] = {}
    sig: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    out: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    done: set[str] = set()
    ov_day: dict[str, dict] = {}
    ov: dict[str, list[dict]] = defaultdict(list)
    one: dict[str, dict] = {}
    for r in rows:
        s, k = r["session"], r["kind"]
        body = json.loads(r["body"])
        if k == "day":
            day[s] = body
        elif k == "signal":
            sig[s].append((r["code"], body))
        elif k == "outcome":
            out[s].append((r["code"], body))
        elif k == "day_outcome":
            done.add(s)
        elif k == "overlay_day":
            ov_day[s] = body
        elif k == "overlay":
            ov[s].append({"key": r["code"], **body})
        elif k in ("overlay_summary", "judgement"):
            one[k] = body
    return {"day": day, "signal": sig, "outcome": out, "done": done, "overlay_day": ov_day, "overlay": ov, "once": one, "keys": {key_of(r) for r in rows}}


# ---- decisions ---------------------------------------------------------------------------------------------------------------------------
@dataclass
class StepReport:
    session: str
    decided: int
    resolved: int
    overlay_days: int
    lost_days: list
    notes: list

    def wire(self) -> dict:
        return {"through": self.session, "decided": self.decided, "days_resolved": self.resolved, "overlay_days": self.overlay_days, "no_data": self.lost_days, "notes": self.notes}


def decide(ws: CORE.Windows, conn: psycopg.Connection, T: date, ix: dict, flags: Callable[[], CORE.SeatFlags], as_of: datetime) -> tuple[list[dict], list[date]]:
    """Rows for every decision day <= T without a day row. Returns (rows, days still waiting)."""
    rows: list[dict] = []
    waiting: list[date] = []
    for d in [d for d in candidate_days(R.FORWARD_START, T) if d.isoformat() not in ix["day"]]:
        win = ws.best(d)
        late = open_days_between(d, T) >= WAIT_SESSIONS
        inp = info = None
        if win is not None and win.covers(d):
            inp, info = ws.prepared(win)
        if inp is None or d not in inp.sessions:
            if late:
                rows.append(_row(d, "day", None, {"status": "no_data", "source": None, "reason": "no raw bars (native or legacy) cover the day"}))
            else:
                waiting.append(d)
            continue
        n_rep, seat_at, late_reports = CORE.seat_status(conn, d, as_of)
        base = {"source": win.source, "rows_not_classified": info["flag_not_ok"], "ref_uncertain": info["ref_uncertain"], "method": METHOD, "seat_reports": n_rep, "seat_reports_late_excluded": late_reports}
        if n_rep < MIN_SEAT_REPORTS:
            if late:
                rows.append(_row(d, "day", None, {"status": "no_seats", **base}))
            else:
                waiting.append(d)
            continue
        sf = flags()
        samples = CORE.limit_up_samples(inp, d, sf)
        for s in samples:
            rows.append(_row(d, "signal", s["code"], {k: v for k, v in s.items() if k != "code"}))
        rows.append(_row(d, "day", None, {"status": "decided", **base, "n_signals": len(samples), "n_flag": sum(1 for s in samples if s["flag"]), "n_l3": sum(1 for s in samples if s["l3"]),
                                          "seat_available_at": seat_at, "seat_history_first_day": sf.first_day}))
    return rows, waiting


# ---- outcomes ----------------------------------------------------------------------------------------------------------------------------
def outcomes(ws: CORE.Windows, ix: dict, rows_now: list[dict]) -> list[dict]:
    """Outcome + day_outcome rows for every decided day whose D+5 exit is observable (a day decided in this run counts)."""
    day = dict(ix["day"])
    sig = {k: list(v) for k, v in ix["signal"].items()}
    for r in rows_now:
        if r["kind"] == "day":
            day[r["session"]] = json.loads(r["body"])
        elif r["kind"] == "signal":
            sig.setdefault(r["session"], []).append((r["code"], json.loads(r["body"])))
    todo = sorted(s for s, b in day.items() if b.get("status") == "decided" and s not in ix["done"])
    out: list[dict] = []
    win = ws.best(None) if todo else None
    if win is None:
        return out
    inp, _ = ws.prepared(win)
    gated = held = None
    for s in todo:
        d = date.fromisoformat(s)
        if d not in inp.sessions:
            continue
        if gated is None:
            gated, held = CORE.legs(inp)
        samples = [{"code": c, **b} for c, b in sig.get(s, [])]
        res = CORE.outcome_bodies(d, inp, gated, held, samples, win.source)
        if res is None:
            continue
        bodies, summary = res
        out += [_row(d, "outcome", c, b) for c, b in bodies]
        out.append(_row(d, "day_outcome", None, summary))
    return out


# ---- paper-replay overlay (section 5) ----------------------------------------------------------------------------------------------------
def paper_rows(conn: psycopg.Connection, lake: LakeStore, dataset: str, sessions: set[str]) -> dict[str, list[dict]]:
    """Stored rows of an fwdlog paper dataset for the given sessions: session -> bodies (the first row of a repeated key wins)."""
    out: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for m in conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (dataset,)).fetchall():
        if not (sessions & set((m["coverage_document"] or {}).get("session_dates", ()))):
            continue
        for r in lake.read_snapshot_frames(m["content_hash"]).iter_rows(named=True):
            k = (r["session_date"], r["row_key"])
            if r["session_date"] in sessions and k not in seen:
                seen.add(k)
                out[r["session_date"]].append(json.loads(r["body"]))
    return out


def _dec(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _ratio(price: Decimal | None, fill: Decimal) -> dict:
    if price is None or fill <= 0:
        return {"price": None, "ret_gross": None, "ret_net": None}
    r = float(price / fill - 1)
    return {"price": str(price), "ret_gross": r, "ret_net": r - float(R.COST)}


def _overlay_key(b: Mapping, day: date, used: set[str]) -> str:
    """Unique row key of one paper fill: the paper row's own opportunity id (it carries the intent), else rule-code-scenario; a repeat inside the day gets a numbered suffix so two
    intents of the same rule, code and scenario can never make ``publish`` refuse the day (which would block every later one)."""
    base = str(b.get("opportunity_id") or f"{day}-{b['rule']}-{b['code']}-{b['scenario']}")
    key, n = base, 1
    while key in used:
        n += 1
        key = f"{base}#{n}"
    used.add(key)
    return key


def overlay_rows(conn: psycopg.Connection, lake: LakeStore, ws: CORE.Windows, ix: dict, T: date, flags: Callable[[], CORE.SeatFlags], as_of: datetime) -> tuple[list[dict], list[date]]:
    """Overlay rows of every paper-replay day D (>= 2026-11-24) that has none yet and whose inputs are known (or are lost after the wait)."""
    cand = [d for d in candidate_days(R.START_OVERLAY, T) if d.isoformat() not in ix["overlay_day"]]
    if not cand:
        return [], []
    need = {d.isoformat() for d in cand} | {next_open(d).isoformat() for d in cand}
    days = paper_rows(conn, lake, PAPER_DAYS, need)
    buys, sells = paper_rows(conn, lake, PAPER_BUYS, need), paper_rows(conn, lake, PAPER_SELLS, need)
    rows: list[dict] = []
    waiting: list[date] = []
    open_px: Callable[[str, date], Decimal | None] | None = None
    for d in cand:
        nxt = next_open(d)
        if nxt > T:
            waiting.append(d)
            continue
        late = open_days_between(nxt, T) >= WAIT_SESSIONS
        s, sn = d.isoformat(), nxt.isoformat()
        paper_ok = s in days and sn in days
        n_rep, seat_at, _ = CORE.seat_status(conn, d, as_of)
        seats_ok = n_rep >= MIN_SEAT_REPORTS
        if (not paper_ok or not seats_ok) and not late:
            waiting.append(d)
            continue
        fills = [b for b in buys.get(s, []) if b.get("rule") in OVERLAY_RULES and b.get("status") in FILLED_STATUS and (b.get("filled_quantity") or 0) > 0 and _dec(b.get("avg_fill_price"))]
        fl = flags().of_day(d) if seats_ok else None
        rows.append(_row(d, "overlay_day", None, {"paper_day": s in days, "paper_next_day": sn in days, "seat_reports": n_rep, "flags_known": seats_ok, "seat_available_at": seat_at,
                                                  "fills": len(fills), "intents": len({(b["rule"], b["code"]) for b in fills})}))
        if open_px is None:
            open_px = _daily_open(ws)
        book = {(x["code"], x["rule"]): x for x in sells.get(sn, []) if x.get("status") == "FILLED"}
        used: set[str] = set()
        for b in sorted(fills, key=lambda x: (str(x.get("decision_time")), str(x.get("opportunity_id")), x["rule"], x["code"], x["scenario"])):
            code, rule, sc = b["code"], b["rule"], b["scenario"]
            fill = _dec(b["avg_fill_price"])
            f = None if fl is None else {**{k: v for k, v in fl.get(code, CORE.NO_FLAG).items()}, "listed": flags().listed(code, d)}
            body: dict[str, Any] = {"rule": rule, "scenario": sc, "status": b["status"], "filled_quantity": b["filled_quantity"], "fill_price": str(fill), "decision_time": b.get("decision_time"),
                                    "next_session": sn, "flags_known": f is not None, "flags": f}
            if f is not None and f["flag"]:
                s0 = book.get((code, "S0_open"))
                cf_px, cf_src = (_dec(s0["price"]), "paper_S0_open") if s0 else (open_px(code, nxt), "daily_bar_open")
                body["counterfactual_d1_open"] = {**_ratio(cf_px, fill), "source": cf_src if cf_px is not None else None}
                actual = {r: _ratio(_dec(x["price"]), fill) for (c, r), x in book.items() if c == code and _dec(x.get("price"))}
                body["actual_exits"] = actual
                body["actual_status"] = "recorded" if actual else ("no_paper_next_day" if sn not in days else "not_in_book")
            rows.append(_row(d, "overlay", _overlay_key(b, d, used), body))
    return rows, waiting


def _daily_open(ws: CORE.Windows) -> Callable[[str, date], Decimal | None]:
    """D+1 open of a code from the daily bars (cents), the fallback of the counterfactual when the paper book has no S0 row (a name that did not close at the limit)."""
    win = ws.best(None)
    if win is None:
        return lambda code, d: None
    inp, _ = ws.prepared(win)
    px = {(c, d): o for c, d, o in inp.panel.select("code", "date", "o").iter_rows() if o is not None}
    return lambda code, d: (Decimal(px[(code, d)]) / 100) if (code, d) in px else None


# ---- the daily step ----------------------------------------------------------------------------------------------------------------------
def step(conn: psycopg.Connection, lake: LakeStore, svc, T: date, *, as_of: datetime, loader=None, flags_loader=None) -> StepReport:
    stored = stored_rows(conn, lake)
    ix = index(stored)
    cand = candidate_days(R.FORWARD_START, T)
    pending = [d for d in cand if d.isoformat() not in ix["day"]]
    unresolved = [date.fromisoformat(s) for s, b in ix["day"].items() if b.get("status") == "decided" and s not in ix["done"]]
    ov_todo = [d for d in cand if d.isoformat() not in ix["overlay_day"]]
    if not (pending or unresolved or ov_todo):
        return StepReport(T.isoformat(), 0, 0, 0, [], ["nothing pending"])
    ws = CORE.Windows(conn, lake, as_of, min(pending + unresolved + ov_todo) - timedelta(days=WINDOW_DAYS), loader=loader)
    cache: dict[str, CORE.SeatFlags] = {}

    def flags() -> CORE.SeatFlags:
        if "f" not in cache:
            cache["f"] = (flags_loader or (lambda c, t: CORE.SeatFlags.load(c, CORE.code_of(c, as_of), t, as_of)))(conn, T)
        return cache["f"]

    new_rows, waiting = decide(ws, conn, T, ix, flags, as_of)
    out_rows = outcomes(ws, ix, new_rows)
    ov_rows, ov_wait = overlay_rows(conn, lake, ws, ix, T, flags, as_of)
    allrows = new_rows + out_rows + ov_rows
    notes = [f"waiting for data: {[d.isoformat() for d in sorted(set(waiting))]}"] if waiting else []
    if ov_wait:
        notes.append(f"overlay waiting: {[d.isoformat() for d in ov_wait]}")
    if allrows:
        publish(svc, allrows, as_of=as_of, stored_keys=ix["keys"])
    return StepReport(T.isoformat(), sum(1 for r in new_rows if r["kind"] == "day" and json.loads(r["body"])["status"] == "decided"), sum(1 for r in out_rows if r["kind"] == "day_outcome"),
                      sum(1 for r in ov_rows if r["kind"] == "overlay_day"), [r["session"] for r in new_rows if r["kind"] == "day" and json.loads(r["body"])["status"] != "decided"], notes)
