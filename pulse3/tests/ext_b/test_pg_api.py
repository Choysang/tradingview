"""Real-PostgreSQL tests of the ext-B API: screening (PEX10), strategy registry (PEX11), analogs / interval statistics (PEX04),
tactic replay (PEX06) and the unified experiment path for playbooks and factors. Synthetic world: proves wiring and invariants."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.adapters.postgres import market_repo
from pulse.application import playbook_service as PS
from pulse.application import screen_service as S
from pulse.application.bindings import RESEARCH_DATASET
from pulse.domain.screening import ast as A

pytestmark = pytest.mark.pg
ASOF = "2026-11-12T00:00:00Z"
PAGE = lambda n=50, c=None: json.dumps({"limit": n, **({"cursor": c} if c else {})})  # noqa: E731
H = lambda k: {"Idempotency-Key": k}  # noqa: E731


def research_ref(conn):
    m = market_repo.latest_ready(conn, RESEARCH_DATASET)
    return market_repo.snapshot_ref(m), m


def parse(client, text):
    r = client.get("/v1/x/parse-screen-text", params={"text": text, "field_catalog_version": "screen-fields-v1", "locale": "zh-CN"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def run_body(conn, ast, key, **kw):
    ref, _ = research_ref(conn)
    return {"idempotency_key": key, "ast": ast, "universe_policy_version": "screen-universe-main-v1", "input_snapshot_refs": [ref],
            "decision_as_of": ASOF, "strategy_instances": [], **kw}


def fetch_all(client, run_id, limit=7):
    rows, cur = [], None
    while True:
        r = client.get("/v1/x/query-screen-run", params={"screen_run_id": run_id, "page": PAGE(limit, cur)})
        assert r.status_code == 200, r.text
        d = r.json()
        rows += d["data"]["rows"]
        cur = d["data"]["page"]["next_cursor"]
        if not cur:
            return rows, d


# ---- PEX10 ----------------------------------------------------------------------------------------------------------
def test_parse_endpoint_returns_flagged_fragments_and_contract_valid_ast(client):
    d = parse(client, "放量上涨 换手率大于5% 剔除ST")
    assert d["complete"] is False and d["ast"] is not None
    assert [(i["text"], i["reason_code"]) for i in d["unresolved"]] == [("换手率大于5%", "unsupported")]
    assert client.get("/v1/x/parse-screen-text", params={"text": "x", "field_catalog_version": "old", "locale": "zh-CN"}).status_code == 400
    assert client.get("/v1/x/parse-screen-text", params={"text": "x", "field_catalog_version": "screen-fields-v1", "locale": "zh-CN", "sql": "1"}).status_code == 400


def test_run_screen_freezes_a_scan_and_matches_direct_evaluation(client, conn, lake, world):
    ast = parse(client, "涨幅大于0 近5日涨幅大于-100% 剔除ST")["ast"]
    r = client.post("/v1/x/run-screen", json=run_body(conn, ast, "k-run-1"), headers=H("k-run-1"))
    assert r.status_code == 200, r.text
    rec = r.json()["data"]["receipt"]
    assert rec["poll_operation_id"] == "queryScreenRun"
    rows, last = fetch_all(client, rec["resource_id"])
    # independent evaluation of the same AST on the same PIT panel
    asof = datetime.fromisoformat(ASOF.replace("Z", "+00:00"))
    panel = S.load_panel(conn, lake, asof, board="main")
    exp_true, exp_unknown = [], []
    for v in panel.views:
        res = A.evaluate_row(ast, S.field_row(panel, v), asof)
        (exp_true if res.value.value == "TRUE" else exp_unknown if res.value.value == "UNKNOWN" else []).append(v.instrument_id)
    got_true = [x["instrument_id"] for x in rows if x["condition_value"] == "true"]
    got_unknown = [x["instrument_id"] for x in rows if x["condition_value"] == "unknown"]
    assert sorted(got_true) == sorted(exp_true) and sorted(got_unknown) == sorted(exp_unknown)
    assert [x["sort_position"] for x in rows] == list(range(1, len(rows) + 1))
    assert len(exp_true) > 0
    cov = last["meta"]["coverage"]
    assert cov["observed_count"] == len(exp_true) and cov["expected_count"] == len(panel.views) and cov["missing_count"] == len(exp_unknown)
    # the supporting values are real, typed features whose key passes the 19-dimension contract
    f = rows[0]["features"][0]
    contracts.check("FeatureKey", f["result"]["key"])
    assert f["result"]["artifact"]["ref"].startswith("screen-value://") and "value=" in f["result"]["artifact"]["ref"]
    # sorted by pct_chg desc among TRUE rows (default sort), tie-break by instrument id
    pct = [float(next(c["result"]["artifact"]["ref"].split("value=")[1].split("&")[0] for c in x["features"] if c["feature_id"] == "pct_chg")) for x in rows
           if x["condition_value"] == "true"]
    assert pct == sorted(pct, reverse=True)


def test_missing_history_gives_unknown_rows_not_false_and_not_zero(client, conn):
    ast = parse(client, "250日涨幅大于0%")["ast"]               # 130 sessions: the field cannot be computed for anybody
    r = client.post("/v1/x/run-screen", json=run_body(conn, ast, "k-unk"), headers=H("k-unk"))
    rid = r.json()["data"]["receipt"]["resource_id"]
    rows, last = fetch_all(client, rid, 40)
    assert rows and all(x["condition_value"] == "unknown" for x in rows)
    assert last["meta"]["coverage"]["observed_count"] == 0 and last["meta"]["coverage"]["missing_count"] == len(rows)
    assert all("missing=" in x["features"][0]["result"]["artifact"]["ref"] for x in rows[:5])
    run = conn.execute("select capability_gaps, true_count, unknown_count from research.screen_run where screen_run_id=%s", (rid,)).fetchone()
    assert run["true_count"] == 0 and any(g["reason_code"] == "field_unknown_for_every_instrument" for g in run["capability_gaps"])


def test_run_screen_is_idempotent_and_detects_changed_payload_and_stale_input(client, conn):
    ast = parse(client, "涨幅大于0")["ast"]
    body = run_body(conn, ast, "k-idem")
    a = client.post("/v1/x/run-screen", json=body, headers=H("k-idem")).json()["data"]
    b = client.post("/v1/x/run-screen", json=body, headers=H("k-idem")).json()["data"]
    assert a == b
    other = run_body(conn, parse(client, "涨幅大于1")["ast"], "k-idem")
    r = client.post("/v1/x/run-screen", json=other, headers=H("k-idem"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    stale = run_body(conn, ast, "k-stale")
    stale["input_snapshot_refs"][0]["content_hash"] = "0" * 64
    r = client.post("/v1/x/run-screen", json=stale, headers=H("k-stale"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_INPUT"
    assert client.post("/v1/x/run-screen", json=body, headers=H("different")).status_code == 400          # header must equal the body key
    bad = run_body(conn, {**ast, "allowed_field_catalog_version": "old"}, "k-cat")
    assert client.post("/v1/x/run-screen", json=bad, headers=H("k-cat")).json()["error"]["code"] == "SCREEN_CATALOG_MISMATCH"


def test_run_is_owner_scoped_cursor_bound_and_csv_exports_whole_run(client, conn):
    ast = parse(client, "涨幅大于-100%")["ast"]
    rid = client.post("/v1/x/run-screen", json=run_body(conn, ast, "k-own"), headers=H("k-own")).json()["data"]["receipt"]["resource_id"]
    other = uuid.UUID(int=999)
    with pytest.raises(S.ScreenError) as e:
        S.get_run(conn, other, uuid.UUID(rid))
    assert e.value.status == 404                                                    # no existence leak across owners
    r = client.get("/v1/x/query-screen-run", params={"screen_run_id": rid, "page": PAGE(3)})
    cur = r.json()["data"]["page"]["next_cursor"]
    assert cur and r.json()["data"]["page"]["snapshot_hash"]
    bad = json.dumps({**json.loads(cur), "hash": "f" * 64})
    r = client.get("/v1/x/query-screen-run", params={"screen_run_id": rid, "page": PAGE(3, bad)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CURSOR_EXPIRED"
    total = conn.execute("select count(*) n from research.screen_run_row where screen_run_id=%s", (rid,)).fetchone()["n"]
    csv_r = client.get("/v1/x/b/screen-run-csv", params={"screen_run_id": rid})
    assert csv_r.status_code == 200 and csv_r.content.startswith(b"\xef\xbb\xbf")
    lines = csv_r.content.decode("utf-8-sig").strip().splitlines()
    assert len(lines) == total + 1 and lines[0].startswith("序号,代码,名称,条件结果")
    assert client.get("/v1/x/query-screen-run", params={"screen_run_id": str(uuid.uuid4()), "page": PAGE()}).status_code == 404


def test_closed_run_rows_are_immutable_in_the_database(conn, client):
    ast = parse(client, "涨幅大于-100%")["ast"]
    rid = client.post("/v1/x/run-screen", json=run_body(conn, ast, "k-imm"), headers=H("k-imm")).json()["data"]["receipt"]["resource_id"]
    import psycopg
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update research.screen_run_row set condition_value='false' where screen_run_id=%s and sort_position=1", (rid,))
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update research.screen_run set true_count=0 where screen_run_id=%s", (rid,))


def test_save_screen_cas_owner_and_listing(client, conn):
    ast = parse(client, "首板")["ast"]
    sid = str(uuid.uuid4())
    body = {"idempotency_key": "s1", "screen_id": sid, "expected_screen_version": 0, "display_name": "我的首板", "ast": ast,
            "universe_policy_version": "screen-universe-main-v1"}
    r = client.post("/v1/x/save-screen", json=body, headers=H("s1"))
    assert r.status_code == 200 and r.json()["data"]["screen"]["version"] == 1
    again = client.post("/v1/x/save-screen", json=body, headers=H("s1")).json()["data"]
    assert again == r.json()["data"]                                                # replay returns the original answer, no second version
    body2 = {**body, "idempotency_key": "s2", "expected_screen_version": 1, "display_name": "改名"}
    assert client.post("/v1/x/save-screen", json=body2, headers=H("s2")).json()["data"]["screen"]["version"] == 2
    stale = {**body, "idempotency_key": "s3", "expected_screen_version": 1}
    r = client.post("/v1/x/save-screen", json=stale, headers=H("s3"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"
    create_again = {**body, "idempotency_key": "s4"}
    assert client.post("/v1/x/save-screen", json=create_again, headers=H("s4")).status_code == 409   # expected 0 only creates
    with pytest.raises(S.ScreenError) as e:                                                          # another principal cannot overwrite
        S.save_screen(conn, uuid.UUID(int=555), {**body, "expected_screen_version": 2}, datetime.now(timezone.utc))
    assert e.value.status == 403
    r = client.get("/v1/x/list-saved-screens", params={"page": PAGE(10)})
    mine = [s for s in r.json()["data"]["screens"] if s["screen_id"] == sid]
    assert len(mine) == 1 and mine[0]["screen_version"] == 2 and mine[0]["display_name"] == "改名"      # restorable from the server, latest only
    bad_ast = {**body, "idempotency_key": "s5", "screen_id": str(uuid.uuid4()), "ast": {**ast, "allowed_field_catalog_version": "x"}}
    assert client.post("/v1/x/save-screen", json=bad_ast, headers=H("s5")).status_code == 400
    r1 = client.get("/v1/x/list-saved-screens", params={"page": PAGE(1)})
    if r1.json()["data"]["page"]["next_cursor"]:
        bad = json.dumps({**json.loads(r1.json()["data"]["page"]["next_cursor"]), "snap": "0" * 64})
        assert client.get("/v1/x/list-saved-screens", params={"page": PAGE(1, bad)}).status_code == 409


def test_screen_can_use_a_research_strategy_signal_without_changing_anything_else(client, conn):
    PS.register_all(conn)
    inst = conn.execute("select i.instance_id, i.strategy_id, i.strategy_version from strategy.instance i join strategy.definition d using (strategy_id, strategy_version)"
                        " where d.specification->>'catalog_id'='PB:kdj_golden_low'").fetchone()
    ast = A.make_ast({"node": "compare", "clock": A.CLOCK, "operator": "eq",
                      "left": {**A.operand("close"), "feature_id": "signal.kdj_golden_low", "unit": "flag"}, "right": A.C.constant(D(1), "flag")})
    ref = {"instance_id": str(inst["instance_id"]), "strategy_id": str(inst["strategy_id"]), "strategy_version": inst["strategy_version"]}
    body = run_body(conn, ast, "k-sig", strategy_instances=[ref])
    r = client.post("/v1/x/run-screen", json=body, headers=H("k-sig"))
    assert r.status_code == 200, r.text
    rid = r.json()["data"]["receipt"]["resource_id"]
    run = conn.execute("select state, error_code from research.screen_run where screen_run_id=%s", (rid,)).fetchone()
    assert run["state"] == "ready", run
    lc = conn.execute("select lifecycle from strategy.instance where instance_id=%s", (inst["instance_id"],)).fetchone()["lifecycle"]
    assert lc == "specified"                                                         # screening never promotes
    # without the instance the same field is not allowed
    no = client.post("/v1/x/run-screen", json=run_body(conn, ast, "k-sig2"), headers=H("k-sig2"))
    assert no.status_code == 400


# ---- PEX11 --------------------------------------------------------------------------------------------------------
def test_register_all_is_idempotent_52_playbooks_6_factors_none_active(conn):
    a = PS.register_all(conn)
    b = PS.register_all(conn)
    assert all(v == 0 for grp in b.values() for v in grp.values())
    n = conn.execute("select count(*) n from strategy.definition where specification ? 'playbook'").fetchone()["n"]
    assert n == 52
    assert conn.execute("select count(*) n from strategy.definition where specification ? 'factor'").fetchone()["n"] == 6
    life = {r["lifecycle"] for r in conn.execute("select distinct i.lifecycle from strategy.instance i join strategy.definition d using (strategy_id, strategy_version)"
                                                  " where d.specification ? 'playbook' or d.specification ? 'factor' or d.specification ? 'tactic'")}
    assert life == {"specified"}
    tags = conn.execute("select count(*) n from strategy.tag_assignment where taxonomy_id='strategy.evidence_label' and node_id='unverified'").fetchone()["n"]
    assert tags >= 52 + 6
    assert conn.execute("select count(*) n from research.promotion_decision").fetchone()["n"] == 0
    assert a is not None


def _draft(name, art_hash="a" * 64):
    return {"display_name": name, "role": "selection", "method_family": "rule", "forecast_target": "t", "horizon_id": "20td",
            "specification_contract_id": "pulse.strategy_dsl@0.1", "specification_schema_version": "0.1",
            "specification_artifact": {"ref": f"artifact:{name}", "content_hash": art_hash, "contract_id": "pulse.strategy_dsl", "schema_version": "0.1"},
            "parent_instances": []}


def test_register_strategy_draft_versions_cas_duplicates_and_visibility(client, conn):
    sid = str(uuid.uuid4())
    body = {"idempotency_key": "d1", "strategy_id": sid, "expected_latest_version": 0, "draft": _draft("我的规则", "b" * 64)}
    r = client.post("/v1/x/register-strategy-draft", json=body, headers=H("d1"))
    assert r.status_code == 200, r.text
    st = r.json()["data"]["strategy"]
    assert st["strategy_version"] == 1 and st["instances"][0]["lifecycle"] == "specified" and st["instances"][0]["implementation_status"] == "not_implemented"
    assert st["specification"]["registration"]["production_permission"] is False
    assert client.post("/v1/x/register-strategy-draft", json=body, headers=H("d1")).json()["data"] == r.json()["data"]
    v2 = {**body, "idempotency_key": "d2", "expected_latest_version": 1, "draft": _draft("我的规则v2", "c" * 64)}
    assert client.post("/v1/x/register-strategy-draft", json=v2, headers=H("d2")).json()["data"]["strategy"]["strategy_version"] == 2
    conflict = {**v2, "idempotency_key": "d3"}
    r = client.post("/v1/x/register-strategy-draft", json=conflict, headers=H("d3"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"
    dup = {"idempotency_key": "d4", "strategy_id": str(uuid.uuid4()), "expected_latest_version": 0, "draft": _draft("同一公式", "b" * 64)}
    r = client.post("/v1/x/register-strategy-draft", json=dup, headers=H("d4"))
    assert [w["code"] for w in r.json()["meta"]["warnings"]] == ["DUPLICATE_FORMULA"]
    q = client.get("/v1/x/query-strategy-version", params={"strategy_id": sid, "strategy_version": 1})
    assert q.status_code == 200 and q.json()["data"]["strategy"]["strategy_version"] == 1 and q.json()["data"]["evaluation_ids"] == []
    assert client.get("/v1/x/query-strategy-version", params={"strategy_id": sid, "strategy_version": 9}).status_code == 404
    with pytest.raises(PS.StrategyError) as e:                      # another principal cannot see or revise a private draft
        PS.query_strategy_version(conn, uuid.UUID(int=4242), uuid.UUID(sid), 1)
    assert e.value.status == 404
    parent = r.json()["data"]["strategy"]["instances"][0]
    child = {"idempotency_key": "d5", "strategy_id": str(uuid.uuid4()), "expected_latest_version": 0,
             "draft": {**_draft("组合", "d" * 64), "parent_instances": [{"instance_id": parent["instance_id"], "strategy_id": parent["strategy_id"], "strategy_version": 1}]}}
    assert client.post("/v1/x/register-strategy-draft", json=child, headers=H("d5")).status_code == 200
    assert conn.execute("select count(*) n from research.strategy_parent where strategy_id=%s", (child["strategy_id"],)).fetchone()["n"] == 1


# ---- unified experiment path -----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def experiments(world):
    from pulse.adapters.lake import LakeStore
    from pulse.adapters.postgres import db
    from pulse.workers import research as W

    c = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    W.ext_b_register(c)
    out = W.ext_b_run_catalog(c, lake, ["PB:value_pe_pb", "PB:kdj_golden_low", "FL:momentum_20", "PB:industry_follower", "PB:small_cap"], key="t1")
    yield {r["catalog_id"]: r["result"] for r in out}, c, lake
    c.close()


def metrics_of(conn, experiment_id):
    return {(m["metric_name"], m["slice_key"]): m for m in conn.execute("select * from research.metric_value where experiment_id=%s", (experiment_id,)).fetchall()}


def test_missing_data_playbooks_are_recorded_failed_trials_with_the_reason(experiments):
    res, conn, _ = experiments
    for cid, code in (("PB:value_pe_pb", "data_not_available:fundamentals_pit"), ("PB:industry_follower", "data_not_available:sector_membership_history"),
                      ("PB:small_cap", "data_not_available:float_cap_history_not_point_in_time")):
        assert res[cid]["state"] == "failed" and res[cid]["error"] == code
        row = conn.execute("select run_state, error_code from research.experiment_run where experiment_id=%s", (res[cid]["experiment_id"],)).fetchone()
        assert row["run_state"] == "failed" and row["error_code"] == code
        assert conn.execute("select count(*) n from research.metric_value where experiment_id=%s", (res[cid]["experiment_id"],)).fetchone()["n"] == 0


def test_playbook_screening_goes_through_context_kernel_costs_and_never_touches_blind(experiments):
    res, conn, lake = experiments
    r = res["PB:kdj_golden_low"]
    assert r["state"] == "completed", r
    m = metrics_of(conn, r["experiment_id"])
    for key in (("net_return", "train"), ("net_return", "validation"), ("net_return_cost_x2", "overall"), ("net_return_cost_x3", "overall"),
                ("benchmark_return_csi300", "validation"), ("trade_count", "overall"), ("fees_paid", "overall"), ("max_drawdown", "overall")):
        assert key in m, key
    assert m[("trade_count", "overall")]["value"] > 0 and m[("fees_paid", "overall")]["value"] > 0
    assert all(x["quality_status"] in ("valid", "partial") for x in m.values())
    assert m[("net_return", "overall")]["quality_status"] == "partial"                 # forward-adjusted research prices are labelled
    run = conn.execute("select e.*, c.validation_end, c.blind_start from research.experiment_run e join research.evaluation_context c using (evaluation_context_id)"
                       " where experiment_id=%s", (r["experiment_id"],)).fetchone()
    assert run["run_purpose"] == "screening" and run["blind_start"] >= run["validation_end"]
    ev = conn.execute("select count(*) n from research.experiment_run where run_purpose='blind_final'").fetchone()["n"]
    assert ev == 0                                                                      # no blind access happened
    # the same experiment through the public getEvaluation contract
    life = conn.execute("select i.lifecycle from strategy.instance i join research.experiment_run e using (instance_id) where e.experiment_id=%s", (r["experiment_id"],)).fetchone()
    assert life["lifecycle"] == "specified"                                              # screening earns no status by itself


def test_factor_lab_metrics_are_stored_per_slice_and_horizon(experiments):
    res, conn, _ = experiments
    r = res["FL:momentum_20"]
    assert r["state"] == "completed", r
    m = metrics_of(conn, r["experiment_id"])
    for h in (5, 20):
        for sl in ("train", "validation"):
            for name in ("rank_ic_mean", "icir", "ic_t_stat", "long_short_return", "quantile_return_q1", "quantile_return_q5", "ic_days"):
                assert (f"{name}_h{h}", sl) in m, (name, h, sl)
    assert m[("ic_days_h5", "train")]["value"] > 0
    miss = [x for x in m.values() if x["value"] is None]
    assert all(x["missing_reason"] for x in miss)                                       # missing is explained, never 0


def test_get_evaluation_serves_the_experiment_with_the_v3_contract(experiments, client):
    res, conn, _ = experiments
    r = client.get(f"/v1/experiments/{res['PB:kdj_golden_low']['experiment_id']}")
    assert r.status_code == 200 and r.json()["data"]["run_state"] == "completed" and len(r.json()["data"]["metrics"]) >= 20


# ---- PEX06 ----------------------------------------------------------------------------------------------------------
def replay_body(conn, key, instance_code, **over):
    ctx = conn.execute("select * from research.evaluation_context order by created_at limit 1").fetchone()
    hyp = conn.execute("select hypothesis_id, hypothesis_version from research.hypothesis limit 1").fetchone()
    inst = conn.execute("select i.instance_id from strategy.instance i join strategy.definition d using (strategy_id, strategy_version)"
                        " where d.specification->>'catalog_id'=%s order by i.created_at limit 1", (instance_code,)).fetchone()
    ids = [str(r["instrument_id"]) for r in conn.execute(
        "select a.instrument_id from core.instrument_alias a where a.source_id='legacy_warehouse' and a.source_symbol in ('600001','600002') order by 1")]
    start = ctx["train_start"].astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    from datetime import timedelta
    spec = {"instrument_ids": ids, "window": {"start": (start + timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                              "end": (ctx["train_end"] - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "cash_cny": "200000", "opening_lots": [{"instrument_id": i, "quantity": 5000, "eligible_on": "2020-01-01", "cost_cny": "50000"} for i in ids],
            "execution_policy_version": "daily-bar-ohlc-path-v1", "cost_policy_version": "user-account-2026-11-08-v1", "benchmark_policy_version": "same-inventory-hold-v1",
            "training_mode": "frozen", "random_seed": 11}
    spec.update(over.pop("spec", {}))
    return {"idempotency_key": key, "instance_id": str(inst["instance_id"]), "hypothesis_id": str(hyp["hypothesis_id"]),
            "hypothesis_version": hyp["hypothesis_version"], "evaluation_context_id": str(ctx["evaluation_context_id"]), "replay_spec": spec, **over}


def test_tactic_replay_exit_rules_vs_hold_baseline_with_full_family(client, experiments):
    _, conn, lake = experiments
    body = replay_body(conn, "tr1", "EX:stop_tight")
    r = client.post("/v1/x/schedule-tactic-replay", json=body, headers=H("tr1"))
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["context_contract_version"] == "3.0.0" and d["receipt"]["poll_operation_id"] == "getEvaluation"
    assert client.post("/v1/x/schedule-tactic-replay", json=body, headers=H("tr1")).json()["data"] == d      # idempotent
    changed = replay_body(conn, "tr1", "EX:stop_tight", spec={"random_seed": 12})
    assert client.post("/v1/x/schedule-tactic-replay", json=changed, headers=H("tr1")).status_code == 409
    exp_id = d["receipt"]["resource_id"]
    assert client.get(f"/v1/experiments/{exp_id}").json()["data"]["run_state"] == "queued"
    out = PS.run_tactic_replay(conn, lake)
    assert out["state"] == "completed", out
    m = metrics_of(conn, exp_id)
    slices = {k[1] for k in m}
    assert "replay" in slices and {f"variant:{k}" for k in ("default", "stop_tight", "stop_wide", "below_ma5", "scale_half")} <= slices   # ALL variants stored
    c = m[("incremental_pnl_vs_hold", "replay")]["value"]
    assert c == m[("active_nav", "replay")]["value"] - m[("baseline_nav", "replay")]["value"]
    assert m[("cash_delta", "replay")]["value"] + m[("inventory_value_delta", "replay")]["value"] == c      # exact bridge
    ev = client.get(f"/v1/experiments/{exp_id}").json()["data"]
    assert ev["run_state"] == "completed" and ev["context"]["evaluation_context_id"] == body["evaluation_context_id"]
    stored = conn.execute("select run_manifest from research.experiment_run where experiment_id=%s", (exp_id,)).fetchone()["run_manifest"]
    assert stored["replay_spec_hash"] and stored["replay_spec"]["cash_cny"] == "200000"          # D06 registered in run_manifest


def test_tactic_replay_intraday_t_reports_path_sensitivity(client, experiments):
    _, conn, lake = experiments
    body = replay_body(conn, "tr2", "TT:reverse")
    d = client.post("/v1/x/schedule-tactic-replay", json=body, headers=H("tr2")).json()["data"]
    out = PS.run_tactic_replay(conn, lake)
    assert out["state"] == "completed", out
    m = metrics_of(conn, d["receipt"]["resource_id"])
    assert ("incremental_pnl_vs_hold", "path_ohlc") in m and ("incremental_pnl_vs_hold", "path_olhc") in m and ("path_sensitivity", "path_spread") in m
    assert any(k[1].startswith("param:") for k in m)                                             # the whole parameter grid is stored


def test_tactic_replay_rejections(client, experiments):
    _, conn, _ = experiments
    r = client.post("/v1/x/schedule-tactic-replay", json=replay_body(conn, "tr3", "EX:stop_tight", spec={"training_mode": "no_fit"}), headers=H("tr3"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    ctx = conn.execute("select validation_end from research.evaluation_context order by created_at limit 1").fetchone()
    late = {"window": {"start": "2026-09-01T00:00:00Z", "end": (ctx["validation_end"].astimezone(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    r = client.post("/v1/x/schedule-tactic-replay", json=replay_body(conn, "tr4", "EX:stop_tight", spec=late), headers=H("tr4"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"             # blind period never mixed in
    r = client.post("/v1/x/schedule-tactic-replay", json=replay_body(conn, "tr5", "EX:stop_tight", spec={"cost_policy_version": "made-up"}), headers=H("tr5"))
    assert r.status_code == 400
    r = client.post("/v1/x/schedule-tactic-replay", json=replay_body(conn, "tr6", "PB:kdj_golden_low"), headers=H("tr6"))
    assert r.status_code == 409                                                                  # a playbook is not a tactic instance


# ---- PEX04 ----------------------------------------------------------------------------------------------------------
def test_analog_study_is_built_by_a_job_and_queries_never_build(client, conn, lake, built):
    from pulse.application.data_service import DataService

    rel = conn.execute("select release_id from decision.release where release_state in ('READY','SUPERSEDED') order by published_at desc limit 1").fetchone()["release_id"]
    iid = conn.execute("select a.instrument_id from core.instrument_alias a where a.source_id='legacy_warehouse' and a.source_symbol='600010'").fetchone()["instrument_id"]
    key = client.get("/v1/x/b/analog-key", params={"instrument_id": str(iid), "release_id": str(rel)}).json()["feature_key_hash"]
    pol = "analog-knn-matured-v1:h10"
    q = {"instrument_id": str(iid), "release_id": str(rel), "feature_key_hash": key, "selection_policy_version": pol, "page": PAGE(25)}
    r = client.get("/v1/x/query-analog-cases", params=q)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESULT_NOT_READY"                 # the query did not build anything
    man = PS.build_analog_study(DataService(conn, lake), iid, rel, pol)
    again = PS.build_analog_study(DataService(conn, lake), iid, rel, pol)
    assert again["snapshot_id"] == man["snapshot_id"]                                                 # identity => one study
    r = client.get("/v1/x/query-analog-cases", params=q)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["study"]["contract_id"] == "pulse.analog_study" and 0 < len(d["neighbors"]) <= 25
    n0 = d["neighbors"][0]
    assert n0["outcome"] is None or n0["outcome"]["name"] == "forward_return_10d"
    assert all(n["outcome_end"] <= r.json()["meta"]["decision_as_of"] or n["outcome"] is None for n in d["neighbors"])
    assert [n["distance"] for n in d["neighbors"]] == sorted(n["distance"] for n in d["neighbors"])
    seen = {(n["instrument_id"], n["case_as_of"]) for n in d["neighbors"]}
    cur = d["page"]["next_cursor"]
    if cur:
        r2 = client.get("/v1/x/query-analog-cases", params={**q, "page": PAGE(25, cur)})
        assert r2.status_code == 200 and not seen & {(n["instrument_id"], n["case_as_of"]) for n in r2.json()["data"]["neighbors"]}
    wrong = client.get("/v1/x/query-analog-cases", params={**q, "feature_key_hash": "1" * 64})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "STALE_INPUT"
    assert client.get("/v1/x/query-analog-cases", params={**q, "selection_policy_version": "pick-best-outcome"}).status_code == 400


def test_interval_statistics_match_hand_calculation_and_refuse_implicit_adjustment(client, conn, lake):
    import polars as pl

    ref, man = research_ref(conn)
    df = lake.read_snapshot_frames(man["content_hash"])
    iid = conn.execute("select a.instrument_id from core.instrument_alias a where a.source_id='legacy_warehouse' and a.source_symbol='600020'").fetchone()["instrument_id"]
    g = df.filter(pl.col("instrument_id") == str(iid)).sort("revision").unique(["instrument_id", "trade_date"], keep="last").sort("trade_date")
    rows = g.select("trade_date", "close_price").rows()
    start_d, end_d = rows[40][0], rows[80][0]
    params = {"instrument_id": str(iid), "snapshot_id": ref["snapshot_id"], "adjustment_mode": "forward_adjusted", "metric_policy_version": "interval-stats-v1",
              "window": json.dumps({"start": start_d.isoformat() + "T00:00:00Z", "end": end_d.isoformat() + "T00:00:00Z"})}
    r = client.get("/v1/x/query-interval-statistics", params=params)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["exploratory"] is True and d["input_snapshot"]["snapshot_id"] == ref["snapshot_id"]
    m = {x["name"]: x for x in d["metrics"]}
    inside = [c for dt, c in rows if start_d <= dt < end_d]
    base = [c for dt, c in rows if dt < start_d][-1]
    expect = D(str(inside[-1])) / D(str(base)) - 1
    assert abs(D(m["period_return"]["value"]) - expect) < D("1e-9") and m["sessions"]["value"] == str(len(inside))
    wrong = client.get("/v1/x/query-interval-statistics", params={**params, "adjustment_mode": "raw"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "UNSUPPORTED_CONTEXT_SHAPE"
    wrong2 = client.get("/v1/x/query-interval-statistics", params={**params, "metric_policy_version": "mine"})
    assert wrong2.status_code == 400
