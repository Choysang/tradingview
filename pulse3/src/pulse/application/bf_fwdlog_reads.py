"""Reads of the fwdlog lane: experiment cards with progress toward the decision day, the stored daily rows, the paper fills and the old scripts' summaries.

Why: the page lists the forward experiments with HOW FAR each is from its pre-registered decision (never a verdict before it) and shows the daily rows.
Invariants: every number is computed from stored, append-only rows (point in time: ``decided_at``); a metric before the decision day is PROVISIONAL and says
"pending until N ..." (``pending_reason``); a missing value is ``None``, never 0; the card says what is registered in the research registry.
Not covered: computing rows (``bf_fwdlog_service``), HTTP (``routes_bf_fwdlog``).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from pulse.application import bf_fwdlog_registry as REG
from pulse.application.bf_fwdlog_service import DS, FwdlogService
from pulse.domain.fwdlog import paper as PP
from pulse.domain.fwdlog import state as ST
from pulse.domain.fwdlog import stats as SS
from pulse.domain.fwdlog.common import D, ZERO, median, q

E077_NUM = {"ret", "cell", "ex", "net"}
E078_NUM = {"x", "x2", "y"}
E089_NUM = {"ew_pct", "up_share", "etf_amount", "late_share", "vwap_gap_pct", "ratio", "hsi_pct", "gold_pct", "oil_pct", "a50_pct", "cnh_pct", "sh_pct", "basis_pct",
            "cffex_gap_min"}
PAPER_NUM = {"return_pct", "vs_close_pct", "price", "cost", "d0_return_pct", "avg_fill_price", "reference_price", "live_fail_rate", "d0_mean_pct", "d0_median_pct",
             "mean_return_pct", "mean_vs_close_pct", "median_vs_close_pct", "better_than_close_share", "failed_rate"}


def revive(obj: Any, keys: set[str]) -> Any:
    """Wire form back to Decimal for the named numeric keys (codes and dates stay strings)."""
    if isinstance(obj, dict):
        return {k: (D(v) if k in keys and isinstance(v, str) else revive(v, keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [revive(v, keys) for v in obj]
    return obj


def bodies(svc: FwdlogService, key: str) -> list[dict]:
    keys = {"E077": E077_NUM, "E078": E078_NUM, "E089": E089_NUM}[key]
    return [revive(r["body"], keys) for r in svc.stored(DS[key])]


# ---- the old scripts' summaries (CLI ``summary``) --------------------------------------------------------------------------------
def paper_pooled(days: list[dict], sells: list[dict], buys: list[dict]) -> dict:
    """Per-rule tables pooled over every decided session (D0, no cost, same columns as the old per-day summary)."""
    done = [s for s in sells if s.get("status") == "FILLED"]
    table = []
    for rule in dict.fromkeys(s["rule"] for s in done):
        g = [s for s in done if s["rule"] == rule]
        vs = [s["vs_close_pct"] for s in g]
        table.append({"rule": rule, "triggered": len(g), "mean_return_pct": q(sum((s["return_pct"] for s in g), ZERO) / D(len(g)), 4),
                      "mean_vs_close_pct": q(sum(vs, ZERO) / D(len(vs)), 4), "median_vs_close_pct": q(median(vs), 4),
                      "better_than_close_share": q(D(sum(1 for v in vs if v > ZERO)) / D(len(vs)), 4), "shadow": bool(g[0].get("shadow"))})
    buy_tab = []
    for rule, sc in sorted({(b["rule"], b["scenario"]) for b in buys}):
        g = [b for b in buys if b["rule"] == rule and b["scenario"] == sc]
        d0 = [b["d0_return_pct"] for b in g if b.get("d0_return_pct") is not None]
        buy_tab.append({"rule": rule, "scenario": sc, "triggered": len(g), "filled": sum(1 for b in g if b["status"] == "FILLED"),
                        "unfilled": sum(1 for b in g if b["status"] == "UNFILLED"), "d0_mean_pct": q(sum(d0, ZERO) / D(len(d0)), 4) if d0 else None,
                        "d0_median_pct": q(median(d0), 4) if d0 else None})
    return {"sessions": len(days), "check_at": PP.PAPER_CHECK_AT, "sell_rules": table, "shadow_buys": buy_tab,
            "pending": None if len(days) >= PP.PAPER_CHECK_AT else f"pending until {PP.PAPER_CHECK_AT} recorded sessions; the tables are descriptive"}


def summaries(svc: FwdlogService) -> dict:
    e77, e78, e89 = bodies(svc, "E077"), bodies(svc, "E078"), bodies(svc, "E089")
    days = [r["body"] for r in svc.stored(DS["PAPER_DAYS"])]
    sells = [revive(r["body"], PAPER_NUM) for r in svc.stored(DS["PAPER_SELLS"])]
    buys = [revive(r["body"], PAPER_NUM) for r in svc.stored(DS["PAPER_BUYS"])]
    return {"e077": SS.e077_summary(e77), "e078": SS.e078_summary(e78), "e089": ST.summary(e89), "paper": paper_pooled(days, sells, buys)}


# ---- cards ---------------------------------------------------------------------------------------------------------------------
def _metric(name: str, value: Any, *, provisional: bool = False, pending: str | None = None) -> dict:
    if isinstance(value, float):
        value = Decimal(repr(round(value, 4)))
    return {"name": name, "value": value, "provisional": provisional, "pending_reason": pending}


def _progress(done: int, target: int, unit: str) -> dict:
    return {"unit": unit, "done": done, "target": target, "remaining": max(0, target - done), "share": q(D(min(done, target)) / D(target), 4)}


def cards(svc: FwdlogService, *, pending: Mapping[str, list[dict]] | None = None) -> dict:
    s = summaries(svc)
    pending = pending or {}
    out = []
    for spec in REG.EXPERIMENTS:
        run = REG.registered(svc.conn, spec)
        dsets = [DS[k] for k in {"E077": ("E077",), "E078": ("E078",), "E089": ("E089",), "PAPER": ("PAPER_DAYS",)}[spec.key]]
        dates = sorted(svc.decided(dsets[0]))
        card = {"experiment_key": spec.key, "title": spec.title, "ledger_ids": list(spec.ledger_ids), "purpose": "shadow", "rules_version": spec.rules_version,
                "preregistration": [{"file": f, "sha256": sha} for f, sha in REG.prereg_shas(spec, svc.frozen).items()],
                "registry": {"registered": run is not None, "experiment_id": None if run is None else run["experiment_id"], "run_state": None if run is None else run["run_state"]},
                "first_session": date.fromisoformat(dates[0]) if dates else None, "last_session": date.fromisoformat(dates[-1]) if dates else None,
                "sessions_decided": len(dates), "pending": list(pending.get(spec.key, [])), "rule_table": None, "notes": []}
        m: list[dict] = []
        if spec.key == "E077":
            x = s["e077"]
            prog, decided, why = _progress(x["decision_days"], x["check_at"], "decision_days"), x["decided"], x["pending"]
            m = [_metric("decision_days", x["decision_days"]), _metric("signal_days", x["signal_days"]), _metric("mean_excess_pct", x["mean_ex"], provisional=not decided, pending=why),
                 _metric("p_boot", x["p_boot"], provisional=not decided, pending=why), _metric("mean_net_pct", x["mean_net"], provisional=not decided, pending=why),
                 _metric("h1_excess_positive_and_p_below_0_05", x["h1"], pending=why), _metric("h2_net_positive", x["h2"], pending=why)]
            verdict = x["verdict"]
        elif spec.key == "E078":
            x = s["e078"]
            prog, decided, why, verdict = _progress(x["days"], x["check_at"], "sessions"), x["decided"], x["pending"], None
            m = [_metric("sessions", x["days"]), _metric("index_days", x["index_days"]), _metric("index_slope", x["index_slope"], provisional=not decided, pending=why),
                 _metric("index_newey_west_t", x["index_nw_t"], provisional=not decided, pending=why), _metric("ew_slope", x["ew_slope"], provisional=not decided, pending=why),
                 _metric("h1_index_t_at_least_2_and_ew_same_sign", x["h1"], pending=why)]
            verdict = None if x["h1"] is None else ("works" if x["h1"] else "fails")
        elif spec.key == "E089":
            x = s["e089"]
            decided = x["panic_episodes"] >= x["h1_check_at"]
            why = None if decided else f"pending until {x['h1_check_at']} independent panic-support episodes (about {x['panic_episodes']} so far); the log changes no rule"
            prog, verdict = _progress(x["panic_episodes"], x["h1_check_at"], "independent_episodes"), None
            m = [_metric("sessions_logged", x["days"]), _metric("ext_missing", x["ext_missing"]), _metric("with_ratio", x["with_ratio"]), _metric("surge_days", x["surge_days"]),
                 _metric("late_heavy_days", x["late_heavy_days"]), _metric("late_share_median", x["late_share_median"]), _metric("panic_episodes", x["panic_episodes"], pending=why),
                 _metric("a50_cnh_missing_days", x["a50_cnh_missing_days"]), _metric("cffex_slow_days", x["cffex_slow_days"])]
            card["notes"] += list(x["todo"])
        else:
            x = s["paper"]
            decided = x["sessions"] >= x["check_at"]
            prog, why, verdict = _progress(x["sessions"], x["check_at"], "recorded_sessions"), x["pending"], None
            m = [_metric("sessions", x["sessions"], pending=why), _metric("sell_rules_compared", len(x["sell_rules"]))]
            card["rule_table"] = x["sell_rules"]
            card["notes"].append("B1 and B3 are shadow records (never booked); E059's box rule S_Box5m is a shadow sell row")
        card.update({"status": "not_started" if not dates else "decision_reached" if decided else "recording", "progress": prog,
                     "decision": {"check_at": spec.check_at, "check_unit": spec.check_unit, "decided": bool(decided), "verdict": verdict, "pending_reason": None if decided else why},
                     "metrics": m})
        out.append(card)
    return {"experiments": out, "notes": ["Every experiment is a shadow record: nothing is booked and no rule is changed by it.",
                                           "A metric marked provisional is shown for progress only; the pre-registered decision needs the day named in pending_reason."]}


def rows_page(svc: FwdlogService, key: str, *, start: date | None = None, end: date | None = None, limit: int = 100, offset: int = 0) -> dict:
    dataset = {"E077": DS["E077"], "E078": DS["E078"], "E089": DS["E089"], "PAPER": DS["PAPER_DAYS"]}[key]
    rows = svc.stored(dataset)
    if start:
        rows = [r for r in rows if r["session_date"] >= start.isoformat()]
    if end:
        rows = [r for r in rows if r["session_date"] <= end.isoformat()]
    page = rows[offset:offset + limit]
    return {"experiment_key": key, "dataset": dataset, "total": len(rows), "rows": [{"session_date": date.fromisoformat(r["session_date"]), "row_key": r["row_key"],
                                                                                    "decided_at": datetime.fromisoformat(r["decided_at"].replace("Z", "+00:00")), "values": r["body"]} for r in page]}


def paper_fills(svc: FwdlogService, day: date, kind: str, *, rule: str | None = None, code: str | None = None, limit: int = 500, offset: int = 0) -> dict:
    dataset = DS["PAPER_BUYS"] if kind == "buys" else DS["PAPER_SELLS"]
    rows = [r["body"] for r in svc.stored(dataset, session_dates=[day.isoformat()])]
    if rule:
        rows = [r for r in rows if r.get("rule") == rule]
    if code:
        rows = [r for r in rows if r.get("code") == code]
    return {"session_date": day, "kind": kind, "total": len(rows), "rows": rows[offset:offset + limit]}
