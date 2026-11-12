"""Community, seats (live recorded + legacy), fund flow on real PostgreSQL from recorded raw feeds (no network).

Spec: FINAL §5/§5.2, sources_sectors §6-§8; SS-A11,A12,A14-A18,A23,A24,A26,A27; gates SS-G08,G09,G11.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import polars as pl
import pytest

from pulse import contracts
from pulse.adapters.postgres import ingest_repo, instrument_repo
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.adapters.sources.fundflow import FundFlowAdapter
from pulse.adapters.sources.guba import GubaAdapter
from pulse.adapters.sources.lhb import LhbAdapter
from pulse.domain.instruments.identity import InstrumentRecord, instrument_uuid
from pulse.domain.intelligence import community, feeds, seats

from . import helpers as h

UTC = timezone.utc
pytestmark = pytest.mark.pg


def _cl(clock, tr=None):
    return h.client(now=clock, transport_=tr)


def _n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()["n"]


def _wire_ts(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


# ---- community --------------------------------------------------------------------------------------------------------------
def test_guba_sample_is_stored_with_declared_scope_and_never_calls_itself_buying(svc, conn, clock):
    posts = feeds.parse_guba_list(h.recorded("guba_list_000001_f.html")).posts
    oldest = min(p.published_at for p in posts if not p.pinned)
    hours = int((clock.t - oldest).total_seconds() // 3600)  # a window that starts after the oldest fetched post
    s = svc.ingest_community(GubaAdapter(_cl(clock)), ["000001"], pages=1, window_hours=max(1, hours - 1))
    rows = conn.execute("SELECT * FROM intelligence.community_observation ORDER BY post_id").fetchall()
    own = [p for p in posts if p.bar_code == "000001" and not p.pinned and clock.t - timedelta(hours=max(1, hours - 1)) <= p.published_at < clock.t]
    assert len(rows) == len(own) > 0 and s.counts["community_observations"] == len(rows)
    r = rows[0]
    assert r["coverage_status"] == "complete_for_declared_scope"
    assert r["sampling_window_end"] == r["sampled_at"] and r["sampling_window_start"] < r["sampling_window_end"]
    assert r["sampling_sort"] == "publish_time_desc" and r["sampling_entry"].startswith("guba_list:000001")
    assert r["like_count"] is None and r["unique_participant_count"] is None  # absent stays NULL, not 0
    assert r["author_pseudonym"].startswith("gb:") and r["available_at"] >= r["sampled_at"] and r["post_revision"] == 1
    assert r["analysis_payload"]["metrics_basis"] == "attention_proxy_not_trading_flow" and r["analysis_payload"]["subject_match"] == "own_stockbar"
    assert all(x["analysis_payload"]["bar_code"] == "000001" for x in rows)
    blob = json.dumps([dict(x) for x in rows], default=str, ensure_ascii=False)
    assert not any(w in blob for w in community.FORBIDDEN_LABELS)
    sample_note = next(n for n in s.notes if n.startswith("sample "))
    assert '"coverage_status": "complete_for_declared_scope"' in sample_note and "not trading flow" in sample_note
    wire = {"community_observation_id": str(r["community_observation_id"]), "source_id": r["source_id"], "post_id": r["post_id"], "post_revision": r["post_revision"],
            "author_pseudonym": r["author_pseudonym"], "post_created_at": _wire_ts(r["post_created_at"]), "sampled_at": _wire_ts(r["sampled_at"]),
            "sampling_entry": r["sampling_entry"], "sampling_sort": r["sampling_sort"], "sampling_window_start": _wire_ts(r["sampling_window_start"]),
            "sampling_window_end": _wire_ts(r["sampling_window_end"]), "coverage_status": r["coverage_status"], "like_count": None, "reply_count": r["reply_count"],
            "unique_participant_count": None, "analysis_schema_version": r["analysis_schema_version"], "analysis_payload": r["analysis_payload"],
            "available_at": _wire_ts(r["available_at"]), "quality_status": r["quality_status"]}
    contracts.check("CommunityObservation", wire)
    # raw HTML kept byte-exact
    raw = conn.execute("SELECT content_hash FROM ingest.raw_object WHERE source_id='eastmoney_guba_comments'").fetchone()
    assert raw["content_hash"] == __import__("hashlib").sha256(h.recorded("guba_list_000001_f.html")).hexdigest()


def test_wide_window_is_partial_and_resampling_adds_rows_not_edits(svc, conn, clock):  # SS-A23: engagement belongs to sampled_at
    svc.ingest_community(GubaAdapter(_cl(clock)), ["000001"], pages=1, window_hours=24 * 30)
    first = conn.execute("SELECT DISTINCT coverage_status FROM intelligence.community_observation").fetchall()
    assert [x["coverage_status"] for x in first] == ["partial"]
    n1 = _n(conn, "SELECT count(*) AS n FROM intelligence.community_observation")
    clock.advance(hours=3)
    svc.ingest_community(GubaAdapter(_cl(clock)), ["000001"], pages=1, window_hours=24 * 30)
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.community_observation") >= 2 * n1 - 0  # same post, later sampled_at: a new observation
    assert _n(conn, "SELECT count(DISTINCT sampled_at) AS n FROM intelligence.community_observation") == 2
    assert _n(conn, "SELECT max(post_revision) AS n FROM intelligence.community_observation") == 1


def test_unregistered_instrument_is_skipped_and_there_is_no_posting_code(svc, conn, clock):  # SS-A24
    s = svc.ingest_community(GubaAdapter(_cl(clock)), ["688001"], pages=1)
    assert any("not registered" in n for n in s.notes)
    import pulse.adapters.sources.guba as g
    import inspect
    src = inspect.getsource(g)
    assert "method=\"POST\"" not in src and ".post(" not in src


# ---- seats: live recorded ---------------------------------------------------------------------------------------------------
@pytest.fixture
def lhb_instruments(conn):
    lst = feeds.parse_lhb_list(h.recorded("lhb_daily_list_20261110.json")).rows
    recs = [InstrumentRecord.build(str(r["SECURITY_CODE"]), str(r.get("SECURITY_NAME_ABBR") or "x")) for r in lst if seats.is_equity_code(str(r["SECURITY_CODE"]))]
    with conn.transaction():
        instrument_repo.upsert_instruments(conn, recs, datetime(2026, 11, 1, tzinfo=UTC), (), "intel-tests")
    return recs


def test_live_lhb_reports_keep_names_verbatim_resolve_only_on_evidence_and_respect_availability(svc, conn, clock, lhb_instruments):
    s = svc.ingest_lhb(LhbAdapter(_cl(clock)), [date(2026, 11, 10)])
    lst = feeds.parse_lhb_list(h.recorded("lhb_daily_list_20261110.json")).rows
    equity = [r for r in lst if seats.is_equity_code(str(r["SECURITY_CODE"]))]
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report") == len(equity) == s.counts["seat_reports_new"]
    assert s.counts["pages_fetched"] == 3
    st = {r["resolution_status"]: r["n"] for r in conn.execute("SELECT resolution_status, count(*) AS n FROM intelligence.seat_report_row GROUP BY 1").fetchall()}
    assert st["resolved"] > 0 and st["report_local_label"] > 0 and st.get("unresolved", 0) > 0
    # resolved <=> entity attached; others never carry an id (DDL also enforces it)
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report_row WHERE (resolution_status='resolved') <> (seat_id IS NOT NULL)") == 0
    row = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE resolution_status='unresolved' LIMIT 1").fetchone()
    assert row["raw_seat_name"] and row["resolution_evidence"]["reason"] and row["resolution_evidence"]["raw_seat_name"] == row["raw_seat_name"]
    # exact amount and unit
    iid = instrument_uuid("XSHE", "000530")
    rep = conn.execute("SELECT * FROM intelligence.seat_report WHERE instrument_id=%s", (iid,)).fetchone()
    buy = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE report_revision_id=%s AND side='buy' AND raw_seat_name LIKE '%%拉萨东环路第二%%'",
                       (rep["report_revision_id"],)).fetchone()
    assert buy["canonical_amount_cny"] == Decimal("16785186") and buy["raw_unit"] == "CNY"
    # SS-A16: the same seat appears on both sides and both rows are kept; SS-A18 numbering stays report-local
    sides = {r["side"] for r in conn.execute("SELECT side FROM intelligence.seat_report_row WHERE report_revision_id=%s AND raw_seat_name LIKE '%%拉萨东环路第二%%'",
                                             (rep["report_revision_id"],)).fetchall()}
    assert sides == {"buy", "sell"}
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report_row WHERE raw_seat_name LIKE '机构专用%%' AND seat_id IS NOT NULL") == 0
    # SS-A14: disclosed after the close; invisible to the intraday decision, visible to the next day's
    assert rep["available_at"] >= datetime(2026, 11, 10, 7, 0, tzinfo=UTC) and rep["rule_version"] == "unknown" and rep["trade_scope"] == "unknown"
    assert repo.seat_reports_for(conn, iid, datetime(2026, 11, 10, 2, 0, tzinfo=UTC)) == []
    assert repo.seat_reports_for(conn, iid, datetime(2026, 11, 11, 1, 0, tzinfo=UTC)) == []  # the platform only fetched it at 20:45 CST on 11-11
    assert len(repo.seat_reports_for(conn, iid, datetime(2026, 11, 12, 1, 0, tzinfo=UTC))) == 1
    # entity versions carry full branch names, evidence and the first-observed basis
    ent = conn.execute("SELECT * FROM intelligence.seat_entity_version LIMIT 1").fetchone()
    assert ent["canonical_name"].startswith(ent["broker_legal_name"]) and ent["alias_evidence"][0]["effective_from_basis"] == "first_observed_in_disclosure"
    assert ent["alias_evidence"][0]["alias"] == ent["canonical_name"]
    # contract
    rows = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE report_revision_id=%s ORDER BY side, rank", (rep["report_revision_id"],)).fetchall()
    wire = {"report_revision_id": str(rep["report_revision_id"]), "instrument_id": str(iid), "source_id": rep["source_id"], "revision": rep["revision"],
            "window_start": rep["window_start"].isoformat(), "window_end": rep["window_end"].isoformat(), "disclosure_reason": rep["disclosure_reason"],
            "rule_version": rep["rule_version"], "published_at": None, "available_at": _wire_ts(rep["available_at"]), "quality_status": rep["quality_status"],
            "rows": [{"side": r["side"], "rank": r["rank"], "raw_seat_name": r["raw_seat_name"], "canonical_amount_cny": contracts.decimal_str(r["canonical_amount_cny"]),
                      "resolution_status": r["resolution_status"], "seat_id": None if r["seat_id"] is None else str(r["seat_id"]), "seat_version": r["seat_version"],
                      "resolution_evidence": r["resolution_evidence"]} for r in rows]}
    contracts.check("SeatReport", wire)


def test_second_run_adds_nothing_and_an_empty_day_is_success_empty(svc, conn, clock, lhb_instruments):
    svc.ingest_lhb(LhbAdapter(_cl(clock)), [date(2026, 11, 10)])
    n = _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report")
    s = svc.ingest_lhb(LhbAdapter(_cl(clock)), [date(2026, 11, 10), date(2026, 11, 8)])
    assert s.counts["reports_unchanged"] == n and s.counts["days_success_empty"] == 1 and "seat_reports_new" not in s.counts
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report") == n
    hrow = conn.execute("SELECT runtime_state, freshness_state FROM ingest.source_health WHERE endpoint_id='daily_list' AND partition_key='day:2026-11-08'").fetchone()
    assert hrow["runtime_state"] == "healthy"


def test_changed_disclosure_appends_a_revision_not_an_overwrite(svc, conn, clock, lhb_instruments):
    svc.ingest_lhb(LhbAdapter(_cl(clock)), [date(2026, 11, 10)])
    doc = json.loads(h.recorded("lhb_seat_buy_20261110.json"))
    doc["result"]["data"][0]["BUY"] = (doc["result"]["data"][0]["BUY"] or 0) + 100
    s = svc.ingest_lhb(LhbAdapter(_cl(clock, h.transport(overrides={"RPT_BILLBOARD_DAILYDETAILSBUY": (200, json.dumps(doc).encode())}))), [date(2026, 11, 10)])
    assert s.counts["seat_reports_revised"] == 1
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report WHERE revision=2") == 1
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report WHERE revision=1") == _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report WHERE revision=2") + s.counts["reports_unchanged"]


def test_seat_style_hypothesis_is_registered_once_and_evaluated_with_pit_gates(conn):
    h1 = seats.SeatStyleHypothesis("raw_name_literal", "东方财富证券股份有限公司拉萨东环路第二证券营业部", "buy", {"board": "first"}, 1, "next_open_gap",
                                   "after this seat appears on the buy list, the next open gap differs from matched controls (hypothesis)", min_sample=2)
    a = repo.register_seat_hypothesis(conn, h1, datetime(2026, 11, 11, tzinfo=UTC))
    b = repo.register_seat_hypothesis(conn, h1, datetime(2026, 11, 11, tzinfo=UTC))
    assert a == b and _n(conn, "SELECT count(*) AS n FROM research.hypothesis") == 1
    row = conn.execute("SELECT * FROM research.hypothesis").fetchone()
    assert row["falsification_conditions"]["min_sample"] == 2 and "hypothesis" in row["mechanism"]


# ---- seats: legacy raw objects -------------------------------------------------------------------------------------------------
def _put_legacy(svc, conn, lake, kind: str, name: str, df: pl.DataFrame):
    buf = io.BytesIO()
    df.write_parquet(buf)
    data = buf.getvalue()
    ref = lake.put_bytes(data, owner="test", kind="raw", suffix=".parquet")
    t0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    with conn.transaction():
        rid, _ = ingest_repo.insert_raw_object(conn, source_id="legacy_warehouse", endpoint_id="evidence_files", source_schema_version="legacy-parquet-warehouse-v1",
                                               source_record_key=f"{kind}/{name}", content_hash=ref.sha256, storage_uri=ref.rel_path,
                                               mime_type="application/vnd.apache.parquet", published_at=None, source_time_precision="unknown",
                                               first_seen_at=t0, ingested_at=t0)
    return rid


def test_legacy_lhb_raw_objects_normalize_with_exact_units_and_their_own_availability(svc, conn, lake, clock):
    svc.data.bootstrap()
    ing = datetime(2026, 10, 31, 3, 41, tzinfo=UTC)
    d = [date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)]
    quiet = "日换手率达到20%的前5只证券"
    stocks = pl.DataFrame({"code": ["000530"] * 5, "date": [d[1], d[3], d[3], d[0], d[2]],
                           "reason": ["日涨幅偏离值达到7%的前5只证券", "连续三个交易日内，涨幅偏离值累计达到20%的证券", quiet, quiet, quiet],
                           "name": ["冰山冷热"] * 5, "ingested_at": [ing] * 5}).with_columns(pl.col("ingested_at").dt.replace_time_zone("UTC"))
    seat_rows = []
    for dt, reason in [(d[1], "日涨幅偏离值达到7%的前5只证券"), (d[3], "连续三个交易日内，涨幅偏离值累计达到20%的证券"), (d[3], "日换手率达到20%的前5只证券")]:
        seat_rows += [
            {"code": "000530", "date": dt, "reason": reason, "side": "buy", "rank": 1, "seat": "东方财富证券股份有限公司拉萨东环路第二证券营业部", "seat_code": "10472087",
             "buy": 0.269411, "sell": 0.21256, "net": 0.056851, "ingested_at": ing},
            {"code": "000530", "date": dt, "reason": reason, "side": "buy", "rank": 2, "seat": "机构专用", "seat_code": "0", "buy": 0.1, "sell": 0.0, "net": 0.1, "ingested_at": ing},
            {"code": "000530", "date": dt, "reason": reason, "side": "sell", "rank": 1, "seat": "东方财富证券股份有限公司拉萨东环路第二证券营业部", "seat_code": "10472087",
             "buy": 0.269411, "sell": 0.21256, "net": 0.056851, "ingested_at": ing}]
    sdf = pl.DataFrame(seat_rows).with_columns(pl.col("ingested_at").dt.replace_time_zone("UTC"))
    _put_legacy(svc, conn, lake, "lhb_stocks", "a.parquet", stocks)
    _put_legacy(svc, conn, lake, "lhb_seats", "a.parquet", sdf)
    s = svc.import_legacy_lhb()
    assert s.counts["seat_reports_new"] == 5
    reps = conn.execute("SELECT * FROM intelligence.seat_report ORDER BY window_end, disclosure_reason").fetchall()
    assert all(r["source_id"] == "legacy_warehouse" and r["available_at"] == ing and r["quality_status"] == "partial" for r in reps)
    three = next(r for r in reps if "连续三个" in r["disclosure_reason"])
    assert three["window_end"] == d[3] and three["window_start"] == d[1]  # sessions 10-26, 10-27, 10-28
    row = conn.execute("SELECT * FROM intelligence.seat_report_row WHERE raw_seat_name LIKE '%%拉萨%%' AND side='buy' LIMIT 1").fetchone()
    assert row["raw_unit"] == "CNY_10000" and row["raw_amount"] == Decimal("2694.11") and row["canonical_amount_cny"] == Decimal("26941100")
    assert row["resolution_evidence"]["source_seat_code"] == "10472087" and row["resolution_status"] == "resolved"
    assert _n(conn, "SELECT count(*) AS n FROM intelligence.seat_report_row WHERE raw_seat_name='机构专用' AND resolution_status='report_local_label' AND seat_id IS NULL") == 3
    ent = conn.execute("SELECT * FROM intelligence.seat_entity_version").fetchall()
    assert len(ent) == 1 and ent[0]["city_code"] == "拉萨"  # one entity, regardless of how many reports mention it
    # PIT: back-filled legacy reports keep the legacy ingest time; they are not visible before it
    iid = instrument_uuid("XSHE", "000530")
    assert repo.seat_reports_for(conn, iid, ing - timedelta(days=1)) == [] and len(repo.seat_reports_for(conn, iid, ing + timedelta(seconds=1))) == 5
    # SS-G08: the entity version became known at import time (11-11), so for a 11-01 snapshot the same rows are still unresolved
    early = repo.seat_rows_as_of(conn, iid, datetime(2026, 11, 1, tzinfo=UTC))
    late = repo.seat_rows_as_of(conn, iid, clock.t + timedelta(days=1))
    assert early and {r["resolution_status_as_of"] for r in early if "拉萨" in r["raw_seat_name"]} == {"unresolved"}
    assert {r["resolution_status_as_of"] for r in late if "拉萨" in r["raw_seat_name"]} == {"resolved"}
    s2 = svc.import_legacy_lhb()
    assert s2.counts["reports_unchanged"] == 5 and "seat_reports_new" not in s2.counts


# ---- fund flow ----------------------------------------------------------------------------------------------------------------
def test_fundflow_rows_are_provider_derived_series_with_exact_values_and_partial_current_day(svc, conn, clock):
    clock.t = datetime(2026, 11, 11, 5, 0, tzinfo=UTC)  # 13:00 CST: today's vendor bar is still accumulating
    s = svc.ingest_fundflow(FundFlowAdapter(_cl(clock)), ["000001"], limit=10)
    assert s.counts["funding_observations"] == 100
    assert _n(conn, "SELECT count(DISTINCT (metric_namespace, metric_name, methodology_version)) AS n FROM intelligence.funding_observation") == 10
    r = conn.execute("SELECT * FROM intelligence.funding_observation WHERE metric_name='main_net_inflow' AND source_record_key='000001|2026-11-09'").fetchone()
    assert r["canonical_value"] == Decimal("13622325") and r["raw_value"] == "13622325.0" and r["raw_unit"] == "CNY" and r["currency"] == "CNY"
    assert r["measurement_class"] == "provider_derived" and r["methodology_id"] == "eastmoney-software-order-size-class" and r["metric_namespace"] == "eastmoney.fund_flow"
    assert r["market_direction"] == "not_applicable" and r["quality_status"] == "valid" and r["field_quality"]["methodology"]["precision_note"].startswith("provider-computed")
    cur = conn.execute("SELECT quality_status FROM intelligence.funding_observation WHERE source_record_key='000001|2026-11-11' AND metric_name='main_net_inflow'").fetchone()
    assert cur["quality_status"] == "partial"
    pct = conn.execute("SELECT * FROM intelligence.funding_observation WHERE metric_name='main_net_inflow_ratio' AND source_record_key='000001|2026-11-09'").fetchone()
    assert pct["raw_unit"] == "percent" and pct["currency"] is None and pct["canonical_value"] == Decimal("0.96")
    wire = {"funding_observation_id": str(r["funding_observation_id"]), "source_id": r["source_id"], "instrument_id": str(r["instrument_id"]), "market_key": None,
            "metric_namespace": r["metric_namespace"], "metric_name": r["metric_name"], "methodology_id": r["methodology_id"], "methodology_version": r["methodology_version"],
            "measurement_class": r["measurement_class"], "side_convention": r["side_convention"], "trade_or_order_basis": r["trade_or_order_basis"],
            "session_scope": r["session_scope"], "market_direction": r["market_direction"], "observation_window_start": _wire_ts(r["observation_window_start"]),
            "observation_window_end": _wire_ts(r["observation_window_end"]), "raw_value": r["raw_value"], "raw_unit": r["raw_unit"],
            "canonical_value": contracts.decimal_str(r["canonical_value"]), "canonical_unit": r["canonical_unit"], "currency": r["currency"], "missing_reason": None,
            "available_at": _wire_ts(r["available_at"]), "quality_status": r["quality_status"], "field_quality": r["field_quality"]}
    contracts.check("FundFlowObservation", wire)


def test_fundflow_revision_when_the_vendor_changes_a_value_and_no_cross_namespace_sum(svc, conn, clock):  # SS-A12
    svc.ingest_fundflow(FundFlowAdapter(_cl(clock)), ["000001"], limit=10)
    doc = json.loads(h.recorded("fundflow_000001.json"))
    parts = doc["data"]["klines"][0].split(",")
    parts[1] = "99999999.0"
    doc["data"]["klines"][0] = ",".join(parts)
    s = svc.ingest_fundflow(FundFlowAdapter(_cl(clock, h.transport(overrides={"fflow/daykline": (200, json.dumps(doc).encode())}))), ["000001"], limit=10)
    assert s.counts["funding_revisions"] == 1 and s.counts["funding_unchanged"] == 99
    revs = conn.execute("SELECT revision, canonical_value FROM intelligence.funding_observation WHERE metric_name='main_net_inflow' AND source_record_key=%s ORDER BY revision",
                        (f"000001|{doc['data']['klines'][0][:10]}",)).fetchall()
    assert [x["revision"] for x in revs] == [1, 2] and revs[1]["canonical_value"] == Decimal("99999999")
    # a different provider with the same metric name is a different series: rows from both can coexist and never merge
    iid = instrument_uuid("XSHE", "000001")
    raw = conn.execute("SELECT raw_object_id FROM ingest.raw_object WHERE source_id='eastmoney_fund_flow'").fetchone()["raw_object_id"]
    conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES ('vendor_b','b','x',now()) ON CONFLICT DO NOTHING")
    # (raw object must belong to the same source: use a vendor_b raw object)
    rid, _ = ingest_repo.insert_raw_object(conn, source_id="vendor_b", endpoint_id="e", source_schema_version="v", source_record_key="k", content_hash="b" * 64,
                                           storage_uri="x", mime_type="application/json", published_at=None, source_time_precision="unknown",
                                           first_seen_at=clock.t, ingested_at=clock.t)
    ok = repo.insert_funding(conn, source_id="vendor_b", raw_object_id=rid, record_key="000001|2026-11-09", revision=1, instrument_id=iid, namespace="vendor_b.flow",
                             name="main_net_inflow", methodology_id="trade-v1", methodology_version="1", measurement_class="provider_derived", side_convention="trade_net",
                             basis="trade", session_scope="regular", direction="not_applicable", window_start=clock.t, window_end=clock.t, raw_value="1", raw_unit="CNY",
                             canonical_value=Decimal(1), canonical_unit="CNY", currency="CNY", missing_reason=None, available_at=clock.t, now=clock.t, quality="valid",
                             field_quality={})
    assert ok
    assert _n(conn, "SELECT count(DISTINCT (source_id, metric_namespace, methodology_id)) AS n FROM intelligence.funding_observation WHERE metric_name='main_net_inflow'") == 2
    assert raw


def test_transport_failure_fails_the_batch_records_health_and_lets_the_next_code_run(svc, conn, clock):
    import httpx

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, content=h.recorded("fundflow_000001.json"))

    s = svc.ingest_fundflow(FundFlowAdapter(_cl(clock, httpx.MockTransport(handler))), ["000001", "000001"], limit=10)
    assert s.counts["transport_errors"] == 1 and s.counts["funding_observations"] == 100
    assert _n(conn, "SELECT count(*) AS n FROM ingest.batch WHERE batch_state='FAILED' AND failure_code='transport_error'") == 1
    assert conn.execute("SELECT consecutive_failures FROM ingest.source_health WHERE source_id='eastmoney_fund_flow'").fetchone()["consecutive_failures"] == 0  # reset by the later success
