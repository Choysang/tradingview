"""Board lane of the legacy backfill (docs/LEGACY_TRIAGE.md #38/#39 watch cards, #30 theme map, #50 palette search).

Why: the web pages need per-instrument cards, per-sector amount/change and a name search that the v3 contract has no slot
for. Each read is a thin aggregation over point-in-time raw daily bars pinned to ONE release; no ranking logic lives here.
Invariants: bars are the latest visible revision at the release's ``decision_as_of`` (``market_today_service.load_bars``);
amount is ``volume_shares x close_raw`` and always labelled an estimate (daily bars carry no turnover amount); a missing bar
or previous bar gives ``null`` plus a reason, never 0; a move beyond ``UNVERIFIABLE_PCT`` (ex-rights, bad data) is not shown
as a change; rank comes from the release's stored market ranking, nothing is re-sorted.
Not covered: intraday quotes (cards show the release session close), name search over history (current labels only).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping, Sequence

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import market_today_service as MT
from pulse.domain.market_state.daily import Bar

METHOD = "amount_estimate = volume_shares x close_raw of the release session; pct = close / previous session close - 1"
SPARK_POINTS = 30
LOOKBACK_DAYS = 70
UNVERIFIABLE_PCT = Decimal(31)         # beyond any board limit (30% + margin): treated as ex-rights / bad data
MAX_CARDS = 60
SEARCH_LIMIT = 20
_Q4, _Q2 = Decimal("0.0001"), Decimal("0.01")
_LIKE_ESCAPE = "\\"


class BoardError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _q(v: Decimal, step: Decimal) -> Decimal:
    return v.quantize(step, rounding=ROUND_HALF_EVEN)


def _pct(last: Bar, prev: Bar) -> tuple[Decimal | None, str | None]:
    if last.c is None or prev.c is None or prev.c == 0:
        return None, "no_previous_bar"
    pct = (last.c / prev.c - 1) * 100
    if abs(pct) > UNVERIFIABLE_PCT:
        return None, "unverifiable_move"
    return _q(pct, _Q4), None


def _amount(b: Bar) -> Decimal | None:
    return None if b.c is None or b.v is None else _q(b.c * b.v, _Q2)


def _bars(conn: psycopg.Connection, release: Mapping[str, Any], ids: Sequence[str]) -> dict[str, list[Bar]]:
    as_of = release["decision_as_of"]
    until = as_of.astimezone(MT.B.CST).date()
    return MT.load_bars(conn, list(ids), as_of, release["availability_policy_version"], until=until, since=until - timedelta(days=LOOKBACK_DAYS))


# ---------------------------------------------------------------------------------------------------- watch cards
def _rank_map(conn: psycopg.Connection, release_id: uuid.UUID, horizon_id: str, target: str, ids: Sequence[str]) -> tuple[dict[str, int], int | None]:
    run = conn.execute("SELECT ranking_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market' AND horizon_id=%s"
                       " AND forecast_target=%s ORDER BY created_at DESC LIMIT 1", (release_id, horizon_id, target)).fetchone()
    if run is None:
        return {}, None
    rid = run["ranking_id"]
    total = conn.execute("SELECT count(*) n FROM decision.ranking_entry WHERE ranking_id=%s", (rid,)).fetchone()["n"]
    rows = conn.execute("SELECT instrument_id, alpha_rank FROM decision.ranking_entry WHERE ranking_id=%s AND instrument_id = ANY(%s)",
                        (rid, [uuid.UUID(i) for i in ids])).fetchall()
    return {str(r["instrument_id"]): r["alpha_rank"] for r in rows}, total


def watch_cards(conn: psycopg.Connection, release: Mapping[str, Any], ids: Sequence[str], horizon_id: str, forecast_target: str) -> dict:
    if not ids:
        raise BoardError(400, "INVALID_CONTRACT", "ids 不能为空")
    if len(ids) > MAX_CARDS:
        raise BoardError(400, "INVALID_CONTRACT", f"一次最多 {MAX_CARDS} 只")
    bars = _bars(conn, release, ids)
    ranks, total = _rank_map(conn, release["release_id"], horizon_id, forecast_target, ids)
    cards = []
    for iid in ids:
        series = bars.get(iid, [])
        if not series:
            cards.append({"instrument_id": iid, "session": None, "last_close": None, "pct_change": None, "pct_missing_reason": "no_bar",
                          "amount_estimate_cny": None, "spark": [], "rank": None})
            continue
        last = series[-1]
        pct, why = _pct(last, series[-2]) if len(series) > 1 else (None, "no_previous_bar")
        rank = None
        if iid in ranks and total:
            rank = {"alpha_rank": ranks[iid], "total": total, "horizon_id": horizon_id, "forecast_target": forecast_target}
        cards.append({"instrument_id": iid, "session": last.d, "last_close": last.c, "pct_change": pct, "pct_missing_reason": why,
                      "amount_estimate_cny": _amount(last),
                      "spark": [{"d": b.d, "c": b.c} for b in series[-SPARK_POINTS:] if b.c is not None], "rank": rank})
    return {"release_id": release["release_id"], "amount_is_estimate": True, "method": METHOD, "cards": cards}


# ---------------------------------------------------------------------------------------------------- theme map
def theme_map(conn: psycopg.Connection, lake: LakeStore, release: Mapping[str, Any], taxonomy_id: str, taxonomy_version: int) -> dict:
    run = conn.execute("SELECT membership_snapshot_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='sector_overview'"
                       " AND taxonomy_id=%s AND taxonomy_version=%s ORDER BY created_at DESC LIMIT 1",
                       (release["release_id"], taxonomy_id, taxonomy_version)).fetchone()
    if run is None or run["membership_snapshot_id"] is None:
        raise BoardError(404, "NOT_FOUND", "该发布中没有这个分类的板块总览")
    snap = run["membership_snapshot_id"]
    _, df = MT.dataset_frame(conn, lake, snap)
    df = df.select(["node_id", "instrument_id"]).unique()
    members: dict[str, list[str]] = {}
    for node, iid in zip(df["node_id"].to_list(), df["instrument_id"].to_list()):
        members.setdefault(node, []).append(str(iid))
    ids = sorted({i for v in members.values() for i in v})
    bars = _bars(conn, release, ids) if ids else {}
    sessions = sorted({b.d for s in bars.values() for b in s})
    session: date | None = sessions[-1] if sessions else None
    prev_session = sessions[-2] if len(sessions) > 1 else None
    per: dict[str, tuple[Decimal | None, Decimal | None]] = {}      # iid -> (amount, pct) on the session
    for iid, s in bars.items():
        by_day = {b.d: b for b in s}
        last = by_day.get(session) if session else None
        if last is None:
            continue
        prev = by_day.get(prev_session) if prev_session else None
        pct = _pct(last, prev)[0] if prev is not None else None
        per[iid] = (_amount(last), pct)
    names = {r["node_id"]: r["display_name"] for r in conn.execute(
        "SELECT node_id, display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND taxonomy_version=%s", (taxonomy_id, taxonomy_version))}
    nodes = []
    for node in sorted(members):
        got = [per[i] for i in members[node] if i in per]
        amounts = [a for a, _ in got if a is not None]
        pcts = [p for _, p in got if p is not None]
        nodes.append({"node_id": node, "name": names.get(node, node), "members": len(members[node]), "observed": len(got), "pct_samples": len(pcts),
                      "amount_estimate_cny": _q(sum(amounts, Decimal(0)), _Q2) if amounts else None,
                      "avg_pct": _q(sum(pcts, Decimal(0)) / len(pcts), _Q4) if pcts else None})
    return {"release_id": release["release_id"], "taxonomy_id": taxonomy_id, "taxonomy_version": taxonomy_version,
            "membership_snapshot_id": snap, "session": session, "amount_is_estimate": True,
            "method": METHOD + "; avg_pct is the equal-weight mean of members with a verifiable move", "nodes": nodes}


# ---------------------------------------------------------------------------------------------------- search
def search_instruments(conn: psycopg.Connection, q: str, limit: int = 12) -> dict:
    term = q.strip()
    if not term:
        return {"query": "", "items": []}
    esc = term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", _LIKE_ESCAPE + "%").replace("_", _LIKE_ESCAPE + "_")
    rows = conn.execute(
        "SELECT i.instrument_id, a.source_symbol AS code, s.display_name AS name, s.is_risk_warning AS st"
        " FROM core.instrument i JOIN core.instrument_alias a ON a.instrument_id=i.instrument_id AND a.source_id='legacy_warehouse'"
        " LEFT JOIN LATERAL (SELECT st.display_name, st.is_risk_warning FROM core.instrument_state st WHERE st.instrument_id=i.instrument_id"
        "  ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true"
        " WHERE i.instrument_type='equity' AND (a.source_symbol LIKE %(pre)s OR s.display_name ILIKE %(sub)s)"
        " ORDER BY (a.source_symbol = %(term)s) DESC, (a.source_symbol LIKE %(pre)s) DESC, a.source_symbol LIMIT %(n)s",
        {"pre": esc + "%", "sub": "%" + esc + "%", "term": term, "n": min(limit, SEARCH_LIMIT)}).fetchall()
    return {"query": term, "items": [{"instrument_id": r["instrument_id"], "code": r["code"], "name": r["name"], "is_risk_warning": r["st"]} for r in rows]}
