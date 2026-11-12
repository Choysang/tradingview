"""getStockEvidence / Community / FundFlow / Seats, getDataStatus, getModelRun and /app-config.json on real PostgreSQL."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from api_seed import HASH, publish, seed_instrument, sha

UTC = timezone.utc


def raw_object(db, source="cninfo_announcements", at=None) -> object:
    rid, at = uuid4(), at or datetime.now(UTC) - timedelta(days=1)
    db.execute("INSERT INTO ingest.raw_object(raw_object_id,source_id,endpoint_id,source_schema_version,source_record_key,content_hash,"
               "storage_uri,mime_type,source_time_precision,first_seen_at,ingested_at) VALUES (%s,%s,'e','s',%s,%s,'file:///r','text/plain',"
               "'second',%s,%s)", (rid, source, str(rid), sha(str(rid)), at, at))
    return rid


def claim(db, code, *, available_at, revision=1, claim_id=None, title="公告") -> tuple:
    """One document + one claim revision linking to it; returns (claim_id, claim_revision_id, document_revision_id)."""
    raw = raw_object(db, at=available_at)
    doc_rev, doc_id, cid, crid = uuid4(), uuid4(), claim_id or uuid4(), uuid4()
    db.execute("INSERT INTO intelligence.document_revision(document_revision_id,document_id,source_id,source_record_id,revision,raw_object_id,"
               "document_status,origin_group,origin_status,title,published_time_precision,first_seen_at,available_at,recorded_at,quality_status,"
               "field_quality) VALUES (%s,%s,'cninfo_announcements',%s,1,%s,'published','og','confirmed_original',%s,'second',%s,%s,%s,'valid','{}')",
               (doc_rev, doc_id, str(doc_rev), raw, title, available_at, available_at, available_at))
    db.execute("INSERT INTO intelligence.claim_revision(claim_revision_id,claim_id,revision,subject_kind,subject_key,predicate,object_payload,"
               "claim_schema_version,assertion_type,claim_status,verification_policy_version,available_at,recorded_at,quality_status)"
               " VALUES (%s,%s,%s,'issuer',%s,'announcement.published',%s,'v1','official_disclosure','supported','p1',%s,%s,'valid')",
               (crid, cid, revision, code, Jsonb({"title": title}), available_at, available_at))
    db.execute("INSERT INTO intelligence.claim_evidence(claim_revision_id,document_revision_id,evidence_ordinal,relation,evidence_span,"
               "extraction_version,available_at,recorded_at) VALUES (%s,%s,1,'supports',%s,'x1',%s,%s)",
               (crid, doc_rev, Jsonb({"field_path": "title"}), available_at, available_at))
    db.commit()
    return cid, crid, doc_rev


# ---- evidence -------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_evidence_is_point_in_time_latest_revision_and_paged(client, db):
    code = f"6{uuid4().int % 100000:05d}"
    iid = seed_instrument(db, code)
    rid = publish(db, as_of=datetime.now(UTC) - timedelta(hours=1))
    base = datetime.now(UTC) - timedelta(days=2)
    cid, old_rev, _ = claim(db, code, available_at=base, title="旧版")
    # revision 2 of the same claim, available before the release: it replaces revision 1 in the answer
    _, new_rev, _ = claim(db, code, available_at=base + timedelta(minutes=1), revision=2, claim_id=cid, title="新版")
    claim(db, code, available_at=datetime.now(UTC) + timedelta(hours=1), title="未来公告")  # after the release decision time
    claim(db, "999999", available_at=base, title="别的股票")
    r = client.get(f"/v1/stocks/{iid}/evidence", params={"release_id": rid})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [c["claim_revision_id"] for c in d["claims"]] == [str(new_rev)]
    assert d["claims"][0]["object_payload"]["title"] == "新版" and len(d["claims"][0]["evidence"]) == 1
    assert [x["title"] for x in d["documents"]] == ["新版"]
    assert r.json()["meta"]["quality_status"] == "valid" and r.json()["meta"]["coverage"]["observed_count"] == 1
    # paging: > PAGE claims, second page continues without overlap, a cursor of another release is refused
    for i in range(51):
        claim(db, code, available_at=base + timedelta(hours=1, seconds=i), title=f"c{i}")
    p1 = client.get(f"/v1/stocks/{iid}/evidence", params={"release_id": rid}).json()
    assert len(p1["data"]["claims"]) == 50 and p1["meta"]["next_cursor"]
    p2 = client.get(f"/v1/stocks/{iid}/evidence", params={"release_id": rid, "cursor": p1["meta"]["next_cursor"]}).json()
    ids1, ids2 = {c["claim_revision_id"] for c in p1["data"]["claims"]}, {c["claim_revision_id"] for c in p2["data"]["claims"]}
    assert ids1.isdisjoint(ids2) and len(ids1 | ids2) == 52 and p2["meta"]["next_cursor"] is None
    rid2 = publish(db, as_of=datetime.now(UTC) - timedelta(hours=1))
    mismatch = client.get(f"/v1/stocks/{iid}/evidence", params={"release_id": rid2, "cursor": p1["meta"]["next_cursor"]})
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "CURSOR_RELEASE_MISMATCH"
    assert client.get(f"/v1/stocks/{iid}/evidence", params={"release_id": rid, "cursor": "!!"}).json()["error"]["code"] == "CURSOR_INVALID"


@pytest.mark.pg
def test_missing_intelligence_is_an_empty_partial_list_with_visible_coverage_gap(client, db):
    iid = seed_instrument(db, "600030")
    rid = publish(db)
    for path in ("evidence", "community", "fund-flow", "seats"):
        r = client.get(f"/v1/stocks/{iid}/{path}", params={"release_id": rid})
        assert r.status_code == 200, (path, r.text)
        body = r.json()
        (key,) = [k for k in body["data"] if k not in ("instrument_id", "release_id") and isinstance(body["data"][k], list)][:1] or [None]
        assert body["data"][key] == [] or key == "documents"
        assert body["meta"]["quality_status"] == "partial"
        cov = body["meta"]["coverage"]
        assert (cov["expected_count"], cov["observed_count"], cov["missing_count"]) == (1, 0, 1)
        assert cov["unverifiable_reasons"] and body["meta"]["next_cursor"] is None
    assert client.get(f"/v1/stocks/{uuid4()}/seats", params={"release_id": rid}).status_code == 404
    assert client.get(f"/v1/stocks/{iid}/seats", params={"release_id": uuid4()}).status_code == 404


@pytest.mark.pg
def test_community_fund_flow_and_seats_return_real_rows_as_of_the_release(client, db):
    iid = seed_instrument(db, "600031")
    rid = publish(db, as_of=datetime.now(UTC))
    t = datetime.now(UTC) - timedelta(hours=5)
    raw = raw_object(db, "apisrc", t)
    db.execute("INSERT INTO intelligence.community_observation(community_observation_id,source_id,raw_object_id,post_id,post_revision,sampled_at,"
               "sampling_entry,sampling_sort,sampling_window_start,sampling_window_end,coverage_status,like_count,analysis_payload,"
               "analysis_schema_version,analysis_model_version,available_at,recorded_at,quality_status) VALUES (%s,'apisrc',%s,'p1',1,%s,'page',"
               "'latest',%s,%s,'partial',7,%s,'v1','m1',%s,%s,'partial')",
               (uuid4(), raw, t, t - timedelta(hours=1), t, Jsonb({"instrument_ids": [str(iid)], "stance": "mixed"}), t, t))
    db.execute("INSERT INTO intelligence.funding_observation(funding_observation_id,source_id,raw_object_id,source_record_key,revision,instrument_id,"
               "metric_namespace,metric_name,methodology_id,methodology_version,measurement_class,side_convention,trade_or_order_basis,session_scope,"
               "market_direction,observation_window_start,observation_window_end,raw_value,raw_unit,canonical_value,canonical_unit,currency,"
               "available_at,recorded_at,quality_status,field_quality) VALUES (%s,'apisrc',%s,'k1',1,%s,'ns','net_inflow','m','1','provider_derived',"
               "'main_force','trade','day','not_applicable',%s,%s,'1.5','CNY_10000',15000,'CNY','CNY',%s,%s,'valid','{}')",
               (uuid4(), raw, iid, t - timedelta(hours=1), t, t, t))
    db.execute("INSERT INTO intelligence.seat_report(report_revision_id,source_id,raw_object_id,source_report_id,revision,exchange_code,instrument_id,"
               "window_start,window_end,disclosure_reason,rule_version,trade_scope,available_at,recorded_at,quality_status) VALUES (%s,'apisrc',%s,'r1',1,"
               "'XSHG',%s,current_date-1,current_date-1,'涨幅偏离','rv1','unknown',%s,%s,'valid')", (rep := uuid4(), raw, iid, t, t))
    db.execute("INSERT INTO intelligence.seat_report_row(report_revision_id,side,rank,raw_seat_name,raw_amount,raw_unit,canonical_amount_cny,"
               "resolution_status,resolution_evidence) VALUES (%s,'buy',1,'某营业部',1.5,'CNY_10000',15000,'unresolved','{}')", (rep,))
    db.commit()
    p = {"release_id": rid}
    com = client.get(f"/v1/stocks/{iid}/community", params=p).json()
    assert com["data"]["observations"][0]["like_count"] == 7 and com["data"]["observations"][0]["reply_count"] is None
    assert com["meta"]["quality_status"] == "partial"  # the only observation is partial
    ff = client.get(f"/v1/stocks/{iid}/fund-flow", params=p).json()
    assert ff["data"]["observations"][0]["canonical_value"] == "15000" and ff["meta"]["quality_status"] == "valid"
    seats = client.get(f"/v1/stocks/{iid}/seats", params=p).json()
    row = seats["data"]["reports"][0]["rows"][0]
    assert row["canonical_amount_cny"] == "15000" and row["resolution_status"] == "unresolved" and row["seat_id"] is None
    # a release decided before the data existed sees none of it
    early = publish(db, as_of=t - timedelta(hours=2))
    assert client.get(f"/v1/stocks/{iid}/seats", params={"release_id": early}).json()["data"]["reports"] == []


# ---- data status ----------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_data_status_separates_enabled_transport_and_freshness(client, db):
    now = datetime.now(UTC)
    db.execute("INSERT INTO ingest.source_health(source_id,endpoint_id,source_schema_version,mapping_version,partition_key,runtime_state,"
               "freshness_state,last_attempt_at,last_transport_ok_at,last_new_record_at,last_canonical_ready_at,consecutive_failures,updated_at)"
               " VALUES ('apisrc','quote','s1','m1','p','healthy','current',%s,%s,NULL,NULL,0,%s) ON CONFLICT DO NOTHING",
               (now - timedelta(hours=1), now - timedelta(hours=1), now - timedelta(hours=1)))
    db.commit()
    r = client.get("/v1/data-status")
    assert r.status_code == 200, r.text
    items = {(i["source_id"], i["endpoint_id"]): i for i in r.json()["data"]["items"]}
    q, quiet = items[("apisrc", "quote")], items[("apisrc", "quiet")]
    assert q["enabled"] is True and q["runtime_state"] == "healthy"
    assert q["freshness_state"] == "unknown"  # the sample is older than the endpoint's 120 s threshold
    assert q["last_transport_ok_at"] is not None and q["last_new_record_at"] is None and q["last_canonical_ready_at"] is None
    assert quiet["enabled"] is False and quiet["runtime_state"] == "unconfigured" and quiet["partition_key"] == "default"
    assert quiet["consecutive_failures"] == 0 and quiet["last_attempt_at"] is None  # absent, not zero-filled timestamps
    assert "HEALTH_SAMPLE_OLD" in {w["code"] for w in r.json()["meta"]["warnings"]}
    assert r.json()["meta"]["quality_status"] == "partial"


# ---- model run ------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_model_run_reads_output_artifact_and_404s(client, db):
    db.execute("INSERT INTO core.contract_version(contract_id,schema_version,schema_document,schema_hash,compatibility_policy,created_at)"
               " VALUES ('ModelOut','1.0.0',%s,%s,'strict',clock_timestamp()) ON CONFLICT DO NOTHING", (Jsonb({"type": "object"}), HASH))
    db.execute("INSERT INTO analysis.model_policy(model_policy_id,model_policy_version,task_kind,provider_id,model_locator,response_contract_id,"
               "response_schema_version,prompt_digest,retrieval_policy_version,capability_requirements,timeout_seconds,token_budget,rollout_state,"
               "created_at) VALUES ('mp-test',1,'research','local','{}','ModelOut','1.0.0',%s,'r1','{}',60,1000,'candidate',clock_timestamp())"
               " ON CONFLICT DO NOTHING", (HASH,))
    done, queued = uuid4(), uuid4()
    for rid, state in ((done, "succeeded"), (queued, "queued")):
        ok = state == "succeeded"
        db.execute("INSERT INTO analysis.model_run(model_run_id,model_policy_id,model_policy_version,input_context_hash,input_manifest,analysis_as_of,"
                   "run_state,publication_eligibility,started_at,completed_at,output_ref,output_hash,usage_document) VALUES (%s,'mp-test',1,%s,'{}',"
                   "clock_timestamp(),%s,'unchecked',%s,%s,%s,%s,'{}')",
                   (rid, HASH, state, datetime.now(UTC) if ok else None, datetime.now(UTC) if ok else None, "file:///out" if ok else None,
                    sha("o") if ok else None))
    db.commit()
    d = client.get(f"/v1/analysis-runs/{done}").json()
    assert d["data"]["output"] == {"ref": "file:///out", "content_hash": sha("o"), "contract_id": "ModelOut", "schema_version": "1.0.0"}
    assert d["meta"]["quality_status"] == "valid"
    q = client.get(f"/v1/analysis-runs/{queued}").json()
    assert q["data"]["output"] is None and q["data"]["started_at"] is None and q["meta"]["quality_status"] == "partial"
    assert client.get(f"/v1/analysis-runs/{uuid4()}").status_code == 404


# ---- app config -----------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_app_config_defaults_env_override_and_file(client, monkeypatch, tmp_path):
    cfg = client.get("/app-config.json").json()
    assert cfg["slot_key"] == "main" and [h["key"] for h in cfg["horizons"]] == ["short", "swing", "long"]
    assert cfg["quote_policy_version"] == "quote-observation-latest-v1" and cfg["session_policy_version"] == "regular_continuous_v1"
    assert cfg["source"] == "defaults"
    monkeypatch.setenv("PULSE_SLOT_KEY", "side")
    f = tmp_path / "cfg.json"
    f.write_text(json.dumps({"taxonomies": [{"id": "x", "version": 2, "label": "X"}]}), encoding="utf-8")
    monkeypatch.setenv("PULSE_APP_CONFIG_FILE", str(f))
    cfg = client.get("/app-config.json").json()
    assert cfg["slot_key"] == "side" and cfg["taxonomies"] == [{"id": "x", "version": 2, "label": "X"}]
