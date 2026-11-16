"""Real-PostgreSQL tests of the research backfill lane: the replay equity / trades store round-trip (worker -> ops.ext_artifact -> API ->
contract), regime attribution on the stored trades, the playbook x regime heatmap and the experiment archive. SYNTHETIC world: proves wiring
and consistency with the existing metrics, not market facts."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.application import bf_research_service as S

pytestmark = pytest.mark.pg
URL = "/v1/x/bf/research"
PLAYBOOKS = ["PB:kdj_golden_low", "PB:turtle_55"]
EXIT_REASONS = {"stop_loss", "take_profit", "trailing_stop", "holding_days", "unknown"}


@pytest.fixture(scope="module")
def runs(world):
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db
    from pulse.workers import research as W

    c = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    W.ext_b_register(c)
    out = W.ext_b_run_catalog(c, lake, [*PLAYBOOKS, "PB:value_pe_pb", "FL:momentum_20"], key="bfr")
    yield {r["catalog_id"]: r["result"] for r in out}, c, lake
    c.close()


def replay(client, cid="PB:kdj_golden_low", **q):
    r = client.get(f"{URL}/playbook-replay", params={"catalog_id": cid, **q})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def metric(conn, eid, name, sl="overall"):
    return conn.execute("select value from research.metric_value where experiment_id=%s and metric_name=%s and slice_key=%s", (eid, name, sl)).fetchone()["value"]


# ---- store round-trip -------------------------------------------------------------------------------------------------
def test_completed_playbook_experiments_store_one_artifact_failed_ones_none(runs):
    res, conn, _ = runs
    for cid in PLAYBOOKS:
        assert res[cid]["state"] == "completed", res[cid]
        n = conn.execute("select count(*) n from ops.ext_artifact where contract_id=%s and body->>'experiment_id'=%s", (S.CONTRACT_ID, str(res[cid]["experiment_id"]))).fetchone()["n"]
        assert n == 1
    assert res["PB:value_pe_pb"]["state"] == "failed"
    assert conn.execute("select count(*) n from ops.ext_artifact where contract_id=%s and body->>'experiment_id'=%s", (S.CONTRACT_ID, str(res["PB:value_pe_pb"]["experiment_id"]))).fetchone()["n"] == 0
    assert conn.execute("select count(*) n from ops.ext_artifact where contract_id=%s and body->>'experiment_id'=%s", (S.CONTRACT_ID, str(res["FL:momentum_20"]["experiment_id"]))).fetchone()["n"] == 0


def test_equity_trades_and_attribution_validate_against_the_contract_and_match_the_stored_metrics(runs, client):
    res, conn, _ = runs
    d = replay(client)
    contracts.check("PlaybookReplay", d, contracts.backfill_id("research"))
    eid = res["PB:kdj_golden_low"]["experiment_id"]
    assert d["experiment"]["experiment_id"] == str(eid) and d["experiment"]["catalog_id"] == "PB:kdj_golden_low"
    eq = d["equity"]
    # the curve reproduces the stored overall metrics (no metric definition was changed)
    assert D(eq["net_return"]) == pytest.approx(metric(conn, eid, "net_return"), abs=D("1e-9"))
    assert D(eq["max_drawdown"]) == pytest.approx(metric(conn, eid, "max_drawdown"), abs=D("1e-9"))
    assert d["trades"]["total"] == metric(conn, eid, "trade_count")
    assert len(eq["points"]) == d["window"]["sessions"]
    # independent drawdown recomputation from the NAV series
    peak = D(d["initial_cash"])
    for p in eq["points"]:
        if p["nav"] is None:
            assert p["drawdown"] is None
            continue
        peak = max(peak, D(p["nav"]))
        assert D(p["drawdown"]) == pytest.approx(D(p["nav"]) / peak - 1, abs=D("1e-9")) and D(p["drawdown"]) <= 0


def test_trade_rows_are_consistent_pit_and_newest_first(runs, client):
    d = replay(client, trades_limit=500)
    items = d["trades"]["items"]
    assert items and d["trades"]["shown"] == len(items) == d["trades"]["total"]
    assert [t["entry_day"] for t in items] == sorted((t["entry_day"] for t in items), reverse=True)
    for t in items:
        if t["status"] == "closed":
            assert t["exit_day"] > t["entry_day"] and t["holding_sessions"] >= 1
            assert D(t["gross_return"]) == pytest.approx(D(t["exit_price"]) / D(t["entry_price"]) - 1, abs=D("1e-9"))
            assert t["exit_reason"] in EXIT_REASONS
        else:
            assert t["exit_day"] is None and t["gross_return"] is None and t["exit_price"] is None and t["exit_reason"] is None
        if t["regime_at_entry"] is not None:
            assert t["regime_at_entry"]["session"] < t["entry_day"]                 # never the entry day's own regime
    assert any(t["status"] == "closed" for t in items)
    limited = replay(client, trades_limit=1)
    assert limited["trades"]["shown"] == 1 and limited["trades"]["total"] == d["trades"]["total"] and limited["trades"]["items"][0] == items[0]


def test_attribution_counts_every_closed_trade_once_per_dimension_and_flags_small_cells(runs, client):
    d = replay(client)
    att = d["attribution"]
    closed = d["trades"]["closed"]
    assert att["closed_trades"] == closed and att["open_trades"] == d["trades"]["open"] and att["min_trades"] == 10
    assert [x["dimension"] for x in att["dimensions"]] == ["trend", "liquidity", "sentiment", "style"]
    for dim in att["dimensions"]:
        assert sum(c["trade_count"] for c in dim["cells"]) + dim["unclassified"]["count"] == closed
        for c in dim["cells"]:
            assert c["insufficient_sample"] == (c["trade_count"] < 10)
            assert (c["avg_return"] is None) == (c["trade_count"] == 0)
    reg = att["regime"]
    if reg["available"]:                                                              # classified trades agree with their stored entry regime
        by_trend: dict[str, list[D]] = {}
        for t in replay(client, trades_limit=500)["trades"]["items"]:
            tag = t["regime_at_entry"]
            if t["status"] == "closed" and tag and tag["basis"]["trend"] == "ok":
                by_trend.setdefault(tag["trend"], []).append(D(t["gross_return"]))
        trend = next(x for x in att["dimensions"] if x["dimension"] == "trend")
        for c in trend["cells"]:
            rs = by_trend.get(c["bucket"], [])
            assert c["trade_count"] == len(rs)
            if rs:
                assert D(c["avg_return"]) == pytest.approx(sum(rs) / len(rs), abs=D("1e-9"))
    else:
        assert reg["reason"] and all(x["unclassified"]["count"] == closed for x in att["dimensions"])


def test_read_by_experiment_id_and_bad_requests(runs, client):
    res, _, _ = runs
    eid = str(res["PB:turtle_55"]["experiment_id"])
    r = client.get(f"{URL}/playbook-replay", params={"experiment_id": eid})
    assert r.status_code == 200 and r.json()["data"]["experiment"]["catalog_id"] == "PB:turtle_55"
    assert client.get(f"{URL}/playbook-replay").status_code == 400                                     # neither
    assert client.get(f"{URL}/playbook-replay", params={"experiment_id": eid, "catalog_id": "PB:turtle_55"}).status_code == 400   # both
    assert client.get(f"{URL}/playbook-replay", params={"experiment_id": "nope"}).status_code == 400
    assert client.get(f"{URL}/playbook-replay", params={"catalog_id": "PB:turtle_55", "trades_limit": 0}).status_code == 400
    assert client.get(f"{URL}/playbook-replay", params={"catalog_id": "PB:value_pe_pb"}).status_code == 404   # failed trial: no replay is invented
    assert client.get(f"{URL}/playbook-replay", params={"catalog_id": "PB:does_not_exist"}).status_code == 404


def test_the_latest_completed_experiment_of_a_playbook_is_served(runs, client):
    res, conn, lake = runs
    from pulse.workers import research as W
    again = W.ext_b_run_catalog(conn, lake, ["PB:kdj_golden_low"], key="bfr-second")[0]["result"]
    assert again["state"] == "completed" and again["experiment_id"] != res["PB:kdj_golden_low"]["experiment_id"]
    assert replay(client)["experiment"]["experiment_id"] == str(again["experiment_id"])
    old = client.get(f"{URL}/playbook-replay", params={"experiment_id": str(res["PB:kdj_golden_low"]["experiment_id"])})
    assert old.status_code == 200                                                                      # the first run is still readable


# ---- heatmap --------------------------------------------------------------------------------------------------------
def test_heatmap_has_one_row_per_playbook_with_stored_replay_and_validates(runs, client):
    r = client.get(f"{URL}/regime-heatmap")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    contracts.check("RegimeHeatmap", d, contracts.backfill_id("research"))
    assert [x["catalog_id"] for x in d["rows"]] == sorted(PLAYBOOKS)            # failed / factor experiments have no row
    assert [c["dimension"] for c in d["columns"]] == ["trend", "liquidity", "sentiment", "style"]
    cols = {c["dimension"]: [b["bucket"] for b in c["buckets"]] for c in d["columns"]}
    for row in d["rows"]:
        for dim in row["dimensions"]:
            assert [c["bucket"] for c in dim["cells"]] == cols[dim["dimension"]]
            assert all(c["insufficient_sample"] == (c["trade_count"] < d["min_trades"]) for c in dim["cells"])
    one = next(x for x in d["rows"] if x["catalog_id"] == "PB:kdj_golden_low")
    assert one["closed_trades"] == replay(client)["attribution"]["closed_trades"]


# ---- archive --------------------------------------------------------------------------------------------------------
def archive(client, **q):
    r = client.get(f"{URL}/experiments", params=q)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    contracts.check("ExperimentArchive", d, contracts.backfill_id("research"))
    return d


def test_archive_lists_newest_first_and_pages_without_gaps_or_repeats(runs, client):
    res, conn, _ = runs
    total = conn.execute("select count(*) n from research.experiment_run").fetchone()["n"]
    full = archive(client, limit=200)
    assert len(full["items"]) == total >= 5 and full["page"]["next_cursor"] is None
    stamps = [i["completed_at"] for i in full["items"] if i["completed_at"]]
    assert len(stamps) >= 4 and stamps == sorted(stamps, reverse=True)                              # newest first
    seen, cur = [], None
    while True:
        d = archive(client, limit=2, **({"cursor": cur} if cur else {}))
        seen += [i["experiment_id"] for i in d["items"]]
        cur = d["page"]["next_cursor"]
        if not cur:
            break
    assert seen == [i["experiment_id"] for i in full["items"]] and len(set(seen)) == len(seen)


def test_archive_filters_instance_purpose_status(runs, client):
    res, conn, _ = runs
    failed = archive(client, status="failed", limit=200)["items"]
    assert failed and all(i["run_state"] == "failed" and i["error_code"] for i in failed)
    assert any(i["catalog_id"] == "PB:value_pe_pb" and i["error_code"] == "data_not_available:fundamentals_pit" for i in failed)
    done = archive(client, status="completed", limit=200)["items"]
    assert done and all(i["run_state"] == "completed" and i["completed_at"] for i in done)
    pb = next(i for i in done if i["catalog_id"] == "PB:kdj_golden_low")
    assert pb["kind"] == "playbook" and pb["has_replay"] is True and pb["trade_count"] is not None and pb["max_drawdown"] is not None
    fl = next(i for i in done if i["catalog_id"] == "FL:momentum_20")
    assert fl["kind"] == "factor" and fl["has_replay"] is False and fl["trade_count"] is None
    mine = archive(client, instance_id=pb["instance_id"], limit=200)["items"]
    assert mine and all(i["instance_id"] == pb["instance_id"] for i in mine)
    assert archive(client, purpose="screening", limit=200)["items"]
    assert archive(client, purpose="blind_final", limit=200)["items"] == []
    assert archive(client, status="queued", limit=200)["items"] == []
    both = archive(client, purpose="screening", status="failed", limit=200)["items"]
    assert {i["experiment_id"] for i in both} == {i["experiment_id"] for i in failed}


def test_archive_rejects_bad_filters(runs, client):
    for q in ({"status": "bogus"}, {"purpose": "bogus"}, {"instance_id": "x"}, {"limit": 0}, {"limit": 201}, {"cursor": "!!"}):
        assert client.get(f"{URL}/experiments", params=q).status_code == 400, q
