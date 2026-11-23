"""E099 read model: progress, H8a / H8b effects and p-values, the pre-registered judgement, the paper-replay overlay summary.

Why: ``batch8 status`` and ``batch8 judge`` show the same numbers; they come from the stored rows only (``bf_batch8_forward``), through the same statistics as the historical run.
Judging (``domain.batch8.rules``): the resolved sessions are the ``day_outcome`` rows in date order. ``sample_cut`` picks the earliest prefix with at least 60 sessions and 150 FLAG samples
(at most 120); the effects and p-values are computed on exactly that prefix, so the answer does not depend on the day ``judge`` is run. ``judge`` REFUSES (writes nothing) before that,
writes ONE ``judgement`` row (judged or inconclusive) the first time the condition holds, and never rewrites it. Holm over H8a / H8b, pass needs |effect| >= 0.252 %.
Overlay (section 5): never judged; ``overlay_summary`` is appended once when 60 overlay days exist: fills, flags, and for FLAG fills the mean of the D+1-open counterfactual, the mean of each
actual exit rule, and their difference (counterfactual minus actual) on the fills that have that actual exit.
Not covered: appending a verdict to section 7 of the document (a person does that).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch8_core as CORE
from pulse.application import bf_batch8_forward as FW
from pulse.domain.batch8 import rules as R
from pulse.domain.batch8 import stats as ST


def resolved(rows: list[dict]) -> tuple[list[str], list[ST.NameRec], dict[str, dict]]:
    """(resolved sessions in date order, name records of those sessions, the day rows)."""
    days = sorted({r["session"] for r in rows if r["kind"] == "day_outcome"})
    recs = [CORE.rec_of(r["session"], r["code"], json.loads(r["body"])) for r in rows if r["kind"] == "outcome" and r["session"] in set(days)]
    day_rows = {r["session"]: json.loads(r["body"]) for r in rows if r["kind"] == "day"}
    return days, recs, day_rows


def blockers(day_rows: dict[str, dict], resolved_days: set[str], cutoff: str) -> list[str]:
    """Why a judged prefix ending at ``cutoff`` would skip a day: a candidate decision day up to the cutoff without a final ``day`` row (still waiting for bars or seats), or a decided day
    that is not resolved yet (a deferred sell). ``no_data`` / ``no_seats`` rows are final. Judging needs the list to be empty (``judge`` then exits 3 and writes nothing)."""
    last = date.fromisoformat(cutoff)
    out = []
    missing = [d.isoformat() for d in FW.candidate_days(R.FORWARD_START, last) if d.isoformat() not in day_rows]
    if missing:
        out.append(f"{len(missing)} decision day(s) without a day row up to {cutoff} (first {missing[0]})")
    pending = sorted(s for s, b in day_rows.items() if s <= cutoff and b.get("status") == "decided" and s not in resolved_days)
    if pending:
        out.append(f"{len(pending)} decided day(s) not resolved up to {cutoff} (first {pending[0]})")
    return out


def sample_stats(days: list[str], recs: list[ST.NameRec]) -> dict[str, Any]:
    keep = set(days)
    sub = [r for r in recs if r.day in keep]
    rep = ST.report(sub, days)
    return {"sessions": len(days), "report": rep, "effects": {h: (rep["whole"][h]["effect"], rep["whole"][h]["p_value"]) for h in R.HYPOTHESES}}


def overlay_summary(rows: list[dict]) -> dict[str, Any]:
    """Section 5 table from the overlay rows (descriptive, no pass rule)."""
    ov = [json.loads(r["body"]) for r in rows if r["kind"] == "overlay"]
    days = [json.loads(r["body"]) for r in rows if r["kind"] == "overlay_day"]
    out: dict[str, Any] = {"overlay_days": len(days), "days_flags_unknown": sum(1 for d in days if not d["flags_known"]), "scenarios": {}}
    for sc in sorted({b["scenario"] for b in ov}):
        g = [b for b in ov if b["scenario"] == sc]
        known = [b for b in g if b["flags_known"]]
        flagged = [b for b in known if b["flags"]["flag"]]
        cf = [b for b in flagged if b.get("counterfactual_d1_open", {}).get("ret_gross") is not None]
        entry: dict[str, Any] = {"fills": len(g), "flags_known": len(known), "L1": sum(1 for b in known if b["flags"]["l1"]), "L3": sum(1 for b in known if b["flags"]["l3"]),
                                 "T1": sum(1 for b in known if b["flags"]["t1"]), "FLAG": len(flagged), "with_counterfactual": len(cf)}
        if cf:
            entry["counterfactual_mean_ret_gross"] = sum(b["counterfactual_d1_open"]["ret_gross"] for b in cf) / len(cf)
            entry["counterfactual_mean_ret_net"] = sum(b["counterfactual_d1_open"]["ret_net"] for b in cf) / len(cf)
        rules: dict[str, dict] = {}
        by_rule: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for b in cf:
            for rule, a in (b.get("actual_exits") or {}).items():
                if a.get("ret_gross") is not None:
                    by_rule[rule].append((a["ret_gross"], b["counterfactual_d1_open"]["ret_gross"]))
        for rule, xs in sorted(by_rule.items()):
            rules[rule] = {"n": len(xs), "actual_mean_ret_gross": sum(a for a, _ in xs) / len(xs), "counterfactual_minus_actual": sum(c - a for a, c in xs) / len(xs)}
        entry["actual_exit_rules"] = rules
        out["scenarios"][sc] = entry
    return out


def overview(conn: psycopg.Connection, lake: LakeStore, today: date | None = None, rows: list[dict] | None = None) -> dict:
    rows = FW.stored_rows(conn, lake) if rows is None else rows
    today = today or datetime.now(timezone.utc).astimezone(FW.CST).date()
    ix = FW.index(rows)
    days, recs, day_rows = resolved(rows)
    cut = R.sample_cut(ST.flag_samples_per_day(recs, days))
    blocked = blockers(day_rows, set(days), days[cut.sessions - 1]) if cut.state != "refused" else []
    if blocked:
        cut = R.Cut("refused", None, cut.flag_n, f"blocked, not judged: {'; '.join(blocked)}")
    m = cut.sessions if cut.state != "refused" else len(days)
    use = days[:m]
    stats = sample_stats(use, recs) if use else {"sessions": 0, "report": None, "effects": {}}
    verdicts = R.judge(stats["effects"]) if cut.state == "judge" else None
    status = defaultdict(int)
    for b in day_rows.values():
        status[b.get("status")] += 1
    late_seats = sum(b.get("seat_reports_late_excluded") or 0 for b in day_rows.values())
    return {"spec_sha256": R.SPEC_SHA256, "forward_start": R.FORWARD_START.isoformat(), "today": today.isoformat(), "days": dict(status), "resolved_sessions": len(days),
            "seat_reports_late_excluded": late_seats, "cut": cut.__dict__, "sample": stats,
            "verdicts": None if verdicts is None else {h: v.__dict__ for h, v in verdicts.items()},
            "stored_judgement": ix["once"].get("judgement"), "overlay": overlay_summary(rows), "stored_overlay_summary": ix["once"].get("overlay_summary")}


def judge_and_store(svc, conn: psycopg.Connection, lake: LakeStore, today: date | None = None, as_of: datetime | None = None) -> dict:
    """Refuse before the sample condition; otherwise append the ``judgement`` row once. Also appends the section 5 overlay summary once, when 60 overlay days exist."""
    as_of = as_of or datetime.now(timezone.utc)
    rows = FW.stored_rows(conn, lake)
    ov = overview(conn, lake, today, rows)
    d = today or as_of.astimezone(FW.CST).date()
    new = []
    if ov["cut"]["state"] in ("judge", "inconclusive") and ov["stored_judgement"] is None:
        new.append(FW._row(d, "judgement", None, {"state": "judged" if ov["cut"]["state"] == "judge" else "inconclusive", "cut": ov["cut"], "sample_sessions": ov["sample"]["sessions"],
                                                  "effects": ov["sample"]["effects"], "verdicts": ov["verdicts"], "report": ov["sample"]["report"], "decided_at": as_of}))
    if ov["overlay"]["overlay_days"] >= R.OVERLAY_SUMMARY_SESSIONS and ov["stored_overlay_summary"] is None:
        new.append(FW._row(d, "overlay_summary", None, {**ov["overlay"], "decided_at": as_of}))
    if new:
        FW.publish(svc, new, as_of=as_of, stored_keys={FW.key_of(r) for r in rows})
    ov["rows_written"] = [r["kind"] for r in new]
    return ov


def _fmt(x: float | None, pct: bool = True) -> str:
    return "-" if x is None else (f"{x:+.4%}" if pct else f"{x:.4f}")


def describe(ov: dict) -> str:
    c, s = ov["cut"], ov["sample"]
    lines = [f"spec {ov['spec_sha256'][:12]}  forward from {ov['forward_start']}  days {ov['days']}  resolved sessions {ov['resolved_sessions']}/{R.MIN_SESSIONS}  "
             f"seat reports ignored for arriving after the next open: {ov['seat_reports_late_excluded']}",
             f"E099 {c['state']}: {c['reason']}"]
    if s["report"]:
        w = s["report"]["whole"]
        a, b = w["H8a"], w["H8b"]
        lines.append(f"  H8a: effect {_fmt(a['effect'])}, p {_fmt(a['p_value'], False)}, FLAG matched {a['n_arm']} in {a['n_eligible']} sessions; "
                     f"H8b: effect {_fmt(b['effect'])}, p {_fmt(b['p_value'], False)}, {b['n_eligible']} eligible sessions (flag {b['n_arm']} / other {b['n_rest']} names); sample {s['sessions']} sessions")
    if ov["verdicts"]:
        for h, v in ov["verdicts"].items():
            lines.append(f"  {h}: {'PASS' if v['passed'] else 'no pass'}: {v['reason']}")
    o = ov["overlay"]
    lines.append(f"overlay (section 5, no rule): {o['overlay_days']} days, " + ", ".join(f"{sc}: {e['fills']} fills, FLAG {e['FLAG']}" for sc, e in o["scenarios"].items()))
    return "\n".join(lines)
