"""Batch 7 read model: progress, matched excess, block-bootstrap p and the pre-registered judgement of E097 (H1 prime) and E098 (H4 prime).

Why: ``batch7 summary`` and ``batch7 judge`` show the same numbers; they come from the stored rows only (``bf_batch7_forward``).
Statistics: excess = filled signals' mean of (return - same-day matched baseline mean), name weighted; the bootstrap series are the decision days with at least one matched signal, slots
are all resolved decision days (``day_outcome`` rows; a day without events is a slot with no signal), block = holding sessions, 10,000 draws, seed 20261120, one-sided on sign x excess.
E098 also reports the control group (listed stocks without institution net buying in the same pct-change quintile): its excess is shown, never judged.
Judging (``domain.batch7.rules.judge``): E097 300 filled signals and at least 60 resolved sessions, E098 150 and 60, or the deadline 2027-06-30; otherwise progress only. ``judge`` writes ONE
``judgement`` row per experiment, the first time the condition holds, and never rewrites it.
Not covered: appending the verdict to section 5 of the document (a person does that).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch7_forward as FW
from pulse.domain.batch7 import rules as R7
from pulse.domain.research import bootstrap as BS


def experiment_stats(rows: list[dict], exp: str) -> dict[str, Any]:
    hyp = FW.EXP[exp]
    _, sign, hold, need = R7.HYPOTHESES[hyp]
    day_rows = {r["session"]: json.loads(r["body"]) for r in rows if r["experiment"] == exp and r["kind"] == "day"}
    outs = {r["session"]: json.loads(r["body"]) for r in rows if r["experiment"] == exp and r["kind"] == "day_outcome"}
    sig_status = Counter(json.loads(r["body"])["status"] for r in rows if r["experiment"] == exp and r["kind"] == "outcome")
    rets = [json.loads(r["body"]).get("ret_net") for r in rows if r["experiment"] == exp and r["kind"] == "outcome"]
    rets = [x for x in rets if x is not None]
    all_days = sorted(outs)
    days = [s for s in all_days if (outs[s].get("k") or 0) > 0]
    k = [outs[s]["k"] for s in days]
    e = [outs[s]["ex_sv_day"] for s in days]
    n_k = sum(k)
    excess = sum(a * b for a, b in zip(e, k)) / n_k if n_k else None
    n_filled = sum(o.get("n_filled") or 0 for o in outs.values())
    p = blocks = reliable = None
    if days:
        bs = BS.block_bootstrap_p(all_days, days, [sign * x for x in e], k, block=hold, seed=R7.SEED, n_boot=R7.N_BOOT)
        p, blocks, reliable = bs.p_value, bs.blocks, bs.reliable
    ck = [(outs[s]["control_k"], outs[s]["control_ex_sv_day"]) for s in all_days if (outs[s].get("control_k") or 0) > 0]
    control = (sum(a * b for a, b in ck) / sum(a for a, _ in ck)) if ck else None
    sources = Counter(b.get("source") for b in day_rows.values())
    status = Counter(b.get("status") for b in day_rows.values())
    return {"experiment": exp, "hypothesis": hyp, "sign": sign, "hold": hold, "needed_signals": need, "days_decided": status.get("decided", 0), "days_no_data": status.get("no_data", 0),
            "days_no_seats": status.get("no_seats", 0), "days_resolved": len(outs), "days_with_signal": len(days), "signals": sum(sig_status.values()), "filled": n_filled,
            "outcome_status": dict(sig_status), "matched": n_k, "excess": excess, "mean_ret_net": (sum(rets) / len(rets)) if rets else None, "p_value": p, "blocks": blocks,
            "reliable": reliable, "control_excess": control, "control_signals": sum(a for a, _ in ck), "sources": {str(k_): v for k_, v in sources.items()}}


def judgement_of(st: dict, today: date) -> R7.Judgement:
    return R7.judge(st["hypothesis"], n_filled=st["filled"], sessions=st["days_resolved"], excess=st["excess"], p_value=st["p_value"], today=today)


def overview(conn: psycopg.Connection, lake: LakeStore, today: date | None = None) -> dict:
    rows = FW.stored_rows(conn, lake)
    today = today or datetime.now(timezone.utc).astimezone(FW.CST).date()
    out = {"spec_sha256": R7.SPEC_SHA256, "forward_start": R7.FORWARD_START.isoformat(), "deadline": R7.DEADLINE.isoformat(), "experiments": {}}
    judged = FW.index(rows)["judged"]
    for exp in FW.EXP:
        st = experiment_stats(rows, exp)
        j = judgement_of(st, today)
        out["experiments"][exp] = {**st, "judgement": j.__dict__, "stored_judgement": judged.get(exp)}
    return out


def judge_and_store(svc, conn: psycopg.Connection, lake: LakeStore, today: date | None = None, as_of: datetime | None = None) -> dict:
    """Judge both experiments; append a ``judgement`` row for an experiment that is judged and has none yet."""
    as_of = as_of or datetime.now(timezone.utc)
    ov = overview(conn, lake, today)
    new = []
    for exp, st in ov["experiments"].items():
        j = st["judgement"]
        if j["state"] == "judged" and st["stored_judgement"] is None:
            new.append(FW._row(exp, today or as_of.astimezone(FW.CST).date(), "judgement", None, {**j, "excess_control": st["control_excess"], "decided_at": as_of}))
    if new:
        FW.publish(svc, new, as_of=as_of)
    ov["judgement_rows_written"] = len(new)
    return ov


def describe(ov: dict) -> str:
    lines = [f"spec {ov['spec_sha256'][:12]}  forward from {ov['forward_start']}  deadline {ov['deadline']}"]
    for exp, st in ov["experiments"].items():
        j = st["judgement"]
        ex = "-" if st["excess"] is None else f"{st['excess']:+.4%}"
        p = "-" if st["p_value"] is None else f"{st['p_value']:.4f}"
        ctl = "" if exp != "E098" else f", control excess {'-' if st['control_excess'] is None else format(st['control_excess'], '+.4%')} ({st['control_signals']} names)"
        lines.append(f"{exp} {st['hypothesis']}: decided days {st['days_decided']} (no data {st['days_no_data']}, no seats {st['days_no_seats']}), resolved sessions {st['days_resolved']}/{R7.MIN_SESSIONS}, "
                     f"filled signals {st['filled']}/{st['needed_signals']}, excess {ex}, p {p}{ctl}; sources {st['sources']}")
        lines.append(f"    {j['state']}: {j['reason']}")
    return "\n".join(lines)
