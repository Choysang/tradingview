"""Forward monitor: rank IC and top-decile excess of every published market ranking once its horizon is observed (backfill #169).

Why: M5-E04 asks for evidence that the published rankings carry information about later returns. The ranking itself is
immutable; its forward statistics are a separate append-only derived dataset (``forward_monitor``) computed from the raw
daily bars once the horizon has been observed. Pending horizons have no row and no number.
Invariants:
* point in time: bars are read with ``available_at <= as_of`` and ``is_final`` (``market_today_service.load_bars``), so a
  statistic never uses a bar that did not exist yet; the horizon is mature only when the observed session calendar holds
  the exit session (``domain.research.forward_ic.matured_window``);
* the forward return of an instrument is the raw return of the ranking outcome method (entry = open of the session after the
  decision session, exit = close ``h`` sessions after it, with the same impossible-move guard) and is GROSS: nothing deducted;
* append-only: a resolved ranking is written once (a new READY snapshot holds only new rows); nothing is overwritten and a
  ranking that is already stored is never recomputed; a ranking whose exit-session coverage is below half of the ranked
  instruments stays pending (it is not frozen on a half-ingested day);
* only releases published in the requested slot (``decision.release_publication``) and in state READY/SUPERSEDED are used;
* cost stays bounded as releases accumulate: each snapshot lists its ranking ids in its coverage document, so finding what is
  resolved reads the database only (older snapshots without the list are read once from the lake), and a ranking still
  unresolved ``MAX_PENDING_DAYS`` after its decision is reported as ``expired`` and no longer pins the bar window back.
Not covered: net-of-cost statistics, benchmark-relative returns, sector rankings, per-regime splits.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Mapping, Sequence

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application import market_today_service as MT
from pulse.application.data_service import DataService
from pulse.contracts import to_wire
from pulse.domain.market_state import outcomes
from pulse.domain.research import forward_ic as F

DATASET = "forward_monitor"
CONTRACT = "pulse.analysis_artifact"            # the registered derived-artifact contract (no new contract_version rows needed)
MIN_COVERAGE_SHARE = Decimal("0.5")
MAX_PENDING_DAYS = 150                          # calendar days; the longest horizon (60 sessions) is about 90
RETURN_BASIS = "gross"
NOTE = ("rank IC = Spearman(-alpha_rank, forward return); excess = top-decile mean minus universe median; "
        "all returns are gross (no cost deducted); pending horizons have no number")


def _jdump(value: Any) -> str:
    return json.dumps(to_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _slot_rankings(conn: psycopg.Connection, slot: str, as_of: datetime, *, release_id: uuid.UUID | None = None,
                   limit_releases: int | None = None) -> list[dict]:
    """Market rankings of the releases published in ``slot`` (newest release first), horizons the outcome method knows."""
    sql = ("SELECT r.ranking_id, r.release_id, r.horizon_id, rel.decision_as_of, rel.availability_policy_version, p.published_at"
           " FROM decision.release_publication p JOIN decision.release rel USING (release_id) JOIN decision.ranking_run r USING (release_id)"
           " WHERE p.slot_key=%s AND r.ranking_scope='market' AND rel.release_state IN ('READY','SUPERSEDED') AND rel.decision_as_of <= %s")
    args: list[Any] = [slot, as_of]
    if release_id is not None:
        sql += " AND r.release_id=%s"
        args.append(release_id)
    rows = conn.execute(sql + " ORDER BY rel.decision_as_of DESC, r.ranking_id", args).fetchall()
    rows = [r for r in rows if r["horizon_id"] in MT.HORIZON_SESSIONS]
    if limit_releases is not None:
        keep: list[uuid.UUID] = []
        for r in rows:
            if r["release_id"] not in keep:
                keep.append(r["release_id"])
        rows = [r for r in rows if r["release_id"] in keep[:limit_releases]]
    return rows


def stored_snapshots(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version",
                        (DATASET,)).fetchall()


def _listed_ids(man: dict) -> set[str] | None:
    """Ranking ids a snapshot holds, from its coverage document; None for snapshots written before the list existed."""
    ids = (man.get("coverage_document") or {}).get("ranking_ids")
    return None if ids is None else set(ids)


def resolved_ids(conn: psycopg.Connection, lake: LakeStore) -> set[str]:
    """Ids of every stored ranking. Reads the lake only for snapshots without an id list."""
    out: set[str] = set()
    for man in stored_snapshots(conn):
        ids = _listed_ids(man)
        if ids is None:
            df = lake.read_snapshot_frames(man["content_hash"])
            ids = set() if df is None else set(df["ranking_id"].to_list())
        out |= ids
    return out


def stored_rows(conn: psycopg.Connection, lake: LakeStore, ranking_ids: Sequence[uuid.UUID] | None = None) -> dict[str, dict]:
    """Resolved rows by ranking id (first stored row wins: a ranking is written once). Only snapshots that can hold a wanted id are read."""
    want = None if ranking_ids is None else {str(i) for i in ranking_ids}
    out: dict[str, dict] = {}
    for man in stored_snapshots(conn):
        listed = _listed_ids(man)
        if want is not None and listed is not None and not (listed & (want - set(out))):
            continue
        df = lake.read_snapshot_frames(man["content_hash"])
        for r in df.iter_rows(named=True):
            if (want is None or r["ranking_id"] in want) and r["ranking_id"] not in out:
                out[r["ranking_id"]] = {**json.loads(r["body"]), "snapshot_id": str(man["snapshot_id"]), "body_sha256": r["body_sha256"]}
    return out


def _returns(ranked: Sequence[tuple[str, int]], series: Mapping[str, Sequence], entry: date, exit_: date) -> dict[str, Decimal | None]:
    return {iid: outcomes.path_return(iid, series.get(iid, ()), entry, exit_).gross for iid, _ in ranked}


def monitor(svc: DataService, *, as_of: datetime, slot: str = B.SLOT_KEY) -> dict[str, Any]:
    """Resolve every published market ranking whose horizon is observed and not stored yet; publish one immutable snapshot."""
    conn, lake = svc.conn, svc.lake
    done = resolved_ids(conn, lake)
    open_ = [r for r in _slot_rankings(conn, slot, as_of) if str(r["ranking_id"]) not in done]
    oldest = as_of - timedelta(days=MAX_PENDING_DAYS)
    todo = [r for r in open_ if r["decision_as_of"] >= oldest]
    expired = len(open_) - len(todo)                 # never resolvable in practice (decision session never observed): reported, not hidden
    if not todo:
        return {"published": False, "evaluated": 0, "pending": 0, "resolved": 0, "expired": expired}
    stocks = MT.load_stocks(conn, as_of, B.AVAILABILITY_POLICY)
    ids = [str(i) for i in stocks]
    since = min(r["decision_as_of"] for r in todo).astimezone(B.CST).date() - timedelta(days=10)
    series = MT.load_bars(conn, ids, as_of, B.AVAILABILITY_POLICY, since=since)
    cal = MT.observed_sessions(series)
    rows, pending = [], 0
    for r in todo:
        sess = MT.release_session_date(conn, lake, r["release_id"]) or max(
            (d for d in cal if d <= r["decision_as_of"].astimezone(B.CST).date()), default=None)
        h = MT.HORIZON_SESSIONS[r["horizon_id"]]
        win = F.matured_window(cal, sess, h)
        if isinstance(win, str):
            pending += 1
            continue
        entry, exit_ = win
        ranked = [(str(e["instrument_id"]), e["alpha_rank"]) for e in conn.execute(
            "SELECT instrument_id, alpha_rank FROM decision.ranking_entry WHERE ranking_id=%s ORDER BY alpha_rank", (r["ranking_id"],))]
        stats = F.evaluate(ranked, _returns(ranked, series, entry, exit_))
        if not ranked or Decimal(stats["n_evaluable"]) < MIN_COVERAGE_SHARE * len(ranked):
            pending += 1                              # the exit day is not (fully) ingested yet: do not freeze half a day
            continue
        body = {"ranking_id": str(r["ranking_id"]), "release_id": str(r["release_id"]), "horizon_id": r["horizon_id"], "horizon_sessions": h,
                "decision_as_of": r["decision_as_of"], "decision_session": sess, "entry_session": entry, "exit_session": exit_,
                "method_version": F.METHOD_VERSION, "return_basis": RETURN_BASIS, "evaluated_at": as_of,
                "record_kind": "reconstruction" if B.reconstructed(r["availability_policy_version"]) else "live_forward", **stats}
        text = _jdump(body)
        rows.append({"ranking_id": str(r["ranking_id"]), "release_id": str(r["release_id"]), "horizon_id": r["horizon_id"],
                     "evaluated_at": as_of.isoformat(), "body": text, "body_sha256": sha256(text.encode("utf-8")).hexdigest()})
    if not rows:
        return {"published": False, "evaluated": len(todo), "pending": pending, "resolved": 0, "expired": expired}
    frame = pl.DataFrame(rows).sort("ranking_id")
    cov = {"dataset": DATASET, "method_version": F.METHOD_VERSION, "return_basis": RETURN_BASIS, "evaluated_at": as_of.isoformat(), "rows": len(rows),
           "ranking_ids": sorted(r["ranking_id"] for r in rows), "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest(), "slot": slot, "note": NOTE}
    man = B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)
    return {"published": True, "evaluated": len(todo), "pending": pending, "resolved": len(rows), "expired": expired,
            "snapshot_id": str(man["snapshot_id"])}


# ---- read model ------------------------------------------------------------------------------------------------------------------
def _sessions_after(conn: psycopg.Connection, session: date | None, as_of: datetime) -> int | None:
    """Observed sessions after ``session`` using the same 'at least half the usual instrument count' rule as the monitor."""
    if session is None:
        return None
    rows = conn.execute(
        "SELECT trade_date, count(DISTINCT instrument_id) n FROM market.bar_projection WHERE timeframe='1d' AND is_final AND available_at <= %s"
        " AND trade_date >= %s AND trade_date <= %s GROUP BY trade_date ORDER BY trade_date", (as_of, session - timedelta(days=7), as_of.astimezone(B.CST).date())).fetchall()
    if not rows:
        return None
    counts = sorted(r["n"] for r in rows)
    full = [r["trade_date"] for r in rows if r["n"] >= 0.5 * counts[len(counts) // 2]]
    return F.sessions_after(full, session)


def _latest_bar_day(conn: psycopg.Connection, decision_as_of: datetime, as_of: datetime) -> date | None:
    """Fallback decision session (the monitor's own rule): the newest observed bar day on or before the decision date."""
    row = conn.execute("SELECT max(trade_date) d FROM market.bar_projection WHERE timeframe='1d' AND is_final AND available_at <= %s AND trade_date <= %s",
                       (as_of, decision_as_of.astimezone(B.CST).date())).fetchone()
    return row["d"]


def read_rows(conn: psycopg.Connection, lake: LakeStore, *, slot: str = B.SLOT_KEY, release_id: uuid.UUID | None = None,
              limit_releases: int = 30, as_of: datetime | None = None) -> list[dict]:
    """One wire row per (published release, market horizon): stored result, or ``pending`` with the progress (never 0)."""
    now = as_of or datetime.now(timezone.utc)
    rankings = _slot_rankings(conn, slot, now, release_id=release_id, limit_releases=limit_releases)
    got = stored_rows(conn, lake, [r["ranking_id"] for r in rankings])
    out = []
    session_cache: dict[uuid.UUID, date | None] = {}
    after_cache: dict[date | None, int | None] = {}
    for r in rankings:
        base = {"release_id": r["release_id"], "ranking_id": r["ranking_id"], "horizon_id": r["horizon_id"],
                "horizon_sessions": MT.HORIZON_SESSIONS[r["horizon_id"]], "decision_as_of": r["decision_as_of"],
                "return_basis": RETURN_BASIS, "method_version": F.METHOD_VERSION}
        hit = got.get(str(r["ranking_id"]))
        if hit is not None:
            out.append({**base, "decision_session": hit["decision_session"], "status": "resolved", "pending_reason": None,
                        "entry_session": hit["entry_session"], "exit_session": hit["exit_session"], "sessions_after": None,
                        **{k: hit[k] for k in ("n_ranked", "n_evaluable", "top_n", "top_evaluable", "rank_ic", "top_decile_mean_gross",
                                               "universe_median_gross", "top_decile_excess", "missing_reason", "record_kind")},
                        "evaluated_at": hit["evaluated_at"], "snapshot_id": hit["snapshot_id"]})
            continue
        if r["release_id"] not in session_cache:
            session_cache[r["release_id"]] = MT.release_session_date(conn, lake, r["release_id"]) or _latest_bar_day(conn, r["decision_as_of"], now)
        sess = session_cache[r["release_id"]]
        if sess not in after_cache:
            after_cache[sess] = _sessions_after(conn, sess, now)
        after = after_cache[sess]
        reason = ("DECISION_SESSION_NOT_OBSERVED" if after is None
                  else "AWAITING_MONITOR_RUN" if after >= base["horizon_sessions"] else "HORIZON_NOT_REACHED")
        out.append({**base, "decision_session": sess, "status": "pending", "pending_reason": reason, "entry_session": None, "exit_session": None,
                    "sessions_after": after, "n_ranked": None, "n_evaluable": None, "top_n": None, "top_evaluable": None, "rank_ic": None,
                    "top_decile_mean_gross": None, "universe_median_gross": None, "top_decile_excess": None, "missing_reason": None,
                    "record_kind": None, "evaluated_at": None, "snapshot_id": None})
    return out
