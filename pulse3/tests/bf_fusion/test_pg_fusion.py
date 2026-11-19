"""Fusion lane on real PostgreSQL: append-only datasets, the reads over HTTP (contract-checked by the route), a refused daily run is recorded,
the registry rows of the forward experiments."""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application import bf_fusion_read as R
from pulse.application import bf_fusion_service as S
from pulse.application.data_service import DataService

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/fusion"
PRINCIPAL = uuid.UUID(int=11)
CODES = [f"60{i:04d}" for i in range(60)]


def scores_for(days: list[date]) -> pl.DataFrame:
    return pl.DataFrame([(c, d, float(len(CODES) - i) / 100) for d in days for i, c in enumerate(CODES)],
                        schema=S.CURRENT_SCHEMA, orient="row")


def book_row(d: date, nav: float) -> dict:
    return {"date": d.isoformat(), "gate_w": 0.25, "new_buys_allowed": 1, "try_buy": ["600001"], "offer_top10": CODES[:10], "holdings": ["600002"],
            "nav": nav, "nav_since_freeze_pct": None, "ungated_nav": 150.5, "scores_sha256_16": "abcd", "rule": "r"}


@pytest.fixture()
def seeded(fresh):
    conn, dsn, lake_root = fresh
    lake = LakeStore(lake_root)
    svc = DataService(conn, lake)
    svc.bootstrap()
    store = S.Store(svc)
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(3)]
    store.publish(S.DS_CURRENT, scores_for(days), {"kind": "current", "last_date": str(days[-1])})
    asof = S.append_asof(pl.DataFrame(schema=S.ASOF_SCHEMA), scores_for(days[:2]), computed_on=date(2026, 11, 3), run_id="r1")
    store.publish(S.DS_ASOF, asof, {"kind": "asof"})
    rows = [("main", d.isoformat(), date(2026, 11, 5), "r1", book_row(d, 168.0 + i)) for i, d in enumerate(days)]
    rows += [("h10", days[-1].isoformat(), date(2026, 11, 5), "r1", {**book_row(days[-1], 160.0), "gate_w": 1.0})]
    rows += [("monitor", days[0].isoformat(), date(2026, 11, 5), "r1",
              {"date": days[0].isoformat(), "n": 100, "ic": 0.02, "decile_spread": 0.5, "top_ex": 0.1, "top_sv": 0.2, "flagged": True, "coverage_fail_days": ["2026-11-05"]}),
             ("monitor", days[0].isoformat(), date(2026, 11, 6), "r2",
              {"date": days[0].isoformat(), "n": 110, "ic": 0.03, "decile_spread": 0.6, "top_ex": 0.1, "top_sv": 0.3, "flagged": False, "coverage_fail_days": [], "replaces_flagged": True}),
             ("x20", days[0].isoformat(), date(2026, 11, 5), "r1", {"date": days[0].isoformat(), "x20": 0.999, "ew": 0.998, "excess_pct": 0.1, "n_x20": 10, "n_ew": 12,
                                                                    "trade_day": True, "turnover_x20": 0.5, "turnover_ew": 0.5}),
             ("os20", days[0].isoformat(), date(2026, 11, 5), "r1", {"date": days[0].isoformat(), "mkt20": -7.5, "flagged": False, "coverage_fail_days": [],
                                                                     **{f"{k}_{g}": v for g in ("OS20_GAP", "OS20_NOGAP", "OS5_GAP") for k, v in (("n", 5), ("ex", -1.5), ("raw", None))}}),
             ("run", days[-1].isoformat(), date(2026, 11, 5), "r1", {"status": "done", "session": days[-1].isoformat(), "seconds": 600.5, "peak_memory_mb": 1700.0})]
    store.publish(S.DS_LOG, S.log_frame(rows), {"kind": "log"})
    return conn, dsn, lake_root, days


def test_store_publish_is_idempotent_and_a_new_snapshot_keeps_the_old_one(seeded):
    conn, _dsn, lake_root, days = seeded
    store = S.Store(DataService(conn, LakeStore(lake_root)))
    cur = store.read(S.DS_CURRENT, S.CURRENT_SCHEMA)
    assert cur.height == 3 * len(CODES)
    n_manifests = conn.execute("SELECT count(*) AS n FROM market.dataset_manifest WHERE dataset_id=%s", (S.DS_CURRENT,)).fetchone()["n"]
    store.publish(S.DS_CURRENT, cur, {"kind": "current"})                    # identical frame: nothing new
    assert conn.execute("SELECT count(*) AS n FROM market.dataset_manifest WHERE dataset_id=%s", (S.DS_CURRENT,)).fetchone()["n"] == n_manifests
    more = S.merge_scores(cur, scores_for([days[-1] + timedelta(days=1)]))
    store.publish(S.DS_CURRENT, more, {"kind": "current"})
    assert store.read(S.DS_CURRENT, S.CURRENT_SCHEMA)["date"].n_unique() == 4
    assert conn.execute("SELECT count(*) AS n FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'", (S.DS_CURRENT,)).fetchone()["n"] == n_manifests + 1


def test_reads_over_http_pass_the_contract_and_show_the_archived_score_not_the_recomputed(seeded, api_on):
    _conn, dsn, lake_root, days = seeded
    c = api_on(dsn, lake_root, PRINCIPAL)
    st = c.get(f"{URL}/status")
    assert st.status_code == 200, st.text
    d = st.json()["data"]
    assert d["current"]["dates"] == 3 and d["asof"]["dates"] == 2 and d["last_run"]["status"] == "done"
    assert d["last_run"]["seconds"] == "600.5" and [b["key"] for b in d["books"]] == ["main", "h10"]
    assert {p["key"] for p in d["preregistrations"]} == {"main", "h10", "monitor", "x20", "os20"}
    assert all(len(f["sha256"]) == 64 for f in d["frozen_data"])
    sc = c.get(f"{URL}/scores", params={"date": days[0].isoformat(), "limit": 5}).json()["data"]
    assert sc["kind"] == "asof" and [r["rank"] for r in sc["rows"]] == [1, 2, 3, 4, 5] and sc["rows"][0]["code"] == CODES[0]
    assert sc["rows"][0]["score"] == "0.6" and sc["rows"][0]["display_name"] is None
    assert c.get(f"{URL}/scores", params={"date": days[2].isoformat()}).json()["data"]["kind"] == "current"      # not archived: the recomputed one
    assert c.get(f"{URL}/scores").json()["data"]["date"] == days[2].isoformat()
    assert c.get(f"{URL}/scores", params={"limit": 0}).status_code == 400
    assert c.get(f"{URL}/scores", params={"date": "yesterday"}).status_code == 400
    assert c.get(f"{URL}/scores", params={"zzz": 1}).status_code == 400
    books = c.get(f"{URL}/books").json()["data"]["books"]
    assert [b["key"] for b in books] == ["main", "h10"] and len(books[0]["rows"]) == 3 and books[0]["rows"][2]["nav"] == "170"
    assert books[0]["rows"][0]["nav_since_freeze_pct"] is None and books[1]["rows"][0]["gate_w"] == "1"
    ex = {e["key"]: e for e in c.get(f"{URL}/experiments").json()["data"]["experiments"]}
    assert len(ex["monitor"]["rows"]) == 1 and ex["monitor"]["rows"][0]["flagged"] is False and ex["monitor"]["rows"][0]["ic"] == "0.03"   # the later row wins
    assert ex["x20"]["rows"][0]["turnover_x20"] == "0.5"
    g = ex["os20"]["rows"][0]["groups"]
    assert [x["group"] for x in g] == ["OS20_GAP", "OS20_NOGAP", "OS5_GAP"] and g[0]["raw"] is None and g[0]["ex"] == "-1.5"
    assert ex["monitor"]["summary"]["unflagged"] == 1


def test_reads_on_an_empty_database_give_empty_lists_not_zeros(fresh, api_on):
    conn, dsn, lake_root = fresh
    DataService(conn, LakeStore(lake_root)).bootstrap()
    c = api_on(dsn, lake_root, PRINCIPAL)
    st = c.get(f"{URL}/status").json()["data"]
    assert st["current"] == {"rows": 0, "dates": 0, "first_date": None, "last_date": None} and st["last_run"] is None
    assert c.get(f"{URL}/scores").json()["data"] == {"date": None, "kind": None, "available_dates": [], "rule": "static40, level-safe, main board", "rows": []}
    assert all(b["rows"] == [] for b in c.get(f"{URL}/books").json()["data"]["books"])
    assert all(e["rows"] == [] for e in c.get(f"{URL}/experiments").json()["data"]["experiments"])


def test_the_routes_need_a_session(fresh, api_on):
    _conn, dsn, lake_root = fresh
    c = api_on(dsn, lake_root, PRINCIPAL)
    c.cookies.clear()
    assert c.get(f"{URL}/status").status_code == 403


def test_a_daily_run_that_cannot_proceed_is_refused_and_recorded_with_its_reason(world):
    from pulse.application import bindings as B

    conn = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    cutoff = datetime(2026, 11, 12, tzinfo=timezone.utc)
    try:
        svc = S.FusionService(conn, lake)
        with pytest.raises(S.FusionStop) as e:
            svc.daily(cutoff)
        assert e.value.code == "cap_dataset_missing"                              # no float-cap snapshot published yet
        ids = [r["instrument_id"] for r in conn.execute("SELECT instrument_id FROM core.instrument WHERE instrument_type='equity' LIMIT 5")]
        caps = pl.DataFrame({"instrument_id": [str(i) for i in ids], "as_of": ["2026-11-10"] * len(ids), "float_cap_cny": [Decimal("5000000000.00")] * len(ids),
                             "total_cap_cny": [Decimal("8000000000.00")] * len(ids), "available_at": [datetime(2026, 11, 10, 9, tzinfo=timezone.utc)] * len(ids)},
                            schema={"instrument_id": pl.String, "as_of": pl.String, "float_cap_cny": pl.Decimal(38, 2), "total_cap_cny": pl.Decimal(38, 2),
                                    "available_at": pl.Datetime("us", "UTC")})
        B.publish_derived(DataService(conn, lake), B.CAP_DATASET, caps, {"dataset": B.CAP_DATASET, "frame_sha256": "t"}, contract_id="pulse.instrument_cap_fact")
        with pytest.raises(S.FusionStop) as e:
            S.FusionService(conn, lake).daily(None)
        assert e.value.code == "history_too_short"                                # the synthetic world holds 130 sessions
        st = R.read_status(conn, lake)
        assert st["last_run"]["status"] == "stopped" and st["last_run"]["code"] == "history_too_short"
        assert st["current"]["rows"] == 0 and st["asof"]["rows"] == 0             # nothing was promoted or archived
        codes = [r["code"] for r in S.Store(DataService(conn, lake)).log_rows("run", by_date=False)]
        assert codes == ["cap_dataset_missing", "history_too_short"]
    finally:
        conn.close()


def test_forward_experiments_are_registered_once_as_shadow_runs_with_their_preregistration_hash(world):
    from pulse.application import bf_fusion_registry as G

    conn = db.connect(world["dsn"], autocommit=True)
    try:
        lake = LakeStore(world["lake_root"])
        first = G.register_forward_experiments(conn, lake)
        second = G.register_forward_experiments(conn, lake)
        assert set(first) == {"main", "h10", "monitor", "x20", "os20"} and all(v["created"] for v in first.values())
        assert not any(v["created"] for v in second.values())
        assert {k: v["experiment_id"] for k, v in first.items()} == {k: v["experiment_id"] for k, v in second.items()}
        rows = conn.execute("SELECT run_purpose, run_state, run_manifest FROM research.experiment_run WHERE run_manifest->>'family_key'=%s", (G.FAMILY,)).fetchall()
        assert len(rows) == 5 and {r["run_purpose"] for r in rows} == {"shadow"} and {r["run_state"] for r in rows} == {"queued"}
        manifest = json.loads((S.SC.DATA / "manifest.json").read_text(encoding="utf-8"))
        assert {r["run_manifest"]["preregistration_sha256"] for r in rows} == set(manifest["preregistrations"].values())
    finally:
        conn.close()
