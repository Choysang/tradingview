"""E095 (auction) and E096 (intraday active buying) forward day step: features, signals, fills, returns, matched baseline, append-only rows.

Why: pre-registration sections 4 and 5. One call decides one session from the imported 60 s recordings (lake dataset ``quote_snapshots_60s``) and the daily bars up to the previous
session, and appends rows; a decided session is never rewritten (a repeated run adds nothing). The first evaluated session is ``FIRST_EVALUATED`` (2026-11-19); earlier recorded
sessions only feed the field check.
Rows (dataset ``batch6_forward``; columns experiment, hypothesis, session, kind, code, body, body_sha256):
  ``day``      one per (hypothesis, session): universe / pool / signal counts, fill statuses, recorder completeness, the day's matched excess (filled signals) and its count;
  ``signal``   one per signal: features, status (filled | blocked_buy | no_open | exit_deferred | no_ask), entry and exit price, gross and net return, matched baseline, excess;
  ``feature``  one per universe stock per session (E095 features, record only: includes ``matched_915``, the overnight-order approximation, and ``unmatched_buy``);
  ``resolution`` a later row that closes an ``exit_deferred`` signal (first sellable point of the following sessions, section 1).
Scope of a session: main board, not ST (name), listed >= 60 sessions, a price and an open on the day; the pool of the matched baseline is every in-scope name with a filled leg.
Snapshots: ``a915`` first poll 09:15:00-09:15:59; ``a919`` last auction poll before 09:20:00; ``a925`` last poll 09:25:00-09:29:59; ``a930`` first poll 09:30:00-09:32:59;
``a1000`` first poll 10:00:00-10:02:59; close = first ``close``-kind poll else first ``full`` poll 15:00:00-15:01:59. Day open = ``open`` of the first snapshot at or after 09:30
(the auction price), else the 09:25 price. Buy blocked when the open is at or above the limit-up price of the previous close. E095 sells at the close; a close at the limit-down price
cannot be sold: the signal is ``exit_deferred`` (no return yet) and is closed by a later ``resolution`` row. E096 buys at the 10:00 ask1 (needs ask1 lots > 0).
Not covered: queueing, the verdict (``bf_batch6_reads``), scheduling.
"""
from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from typing import Any, Mapping

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.application.bf_batch6_inputs import cents, load_scope
from pulse.application.bf_live_service import DATASET_QUOTES, LiveService
from pulse.contracts import to_wire
from pulse.domain.batch6 import auction as AU
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import rules as R
from pulse.domain.research import baseline as BL

DATASET = "batch6_forward"
CONTRACT = "pulse.analysis_artifact"
METHOD = "batch6-forward-v1"
FIRST_EVALUATED = date(2026, 11, 19)
CST = timezone(timedelta(hours=8))
UTC = timezone.utc
COLS = ("code", "name", "kind", "poll_at", "price", "prev_close", "open", "limit_up", "bid1_px", "bid1_lots", "bid2_lots", "bid3_lots", "bid4_lots", "bid5_lots",
        "ask1_px", "ask1_lots", "ask2_lots", "volume_lots", "amount_yuan", "outer_lots", "inner_lots", "float_cap")
SEC = lambda h, m, s=0: h * 3600 + m * 60 + s            # noqa: E731


def _f(v) -> float | None:
    return None if v is None else float(v)


def read_filtered(live: LiveService, man: Mapping) -> pl.DataFrame:
    """The members of one snapshot version: main board, ``COLS``, only the rows of the five windows the experiments use, with ``sec`` (CST seconds of the day)."""
    lake = live.lake
    m = lake.read_manifest(man["content_hash"])
    keep = ((pl.col("sec") < SEC(9, 31)) | ((pl.col("sec") >= SEC(9, 30)) & (pl.col("sec") < SEC(9, 33))) | ((pl.col("sec") >= SEC(10, 0)) & (pl.col("sec") < SEC(10, 3)))
            | (pl.col("sec") >= SEC(15, 0)) & (pl.col("sec") < SEC(15, 7)))
    out = []
    for x in m.members:
        if not x["path"].endswith(".parquet"):
            continue
        f = pl.read_parquet(io.BytesIO(lake.read_object(x)), columns=list(COLS))
        f = f.filter(pl.col("code").str.slice(0, 3).is_in(list(R.MAIN_PREFIXES)))
        f = f.with_columns(pl.col("poll_at").dt.convert_time_zone("Asia/Shanghai").dt.hour().cast(pl.Int64).mul(3600)
                           .add(pl.col("poll_at").dt.convert_time_zone("Asia/Shanghai").dt.minute().cast(pl.Int64) * 60)
                           .add(pl.col("poll_at").dt.convert_time_zone("Asia/Shanghai").dt.second().cast(pl.Int64)).alias("sec"))
        out.append(f.filter(keep))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def _pick(f: pl.DataFrame, cond: pl.Expr, last: bool) -> dict[str, dict]:
    g = f.filter(cond).sort(["code", "poll_at"], maintain_order=True)
    g = g.group_by("code", maintain_order=True).last() if last else g.group_by("code", maintain_order=True).first()
    return {r["code"]: r for r in g.iter_rows(named=True)}


@dataclass
class Snaps:
    a915: dict[str, dict]
    a919: dict[str, dict]
    a925: dict[str, dict]
    a930: dict[str, dict]
    a1000: dict[str, dict]
    close: dict[str, dict]


def snapshots(f: pl.DataFrame) -> Snaps:
    auction = pl.col("kind").is_in(["auction", "auction_hot", "auction_final"])
    close = _pick(f, pl.col("kind") == "close", False)
    fallback = _pick(f, (pl.col("kind") == "full") & (pl.col("sec") >= SEC(15, 0)) & (pl.col("sec") < SEC(15, 2)), False)
    return Snaps(_pick(f, auction & (pl.col("sec") >= SEC(9, 15)) & (pl.col("sec") < SEC(9, 16)), False),
                 _pick(f, auction & (pl.col("sec") >= SEC(9, 15)) & (pl.col("sec") < SEC(9, 20)), True),
                 _pick(f, (pl.col("sec") >= SEC(9, 25)) & (pl.col("sec") < SEC(9, 30)), True),
                 _pick(f, (pl.col("sec") >= SEC(9, 30)) & (pl.col("sec") < SEC(9, 33)), False),
                 _pick(f, (pl.col("sec") >= SEC(10, 0)) & (pl.col("sec") < SEC(10, 3)), False), {**fallback, **close})


@dataclass(frozen=True)
class History:
    avg_amount_20: Mapping[str, float]
    vol20: Mapping[str, float]
    sessions: list[date]
    listed_ok: Mapping[str, date]
    st: frozenset[str]


def load_history(conn: psycopg.Connection, day: date, as_of: datetime) -> History:
    code_of, iid_of, scope0, listed_on = load_scope(conn, as_of)
    series = {}
    ids = list(code_of)
    for i in range(0, len(ids), 400):
        series.update(MT.load_bars(conn, ids[i:i + 400], as_of, B.AVAILABILITY_POLICY, since=day - timedelta(days=60), until=day - timedelta(days=1)))
    counts: dict[date, int] = {}
    for bars in series.values():
        for b in bars:
            counts[b.d] = counts.get(b.d, 0) + 1
    from pulse.application.bf_batch6_inputs import observed_full_sessions
    sessions = observed_full_sessions(counts)
    full = set(sessions)
    avg, vol = {}, {}
    for iid, bars in series.items():
        code = code_of[iid]
        bs = [b for b in bars if b.d in full and b.c is not None and b.c > 0]
        last20 = bs[-20:]
        if len(last20) >= 15 and all(b.v is not None for b in last20):
            avg[code] = sum(float(b.c) * b.v for b in last20) / len(last20)
        closes = [float(b.c) for b in bs[-21:]]
        if len(closes) >= 11:
            r = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            m = sum(r) / len(r)
            vol[code] = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    # calendar for the listing rule: observed sessions before D plus D itself
    all_sessions = sorted(full | {day})
    ok = {c: v for c, lo in listed_on.items() if (v := R.listed_ok_from(lo, all_sessions)) is not None}
    return History(avg, vol, sessions, ok, scope0.st)


def _px(r: dict | None, key: str) -> float | None:
    if r is None:
        return None
    v = _f(r.get(key))
    return v if v is not None and v > 0 else None


def day_open(s: Snaps, code: str) -> float | None:
    o = _px(s.a930.get(code), "open")
    return o if o is not None else (_px(s.a925.get(code), "open") or _px(s.a925.get(code), "price"))


def day_features(day: date, s: Snaps, hist: History, unmatched_ok: bool) -> tuple[dict[str, AU.AuctionFeatures], dict[str, dict]]:
    feats: dict[str, AU.AuctionFeatures] = {}
    base: dict[str, dict] = {}
    for code, r25 in s.a925.items():
        prev = _px(r25, "prev_close")
        name = r25.get("name")
        if prev is None or R.is_st(name, code in hist.st):
            continue
        if code not in hist.listed_ok or hist.listed_ok[code] > day:
            continue
        close = s.close.get(code)
        o = day_open(s, code)
        if o is None or close is None or _px(close, "price") is None:
            continue
        bid5 = sum(_f(r25.get(f"bid{i}_lots")) or 0.0 for i in range(1, 6))
        r19, r15 = s.a919.get(code), s.a915.get(code)
        feats[code] = AU.features(prev_close=prev, price_919=_px(r19, "price"), price_925=_px(r25, "price"), matched_volume_925=_f(r25.get("volume_lots")),
                                  amount_925=_f(r25.get("amount_yuan")), avg_amount_20=hist.avg_amount_20.get(code), bid_lots_5_925=bid5,
                                  matched_915=_f(r15.get("bid1_lots")) if r15 else None, unmatched_ok=unmatched_ok)
        base[code] = {"prev": prev, "open": o, "close": _px(close, "price"), "name": name, "float_cap": _f(r25.get("float_cap")), "vol20": hist.vol20.get(code)}
    return feats, base


def legs_open_close(base: Mapping[str, dict]) -> dict[str, dict]:
    """E095 legs: buy at the day open, sell at the close (status per name)."""
    out = {}
    for c, b in base.items():
        up, down = R.limit_cents(cents_of(b["prev"]))
        o, cl = cents_of(b["open"]), cents_of(b["close"])
        if not R.buy_filled(o, up):
            out[c] = {"status": "blocked_buy", "entry": b["open"], "exit": None, "ret": None}
        elif not R.sell_possible(cl, down):
            out[c] = {"status": "exit_deferred", "entry": b["open"], "exit": None, "ret": None}
        else:
            out[c] = {"status": "filled", "entry": b["open"], "exit": b["close"], "ret": b["close"] / b["open"] - 1.0}
    return out


def cents_of(x: float) -> int:
    return int(round(x * 100))


def legs_ask_close(base: Mapping[str, dict], s: Snaps) -> dict[str, dict]:
    """E096 legs: buy at the 10:00 snapshot ask1, sell at the close."""
    out = {}
    for c, b in base.items():
        r = s.a1000.get(c)
        ask, lots = _px(r, "ask1_px"), _f(r.get("ask1_lots")) if r else None
        if ask is None or not lots or lots <= 0:
            out[c] = {"status": "no_ask", "entry": None, "exit": None, "ret": None}
            continue
        up, down = R.limit_cents(cents_of(b["prev"]))
        if not R.buy_filled(cents_of(ask), up):
            out[c] = {"status": "blocked_buy", "entry": ask, "exit": None, "ret": None}
        elif not R.sell_possible(cents_of(b["close"]), down):
            out[c] = {"status": "exit_deferred", "entry": ask, "exit": None, "ret": None}
        else:
            out[c] = {"status": "filled", "entry": ask, "exit": b["close"], "ret": b["close"] / ask - 1.0}
    return out


def baseline_rows(legs: Mapping[str, dict], base: Mapping[str, dict], signals: list[str]) -> tuple[dict[str, dict], dict]:
    """Matched baseline of one day: pool = filled names; returns per filled signal {ret, base_sv, ex_sv, ...} and the day summary."""
    pool_rows = [(c, legs[c]["ret"], base[c]["float_cap"], base[c]["vol20"]) for c in legs if legs[c]["status"] == "filled"]
    pool = EV.build_pool(pool_rows)
    means = BL.matched_means(pool)
    q = {p.key: p for p in pool}
    per: dict[str, dict] = {}
    for c in signals:
        if legs.get(c, {}).get("status") != "filled" or c not in means or "sv" not in means[c]:
            continue
        m = means[c]
        ret = q[c].ret
        per[c] = {"size5": q[c].size5, "vol5": q[c].vol5, "base_all": float(m["all"]), "base_size": float(m["size"]), "base_sv": float(m["sv"]), "base_svi": float(m["svi"]),
                  "ex_sv": float(ret - m["sv"]), "ex_all": float(ret - m["all"])}
    summary = {"n_pool": len(pool_rows), "k": len(per), "ex_sv_day": (sum(v["ex_sv"] for v in per.values()) / len(per)) if per else None}
    return per, summary


def _row(exp: str, hyp: str, session: date, kind: str, code: str | None, body: Mapping) -> dict:
    text = json.dumps(to_wire(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"experiment": exp, "hypothesis": hyp, "session": session.isoformat(), "kind": kind, "code": code or "", "body": text,
            "body_sha256": sha256(text.encode("utf-8")).hexdigest()}


def decide_day(day: date, s: Snaps, hist: History, *, unmatched_ok: bool, complete: bool) -> list[dict]:
    feats, base = day_features(day, s, hist, unmatched_ok)
    rows: list[dict] = []
    for c, f in feats.items():
        rows.append(_row("E095", "-", day, "feature", c, {"gap_919": f.gap_919, "gap_925": f.gap_925, "fade": f.fade, "amt_925_ratio": f.amt_925_ratio, "unmatched_buy": f.unmatched_buy,
                                                          "matched_915": f.matched_915}))
    legs95 = legs_open_close(base)
    chg10 = {c: AU.gap(_px(s.a1000.get(c), "price"), b["prev"]) for c, b in base.items()}
    abr = {c: AU.abr(_f(s.a930.get(c, {}).get("outer_lots")), _f(s.a930.get(c, {}).get("inner_lots")), _f(s.a1000.get(c, {}).get("outer_lots")),
                      _f(s.a1000.get(c, {}).get("inner_lots"))) if c in s.a930 and c in s.a1000 else None for c in base}
    ask_lots = {c: _f(s.a1000.get(c, {}).get("ask1_lots")) for c in base}
    legs96 = legs_ask_close(base, s)
    plan = (("E095", "H5", AU.h5_signals(feats), legs95), ("E095", "H6", AU.h6_signals(feats), legs95), ("E096", "H7", AU.h7_signals(chg10, abr, ask_lots), legs96))
    for exp, hyp, signals, legs in plan:
        per, summ = baseline_rows(legs, base, signals)
        counts: dict[str, int] = {}
        for c in signals:
            st = legs[c]["status"]
            counts[st] = counts.get(st, 0) + 1
            row = {"features": {k: v for k, v in (feats[c].__dict__ if c in feats else {}).items()}, "abr_30": abr.get(c), "change_10": chg10.get(c), "status": st,
                   "entry": legs[c]["entry"], "exit": legs[c]["exit"], "ret_gross": legs[c]["ret"],
                   "ret_net": None if legs[c]["ret"] is None else legs[c]["ret"] - float(R.COST), **per.get(c, {})}
            rows.append(_row(exp, hyp, day, "signal", c, row))
        rows.append(_row(exp, hyp, day, "day", None, {"n_universe": len(base), "n_pool": summ["n_pool"], "n_signals": len(signals), "n_filled": counts.get("filled", 0),
                                                      "statuses": counts, "k": summ["k"], "ex_sv_day": summ["ex_sv_day"], "recording_complete": complete,
                                                      "unmatched_buy_computed": unmatched_ok, "method": METHOD}))
    return rows


def deferred_resolutions(conn: psycopg.Connection, rows_before: list[dict], through: date, as_of: datetime) -> list[dict]:
    """Resolution rows for ``exit_deferred`` signals whose first sellable point after the signal session is now observable (idempotent: resolved ones are skipped)."""
    resolved = {(r["experiment"], r["hypothesis"], r["session"], r["code"]) for r in rows_before if r["kind"] == "resolution"}
    todo = []
    for r in rows_before:
        if r["kind"] != "signal" or (r["experiment"], r["hypothesis"], r["session"], r["code"]) in resolved:
            continue
        body = json.loads(r["body"])
        if body.get("status") == "exit_deferred":
            todo.append((r, body))
    if not todo:
        return []
    code_of, iid_of, _, _ = load_scope(conn, as_of)
    out = []
    for r, body in todo:
        d0 = date.fromisoformat(r["session"])
        iid = iid_of.get(r["code"])
        if iid is None:
            continue
        d_bar = {b.d: b for b in MT.load_bars(conn, [iid], as_of, B.AVAILABILITY_POLICY, since=d0 - timedelta(days=10), until=through).get(iid, [])}
        allb = sorted(d_bar.values(), key=lambda b: b.d)
        found = None
        for i, b in enumerate(allb):
            if b.d <= d0:
                continue
            prev = allb[i - 1]
            down = R.limit_cents(cents(prev.c))[1]
            for kind, px in (("open", b.o), ("close", b.c)):
                if R.sell_possible(cents(px), down):
                    found = (b.d, kind, float(px))
                    break
            if found:
                break
        if found is None:
            continue
        entry = body["entry"]
        ret = found[2] / entry - 1.0
        out.append(_row(r["experiment"], r["hypothesis"], d0, "resolution", r["code"], {"exit_day": found[0], "exit_point": found[1], "exit": found[2], "ret_gross": ret,
                                                                                         "ret_net": ret - float(R.COST)}))
    return out


def publish(svc, rows: list[dict], *, days: list[date], as_of: datetime) -> dict:
    frame = pl.DataFrame(rows).sort(["session", "experiment", "hypothesis", "kind", "code"])
    cov = {"dataset": DATASET, "method": METHOD, "spec_sha256": R.SPEC_SHA256, "sessions": sorted(d.isoformat() for d in days), "rows": frame.height,
           "evaluated_at": as_of.isoformat(), "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest()}
    return B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)


def stored_manifests(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (DATASET,)).fetchall()


def decided_sessions(conn: psycopg.Connection) -> set[str]:
    out: set[str] = set()
    for m in stored_manifests(conn):
        out |= set((m.get("coverage_document") or {}).get("sessions") or ())
    return out


def stored_rows(conn: psycopg.Connection, lake: LakeStore) -> list[dict]:
    rows: list[dict] = []
    for m in stored_manifests(conn):
        df = lake.read_snapshot_frames(m["content_hash"])
        rows += df.to_dicts()
    return rows


@dataclass
class DayReport:
    session: str
    decided: bool
    reason: str | None = None
    rows: int = 0
    hypotheses: dict = field(default_factory=dict)


def run_day(svc, live: LiveService, day: date, *, as_of: datetime, unmatched_ok: bool, force_ready: bool = False, hist_loader=None) -> tuple[DayReport, list[dict]]:
    """Decide one session (returns its rows; the caller publishes). ``force_ready`` skips the recording-complete gate (tests only)."""
    if day < FIRST_EVALUATED:
        return DayReport(day.isoformat(), False, "before the first evaluated session (field check only)"), []
    man = live.latest_ready(DATASET_QUOTES, day, as_of)
    if man is None:
        return DayReport(day.isoformat(), False, "no imported recording for the session"), []
    complete = bool(man["coverage_document"].get("source_complete"))
    if not complete and not force_ready:
        return DayReport(day.isoformat(), False, "recording not complete yet"), []
    f = read_filtered(live, man)
    s = snapshots(f)
    hist = (hist_loader or load_history)(svc.conn, day, as_of)
    rows = decide_day(day, s, hist, unmatched_ok=unmatched_ok, complete=complete)
    rep = DayReport(day.isoformat(), True, None, len(rows))
    for r in rows:
        if r["kind"] == "day":
            rep.hypotheses[r["hypothesis"]] = json.loads(r["body"])
    return rep, rows
