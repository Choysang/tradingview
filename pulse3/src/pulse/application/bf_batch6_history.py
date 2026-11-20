"""E093 (leader linkage) and E094 (seat expansion) historical runs: signals, fills, matched excess, block bootstrap, Holm.

Why: pre-registration sections 2 and 3, run once on the pulse3 daily bars (and the imported seat reports). The rules live in ``domain.batch6``; this module builds the
signals from the inputs, runs ``domain.batch6.panel.forward`` for the holding periods and ``domain.batch6.evaluate`` for each hypothesis in each segment.
Invariants: decision days are the observed sessions after a warm-up of ``WARMUP_SESSIONS`` (streaks start at the first observed session); a signal is (D, code) with D after the
close; a signal's status comes from the forward legs (filled / blocked_buy / no_bar / pending_exit / pending_entry); the baseline pool of a day is every in-scope name with a filled
leg that day; only the pre-registered hypotheses are evaluated; the result is published as one immutable derived dataset version (``batch6_history``).
Not covered: forward experiments (``bf_batch6_forward``), reads (``bf_batch6_reads``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Mapping

import polars as pl

from pulse.application import bindings as B
from pulse.application.bf_batch6_inputs import WARMUP_SESSIONS, HistoryInputs
from pulse.contracts import to_wire
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import roles as RL
from pulse.domain.batch6 import rules as R
from pulse.domain.research import baseline as BL

DATASET = "batch6_history"
CONTRACT = "pulse.analysis_artifact"
METHOD = "batch6-history-v1"
LHASA, INSTITUTION = "拉萨", "机构专用"
INSTITUTION_SHARE = 0.05


def members_by_day(panel: pl.DataFrame) -> dict[date, dict[str, list[RL.Member]]]:
    out: dict[date, dict[str, list[RL.Member]]] = defaultdict(lambda: defaultdict(list))
    sub = panel.filter(pl.col("in_scope") & pl.col("sw2").is_not_null()).select("date", "sw2", "code", "streak", "lu", "amt", "cap")
    for d, sw2, code, streak, lu, amt, cap in sub.iter_rows():
        out[d][sw2].append(RL.Member(code, int(streak), bool(lu), float(amt), None if cap is None else float(cap)))
    return out


def e093_signals(inp: HistoryInputs) -> tuple[dict[str, list[tuple[date, str]]], list[dict]]:
    """H1 (break) and H2 (promotion) signals and the event list."""
    mem = members_by_day(inp.panel)
    sess = inp.sessions
    leaders: dict[date, dict[str, RL.Member]] = {}
    for d in sess:
        leaders[d] = {sw: l for sw, ms in mem.get(d, {}).items() if (l := RL.leader_of(ms)) is not None}
    sig: dict[str, list[tuple[date, str]]] = {"H1": [], "H2": []}
    events: list[dict] = []
    for i in range(max(1, WARMUP_SESSIONS), len(sess)):
        d, prev = sess[i], sess[i - 1]
        for sw, pl_ in leaders[prev].items():
            today = mem.get(d, {}).get(sw, [])
            ev = RL.event_of(pl_, today)
            if ev is None:
                continue
            tg = RL.targets(ev, today)
            hyp = "H1" if ev.kind == "break" else "H2"
            events.append({"day": d.isoformat(), "industry": sw, "kind": ev.kind, "leader": ev.leader, "leader_streak_prev": ev.leader_streak_prev, "targets": len(tg)})
            sig[hyp] += [(d, c) for c in tg]
    for h in sig:
        sig[h] = sorted(set(sig[h]))
    return sig, events


def seat_tables(inp: HistoryInputs) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(lhasa count per stock-day, institution net buy per stock-day), single-day reports, de-duplicated rows."""
    s = inp.seats.filter(pl.col("amount").is_not_null())
    lh = (s.filter((pl.col("side") == "buy") & pl.col("seat").str.contains(LHASA)).unique(["code", "date", "seat"]).group_by("code", "date").agg(pl.len().alias("lhasa")))
    inst = s.filter(pl.col("seat") == INSTITUTION)
    net = inst.group_by("code", "date").agg((pl.when(pl.col("side") == "buy").then(pl.col("amount")).otherwise(0.0).sum()
                                             - pl.when(pl.col("side") == "sell").then(pl.col("amount")).otherwise(0.0).sum()).alias("net"))
    return lh, net


def e094_signals(inp: HistoryInputs) -> tuple[dict[str, list[tuple[date, str]]], list[dict]]:
    lh, net = seat_tables(inp)
    sessions = set(inp.sessions[WARMUP_SESSIONS:])
    panel = inp.panel
    day_ok = panel.filter(pl.col("in_scope")).select("code", "date", "sw2", "amt")
    sig: dict[str, list[tuple[date, str]]] = {"H3": [], "H4": []}
    info: list[dict] = []
    # H3: industries with a member showing >= 3 distinct Lhasa buy seats; signals = in-scope members of that industry that are not on the list that day
    flagged = lh.filter(pl.col("lhasa") >= 3).join(panel.select("code", "date", "sw2"), on=["code", "date"], how="inner").filter(pl.col("sw2").is_not_null())
    by_day_sw: dict[date, set[str]] = defaultdict(set)
    for code, d, n, sw2 in flagged.select("code", "date", "lhasa", "sw2").iter_rows():
        if d in sessions:
            by_day_sw[d].add(sw2)
    lhb_by_day: dict[date, set[str]] = defaultdict(set)
    for code, d in inp.lhb_days:
        lhb_by_day[d].add(code)
    members = day_ok.filter(pl.col("sw2").is_not_null()).group_by("date", "sw2").agg(pl.col("code")).iter_rows()
    mem_idx = {(d, sw2): codes for d, sw2, codes in members}
    for d, sws in by_day_sw.items():
        for sw2 in sws:
            codes = [c for c in mem_idx.get((d, sw2), []) if c not in lhb_by_day.get(d, set())]
            sig["H3"] += [(d, c) for c in codes]
            info.append({"day": d.isoformat(), "industry": sw2, "kind": "lhasa_industry", "targets": len(codes)})
    # H4: institution net buy > 0 and >= 5 % of the day's turnover (estimate)
    j = net.join(day_ok, on=["code", "date"], how="inner").filter((pl.col("net") > 0) & (pl.col("net") >= INSTITUTION_SHARE * pl.col("amt")))
    for code, d, n in j.select("code", "date", "net").iter_rows():
        if d in sessions:
            sig["H4"].append((d, code))
            info.append({"day": d.isoformat(), "kind": "institution", "code": code, "net": n})
    for h in sig:
        sig[h] = sorted(set(sig[h]))
    return sig, info


def day_tables(panel: pl.DataFrame, fwd: pl.DataFrame, days: set[date]) -> dict[date, pl.DataFrame]:
    """Per day, the in-scope names with a filled leg: code, ret, cap, vol20 (the day's matching pool)."""
    f = fwd.filter((pl.col("status") == "filled") & pl.col("date").is_in(list(days))).select("code", "date", "ret")
    j = f.join(panel.filter(pl.col("in_scope")).select("code", "date", "cap", "vol20"), on=["code", "date"], how="inner")
    return {d: g.select("code", "ret", "cap", "vol20") for (d,), g in j.partition_by("date", as_dict=True, maintain_order=False).items()}


def run_hypothesis(hyp: str, signals: list[tuple[date, str]], fwd: pl.DataFrame, inp: HistoryInputs, cache: dict | None = None,
                   needed: Mapping[tuple[int, str], set[str]] | None = None, seed: int | None = None,
                   last_day: date | None = None) -> dict[str, Any]:
    """One hypothesis in both segments. Memory: the matching pool of a day is built and its baseline means computed ONCE per (hold, day) (``cache``), then reduced to the names that are
    signals of some hypothesis with that hold (``needed``); the full pool of the day is dropped (a three-year panel would not fit the server's budget otherwise)."""
    exp, sign, hold = R.HYPOTHESES[hyp]
    sess = inp.sessions
    valid_days = [d for d in sess[WARMUP_SESSIONS:] if last_day is None or d <= last_day]
    sdf = pl.DataFrame({"date": [d for d, _ in signals], "code": [c for _, c in signals]}, schema={"date": pl.Date, "code": pl.String}).join(
        fwd.select("code", "date", "status", "ret"), on=["code", "date"], how="left").with_columns(pl.col("status").fill_null("not_in_panel"))
    out: dict[str, Any] = {"hypothesis": hyp, "experiment": exp, "sign": sign, "hold": hold, "segments": {}, "days": []}
    cache = cache if cache is not None else {}
    filled_days = set(sdf.filter(pl.col("status") == "filled")["date"].to_list())
    tables = day_tables(inp.panel, fwd, {d for d in filled_days if (hold, d.isoformat()) not in cache})
    mine: dict[str, set[str]] = defaultdict(set)
    for d, c in signals:
        mine[d.isoformat()].add(c)
    for d in sorted(filled_days):
        key = (hold, d.isoformat())
        if key in cache:
            continue
        g = tables[d]
        pool = EV.build_pool(list(g.iter_rows()))
        means = BL.matched_means(pool)
        keep = (needed or {}).get(key) or mine[d.isoformat()]
        cache[key] = ([p for p in pool if p.key in keep], {k: v for k, v in means.items() if k in keep})
        del tables[d]
    for seg in ("PRE", "DEV"):
        seg_days = [d for d in valid_days if R.segment_of(d) == seg]
        ss = sdf.filter(pl.col("date").is_in(seg_days))
        counts = {k: v for k, v in ss.group_by("status").len().iter_rows()}
        filled_by_day: dict[str, list[str]] = defaultdict(list)
        for d, c in ss.filter(pl.col("status") == "filled").select("date", "code").iter_rows():
            filled_by_day[d.isoformat()].append(c)
        pool_by_day = {k: cache[(hold, k)][0] for k in filled_by_day}
        means_by_day = {k: cache[(hold, k)][1] for k in filled_by_day}
        res = EV.evaluate_segment(hypothesis=hyp, segment=seg, sign=sign, hold=hold, all_days=[d.isoformat() for d in seg_days], signals_by_day=filled_by_day,
                                  pool_by_day=pool_by_day, status_counts=counts, n_signals=ss.height, means_by_day=means_by_day, seed=seed)
        out["segments"][seg] = res
    return out


def run(inp: HistoryInputs, *, hypotheses: tuple[str, ...] = ("H1", "H2", "H3", "H4")) -> dict[str, Any]:
    n = len(inp.sessions)
    f3 = PN.forward(inp.panel, 3, n)
    f5 = PN.forward(inp.panel, 5, n)
    s93, events = e093_signals(inp)
    s94, info = e094_signals(inp)
    sigs = {**s93, **s94}
    cache: dict = {}
    needed: dict[tuple[int, str], set[str]] = defaultdict(set)
    for h in hypotheses:
        for d, c in sigs[h]:
            needed[(R.HYPOTHESES[h][2], d.isoformat())].add(c)
    results = {h: run_hypothesis(h, sigs[h], f3 if R.HYPOTHESES[h][2] == 3 else f5, inp, cache, needed) for h in hypotheses}
    pre_p = {h: results[h]["segments"]["PRE"].p_value for h in hypotheses}
    adj = EV.holm({**{h: None for h in R.HYPOTHESES}, **pre_p})
    verdicts = {h: EV.judge(h, results[h]["segments"]["PRE"], results[h]["segments"]["DEV"], adj[h]) for h in hypotheses}
    ev_counts: dict[str, int] = defaultdict(int)
    for e in events:
        ev_counts[f"{e['kind']}:{R.segment_of(date.fromisoformat(e['day']))}"] += 1
    return {"results": results, "holm": adj, "verdicts": verdicts, "events": events, "event_counts": dict(ev_counts), "e094_info_count": len(info),
            "decision_days": {"first": inp.sessions[WARMUP_SESSIONS].isoformat(), "last": inp.sessions[-1].isoformat(), "sessions": n},
            "notes": inp.notes, "cap": inp.cap_note, "bars_source": inp.source, "lhb_stock_days": len(inp.lhb_days)}


def to_rows(out: Mapping[str, Any], *, run_id: str, as_of: datetime) -> pl.DataFrame:
    rows = []
    for h, r in out["results"].items():
        for seg, res in r["segments"].items():
            body = to_wire({**asdict(res), "holm_p_adj": out["holm"][h] if seg == "PRE" else None})
            rows.append({"run_id": run_id, "kind": "result", "key": f"{h}:{seg}", "body": json.dumps(body, sort_keys=True, ensure_ascii=False)})
        v = out["verdicts"][h]
        rows.append({"run_id": run_id, "kind": "verdict", "key": h, "body": json.dumps(to_wire(asdict(v)), sort_keys=True, ensure_ascii=False)})
    for e in out["events"]:
        rows.append({"run_id": run_id, "kind": "event", "key": f"{e['day']}:{e['industry']}", "body": json.dumps(e, sort_keys=True, ensure_ascii=False)})
    meta = {k: out[k] for k in ("event_counts", "decision_days", "notes", "cap", "lhb_stock_days", "bars_source")}
    rows.append({"run_id": run_id, "kind": "meta", "key": "run", "body": json.dumps(to_wire({**meta, "as_of": as_of, "spec_sha256": R.SPEC_SHA256, "method": METHOD}), sort_keys=True, ensure_ascii=False)})
    return pl.DataFrame(rows)


def publish(svc, out: Mapping[str, Any], *, as_of: datetime) -> dict:
    run_id = sha256(json.dumps(to_wire({"as_of": as_of, "m": METHOD, "src": out.get("bars_source")}), sort_keys=True).encode()).hexdigest()[:16]
    frame = to_rows(out, run_id=run_id, as_of=as_of).sort(["kind", "key"])
    cov = {"dataset": DATASET, "method": METHOD, "run_id": run_id, "spec_sha256": R.SPEC_SHA256, "as_of": as_of.isoformat(), "rows": frame.height,
           "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)
