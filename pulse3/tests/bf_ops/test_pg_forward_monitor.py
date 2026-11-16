"""Forward monitor on a private PostgreSQL: IC and excess equal a hand computation, immature horizons are pending,
the dataset is append-only and point in time (#169, M5-E04)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal as D
from statistics import median

import pytest

from ext_c import support as S
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.application import forward_monitor as FM
from pulse.application.data_service import DataService
from w02_helpers import ready_all, staged

pytestmark = pytest.mark.pg
STEPS = [D("0.30"), D("0.10"), D("0.25"), D("0.05"), D("0.20"), D("0.15"), D("0.00"), D("-0.05"), D("0.12"), D("0.08")]   # alpha_rank 1..10
Q = D("0.000001")


def fwd(step: D, exit_idx: int) -> D:
    """Hand formula of the seeded bars: entry = open of session 3 (base + 3 step), exit = close of session exit_idx (base + step*exit_idx + 0.5)."""
    return (D(10) + step * exit_idx + D("0.5")) / (D(10) + step * 3) - 1


def seed(conn):
    ids = [S.seed_instrument(conn, f"60190{i}", f"监控{i}") for i in range(10)]
    for table in ("core.instrument_state", "core.instrument_alias"):         # known since long before the monitored dates (point-in-time loads)
        conn.execute(f"UPDATE {table} SET available_at='2020-01-01Z' WHERE instrument_id = ANY(%s)", (ids,))
    snap = S.seed_manifest(conn)
    days = S.weekdays(date(2026, 10, 5), 25)                       # idx 0 = 10-05 ... idx 24 = 11-06
    for iid, st in zip(ids, STEPS):
        S.seed_bars(conn, iid, snap, days, base=D("10"), step=st)
    rel = PgAnalysisRelease(conn)
    rid, stage = staged(rel, "main", as_of=S.t(2026, 10, 7, 16, 0))   # decision session = idx 2 (10-07)
    ready_all(rel, rid, ["plans", "ranking"])
    for h in ("5td", "20td"):
        S.seed_ranking(conn, rid, ids, h)
    rel.publish(stage.stage_id, producer_version="t")
    return ids, days, rid


def expected(exit_idx: int) -> dict:
    rets = [fwd(s, exit_idx) for s in STEPS]
    med = D(str(median(rets)))
    # steps ascending -> return ranks 10,5,9,3,8,7,2,1,6,4 for alpha_rank 1..10 ; d = 0,4,-1,4,-2,-2,2,2,-4,-3 ; sum d^2 = 74
    ic = (D(1) - D(6 * 74) / D(10 * 99)).quantize(Q, rounding=ROUND_HALF_EVEN)
    return {"rank_ic": ic, "median": med.quantize(Q, rounding=ROUND_HALF_EVEN), "top": rets[0].quantize(Q, rounding=ROUND_HALF_EVEN),
            "excess": (rets[0] - med).quantize(Q, rounding=ROUND_HALF_EVEN)}


@pytest.fixture()
def world_m(fresh, tmp_path):
    conn, dsn = fresh
    ids, days, rid = seed(conn)
    svc = DataService(conn, LakeStore(tmp_path / "lake"))
    return conn, dsn, svc, rid, tmp_path


def test_pending_until_the_exit_bar_is_available_then_equals_the_hand_computation(world_m):
    conn, _, svc, rid, _ = world_m
    # exit session idx 7 (10-14) closes at 15:00 CST and its bar is available 2h later: one hour before that nothing is mature
    early = FM.monitor(svc, as_of=S.t(2026, 10, 14, 16, 0))
    assert early["published"] is False and early["resolved"] == 0 and early["pending"] == 2
    assert FM.stored_snapshots(conn) == []

    out = FM.monitor(svc, as_of=S.t(2026, 10, 20, 19, 0))               # sessions up to idx 11 are visible
    assert out["published"] is True and out["resolved"] == 1 and out["pending"] == 1
    rows = {r["horizon_id"]: r for r in FM.read_rows(conn, svc.lake, as_of=S.t(2026, 10, 20, 19, 0))}
    five, twenty = rows["5td"], rows["20td"]
    e = expected(7)
    assert five["status"] == "resolved" and five["return_basis"] == "gross"
    assert (five["entry_session"], five["exit_session"]) == ("2026-10-08", "2026-10-14")
    assert D(five["rank_ic"]) == e["rank_ic"] and D(five["universe_median_gross"]) == e["median"]
    assert D(five["top_decile_mean_gross"]) == e["top"] and D(five["top_decile_excess"]) == e["excess"]
    assert (five["n_ranked"], five["n_evaluable"], five["top_n"], five["top_evaluable"]) == (10, 10, 1, 1)
    # the 20-session horizon is pending with no numbers (never 0) and shows its progress: 9 sessions observed after the decision session
    assert twenty["status"] == "pending" and twenty["pending_reason"] == "HORIZON_NOT_REACHED" and twenty["sessions_after"] == 9
    assert all(twenty[k] is None for k in ("rank_ic", "top_decile_excess", "top_decile_mean_gross", "universe_median_gross", "entry_session", "snapshot_id"))


def test_append_only_resolved_rows_are_never_rewritten_and_a_rerun_adds_nothing(world_m):
    conn, _, svc, rid, _ = world_m
    FM.monitor(svc, as_of=S.t(2026, 10, 20, 19, 0))
    first = FM.stored_rows(conn, svc.lake)
    (rid5,) = first
    assert FM.monitor(svc, as_of=S.t(2026, 10, 20, 19, 0))["published"] is False              # nothing new: no snapshot
    out = FM.monitor(svc, as_of=S.t(2026, 11, 9, 12, 0))                                     # idx 22 now observed: the 20-session horizon matures
    assert out["published"] is True and out["resolved"] == 1
    after = FM.stored_rows(conn, svc.lake)
    assert len(after) == 2 and after[rid5] == first[rid5]                                    # the 5-session row is byte-identical, same snapshot
    assert len(FM.stored_snapshots(conn)) == 2
    twenty = next(r for k, r in after.items() if k != rid5)
    e = expected(22)
    assert twenty["horizon_id"] == "20td" and D(twenty["rank_ic"]) == e["rank_ic"] and D(twenty["top_decile_excess"]) == e["excess"]
    assert FM.monitor(svc, as_of=S.t(2026, 11, 9, 12, 0))["published"] is False
    mf = conn.execute("SELECT coverage_document FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version", (FM.DATASET,)).fetchall()
    assert all(m["coverage_document"]["return_basis"] == "gross" and m["coverage_document"]["method_version"] == "forward-ic-v1" for m in mf)


def test_only_published_releases_of_the_slot_count(world_m):
    conn, _, svc, rid, _ = world_m
    rel = PgAnalysisRelease(conn)
    other, stage = staged(rel, "other-slot", as_of=S.t(2026, 10, 7, 16, 0))
    ready_all(rel, other, ["plans", "ranking"])
    rel.publish(stage.stage_id, producer_version="t")
    ids = {r["release_id"] for r in FM.read_rows(conn, svc.lake, as_of=S.t(2026, 10, 20, 19, 0))}
    assert ids == {rid}


def test_http_read_is_contract_checked_needs_a_session_and_serves_pending(world_m, api_on, monkeypatch):
    conn, dsn, svc, rid, tmp = world_m
    monkeypatch.setenv("PULSE_LAKE", str(tmp / "lake"))
    FM.monitor(svc, as_of=S.t(2026, 10, 20, 19, 0))
    me = uuid.uuid4()
    api = api_on(dsn, me)
    assert api.c.get("/v1/x/bf/ops/forward-monitor").status_code == 403
    assert api.get("/v1/x/bf/ops/forward-monitor", nope=1).status_code == 400
    r = api.get("/v1/x/bf/ops/forward-monitor", limit=10)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["slot_key"] == "main" and data["return_basis"] == "gross" and data["resolved_count"] == 1 and data["pending_count"] == 1
    five = next(x for x in data["rows"] if x["horizon_id"] == "5td")
    assert five["rank_ic"] == f"{expected(7)['rank_ic']:.6f}"
    # real time has all 25 bars: the 20-session horizon reached its sessions but no monitor run stored it yet
    twenty = next(x for x in data["rows"] if x["horizon_id"] == "20td")
    assert twenty["status"] == "pending" and twenty["pending_reason"] == "AWAITING_MONITOR_RUN" and twenty["rank_ic"] is None
    assert api.get("/v1/x/bf/ops/forward-monitor", release_id=uuid.uuid4()).json()["data"]["rows"] == []
