"""Backfill lane ``research``: playbook replay equity / trades, regime attribution, experiment archive (LEGACY_TRIAGE #17 #18 #20 #24 #76).

Why: the playbook replay (``domain.playbooks.replay``) already produces a daily NAV (net of the user cost profile, through the one
ExecutionKernel and ledger) and a trade list, but the unified experiment path stored only scalar metrics. This module keeps the
replay's equity and trades per experiment and serves them, with the regime attribution, to the playbook page and the lab page.
Storage (interim, no ad hoc DDL): one immutable ``ops.ext_artifact`` per completed playbook experiment (kind ``playbook-replay``,
contract ``urn:pulse:contracts:backfill:research:v1``), found again by ``body.experiment_id``. The proposed table is in
``migrations/proposals/bf_research_trades.sql``; until it is applied the artifact is the store.
Invariants: only the x1 (user cost profile) replay is stored; nothing is recomputed on read except drawdown / attribution, which are
pure functions of the stored series (the browser only draws); the regime of every trade is read once at evaluation time from the release's
market-state regime panel and stored on the trade (point-in-time: see ``domain.playbooks.attribution``), so a heatmap does not change when a
newer release arrives; missing regime data is recorded as a reason, never replaced by a default label; money / ratios are decimal strings
with at most 12 fraction digits; an experiment without an artifact (it ran before this lane, or it failed) has no replay: the read answers
404, it never invents one.
Not covered: net-of-fee per-trade returns (the replay trade record has no fee), the x2 / x3 stress curves, blind-slice runs.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.application import market_today_service as MT
from pulse.application import ops_service as OPS
from pulse.domain.playbooks import attribution as AT
from pulse.domain.playbooks import exits as EX
from pulse.domain.playbooks.replay import ReplayResult

CONTRACT_ID = "urn:pulse:contracts:backfill:research:v1"
SCHEMA_VERSION = "1.0.0"
ARTIFACT_KIND = "playbook-replay"
ARTIFACT_VERSION = "playbook-replay-v1"
PUBLIC_RELEASE_STATES = ("READY", "SUPERSEDED")
RUN_PURPOSES = ("screening", "validation", "shadow", "blind_final")
RUN_STATES = ("queued", "running", "completed", "failed", "cancelled")
D = Decimal
q12 = AT.q12


class ResearchReadError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --------------------------------------------------------------------------------------------------------
# regime timeline (read from the release's market-state panel)
# --------------------------------------------------------------------------------------------------------
def regime_timeline(conn: psycopg.Connection, lake: LakeStore, decision_as_of: datetime) -> dict[str, Any]:
    """The regime points of the newest public release decided at or before ``decision_as_of``.

    Labels use only data up to their own day (``domain.market_state.regime``), so a point of a later release is as point-in-time as one
    of the release that existed then. Returns ``{"available": False, "reason": ...}`` when there is nothing to read."""
    rel = conn.execute(
        "SELECT release_id FROM decision.release WHERE release_state = ANY(%s) AND decision_as_of <= %s"
        " ORDER BY decision_as_of DESC, published_at DESC LIMIT 1", (list(PUBLIC_RELEASE_STATES), decision_as_of)).fetchone()
    if rel is None:
        return {"available": False, "reason": "NO_RELEASE", "points": []}
    comp = MT.release_component(conn, rel["release_id"])
    if comp is None:
        return {"available": False, "reason": "NO_MARKET_STATE_COMPONENT", "release_id": str(rel["release_id"]), "points": []}
    _, df = MT.dataset_frame(conn, lake, comp["snapshot_id"])
    hit = df.filter(df["panel"] == "regime")
    if hit.height != 1:
        return {"available": False, "reason": "NO_REGIME_PANEL", "release_id": str(rel["release_id"]), "points": []}
    body = json.loads(hit["body"][0])
    if not body.get("available"):
        return {"available": False, "reason": str(body.get("reason") or "REGIME_UNAVAILABLE"), "release_id": str(rel["release_id"]), "points": []}
    return {"available": True, "reason": None, "release_id": str(rel["release_id"]), "version": body["version"],
            "through": (body.get("index") or {}).get("through"), "points": body["points"]}


def _regime_header(tl: Mapping[str, Any]) -> dict[str, Any]:
    return {"available": bool(tl["available"]), "reason": tl.get("reason"), "regime_version": tl.get("version"),
            "release_id": tl.get("release_id"), "through": None if tl.get("through") is None else str(tl["through"]),
            "basis": "last completed session before the entry day"}


# --------------------------------------------------------------------------------------------------------
# artifact: build + store (called by the evaluator)
# --------------------------------------------------------------------------------------------------------
def build_replay_artifact(*, experiment_id: UUID, instance_id: UUID, playbook_id: str, rules: EX.ExitRules, run: ReplayResult,
                          sessions: list[str], a: int, c: int, views: Mapping[str, Any], timeline: Mapping[str, Any]) -> dict[str, Any]:
    """The stored body: daily NAV, trades (with exit reason and entry regime) and the regime header. Pure given its inputs."""
    pos = {s: k for k, s in enumerate(sessions)}
    raw_trades = []
    for t in run.trades:
        view = views.get(t.instrument_id)
        closed = t.exit_day is not None and t.exit_price is not None
        raw_trades.append({
            "instrument_id": t.instrument_id, "code": None if view is None else view.code, "entry_day": t.entry_day, "exit_day": t.exit_day,
            "quantity": t.quantity, "entry_price": q12(t.entry_price), "exit_price": q12(t.exit_price) if closed else None,
            "gross_return": q12(t.gross_return) if closed else None,
            "holding_sessions": pos[t.exit_day] - pos[t.entry_day] if closed and t.exit_day in pos and t.entry_day in pos else None,
            "exit_reason": (AT.reconstruct_exit_reason(rules, view, t.entry_day, t.exit_day, float(t.entry_price))
                            if closed and view is not None else None),
            "status": "closed" if closed else "open"})
    trades = AT.tag_trades(raw_trades, timeline["points"]) if timeline["available"] else [{**t, "regime_at_entry": None} for t in raw_trades]
    return {"version": ARTIFACT_VERSION, "experiment_id": str(experiment_id), "instance_id": str(instance_id), "catalog_id": f"PB:{playbook_id}",
            "playbook_id": playbook_id, "cost_policy_version": run.cost_policy_version, "price_basis": run.price_basis,
            "initial_cash": q12(D(str(run.initial["opening_cash"]))), "window": {"start": sessions[a], "end": sessions[c - 1], "sessions": c - a},
            "nav": [{"d": d, "nav": q12(n)} for d, n in run.nav], "trades": trades, "regime": _regime_header(timeline)}


def record_playbook_replay(conn: psycopg.Connection, lake: LakeStore, spec: Any, rec: Any, pb: Any, capture: Mapping[str, Any]) -> dict:
    """Evaluator hook: store the x1 replay of ``rec`` as an immutable artifact (display only: the caller logs a failure and keeps the metrics)."""
    timeline = regime_timeline(conn, lake, spec.decision_as_of)
    body = build_replay_artifact(experiment_id=rec.experiment_id, instance_id=rec.instance_id, playbook_id=pb.id, rules=EX.ExitRules.from_spec(pb.exits),
                                 run=capture["run"], sessions=capture["sessions"], a=capture["a"], c=capture["c"], views=capture["views"],
                                 timeline=timeline)
    return OPS.put_artifact(conn, kind=ARTIFACT_KIND, contract_id=CONTRACT_ID, schema_version=SCHEMA_VERSION, body=body)


# --------------------------------------------------------------------------------------------------------
# artifact: read
# --------------------------------------------------------------------------------------------------------
_LATEST_SQL = (
    "SELECT a.body, a.ref, r.experiment_id, r.completed_at FROM ops.ext_artifact a"
    " JOIN research.experiment_run r ON r.experiment_id = (a.body->>'experiment_id')::uuid AND r.run_state = 'completed'"
    " WHERE a.contract_id = %s AND a.body->>'version' = %s")


def load_replay(conn: psycopg.Connection, *, experiment_id: UUID | None = None, catalog_id: str | None = None) -> dict[str, Any] | None:
    """The stored replay of one experiment, or the latest completed one of a catalog id (``PB:<id>``)."""
    if (experiment_id is None) == (catalog_id is None):
        raise ValueError("exactly one of experiment_id / catalog_id")
    if experiment_id is not None:
        row = conn.execute(_LATEST_SQL + " AND a.body->>'experiment_id' = %s LIMIT 1", (CONTRACT_ID, ARTIFACT_VERSION, str(experiment_id))).fetchone()
    else:
        row = conn.execute(_LATEST_SQL + " AND a.body->>'catalog_id' = %s ORDER BY r.completed_at DESC, r.experiment_id DESC LIMIT 1",
                           (CONTRACT_ID, ARTIFACT_VERSION, catalog_id)).fetchone()
    return None if row is None else {"body": row["body"], "completed_at": row["completed_at"], "ref": row["ref"]}


def equity_of(nav: list[Mapping[str, Any]], initial: Decimal) -> dict[str, Any]:
    """Equity points with drawdown. Peak starts at the initial cash (the same definition as the ``max_drawdown`` metric);
    a missing NAV stays missing and does not move the peak."""
    peak, worst = initial, D(0)
    points, last = [], None
    for p in nav:
        n = None if p["nav"] is None else D(str(p["nav"]))
        if n is None:
            points.append({"d": p["d"], "nav": None, "drawdown": None})
            continue
        peak = max(peak, n)
        dd = n / peak - 1
        worst = min(worst, dd)
        last = n
        points.append({"d": p["d"], "nav": q12(n), "drawdown": q12(dd)})
    return {"points": points, "max_drawdown": q12(worst) if last is not None else None, "final_nav": None if last is None else q12(last),
            "net_return": None if last is None or initial == 0 else q12(last / initial - 1)}


def replay_view(stored: Mapping[str, Any], trades_limit: int) -> dict[str, Any]:
    """Wire payload ``PlaybookReplay`` from a stored artifact."""
    body = stored["body"]
    initial = D(str(body["initial_cash"]))
    trades = body["trades"]
    recent = sorted(trades, key=lambda t: (t["entry_day"], t["instrument_id"]), reverse=True)[:trades_limit]
    att = AT.attribute(trades)
    reg = body["regime"]
    n_closed = sum(1 for t in trades if t["status"] == "closed")
    return {
        "experiment": {"experiment_id": body["experiment_id"], "instance_id": body["instance_id"], "catalog_id": body["catalog_id"],
                       "playbook_id": body["playbook_id"], "completed_at": stored["completed_at"], "cost_policy_version": body["cost_policy_version"],
                       "price_basis": body["price_basis"], "artifact_ref": stored["ref"]},
        "initial_cash": q12(initial), "window": body["window"], "equity": equity_of(body["nav"], initial),
        "trades": {"total": len(trades), "closed": n_closed, "open": len(trades) - n_closed, "shown": len(recent), "items": recent},
        "attribution": {"regime": reg, **att},
        "caveats": [
            "净值曲线是执行内核按用户费用口径回放的结果，已扣费；价格为前复权研究序列，只用于研究。",
            "单笔收益为成交均价之比（毛收益，未扣费）；费用体现在净值曲线里。",
            f"环境归因取入场前最后一个已完成交易日的市场环境；某类少于 {AT.MIN_TRADES} 笔时标「样本不足」，不能当作证据。",
            "卖出原因由同一套卖出规则在持有期逐日复算得出。"]}


def heatmap_view(conn: psycopg.Connection) -> dict[str, Any]:
    """Wire payload ``RegimeHeatmap``: the latest completed experiment of every playbook that has a stored replay."""
    rows = conn.execute(
        "SELECT DISTINCT ON (a.body->>'catalog_id') a.body, r.experiment_id, r.completed_at FROM ops.ext_artifact a"
        " JOIN research.experiment_run r ON r.experiment_id = (a.body->>'experiment_id')::uuid AND r.run_state = 'completed'"
        " WHERE a.contract_id = %s AND a.body->>'version' = %s ORDER BY a.body->>'catalog_id', r.completed_at DESC, r.experiment_id DESC",
        (CONTRACT_ID, ARTIFACT_VERSION)).fetchall()
    per, meta = {}, {}
    for r in rows:
        b = r["body"]
        per[b["catalog_id"]] = AT.attribute(b["trades"])
        meta[b["catalog_id"]] = (b, r)
    hm = AT.heatmap(per)
    out_rows = []
    for row in hm["rows"]:
        b, r = meta[row["playbook"]]
        out_rows.append({"catalog_id": b["catalog_id"], "playbook_id": b["playbook_id"], "experiment_id": b["experiment_id"], "completed_at": r["completed_at"],
                         "regime_available": bool(b["regime"]["available"]), "closed_trades": row["closed_trades"], "dimensions": row["dimensions"]})
    return {"version": hm["version"], "min_trades": AT.MIN_TRADES, "columns": hm["columns"], "rows": out_rows,
            "note": "每格 = 该战法在入场当日环境下的已平仓交易平均毛收益；少于样本门槛的格子标「样本不足」。环境标签是未校准的描述，不是预测。"}


# --------------------------------------------------------------------------------------------------------
# experiment archive
# --------------------------------------------------------------------------------------------------------
KIND_BY_PREFIX = {"PB": "playbook", "FL": "factor", "EX": "exit_rule", "TT": "intraday_t"}
HEADLINES = (("net_return", "validation", "net_return_validation"), ("max_drawdown", "overall", "max_drawdown"), ("trade_count", "overall", "trade_count"))
SORT_AT = "COALESCE(r.completed_at, r.started_at, c.created_at)"


def encode_cursor(sort_at: datetime, experiment_id: UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([contracts.utc_z(sort_at), str(experiment_id)]).encode()).decode()


def decode_cursor(token: str) -> tuple[datetime, UUID]:
    try:
        ts, eid = json.loads(base64.urlsafe_b64decode(token.encode()))
        return datetime.fromisoformat(ts.replace("Z", "+00:00")), UUID(eid)
    except (ValueError, TypeError, KeyError) as e:
        raise ResearchReadError(400, "INVALID_CONTRACT", "游标无效") from e


def archive(conn: psycopg.Connection, *, instance_id: UUID | None, purpose: str | None, status: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
    """Past experiments, newest first (completion time, else start, else the context's creation: the table has no creation column).

    Filters: instance, run purpose, run state. Keyset paging on (sort instant, experiment id)."""
    if purpose is not None and purpose not in RUN_PURPOSES:
        raise ResearchReadError(400, "INVALID_CONTRACT", f"purpose 必须是 {', '.join(RUN_PURPOSES)}")
    if status is not None and status not in RUN_STATES:
        raise ResearchReadError(400, "INVALID_CONTRACT", f"status 必须是 {', '.join(RUN_STATES)}")
    if not 1 <= limit <= 200:
        raise ResearchReadError(400, "INVALID_CONTRACT", "limit 必须在 1 到 200 之间")
    where, args = ["true"], []
    for col, val in (("r.instance_id", instance_id), ("r.run_purpose", purpose), ("r.run_state", status)):
        if val is not None:
            where.append(f"{col} = %s")
            args.append(val)
    if cursor:
        ts, eid = decode_cursor(cursor)
        where.append(f"({SORT_AT}, r.experiment_id) < (%s, %s)")
        args += [ts, eid]
    rows = conn.execute(
        f"SELECT r.experiment_id, r.instance_id, r.run_purpose, r.run_state, r.started_at, r.completed_at, r.error_code, {SORT_AT} AS sort_at,"
        " d.display_name, d.method_family, d.specification->>'catalog_id' AS catalog_id"
        " FROM research.experiment_run r JOIN research.evaluation_context c USING (evaluation_context_id)"
        " JOIN strategy.instance i ON i.instance_id = r.instance_id"
        " JOIN strategy.definition d ON d.strategy_id = i.strategy_id AND d.strategy_version = i.strategy_version"
        f" WHERE {' AND '.join(where)} ORDER BY sort_at DESC, r.experiment_id DESC LIMIT %s", (*args, limit + 1)).fetchall()
    page = rows[:limit]
    ids = [r["experiment_id"] for r in page]
    head: dict[UUID, dict[str, Any]] = {i: {} for i in ids}
    replay_ids: set[str] = set()
    if ids:
        for m in conn.execute("SELECT experiment_id, metric_name, slice_key, value FROM research.metric_value WHERE experiment_id = ANY(%s)"
                              " AND metric_name = ANY(%s)", (ids, [h[0] for h in HEADLINES])).fetchall():
            for name, sl, out in HEADLINES:
                if m["metric_name"] == name and m["slice_key"] == sl:
                    head[m["experiment_id"]][out] = m["value"]
        replay_ids = {r["eid"] for r in conn.execute(
            "SELECT body->>'experiment_id' AS eid FROM ops.ext_artifact WHERE contract_id = %s AND body->>'version' = %s"
            " AND body->>'experiment_id' = ANY(%s)", (CONTRACT_ID, ARTIFACT_VERSION, [str(i) for i in ids])).fetchall()}
    items = []
    for r in page:
        cid = r["catalog_id"]
        h = head[r["experiment_id"]]
        items.append({
            "experiment_id": r["experiment_id"], "instance_id": r["instance_id"], "display_name": r["display_name"], "catalog_id": cid,
            "kind": KIND_BY_PREFIX.get((cid or "").split(":", 1)[0], "other"), "run_purpose": r["run_purpose"], "run_state": r["run_state"],
            "started_at": r["started_at"], "completed_at": r["completed_at"], "error_code": r["error_code"],
            "net_return_validation": h.get("net_return_validation"), "max_drawdown": h.get("max_drawdown"), "trade_count": h.get("trade_count"),
            "has_replay": str(r["experiment_id"]) in replay_ids})
    nxt = encode_cursor(rows[limit - 1]["sort_at"], rows[limit - 1]["experiment_id"]) if len(rows) > limit else None
    return {"items": items, "page": {"next_cursor": nxt, "limit": limit},
            "order": "按完成时间（未完成的按开始时间，再按评价上下文创建时间）由新到旧；实验表没有创建时间列"}
