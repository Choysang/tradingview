"""E099 historical robustness run (pre-registration section 4): H8a and H8b on the limit-up closes of 2023-11-01 .. 2026-11-20. REPORT ONLY, never decides.

Why: E048 has already seen these listed stocks, so the history cannot confirm anything; the document asks for ONE run whose only use is "is the direction the same as the forward result".
This module computes the same statistics as the forward judge (``bf_batch8_core`` + ``domain.batch8.stats``) over every decision day in the window whose D+5 exit is observable, and states
the direction of each effect against the pre-registered negative sign. There is no pass, no Holm and no sample-size rule here; ``decides`` is always False.
Source: native raw bars when they cover the last requested day, else the legacy raw bars (which start 2025-10-31): the report states the sessions actually used and ``window_complete``;
the seat history is read from 2023-01-01 whatever the bar window is, point in time as of ``as_of`` (one revision per report; a window before 2026-11-24 was bulk imported, so its report is cut at ``as_of`` only) (the DB holds reports from 2023-10-31, so T1 needs a warm-up: ``t1_warmup_note``).
Not covered: the forward confirmation (``bf_batch8_forward``), any decision.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bf_batch8_core as CORE
from pulse.application import bindings as B
from pulse.contracts import to_wire
from pulse.domain.batch8 import rules as R
from pulse.domain.batch8 import stats as ST

DATASET = "batch8_history"
CONTRACT = "pulse.analysis_artifact"
WARMUP_DAYS = 130                           # calendar days of bars before the first decision day: streaks, 20-day volatility, the 60-session listing rule


def direction(effect: float | None) -> str | None:
    if effect is None:
        return None
    return "negative (the pre-registered sign)" if effect < 0 else "not negative (opposite to the pre-registered sign)"


def collect(inp, flags: CORE.SeatFlags, days: list[date], source: str) -> tuple[list[ST.NameRec], list[str], dict[str, int]]:
    """Name records and resolved sessions of the decision ``days`` (days whose D+5 exit is not observable are counted, not used)."""
    gated, held = CORE.legs(inp)
    recs: list[ST.NameRec] = []
    resolved: list[str] = []
    skipped = {"not_observable": 0, "not_in_sessions": 0}
    for d in days:
        if d not in inp.sessions:
            skipped["not_in_sessions"] += 1
            continue
        res = CORE.outcome_bodies(d, inp, gated, held, CORE.limit_up_samples(inp, d, flags), source)
        if res is None:
            skipped["not_observable"] += 1
            continue
        resolved.append(d.isoformat())
        recs += [CORE.rec_of(d.isoformat(), c, b) for c, b in res[0]]
    return recs, resolved, skipped


def run(conn: psycopg.Connection, lake: LakeStore, *, as_of: datetime, since: date = R.HISTORY_START, until: date = R.HISTORY_END, flags: CORE.SeatFlags | None = None) -> dict[str, Any]:
    code_of = CORE.code_of(conn, as_of)
    win = IN7.pick_window(conn, lake, as_of, since - timedelta(days=WARMUP_DAYS), code_of, need_day=until)
    if win is None:
        return {"status": "no_data", "reason": f"neither native nor legacy raw bars cover {until}", "window_complete": False, "decides": False}
    inp, info = IN7.prepare(conn, lake, win, as_of=as_of, with_seats=False)
    flags = flags or CORE.SeatFlags.load(conn, code_of, until, as_of)
    days = [d for d in inp.sessions if since <= d <= until]
    recs, resolved, skipped = collect(inp, flags, days, win.source)
    first = days[0] if days else None
    complete = bool(first is not None and first <= since + timedelta(days=31))
    rep = ST.report(recs, resolved) if resolved else None
    whole = rep["whole"] if rep else None
    dirs = None if whole is None else {h: direction(whole[h]["effect"]) for h in R.HYPOTHESES}
    n_flag = sum(1 for r in recs if r.flag)
    return {"status": "ok" if rep else "no_sample", "source": win.source, "window_requested": [since.isoformat(), until.isoformat()],
            "window_used": [days[0].isoformat(), days[-1].isoformat(), len(days)] if days else None, "window_complete": complete, "sessions_resolved": len(resolved), "skipped": skipped,
            "names": len(recs), "flag_names": n_flag, "flag_matched": sum(1 for r in recs if r.flag and r.held_ex is not None), "report": rep, "direction_vs_preregistered": dirs,
            "seat_history": {"first_day": flags.first_day, "rows": flags.rows},
            "t1_warmup_note": "a seat needs 5 settled buys before D; reports start 2023-10-31 in the local store, so T1 is thin early in the window",
            "flag_info": {k: v for k, v in info.items() if k != "notes"}, "notes": info["notes"], "decides": False}


def publish(svc, out: dict, *, as_of: datetime) -> dict:
    body = json.dumps(to_wire(out), sort_keys=True, ensure_ascii=False, default=str)
    rid = sha256(f"{as_of.isoformat()}{out.get('source')}".encode()).hexdigest()[:16]
    frame = pl.DataFrame({"run_id": [rid], "kind": ["report"], "body": [body]})
    cov = {"dataset": DATASET, "spec_sha256": R.SPEC_SHA256, "run_id": rid, "as_of": as_of.isoformat(), "frame_sha256": sha256(frame.write_csv().encode()).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)
