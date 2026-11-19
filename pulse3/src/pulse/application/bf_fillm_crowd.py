"""Crowd and linkage (legacy rows #32 / #90 / #155): industry diffusion, institutional net buy, name clusters (not available).

Why: the old site's crowd page showed (1) industries moving as a pack with a leader and followers, (2) same-character name
clusters, (3) Dragon-Tiger seats with institutional net buy. pulse3 rebuilds (1) and (3) from point-in-time data pinned to a release.
Industry diffusion (``DIFFUSION_VERSION``): for every industry of the release membership snapshot, per session of the last
``SPREAD_SESSIONS``: limit-ups (verified price-limit rule), share of members up among members with a plausible move, and the
number of strong followers (not limit-up, change >= ``FOLLOWER_PCT``). The last session also names the leader (limit-up with the
highest streak, then estimated amount) and up to ``FOLLOWERS`` followers. Industries with fewer than ``MIN_LIMIT_UPS`` limit-ups on
the last session are not listed (the old rule). The membership is the snapshot known at the release; it is applied to the earlier
sessions of the spread as well (stated in ``method``), so the spread is not a membership history.
Institutional net buy: from the stored Dragon-Tiger reports (``intelligence.seat_report``) of the release session, single-session
windows only (a multi-day window sums several sessions and overlaps, it is never added). The anonymous pool of institutions is a
report-local label (not one institution); net = buy - sell of each distinct (stock, buy, sell) row, so an institution that is on both
lists counts once. A stock on the list without an institution row is NOT shown as 0 (the report is a top-5 table: unlisted trades
are unknown); a row whose buy or sell amount was not disclosed gives net = null with the reason.
Name clusters: ``unavailable``; the old rule used the names of the current day for every past day (a rename or an ST prefix changes
the characters), and pulse3 stores no name history. Not faked.
Not covered: concept boards, seat track records (smart seats / one-day trips: no settled-event history yet).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import market_today_service as MT
from pulse.domain.market_rules import PriceLimitRegistry
from pulse.domain.market_state import daily

DIFFUSION_VERSION = "industry-diffusion-v1"
SPREAD_SESSIONS = 5
MIN_LIMIT_UPS = 2
TOP_INDUSTRIES = 8
FOLLOWERS = 4
FOLLOWER_PCT = Decimal(3)
LOOKBACK_DAYS = 70
INSTITUTION = re.compile(r"^机构专用\s*\d*$")
TOP_INSTITUTIONS = 20
_Q4 = Decimal("0.0001")
NAME_CLUSTER_REASON = ("pulse3 keeps no security-name history: the old rule clustered on the characters of the names of the current day for every "
                       "past session, and renames or ST prefixes change them, so a past day cluster would be fabricated. Retire the row; "
                       "revive only with a dated name history (core.instrument_state keeps names from the import date only).")


def _q(v: Decimal) -> Decimal:
    return v.quantize(_Q4, rounding=ROUND_HALF_EVEN)


def sector_run(conn: psycopg.Connection, release_id, taxonomy_id: str | None, taxonomy_version: int | None) -> dict | None:
    sql = ("SELECT taxonomy_id, taxonomy_version, membership_snapshot_id FROM decision.ranking_run WHERE release_id=%s"
           " AND ranking_scope='sector_overview' AND membership_snapshot_id IS NOT NULL")
    args: list[Any] = [release_id]
    if taxonomy_id is not None:
        sql += " AND taxonomy_id=%s"
        args.append(taxonomy_id)
        if taxonomy_version is not None:
            sql += " AND taxonomy_version=%s"
            args.append(taxonomy_version)
    return conn.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()


def _stock_wire(stocks: Mapping[str, daily.Stock], row: daily.InstrumentRow) -> dict:
    s = stocks.get(row.iid)
    return {"instrument_id": row.iid, "code": None if s is None else s.code, "name": None if s is None else s.name,
            "pct_change": None if row.pct is None else _q(row.pct), "is_limit_up": row.limit_up, "limit_streak": row.streak}


def diffusion(conn: psycopg.Connection, lake: LakeStore, release: Mapping[str, Any], run: Mapping[str, Any]) -> tuple[list[dict], list[date], list[str]]:
    """(industries, sessions of the spread, notes) for the release."""
    _, df = MT.dataset_frame(conn, lake, run["membership_snapshot_id"])
    members: dict[str, list[str]] = {}
    for node, iid in zip(df["node_id"].to_list(), df["instrument_id"].to_list()):
        members.setdefault(node, []).append(str(iid))
    as_of, availability = release["decision_as_of"], release["availability_policy_version"]
    stocks_all = MT.load_stocks(conn, as_of, availability)
    ids = sorted({i for v in members.values() for i in v} & set(stocks_all))
    if not ids:
        return [], [], ["no main-board member of the membership snapshot has instrument data"]
    until = as_of.astimezone(MT.B.CST).date()
    series = MT.load_bars(conn, ids, as_of, availability, until=until, since=until - timedelta(days=LOOKBACK_DAYS))
    sessions = MT.observed_sessions(series)
    if len(sessions) < 2:
        return [], sessions, ["fewer than two observed sessions"]
    spread_days = sessions[-SPREAD_SESSIONS:]
    stocks = {i: stocks_all[i] for i in ids}
    reg = PriceLimitRegistry()
    day_rows: dict[date, dict[str, daily.InstrumentRow]] = {}
    for d in spread_days:
        k = sessions.index(d)
        window = sessions[0:k + 1] if d == sessions[-1] else sessions[max(k - 1, 0):k + 1]    # the last day sees the whole window for streaks
        day_rows[d] = daily.compute_history(stocks, series, reg, window).last_rows
    last = spread_days[-1]
    last_rows = day_rows[last]
    amount: dict[str, Decimal | None] = {}
    for iid, bars in series.items():
        b = next((x for x in reversed(bars) if x.d == last), None)
        amount[iid] = None if b is None or b.c is None or b.v is None else b.c * b.v
    names = {r["node_id"]: r["display_name"] for r in conn.execute(
        "SELECT node_id, display_name FROM core.taxonomy_node WHERE taxonomy_id=%s AND taxonomy_version=%s", (run["taxonomy_id"], run["taxonomy_version"]))}

    def day_stats(node: str, d: date) -> dict:
        rows = [day_rows[d][i] for i in members[node] if i in day_rows[d]]
        valid = [r for r in rows if r.pct is not None]
        ups = sum(1 for r in valid if r.pct > 0)
        return {"session": d, "observed": len(rows), "limit_ups": sum(1 for r in rows if r.limit_up),
                "up_share": None if not valid else _q(Decimal(ups) * 100 / len(valid)),
                "strong_followers": sum(1 for r in valid if r.limit_up is not True and r.pct >= FOLLOWER_PCT)}

    ranked = []
    for node in sorted(members):
        rows = [last_rows[i] for i in members[node] if i in last_rows]
        lu = [r for r in rows if r.limit_up]
        if len(lu) < MIN_LIMIT_UPS:
            continue
        pcts = [r.pct for r in rows if r.pct is not None]
        avg = None if not pcts else _q(sum(pcts, Decimal(0)) / len(pcts))
        ranked.append((len(lu), avg if avg is not None else Decimal(-1000), node, rows, lu, avg))
    ranked.sort(key=lambda t: (-t[0], -t[1], t[2]))
    out = []
    for _, _, node, rows, lu, avg in ranked[:TOP_INDUSTRIES]:
        leader = max(lu, key=lambda r: (r.streak, amount.get(r.iid) or Decimal(0), r.iid))
        foll = sorted((r for r in rows if r.limit_up is not True and r.pct is not None and r.pct >= FOLLOWER_PCT), key=lambda r: (-r.pct, r.iid))[:FOLLOWERS]
        st = day_stats(node, last)
        out.append({"node_id": node, "name": names.get(node, node), "members": len(members[node]), "observed": st["observed"], "limit_ups": st["limit_ups"],
                    "up_share": st["up_share"], "avg_pct": avg, "leader": _stock_wire(stocks, leader),
                    "followers": [_stock_wire(stocks, r) for r in foll], "spread": [day_stats(node, d) for d in spread_days]})
    return out, spread_days, []


# ---- institutional net buy ---------------------------------------------------------------------------------------------------
def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def institution_net(conn: psycopg.Connection, session: date | None, cutoff) -> dict:
    basis = ("single-session Dragon-Tiger reports available by the release cutoff; institution-pool rows only; net = buy - sell per distinct "
             "(stock, buy, sell); top-5 tables, so a stock without an institution row is unknown, not 0")
    if session is None:
        return {"status": "no_seat_reports", "session_date": None, "rows": [], "reports_seen": 0, "basis": basis, "notes": ["the release has no session date"]}
    rows = conn.execute(
        "SELECT rep.instrument_id, rep.report_revision_id, r.side, r.raw_seat_name, r.canonical_amount_cny AS amount, r.resolution_evidence->'source_amounts' AS src"
        " FROM (SELECT DISTINCT ON (source_id, source_report_id) * FROM intelligence.seat_report WHERE window_end=%s AND window_start=window_end"
        "       AND available_at <= %s ORDER BY source_id, source_report_id, revision DESC) rep"
        " JOIN intelligence.seat_report_row r ON r.report_revision_id=rep.report_revision_id", (session, cutoff)).fetchall()
    reports = {r["report_revision_id"] for r in rows}
    if not rows:
        n = conn.execute("SELECT count(*) n FROM intelligence.seat_report WHERE window_end=%s AND window_start=window_end AND available_at <= %s",
                         (session, cutoff)).fetchone()["n"]
        return {"status": "no_seat_reports", "session_date": session, "rows": [], "reports_seen": n, "basis": basis,
                "notes": ["no Dragon-Tiger report of this session was available at the cutoff (the list is published after the close; run intel seats)"]}
    per: dict[Any, dict[tuple, dict]] = {}
    for r in rows:
        if not INSTITUTION.match(r["raw_seat_name"] or ""):
            continue
        src = r["src"] or {}
        buy = _dec(src.get("buy_cny")) if src.get("buy_cny") is not None else (r["amount"] if r["side"] == "buy" else None)
        sell = _dec(src.get("sell_cny")) if src.get("sell_cny") is not None else (r["amount"] if r["side"] == "sell" else None)
        per.setdefault(r["instrument_id"], {})[(buy, sell)] = {"buy": buy, "sell": sell}
    if not per:
        return {"status": "no_institution_rows", "session_date": session, "rows": [], "reports_seen": len(reports), "basis": basis,
                "notes": ["reports exist but none lists the institution pool among its seats"]}
    ids = list(per)
    names = {r["instrument_id"]: r for r in conn.execute(
        "SELECT a.instrument_id, a.source_symbol AS code, s.display_name AS name FROM core.instrument_alias a"
        " LEFT JOIN LATERAL (SELECT st.display_name FROM core.instrument_state st WHERE st.instrument_id=a.instrument_id"
        "  ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true"
        " WHERE a.source_id='legacy_warehouse' AND a.instrument_id = ANY(%s)", (ids,))}
    out = []
    for iid, pairs in per.items():
        buys = [p["buy"] for p in pairs.values()]
        sells = [p["sell"] for p in pairs.values()]
        known = all(b is not None for b in buys) and all(s is not None for s in sells)
        buy = sum(buys, Decimal(0)) if known else None
        sell = sum(sells, Decimal(0)) if known else None
        net = buy - sell if known else None
        out.append({"instrument_id": iid, "code": names.get(iid, {}).get("code"), "name": names.get(iid, {}).get("name"), "institution_rows": len(pairs),
                    "buy_cny": buy, "sell_cny": sell, "net_cny": net,
                    "net_reason": None if net is not None else "the disclosed row has no buy or no sell amount; net is not guessed"})
    out.sort(key=lambda x: (x["net_cny"] is None, -(x["net_cny"] or Decimal(0)), str(x["instrument_id"])))
    return {"status": "ok", "session_date": session, "rows": out[:TOP_INSTITUTIONS], "reports_seen": len(reports), "basis": basis,
            "notes": [f"{len(out)} stocks with institution rows; the {TOP_INSTITUTIONS} largest net buyers are listed"] if len(out) > TOP_INSTITUTIONS else []}


def snapshot(conn: psycopg.Connection, lake: LakeStore, release: Mapping[str, Any], taxonomy_id: str | None = None, taxonomy_version: int | None = None) -> dict:
    run = sector_run(conn, release["release_id"], taxonomy_id, taxonomy_version)
    notes = ["industry membership is the snapshot known at the release; the same membership is applied to the earlier sessions of the spread"]
    if run is None:
        inds, spread, extra = [], [], ["the release has no sector overview (no membership snapshot): industry diffusion is not available"]
        tax, ver, snap = taxonomy_id or "", taxonomy_version or 0, None
    else:
        inds, spread, extra = diffusion(conn, lake, release, run)
        tax, ver, snap = run["taxonomy_id"], run["taxonomy_version"], run["membership_snapshot_id"]
    session = spread[-1] if spread else MT.release_session_date(conn, lake, release["release_id"])
    return {"release_id": release["release_id"], "session_date": session, "taxonomy_id": tax, "taxonomy_version": ver, "membership_snapshot_id": snap,
            "method": f"{DIFFUSION_VERSION}: limit-ups, share of members up, strong followers (>= {FOLLOWER_PCT}% and not limit-up) per session; "
                      f"leader = limit-up with the highest streak, then estimated amount (volume x close); industries with >= {MIN_LIMIT_UPS} limit-ups",
            "sessions_in_spread": spread, "industries": inds, "institutions": institution_net(conn, session, release["decision_as_of"]),
            "name_clusters": {"status": "unavailable", "reason": NAME_CLUSTER_REASON, "retire_recommended": True}, "notes": notes + extra}
