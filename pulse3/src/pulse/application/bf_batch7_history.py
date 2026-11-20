"""E097 historical robustness check (pre-registration 2.3): the E093 H1 rule recomputed with EXACT limit flags from raw prices, reported next to the forward-adjusted version. Never decides.

Why: batch 6 section 7.4 found H1 on forward-adjusted bars whose limit flags miss about 3 % of limit-ups. This command recomputes the same rule (roles, break events, D+1 open to D+3 close, fills,
E062 matched baseline, E082 block bootstrap) with ``limit_flags_raw`` flags over the raw history 2022-12 to 2026-11-18 and, on the same sessions, with the adjusted panel of batch 6, and says
how many leaders / breaks / followers / core stocks differ. The seed is the batch 7 seed (20261120) for both variants; the numbers published in 7.4 (seed 20261118) are quoted as they were.
Rules: if the exact result has the opposite sign of 7.4 the document says H1 goes back to "undecided" before the forward confirmation; this module only reports (``direction_vs_74``).
Source: native raw bars when they cover the requested end day, else the legacy raw bars (which start 2025-10-31): the report states the sessions actually used and ``window_complete``.
Not covered: any decision; the forward confirmation (``bf_batch7_forward``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch6_history as H6
from pulse.application import bf_batch6_inputs as IN6
from pulse.application import bf_batch7_inputs as IN7
from pulse.application import bindings as B
from pulse.application.bf_batch6_inputs import WARMUP_SESSIONS, HistoryInputs, load_scope
from pulse.contracts import to_wire
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import roles as RL
from pulse.domain.batch6 import rules as R6
from pulse.domain.batch7 import rules as R7

DATASET = "batch7_history"
CONTRACT = "pulse.analysis_artifact"
#: H1 as published in batch 6 section 7.4 (forward-adjusted bars, 2023-11-15 .. 2026-11-18, seed 20261118): PRE then DEV
REF_74 = {"PRE": {"signals": 1672, "matched": 1618, "excess": -0.00875, "p": 0.0000, "mean_ret_net": -0.0088},
          "DEV": {"signals": 260, "matched": 251, "excess": -0.01080, "p": 0.0206, "mean_ret_net": -0.0228}}


def breaks_with_roles(inp: HistoryInputs, last_day: date | None = None) -> list[dict]:
    """Every leader break of the window with its targets and roles (E093 H1 rule)."""
    mem = H6.members_by_day(inp.panel)
    sess = inp.sessions
    leaders: dict[date, dict] = {d: {sw: l for sw, ms in mem.get(d, {}).items() if (l := RL.leader_of(ms)) is not None} for d in sess}
    out = []
    for i in range(max(1, WARMUP_SESSIONS), len(sess)):
        d = sess[i]
        if last_day is not None and d > last_day:
            break
        for sw, pl_ in leaders[sess[i - 1]].items():
            today = mem.get(d, {}).get(sw, [])
            ev = RL.event_of(pl_, today)
            if ev is not None and ev.kind == "break":
                out.append({"day": d, "industry": sw, "leader": ev.leader, "targets": RL.targets_with_roles(ev, today)})
    return out


def variant_result(inp: HistoryInputs, label: str, last_day: date) -> dict[str, Any]:
    brks = breaks_with_roles(inp, last_day)
    signals = sorted({(b["day"], c) for b in brks for c, _ in b["targets"]})
    f3 = PN.forward(inp.panel, 3, len(inp.sessions))
    res = H6.run_hypothesis("H1", signals, f3, inp, {}, None, seed=R7.SEED, last_day=last_day)
    segs = {}
    for seg, r in res["segments"].items():
        d = asdict(r)
        d.pop("n_unfilled", None)
        d["unfilled"] = dict(r.n_unfilled)
        segs[seg] = d
    return {"label": label, "sessions": [inp.sessions[0].isoformat(), inp.sessions[-1].isoformat(), len(inp.sessions)], "breaks": len(brks), "signals": len(signals), "segments": segs,
            "_breaks": brks}


def compare(exact: list[dict], adj: list[dict]) -> dict[str, Any]:
    ek = {(b["day"], b["industry"], b["leader"]) for b in exact}
    ak = {(b["day"], b["industry"], b["leader"]) for b in adj}
    e_role = {(b["day"], c): r for b in exact for c, r in b["targets"]}
    a_role = {(b["day"], c): r for b in adj for c, r in b["targets"]}
    both = e_role.keys() & a_role.keys()
    # a leader seen by only one variant in a (day, industry) both variants have an event for counts as a different leader
    ed = {(d, i): l for d, i, l in ek}
    ad = {(d, i): l for d, i, l in ak}
    common_di = ed.keys() & ad.keys()
    return {"breaks_exact": len(ek), "breaks_adjusted": len(ak), "breaks_both": len(ek & ak), "breaks_only_exact": len(ek - ak), "breaks_only_adjusted": len(ak - ek),
            "same_day_industry_different_leader": sum(1 for k in common_di if ed[k] != ad[k]),
            "targets_exact": len(e_role), "targets_adjusted": len(a_role), "targets_both": len(both), "targets_only_exact": len(e_role.keys() - a_role.keys()),
            "targets_only_adjusted": len(a_role.keys() - e_role.keys()), "same_target_different_role": sum(1 for k in both if e_role[k] != a_role[k]),
            "followers_exact": sum(1 for r in e_role.values() if r == "follower"), "followers_adjusted": sum(1 for r in a_role.values() if r == "follower"),
            "core_exact": sum(1 for r in e_role.values() if r == "core"), "core_adjusted": sum(1 for r in a_role.values() if r == "core")}


def run(conn: psycopg.Connection, lake: LakeStore, *, as_of: datetime, since: date = R7.HISTORY_START, until: date = R7.HISTORY_END) -> dict[str, Any]:
    code_of = load_scope(conn, as_of)[0]
    win = IN7.pick_window(conn, lake, as_of, since, code_of, need_day=until)
    if win is None:
        return {"status": "no_data", "reason": f"neither native nor legacy raw bars cover {until}", "window_complete": False}
    inp, info = IN7.prepare(conn, lake, win, as_of=as_of)
    first = inp.sessions[0]
    exact = variant_result(inp, f"exact limit flags from raw bars ({win.source})", until)
    adj_inp = IN6.prepare_history(conn, lake, as_of=as_of, since=first, source="adjusted")
    adj = variant_result(adj_inp, "forward-adjusted bars (batch 6 definition)", until)
    cmp_ = compare(exact.pop("_breaks"), adj.pop("_breaks"))
    pre = exact["segments"]["PRE"]
    complete = bool(first <= since + timedelta(days=31))
    if not complete:
        direction = f"not comparable: the raw window starts {first}, the requested start is {since}"
    elif pre.get("excess") is None:
        direction = None
    else:
        direction = "same as 7.4 (negative)" if pre["excess"] < 0 else "OPPOSITE to 7.4 (not negative): H1 goes back to undecided before the forward confirmation (2.3)"
    return {"status": "ok", "source": win.source, "window_requested": [since.isoformat(), until.isoformat()], "window_used": exact["sessions"],
            "window_complete": complete, "exact": exact, "adjusted": adj, "compare": cmp_, "reference_74": REF_74,
            "direction_vs_74": direction, "flag_info": {k: v for k, v in info.items() if k != "notes"}, "notes": info["notes"], "decides": False}


def publish(svc, out: dict, *, as_of: datetime) -> dict:
    body = json.dumps(to_wire(out), sort_keys=True, ensure_ascii=False, default=str)
    rid = sha256(f"{as_of.isoformat()}{out.get('source')}".encode()).hexdigest()[:16]
    frame = pl.DataFrame({"run_id": [rid], "kind": ["report"], "body": [body]})
    cov = {"dataset": DATASET, "spec_sha256": R7.SPEC_SHA256, "run_id": rid, "as_of": as_of.isoformat(), "frame_sha256": sha256(frame.write_csv().encode()).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)
