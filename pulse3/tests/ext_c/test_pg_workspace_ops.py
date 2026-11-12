"""PEX14 / PEX15 on real PostgreSQL: workspace revisions and tombstones, index, exports, registered jobs, dataset status, job runs."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse.adapters.postgres._canon import canonical_json, sha256_of
from pulse.adapters.postgres.ui_repo import PgUiRepo
from pulse.application import alert_service as AS
from pulse.application import ops_service, workspace_service as WS
from pulse.contracts import utc_z

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc


def layout(conn, principal, name="布局", doc=None):
    lid = uuid.uuid4()
    PgUiRepo(conn).save_layout(principal, lid, {"expected_layout_version": 0, "display_name": name, "layout_document": doc or {"panels": []}})
    return lid


def test_delete_tombstone_replay_conflict_owner_and_no_resurrection(make_api, conn, principal):
    api = make_api(principal)
    lid = layout(conn, principal)
    PgUiRepo(conn).save_layout(principal, lid, {"expected_layout_version": 1, "display_name": "布局2", "layout_document": {"panels": [1]}})
    body = {"object_kind": "layout", "object_id": str(lid), "expected_version": 1}
    assert api.post("delete-workspace-item", body, key="d0").json()["error"]["code"] == "VERSION_CONFLICT"            # another device already changed it to v2
    body["expected_version"] = 2
    r = api.post("delete-workspace-item", body, key="d1")
    assert r.status_code == 200, r.text
    tomb = r.json()["data"]["tombstone"]
    assert tomb["version"] == 3 and tomb["object_id"] == str(lid)
    assert api.post("delete-workspace-item", body, key="d1").json()["data"]["tombstone"] == tomb                      # same key: the original receipt
    assert api.post("delete-workspace-item", body, key="d2").json()["data"]["tombstone"] == tomb                      # another key, same delete: replayed, not re-applied
    assert conn.execute("SELECT count(*) n FROM ui.workspace_revision WHERE object_id=%s AND operation='delete'", (lid,)).fetchone()["n"] == 1
    with pytest.raises(Exception):                                                                                    # the id cannot come back
        PgUiRepo(conn).save_layout(principal, lid, {"expected_layout_version": 0, "display_name": "x", "layout_document": {}})
    # both sealed versions are readable; the tombstone is not an empty body
    rv = lambda v: api.get("query-workspace-revision", object_kind="layout", object_id=lid, object_version=v)           # noqa: E731
    v2 = rv(2).json()["data"]
    assert v2["deleted"] is False and v2["body"]["body_contract_id"] == "pulse.workspace.layout"
    old = api.c.get("/v1/x/c/artifact", params={"ref": v2["body"]["body_artifact"]["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    assert old["display_name"] == "布局2"
    assert rv(3).json()["data"] == {"body": None, "deleted": True}
    assert rv(1).status_code == 404                                                                                   # v1 was never sealed (no history before this feature): not faked
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.post("delete-workspace-item", {**body, "object_id": str(layout(conn, principal))}, key="d9").status_code == 404
    assert api2.get("query-workspace-revision", object_kind="layout", object_id=lid, object_version=2).status_code == 404
    assert api.post("delete-workspace-item", {**body, "object_kind": "everything"}, key="d8").status_code == 400


def test_deleting_an_annotation_stops_its_alert_rules_but_keeps_them(make_api, conn, principal):
    api = make_api(principal)
    aid = uuid.uuid4()
    PgUiRepo(conn).save_annotation(principal, aid, {"expected_annotation_version": 0, "instrument_id": None, "watchlist_id": None, "annotation_kind": "drawing",
                                                    "anchor_release_id": None, "anchor_snapshot_id": None, "body": {"line": [1, 2]}})
    inbox = next(d["destination_id"] for d in AS.ensure_destinations(conn, principal) if d["channel"] == "in_app")
    rid = uuid.uuid4()
    body = {"scope": {"scope_kind": "news_tier", "scope_key": "P0", "membership_snapshot_id": None},
            "condition": {"node": "compare", "clock": {"kind": "event_available"}, "operator": "eq", "left": {"kind": "constant", "value": "1", "unit": "count"}, "right": {"kind": "constant", "value": "1", "unit": "count"}},
            "policy_version": "alert-rule-v1", "annotation_id": str(aid), "annotation_version": 1, "destination_ids": [str(inbox)], "enabled": True}
    AS.save_rule(conn, principal, {"rule_id": rid, "expected_rule_version": 0, "body": body})
    # a pin on an annotation version that is not the current one is refused (the line moved)
    with pytest.raises(AS.AlertError) as e:
        AS.save_rule(conn, principal, {"rule_id": uuid.uuid4(), "expected_rule_version": 0, "body": {**body, "annotation_version": 7}})
    assert e.value.code == "VERSION_CONFLICT"
    assert api.post("delete-workspace-item", {"object_kind": "annotation", "object_id": str(aid), "expected_version": 1}, key="a1").status_code == 200
    cur = AS.current_rules(conn, principal)[0]
    assert cur["rule_version"] == 2 and cur["body"]["enabled"] is False and cur["body"]["annotation_id"] == str(aid)         # pin kept, no new triggers
    assert conn.execute("SELECT count(*) n FROM notification.alert_rule WHERE rule_id=%s", (rid,)).fetchone()["n"] == 2
    # the pinned old version of the drawing is still locatable
    rv = api.get("query-workspace-revision", object_kind="annotation", object_id=aid, object_version=1).json()["data"]
    assert rv["deleted"] is False and rv["body"]["body_contract_id"] == "pulse.workspace.annotation"


def test_workspace_index_lists_the_six_kinds_by_owner(make_api, conn, principal):
    api = make_api(principal)
    layout(conn, principal, "普通布局")
    layout(conn, principal, "图表态", {"body_type": "chart_state", "symbol": "x"})
    wl = uuid.uuid4()
    PgUiRepo(conn).save_watchlist(principal, wl, {"expected_watchlist_version": 0, "display_name": "自选", "description": None, "members": []})
    for kind in ("drawing", "note", "review"):
        PgUiRepo(conn).save_annotation(principal, uuid.uuid4(), {"expected_annotation_version": 0, "instrument_id": None, "watchlist_id": None, "annotation_kind": kind,
                                                                 "anchor_release_id": None, "anchor_snapshot_id": None, "body": {"k": kind}})
    got = {}
    for kind in ("watchlists", "layouts", "chart_states", "drawings", "notes", "reviews"):
        r = api.get("query-workspace-index", kind=kind, page={"limit": 10})
        assert r.status_code == 200, (kind, r.text)
        got[kind] = r.json()["data"]
    assert [w["display_name"] for w in got["watchlists"]["watchlists"]] == ["自选"] and got["watchlists"]["layouts"] == []
    assert [l["display_name"] for l in got["layouts"]["layouts"]] == ["普通布局"] and [l["display_name"] for l in got["chart_states"]["layouts"]] == ["图表态"]
    assert [a["annotation_kind"] for a in got["drawings"]["annotations"]] == ["drawing"] and [a["annotation_kind"] for a in got["reviews"]["annotations"]] == ["review"]
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.get("query-workspace-index", kind="layouts", page={"limit": 10}).json()["data"]["layouts"] == []
    assert api.get("query-workspace-index", kind="stuff", page={"limit": 10}).status_code == 400


def export_world(conn):
    iid = S.seed_instrument(conn, "600911", "导出股")
    snap = S.seed_manifest(conn)
    S.seed_bars(conn, iid, snap, S.weekdays(date(2026, 10, 1), 10), base=Decimal("10"), step=Decimal("0.5"))
    m = conn.execute("SELECT manifest_version, content_hash FROM market.dataset_manifest WHERE snapshot_id=%s", (snap,)).fetchone()
    return iid, {"snapshot_id": str(snap), "dataset_id": "bars_raw_1d", "manifest_version": m["manifest_version"], "content_hash": m["content_hash"]}


def drain(conn):
    while ops_service.run_next(conn) is not None:
        pass


def test_export_is_async_pinned_to_inputs_and_png_carries_its_signature(make_api, conn, principal):
    api = make_api(principal)
    iid, ref = export_world(conn)
    lid = layout(conn, principal)
    WS.seal_current(conn, principal, "layout", lid)
    art = conn.execute("SELECT a.content_hash FROM ui.workspace_revision r JOIN ops.ext_artifact a ON a.ref=r.body_artifact WHERE r.object_id=%s", (lid,)).fetchone()["content_hash"]
    fields = [{"dataset_id": "bars_raw_1d", "field_names": ["trade_date", "instrument_id", "close_price_raw"], "contract_id": "c", "schema_version": "1"}]
    req = {"input_snapshot_refs": [ref], "field_projection": fields, "format": "csv", "workspace_objects": []}
    r = api.post("schedule-workspace-export", req, key="e1")
    assert r.status_code == 202, r.text
    rc = r.json()["data"]["receipt"]
    assert rc["poll_operation_id"] == "queryExportArtifact" and rc["run_state"] == "queued"
    again = api.post("schedule-workspace-export", req, key="e2").json()["data"]["receipt"]
    assert again["resource_id"] == rc["resource_id"]                                                                 # the same spec is the same export
    q = api.get("query-export-artifact", export_id=rc["resource_id"]).json()["data"]
    assert q["artifact"] is None and q["job"]["run_state"] == "queued"                                               # not done: no artifact
    drain(conn)
    q = api.get("query-export-artifact", export_id=rc["resource_id"]).json()["data"]
    assert q["job"]["run_state"] == "succeeded" and q["artifact"]["contract_id"] == "pulse.ext_c.export_output"
    out = api.c.get("/v1/x/c/artifact", params={"ref": q["artifact"]["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    text = base64.b64decode(out["data_b64"]).decode("utf-8").splitlines()
    assert text[0] == "trade_date,instrument_id,close_price_raw" and len(text) == 11 and out["meta"]["adjustment"] == "raw"
    # png: fixed layout version + metadata in text chunks
    png = api.post("schedule-workspace-export", {**req, "format": "png", "workspace_objects": [{"object_id": str(lid), "version": 1, "content_hash": art}]}, key="e3").json()["data"]["receipt"]
    drain(conn)
    qa = api.get("query-export-artifact", export_id=png["resource_id"]).json()["data"]["artifact"]
    raw = base64.b64decode(api.c.get("/v1/x/c/artifact", params={"ref": qa["ref"]}, cookies=api._cookies()).json()["data"]["body"]["data_b64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and b"Adjustment\x00raw" in raw and f"{lid}@1".encode() in raw and b"AsOf" in raw
    # a layout changed after scheduling does not alter the stored spec
    spec_ref = conn.execute("SELECT export_spec_artifact s FROM ops.export_request WHERE export_id=%s", (png["resource_id"],)).fetchone()["s"]
    assert json.loads(json.dumps(conn.execute("SELECT body FROM ops.ext_artifact WHERE ref=%s", (spec_ref,)).fetchone()["body"]))["workspace_objects"][0]["version"] == 1
    # inputs are checked
    assert api.post("schedule-workspace-export", {**req, "input_snapshot_refs": [{**ref, "content_hash": "0" * 64}]}, key="e4").status_code == 409
    assert api.post("schedule-workspace-export", {**req, "field_projection": [{**fields[0], "dataset_id": "other"}]}, key="e5").status_code == 400
    assert api.post("schedule-workspace-export", {**req, "workspace_objects": [{"object_id": str(lid), "version": 1, "content_hash": "1" * 64}]}, key="e6").status_code == 409
    assert api.post("schedule-workspace-export", {**req, "format": "xlsx"}, key="e7").status_code == 400
    # another user cannot read the export; an expired export hands out no artifact
    stranger = uuid.uuid4()
    assert make_api(principal, stranger).as_(stranger).get("query-export-artifact", export_id=rc["resource_id"]).status_code == 404
    expired = WS.query_export(conn, principal, uuid.UUID(rc["resource_id"]), datetime.now(UTC) + timedelta(days=30))
    assert expired["artifact"] is None
    # data that is not raw daily bars is refused by the job (failed with a code), never invented
    other = S.seed_manifest(conn, "quotes_l1")
    m = conn.execute("SELECT manifest_version, content_hash FROM market.dataset_manifest WHERE snapshot_id=%s", (other,)).fetchone()
    bad = api.post("schedule-workspace-export", {**req, "input_snapshot_refs": [{"snapshot_id": str(other), "dataset_id": "quotes_l1", "manifest_version": m["manifest_version"], "content_hash": m["content_hash"]}],
                                                   "field_projection": [{**fields[0], "dataset_id": "quotes_l1"}]}, key="e8").json()["data"]["receipt"]
    drain(conn)
    assert api.get("query-export-artifact", export_id=bad["resource_id"]).json()["data"]["job"]["error_code"] == "ValueError"


# ---- registered jobs ----------------------------------------------------------------------------------------------------------------
def param_artifact(params: dict) -> dict:
    raw = canonical_json(params).encode("utf-8")
    return {"ref": "inline-json:" + base64.urlsafe_b64encode(raw).decode().rstrip("="), "content_hash": hashlib.sha256(raw).hexdigest(), "contract_id": "pulse.job_parameters", "schema_version": "1"}


def job_defs(conn):
    return {d["application_operation"]: d for d in ops_service.definitions_wire(conn)}


def test_run_registered_job_needs_ops_permission_exact_definition_and_parameters(make_api, conn, principal, monkeypatch):
    api = make_api(principal)
    d = job_defs(conn)["AlertService.scan"]
    body = {"job_id": str(d["job_id"]), "job_version": 1, "expected_definition_hash": d["definition_hash"], "parameter_artifact": param_artifact({}), "input_snapshot_refs": [],
            "scheduled_for": utc_z(datetime.now(UTC))}
    r = api.post("run-registered-job", body, key="j1")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"                                          # ordinary user: no ops:run
    assert api.c.get("/v1/x/c/job-definitions", cookies=api._cookies()).status_code == 403
    monkeypatch.setenv("PULSE_OPS_PRINCIPALS", str(principal))
    r = api.post("run-registered-job", body, key="j1")
    assert r.status_code == 202, r.text
    rc = r.json()["data"]["receipt"]
    assert rc["poll_operation_id"] == "getJobRun" and rc["resource_id"] == rc["job_run_id"]
    assert api.post("run-registered-job", body, key="j1").json()["data"]["receipt"] == rc                              # replay
    assert api.post("run-registered-job", {**body, "scheduled_for": utc_z(datetime.now(UTC) + timedelta(days=1))}, key="j1").status_code == 409
    assert api.post("run-registered-job", {**body, "expected_definition_hash": "0" * 64}, key="j2").json()["error"]["code"] == "VERSION_CONFLICT"
    assert api.post("run-registered-job", {**body, "parameter_artifact": {**body["parameter_artifact"], "content_hash": "0" * 64}}, key="j3").status_code == 400
    prop = job_defs(conn)["PortfolioService.compile_proposal"]
    assert api.post("run-registered-job", {**body, "job_id": str(prop["job_id"]), "expected_definition_hash": prop["definition_hash"]}, key="j4").status_code == 403    # command-only job
    assert api.post("run-registered-job", {**body, "job_id": str(uuid.uuid4())}, key="j5").status_code == 404
    arg = conn.execute("SELECT principal_id, parameter_hash FROM ops.job_run_argument WHERE job_run_id=%s", (rc["job_run_id"],)).fetchone()
    assert arg["principal_id"] == principal and arg["parameter_hash"] == body["parameter_artifact"]["content_hash"]
    # the worker runs it with the stored arguments; a stale epoch cannot commit
    done = ops_service.run_next(conn, job_id=d["job_id"])
    assert done["state"] == "succeeded" and "claims_read" in done["result"]
    run = api.c.get(f"/v1/jobs/{rc['job_run_id']}", cookies=api._cookies()).json()["data"]
    assert run["run_state"] == "succeeded" and run["attempt_count"] == 1
    from pulse.adapters.postgres.jobs import PgJobStore
    from pulse.application.ports.errors import FencedOut
    with pytest.raises(FencedOut):
        PgJobStore(conn).succeed(uuid.UUID(rc["job_run_id"]), 99, output_ref="x", output_hash="y")


def test_job_runs_are_scoped_to_the_viewer_and_dataset_status_keeps_states_apart(make_api, conn, principal, monkeypatch):
    api = make_api(principal)
    other = uuid.uuid4()
    monkeypatch.setenv("PULSE_OPS_PRINCIPALS", str(other))
    # a user's own export job is visible to them; an ops principal also sees the rest
    iid, ref = export_world(conn)
    rc = api.post("schedule-workspace-export", {"input_snapshot_refs": [ref], "field_projection": [], "format": "csv", "workspace_objects": []}, key="x").json()["data"]["receipt"]
    win = {"start": utc_z(datetime.now(UTC) - timedelta(days=1)), "end": utc_z(datetime.now(UTC) + timedelta(days=1))}
    mine = api.get("query-job-runs", window=win, page={"limit": 50}).json()["data"]["jobs"]
    assert rc["job_run_id"] in {j["job_run_id"] for j in mine}
    api_ops = make_api(principal, other).as_(other)
    assert rc["job_run_id"] in {j["job_run_id"] for j in api_ops.get("query-job-runs", window=win, page={"limit": 100}).json()["data"]["jobs"]}
    stranger = uuid.uuid4()
    seen = make_api(principal, stranger).as_(stranger).get("query-job-runs", window=win, page={"limit": 100}).json()["data"]["jobs"]
    assert rc["job_run_id"] not in {j["job_run_id"] for j in seen}                                                    # a known UUID is not enough
    # dataset status: READY snapshot, FAILED newest, never-delivered dataset
    ds = f"health_{uuid.uuid4().hex[:6]}"
    S.seed_manifest(conn, ds)
    S.seed_manifest(conn, ds, "FAILED")
    got = api.get("query-dataset-status", dataset_id=ds, page={"limit": 5}).json()["data"]["datasets"][0]
    assert got["manifest_state"] == "READY" and got["ready_snapshot"] is not None                                    # newest READY, not the failed attempt
    assert any(i["reason_code"] == "LATEST_FAILED" for i in got["issues"])                                           # but the failure is not hidden
    allr = api.get("query-dataset-status", dataset_id="all", page={"limit": 2}).json()
    assert len(allr["data"]["datasets"]) == 2 and allr["data"]["page"]["next_cursor"]
    nxt = api.get("query-dataset-status", dataset_id="all", page={"limit": 2, "cursor": allr["data"]["page"]["next_cursor"]})
    assert nxt.status_code == 200
    assert api.get("query-dataset-status", dataset_id="all", page={"limit": 3, "cursor": allr["data"]["page"]["next_cursor"]}).status_code == 400    # a cursor belongs to its query


def test_source_runtime_and_freshness_are_reported_apart_from_snapshot_state(make_api, conn, principal):
    api = make_api(principal)
    ds = f"srcdata_{uuid.uuid4().hex[:6]}"
    src = f"hsrc_{uuid.uuid4().hex[:6]}"
    conn.execute("INSERT INTO core.source(source_id,display_name,provider_uri,created_at) VALUES (%s,%s,'urn:x',now())", (src, src))
    conn.execute("INSERT INTO core.source_endpoint(source_id,endpoint_id,source_schema_version,mapping_version,contract_id,schema_version,mapping_hash,enabled,transport_kind,stale_after_seconds,policy_document,created_at)"
                 " VALUES (%s,'ep','s1','m1','c.test','1.0.0',%s,true,'poll',60,'{}',now())", (src, S.HASH))
    batch, snap = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,started_at) VALUES (%s,%s,'ep','s1','m1','p','INGESTING',now())", (batch, src))
    conn.execute("INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,contract_id,schema_version,row_count,coverage_document,created_at,ready_at,available_watermark)"
                 " VALUES (%s,%s,1,%s,'READY','file:///x',%s,'c.test','1.0.0',3,'{}',now(),now(),now())", (snap, ds, batch, S.sha(str(snap))))
    def issues():
        return {i["reason_code"] for i in api.get("query-dataset-status", dataset_id=ds, page={"limit": 5}).json()["data"]["datasets"][0]["issues"]}
    assert issues() == set()                                                    # enabled, READY, no health row: nothing claimed either way
    health = lambda rt, fr: conn.execute("INSERT INTO ingest.source_health(source_id,endpoint_id,source_schema_version,mapping_version,partition_key,runtime_state,freshness_state,consecutive_failures,updated_at)"  # noqa: E731
                                         " VALUES (%s,'ep','s1','m1','default',%s,%s,2,now()) ON CONFLICT (source_id,endpoint_id,source_schema_version,mapping_version,partition_key) DO UPDATE SET runtime_state=EXCLUDED.runtime_state, freshness_state=EXCLUDED.freshness_state", (src, rt, fr))
    health("unavailable", "stale")
    assert issues() == {"RUNTIME_UNAVAILABLE"}                                  # READY snapshot, broken source: both facts visible
    health("healthy", "not_expected")
    assert issues() == set()                                                    # market closed is not an outage
    health("healthy", "stale")
    assert issues() == {"FRESHNESS_STALE"}
