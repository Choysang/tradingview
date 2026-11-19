"""Batch 6 read model: stored historical result (E093 / E094), forward progress and descriptive / judged statistics (E095 / E096).

Why: the web page, the API and ``batch6 summary`` show the same numbers; all statistics are computed on the server from stored rows.
Invariants (pre-registration section 4): a forward experiment is judged only with 40 signal days (days with at least one signal in any of its hypotheses) and at least 25 signal days for each
hypothesis; at 20 signal days only descriptive figures are shown (``state = descriptive``); before that ``collecting``. Judged figures: matched excess (filled signals, name weighted),
one-sided block bootstrap p (block = 1 session, all evaluated sessions are slots), Holm over the family of seven (the four historical PRE p-values count when stored), pass = sign correct,
|excess| above the 0.252 % round-trip cost, adjusted p < 0.05. Deferred exits are not part of the excess (they have no same-day baseline); their later resolution is counted separately.
Not covered: the daily step itself (``bf_batch6_forward``), the history computation (``bf_batch6_history``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from typing import Any

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bf_batch6_fieldcheck as FC
from pulse.application import bf_batch6_forward as FW
from pulse.application import bf_batch6_history as HI
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import rules as R
from pulse.domain.research import bootstrap as BS

DESCRIPTIVE_AT = 20
JUDGE_AT = 40
MIN_PER_HYPOTHESIS = 25
FORWARD = (("E095", "H5"), ("E095", "H6"), ("E096", "H7"))


def history_latest(conn: psycopg.Connection, lake: LakeStore) -> dict | None:
    man = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version DESC LIMIT 1", (HI.DATASET,)).fetchone()
    if man is None:
        return None
    df = lake.read_snapshot_frames(man["content_hash"])
    out: dict[str, Any] = {"results": {}, "verdicts": {}, "meta": None, "events": []}
    for r in df.iter_rows(named=True):
        body = json.loads(r["body"])
        if r["kind"] == "result":
            out["results"][r["key"]] = body
        elif r["kind"] == "verdict":
            out["verdicts"][r["key"]] = body
        elif r["kind"] == "meta":
            out["meta"] = body
        else:
            out["events"].append(body)
    out["manifest_version"] = man["manifest_version"]
    return out


def forward_stats(rows: list[dict], hist_pre_p: dict[str, float | None]) -> dict[str, dict]:
    by: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[(r["experiment"], r["hypothesis"])][r["kind"]].append((r["session"], r["code"], json.loads(r["body"])))
    sessions_by_exp: dict[str, set[str]] = defaultdict(set)
    sig_days_exp: dict[str, set[str]] = defaultdict(set)
    out: dict[str, dict] = {}
    for exp, hyp in FORWARD:
        d = by.get((exp, hyp), {})
        days = {s: b for s, _, b in d.get("day", [])}
        sessions_by_exp[exp] |= set(days)
        sig_days = {s for s, b in days.items() if (b.get("n_signals") or 0) > 0}
        sig_days_exp[exp] |= {s for s, b in days.items() if (b.get("n_signals") or 0) > 0}
        out[hyp] = {"experiment": exp, "days": days, "sig_days": sig_days, "signals": d.get("signal", []), "resolutions": d.get("resolution", [])}
    res: dict[str, dict] = {}
    pvals: dict[str, float | None] = {**hist_pre_p}
    prelim: dict[str, dict] = {}
    for exp, hyp in FORWARD:
        o = out[hyp]
        sign, hold = R.HYPOTHESES[hyp][1], R.HYPOTHESES[hyp][2]
        days = o["days"]
        sig = [b for _, _, b in o["signals"]]
        n_sig = len(sig)
        n_filled = sum(1 for b in sig if b.get("status") == "filled")
        late = sum(1 for _, _, b in o["resolutions"])
        ex_days = sorted(s for s, b in days.items() if (b.get("k") or 0) > 0)
        k = [days[s]["k"] for s in ex_days]
        e = [days[s]["ex_sv_day"] for s in ex_days]
        n_matched = sum(k)
        ex = sum(a * b for a, b in zip(e, k)) / n_matched if n_matched else None
        rets = [b["ret_net"] for b in sig if b.get("status") == "filled" and b.get("ret_net") is not None]
        p = None
        blocks = 0
        reliable = False
        if len(ex_days) >= 1:
            bs = BS.block_bootstrap_p(sorted(days), ex_days, [sign * x for x in e], k, block=hold, seed=R.SEED, n_boot=R.N_BOOT)
            p, blocks, reliable = bs.p_value, bs.blocks, bs.reliable
        prelim[hyp] = {"experiment": exp, "hypothesis": hyp, "sign": sign, "days_evaluated": len(days), "signal_days": len(o["sig_days"]),
                       "signals": n_sig, "filled": n_filled, "exit_deferred": sum(1 for b in sig if b.get("status") == "exit_deferred"), "resolved_later": late,
                       "fill_rate": (n_filled / n_sig) if n_sig else None, "mean_ret_net": (sum(rets) / len(rets)) if rets else None, "excess": ex,
                       "n_matched": n_matched, "p_value": p, "blocks": blocks, "reliable": reliable}
    for exp in {x[0] for x in FORWARD}:
        any_days = len(sig_days_exp[exp])
        hyps = [h for e_, h in FORWARD if e_ == exp]
        ok_each = all(prelim[h]["signal_days"] >= MIN_PER_HYPOTHESIS for h in hyps)
        state = "judged" if any_days >= JUDGE_AT and ok_each else "descriptive" if any_days >= DESCRIPTIVE_AT else "collecting"
        for h in hyps:
            prelim[h].update({"experiment_signal_days": any_days, "state": state})
            if state == "judged":
                pvals[h] = prelim[h]["p_value"]
    adj = EV.holm({**{h: None for h in R.HYPOTHESES}, **pvals})
    for h, st in prelim.items():
        st["holm_p_adj"] = adj.get(h) if st["state"] == "judged" else None
        if st["state"] == "judged":
            ex_ = st["excess"]
            sign_ok = ex_ is not None and ex_ * st["sign"] > 0
            size_ok = ex_ is not None and abs(ex_) > float(R.COST)
            sig_ok = st["holm_p_adj"] is not None and st["holm_p_adj"] < R.ALPHA
            st["passed"] = bool(sign_ok and size_ok and sig_ok and st["reliable"])
            st["reason"] = f"sign {'ok' if sign_ok else 'wrong'}; size {'ok' if size_ok else 'below 0.252%'}; Holm p {st['holm_p_adj']}; {'reliable' if st['reliable'] else 'fewer than 10 blocks'}"
        else:
            st["passed"] = None
            st["reason"] = None if st["state"] == "descriptive" else "collecting: fewer than 20 signal days"
            if st["state"] == "collecting":
                st["p_value"] = None                       # descriptive: p is shown for orientation only (a progress value); collecting: nothing to show
    return prelim


def overview(conn: psycopg.Connection, lake: LakeStore) -> dict:
    hist = history_latest(conn, lake)
    pre_p = {}
    if hist:
        for key, body in hist["results"].items():
            h, seg = key.split(":")
            if seg == "PRE":
                pre_p[h] = body.get("p_value")
    fwd_rows = FW.stored_rows(conn, lake)
    fwd = forward_stats(fwd_rows, pre_p)
    return {"spec_sha256": R.SPEC_SHA256, "history": hist, "forward": fwd, "fieldcheck": FC.stored(conn, lake), "first_evaluated": FW.FIRST_EVALUATED.isoformat(),
            "sessions_decided": sorted(FW.decided_sessions(conn))}


def describe_summary(ov: dict) -> str:
    lines = [f"spec {ov['spec_sha256'][:12]}  first evaluated {ov['first_evaluated']}  decided sessions {len(ov['sessions_decided'])}"]
    for h, st in ov["forward"].items():
        lines.append(f"{st['experiment']} {h}: days {st['days_evaluated']}, signal days {st['signal_days']}/{JUDGE_AT} (experiment total {st['experiment_signal_days']}), "
                     f"signals {st['signals']}, filled {st['filled']}, state {st['state']}"
                     + (f", excess {st['excess']:+.4%} net ret {st['mean_ret_net']:+.4%}" if st["state"] != "collecting" and st["excess"] is not None and st["mean_ret_net"] is not None else ""))
    return "\n".join(lines)


# ---- wire form (contract ``backfill_batch6.schema.json``) ----------------------------------------------------------------------------
def _d(x) -> str | None:
    from decimal import Decimal

    from pulse.contracts import decimal_str
    if x is None:
        return None
    return decimal_str(Decimal(repr(float(x))).quantize(Decimal("0.00000001")))


def wire(ov: dict) -> dict:
    """The ``Overview`` payload: every ratio a decimal string, missing = null."""
    h = ov["history"]
    history = None
    if h is not None:
        rows = []
        for key, b in sorted(h["results"].items()):
            hyp, seg = key.split(":")
            rows.append({"hypothesis": hyp, "experiment": R.HYPOTHESES[hyp][0], "segment": seg, "sign": b["sign"], "hold": b["hold"], "signals": b["n_signals"],
                         "attempted": b["n_attempted"], "filled": b["n_filled"], "fill_rate": _d(b["fill_rate"]), "matched": b["n_matched"], "signal_days": b["signal_days"],
                         "mean_ret_net": _d(b["mean_ret_net"]), "mean_baseline": _d(b["mean_baseline"]), "excess": _d(b["excess"]), "p_value": _d(b["p_value"]),
                         "holm_p_adj": _d(b.get("holm_p_adj")), "blocks": b["blocks"], "reliable": bool(b["reliable"]), "note": b["note"]})
        meta = h["meta"] or {}
        history = {"as_of": meta.get("as_of"), "decision_days": meta.get("decision_days"), "rows": rows,
                   "verdicts": [{"hypothesis": k, "passed": bool(v["passed"]), "reason": v["reason"]} for k, v in sorted(h["verdicts"].items())],
                   "event_counts": [{"key": k, "count": v} for k, v in sorted((meta.get("event_counts") or {}).items())], "notes": list(meta.get("notes") or [])}
    fwd = []
    for hyp, s in sorted(ov["forward"].items()):
        fwd.append({"experiment": s["experiment"], "hypothesis": hyp, "sign": s["sign"], "days_evaluated": s["days_evaluated"], "signal_days": s["signal_days"],
                    "experiment_signal_days": s["experiment_signal_days"], "target_days": JUDGE_AT, "signals": s["signals"], "filled": s["filled"],
                    "exit_deferred": s["exit_deferred"], "resolved_later": s["resolved_later"], "fill_rate": _d(s["fill_rate"]), "mean_ret_net": _d(s["mean_ret_net"]),
                    "excess": _d(s["excess"]), "p_value": _d(s["p_value"]), "holm_p_adj": _d(s["holm_p_adj"]), "state": s["state"], "passed": s["passed"], "reason": s["reason"]})
    fc = ov["fieldcheck"]
    field = None
    if fc is not None:
        o = fc["overall"]
        field = {"sessions": list(fc["sessions"]), "unmatched_buy_computable": bool(o["unmatched_buy_computable"]), "bid1_ask1_lots_0915_0924": o["bid1_ask1_lots_0915_0924"],
                 "bid2_ask2_lots_0915_0924": o["bid2_ask2_lots_0915_0924"], "bid_ask_book_at_0925": o["bid_ask_book_at_0925"], "unmatched_buy_definition": o["unmatched_buy_definition"],
                 "auction_rows": o["auction_rows"], "final_rows": o["final_rows"],
                 "shares": [{"name": k, "value": _d(o[k])} for k in ("share_bid1_lots_eq_ask1_lots", "share_bid1_ask1_px_eq_price", "share_bid2_and_ask2_both_positive",
                                                                      "share_final_amount_eq_lots_x_price", "share_final_book_uncrossed")]}
    return {"spec_sha256": ov["spec_sha256"], "first_evaluated": ov["first_evaluated"], "history": history, "forward": fwd, "fieldcheck": field,
            "sessions_decided": ov["sessions_decided"]}
