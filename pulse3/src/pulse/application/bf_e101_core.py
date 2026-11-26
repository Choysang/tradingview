"""E101 shared steps of the forward recorder, the plan reports and the historical run: calendar events, the main-board market series, the two groups and their legs.

Why: ``bf_e101_forward`` (append-only rows), ``bf_e101_plan`` (reports) and the history run must use one definition each (pre-registration 2026-11-25 sections 1-3).
Invariants:
  * market (section 1): equal weight over main-board names in scope on the previous session that also traded on it; return = close over the exact reference price (``pct`` of
    the batch 7 exact panel, so an ex-rights day is not a fake loss), gap = open over the same reference, intraday = close / open - 1.
  * groups at decision day p (the session before D0 for H3, before R for H4), in-scope names of p only:
      leader = highest limit-up streak at the p close when that streak is at least 3 (ties all taken), else empty;
      core = float cap (batch 6 estimate) in the top 10 % AND 20-session return (product of the last 20 exact daily returns, all present) in the top 20 %, ties at a cut included.
  * legs: batch 6 fills from p (buy at the next open unless it opens at the limit-up price, sell at the close ``hold`` sessions later with the limit-down deferral);
    excess = leg return minus the E062 matched baseline of the day (pool = every in-scope name with a filled leg of that hold from p).
  * events: pulse3's calendar (``calendar.event_revision``, newest revision usable at ``as_of``, not cancelled, timed rows only), ``known_at`` = the first ``available_at`` of the
    event; or the E073 history file (``.work/lab/ext/calendar_hist.parquet``: naive Asia/Shanghai times, timed rows only, no timestamp of knowledge).
Not covered: rows and judging (``bf_e101_forward``), reports (``bf_e101_plan``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import psycopg

from pulse.application import bf_batch6_history as H6
from pulse.application import bf_batch7_forward as FW7
from pulse.application.bf_batch6_inputs import HistoryInputs
from pulse.domain.batch6 import evaluate as EV
from pulse.domain.batch6 import panel as PN
from pulse.domain.batch6 import rules as R6
from pulse.domain.e101 import rules as R
from pulse.domain.research import baseline as BL

CALENDAR_MARGIN_DAYS = 20


# ---- events --------------------------------------------------------------------------------------------------------------------------------
def open_sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if FW7.open_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


USABLE_STATUS = ("scheduled", "occurred", "corrected")


def calendar_rows(conn: psycopg.Connection, as_of: datetime) -> list[dict]:
    """US calendar rows usable at ``as_of``. The NEWEST visible revision decides (an older timed revision must not hide a postponement or a date-only
    reschedule): the event is kept only when that revision is timed and scheduled / occurred / corrected. ``known_at`` = the first visible revision that
    already carried the same release time."""
    rows = conn.execute(
        "SELECT DISTINCT ON (calendar_event_id) calendar_event_id, name, scheduled_at, status, time_precision,"
        " (SELECT min(x.available_at) FROM calendar.event_revision x WHERE x.calendar_event_id = e.calendar_event_id AND x.available_at <= %s"
        "  AND x.scheduled_at = e.scheduled_at) known_at"
        " FROM calendar.event_revision e WHERE region='US' AND available_at <= %s ORDER BY calendar_event_id, revision DESC", (as_of, as_of)).fetchall()
    return [{"name": r["name"], "release_at": r["scheduled_at"], "known_at": r["known_at"], "calendar_event_id": str(r["calendar_event_id"])}
            for r in rows if r["status"] in USABLE_STATUS and r["time_precision"] in ("minute", "approx_minute") and r["scheduled_at"] is not None]


def history_rows(path: Path) -> list[dict]:
    df = pl.read_parquet(path).filter(pl.col("timed"))
    return [{"name": n, "release_at": s.replace(tzinfo=R.CST), "known_at": None} for s, n in df.select("start", "name").iter_rows()]


def events_of(rows: list[dict], sessions: list[date] | None = None) -> list[R.Event]:
    """Classified events; ``sessions`` defaults to the open-day calendar around the rows."""
    if not rows:
        return []
    if sessions is None:
        lo = min(r["release_at"] for r in rows).date() - timedelta(days=CALENDAR_MARGIN_DAYS)
        hi = max(r["release_at"] for r in rows).date() + timedelta(days=CALENDAR_MARGIN_DAYS)
        sessions = open_sessions(lo, hi)
    return R.build_events(rows, sessions)


# ---- market series ---------------------------------------------------------------------------------------------------------------------
def with_helpers(panel: pl.DataFrame) -> pl.DataFrame:
    """Adds ``prev_ok`` (in scope and traded on the previous session), ``ref`` (exact reference price in cents) and ``r20`` (20-session exact return)."""
    p = panel.sort(["code", "si"])
    return p.with_columns(
        ((pl.col("si").shift(1).over("code") == pl.col("si") - 1) & pl.col("in_scope").shift(1).over("code").fill_null(False)).alias("prev_ok"),
        pl.when(pl.col("pct").is_not_null() & (pl.col("pct") > -1)).then(pl.col("c").cast(pl.Float64) / (1.0 + pl.col("pct"))).otherwise(None).alias("ref"),
        pl.when(pl.col("si") - pl.col("si").shift(R.CORE_RET_SESSIONS - 1).over("code") == R.CORE_RET_SESSIONS - 1)      # 20 consecutive sessions, no suspension gap
        .then(pl.col("pct").log1p().rolling_sum(window_size=R.CORE_RET_SESSIONS, min_samples=R.CORE_RET_SESSIONS).over("code").exp() - 1.0)
        .otherwise(None).alias("r20"))


def market_days(panel: pl.DataFrame) -> dict[date, dict[str, Any]]:
    """Per session: equal-weight return, gap and intraday move of the main board (section 1 market definition), and the number of names."""
    x = panel.filter(pl.col("prev_ok") & pl.col("pct").is_not_null() & pl.col("ref").is_not_null() & (pl.col("c") > 0))
    g = x.group_by("date").agg(pl.col("pct").mean().alias("r"), (pl.col("o").cast(pl.Float64) / pl.col("ref") - 1.0).mean().alias("gap"),
                               (pl.col("c").cast(pl.Float64) / pl.col("o").cast(pl.Float64) - 1.0).mean().alias("intra"), pl.len().alias("n"))
    return {d: {"r": r, "gap": gp, "intra": it, "n": int(n)} for d, r, gp, it, n in g.iter_rows()}


# ---- groups and legs -------------------------------------------------------------------------------------------------------------------
def groups_at(panel: pl.DataFrame, day: date) -> dict[str, list[str]]:
    p = panel.filter((pl.col("date") == day) & pl.col("in_scope"))
    leader: list[str] = []
    if p.height:
        top = p["streak"].max()
        if top is not None and top >= R.LEADER_MIN_STREAK:
            leader = sorted(p.filter(pl.col("streak") == top)["code"].to_list())
    caps = {c: float(v) for c, v in p.select("code", "cap").iter_rows() if v is not None}
    rets = {c: float(v) for c, v in p.select("code", "r20").iter_rows() if v is not None}
    core = sorted(EV.percent_rank_top(caps, R.CORE_CAP_SHARE) & EV.percent_rank_top(rets, R.CORE_RET_SHARE))
    return {"leader": leader, "core": core}


def previous_session(sessions: list[date], day: date) -> date | None:
    earlier = [d for d in sessions if d < day]
    return earlier[-1] if earlier else None


def _pool(inp: HistoryInputs, fwd: pl.DataFrame, day: date) -> tuple[dict, dict]:
    tables = H6.day_tables(inp.panel, fwd, {day})
    if day not in tables:
        return {}, {}
    pool = EV.build_pool(list(tables[day].iter_rows()))
    return BL.matched_means(pool), {p.key: p.ret for p in pool}


def leg_bodies(inp: HistoryInputs, fwd: pl.DataFrame, day: date, groups: dict[str, list[str]], hold: int) -> dict[str, Any] | None:
    """Per group and name: leg status, gross / net return, baseline and excess from decision day ``day``; None while a leg's exit is not observable yet."""
    n = len(inp.sessions)
    i = inp.sessions.index(day)
    codes = sorted({c for g in groups.values() for c in g})
    legs = FW7.leg_dict(fwd, day, codes)
    if any(legs.get(c, {}).get("status") in ("pending_entry", "pending_exit") for c in codes) and (n - 1) < i + hold + R6.MAX_DEFER_SESSIONS:
        return None
    means, qret = _pool(inp, fwd, day)
    out: dict[str, Any] = {"decision_day": day.isoformat(), "hold": hold, "pool": len(qret), "groups": {}}
    for g, members in groups.items():
        names = []
        for c in members:
            leg = legs.get(c)
            st = "no_bar" if leg is None else ("exit_unresolved" if leg["status"] == "pending_exit" else leg["status"])
            body: dict[str, Any] = {"code": c, "status": st}
            if st == "filled":
                body.update({"ret_gross": leg["ret"], "ret_net": leg["ret"] - float(R.COST)})
                if c in means and "sv" in means[c]:
                    body.update({"base_sv": float(means[c]["sv"]), "ex_sv": float(qret[c] - means[c]["sv"])})
                else:
                    body.update({"base_sv": None, "ex_sv": None, "note": "no matched baseline (cap or volatility missing)"})
            names.append(body)
        ex = [b["ex_sv"] for b in names if b.get("ex_sv") is not None]
        out["groups"][g] = {"names": names, "filled": sum(1 for b in names if b["status"] == "filled"), "matched": len(ex),
                            "mean_ex_sv": (sum(ex) / len(ex)) if ex else None}
    return out


def legs_for(inp: HistoryInputs, hold: int) -> pl.DataFrame:
    return PN.forward(inp.panel, hold, len(inp.sessions))


def group_records(event_key: str, body: dict) -> list[tuple[str, str, str, str, float]]:
    """(event key, decision day, group, code, excess) of the matched names of an outcome body."""
    out = []
    for g, gb in body["groups"].items():
        for b in gb["names"]:
            if b.get("ex_sv") is not None:
                out.append((event_key, body["decision_day"], g, b["code"], float(b["ex_sv"])))
    return out

