"""Real-PG tests on recorded live endpoints (MockTransport, no network): raw->observation->bars->manifest, health, announcements."""
from __future__ import annotations

import hashlib
import itertools
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, market_repo
from pulse.adapters.sources.cninfo import CninfoAdapter
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.adapters.sources.sina import SinaAdapter
from pulse.adapters.sources.tencent import TencentAdapter
from pulse.application.data_service import DataService

from .helpers import LEGACY_MINI, mock_transport, recorded

pytestmark = pytest.mark.pg
T0 = datetime(2026, 11, 11, 8, 30, tzinfo=timezone.utc)  # 16:30 CST: after the 16:00 daily finality boundary
CODES = ["000001", "600000", "002415"]


def clock_from(start):
    c = itertools.count()
    return lambda: start + timedelta(seconds=next(c))


def client(transport, fetched_at=None) -> RateLimitedClient:
    return RateLimitedClient(min_interval_seconds=0, transport=transport, now=clock_from(fetched_at or T0 - timedelta(minutes=1)))


@pytest.fixture()
def svc(fresh_dsn, tmp_path):
    conn = db.connect(fresh_dsn, autocommit=True)
    s = DataService(conn, LakeStore(tmp_path / "lake"), clock=clock_from(T0))
    s.bootstrap()
    s.import_legacy(LegacyWarehouse(LEGACY_MINI), date(2025, 11, 1))
    yield s
    conn.close()


def rows(svc, sql, *a):
    return svc.conn.execute(sql, a).fetchall()


def test_live_daily_sina_stores_raw_projects_valid_bars_and_publishes(svc):
    s = svc.ingest_daily(SinaAdapter(client(mock_transport())), "sina", CODES, count=30)
    assert s.counts["captures_stored"] == 3 and s.counts["observations_created"] == 90
    raws = rows(svc, "select content_hash, storage_uri from ingest.raw_object where source_id='sina' and endpoint_id='kline_daily'")
    assert len(raws) == 3
    for r in raws:  # raw bytes kept untouched
        assert hashlib.sha256((svc.lake.root / r["storage_uri"]).read_bytes()).hexdigest() == r["content_hash"]
    bars = rows(svc, "select * from market.bar_projection where timeframe='1d' order by instrument_id, trade_date")
    assert len(bars) == 90
    last = [b for b in bars if b["trade_date"] == date(2026, 11, 10) and str(b["instrument_id"]).startswith("260eb48a")][0]
    assert last["volume_shares"] == 74888996 and last["turnover_amount_cny"] is None  # exact shares; missing amount stays NULL
    assert last["quality_status"] == "partial" and last["is_final"] is True and last["available_at"] >= last["bar_close_time"]
    snap = rows(svc, "select * from market.dataset_manifest where dataset_id='bars_raw_1d'")[0]
    assert snap["manifest_state"] == "READY" and snap["row_count"] == 90
    assert svc.lake.read_snapshot_frames(snap["content_hash"]).height == 90
    # every projected bar can be rendered as a contract-valid CanonicalBar
    from pulse.domain.market import builders
    obs = rows(svc, "select canonical_payload, field_quality, entity_key from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.source_id='sina' limit 5")
    for o in obs:
        p = {**o["canonical_payload"], "instrument_id": o["entity_key"], "snapshot_id": str(snap["snapshot_id"]), "currency": "CNY",
             "exchange_reference_price_raw": None, "field_quality": o["field_quality"], "available_at": "2026-11-11T08:30:00Z",
             "quality_status": "partial"}
        p.pop("adjustment_version", None)
        contracts.check("CanonicalBar", p)
    health = rows(svc, "select * from ingest.source_health where source_id='sina' and endpoint_id='kline_daily'")[0]
    assert health["runtime_state"] == "healthy" and health["last_new_record_at"] is not None and health["last_canonical_ready_at"] is not None


def test_todays_bar_before_16_cst_is_not_final(svc):
    early = clock_from(datetime(2026, 11, 11, 7, 5, tzinfo=timezone.utc))  # 15:05 CST
    svc.clock = early
    svc.ingest_daily(TencentAdapter(client(mock_transport(), datetime(2026, 11, 11, 7, 4, tzinfo=timezone.utc))), "tencent", ["000001"], count=30)
    bars = rows(svc, "select trade_date, is_final from market.bar_projection where timeframe='1d' order by trade_date")
    assert [b["is_final"] for b in bars if b["trade_date"] == date(2026, 11, 11)] == [False]
    assert all(b["is_final"] for b in bars if b["trade_date"] < date(2026, 11, 11))


def test_second_fetch_of_same_data_adds_no_facts_and_changed_data_appends_a_revision(svc):
    sina = SinaAdapter(client(mock_transport()))
    svc.ingest_daily(sina, "sina", ["000001"], count=30)
    n1 = rows(svc, "select count(*) n from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.source_id='sina'")[0]["n"]
    again = svc.ingest_daily(sina, "sina", ["000001"], count=30)
    assert again.counts["observations_unchanged"] == 30 and rows(svc, "select count(*) n from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.source_id='sina'")[0]["n"] == n1
    body = recorded("sina_kline_240_sz000001.json").replace(b'"close":"11.860","volume":"74888996"', b'"close":"11.850","volume":"74888996"')
    assert body != recorded("sina_kline_240_sz000001.json")
    rev = svc.ingest_daily(SinaAdapter(client(mock_transport(overrides={r"getKLineData.*sz000001": httpx.Response(200, content=body)}))), "sina", ["000001"], count=30)
    assert rev.counts["observation_revisions_appended"] == 1
    r = rows(svc, "select observation_revision, canonical_payload->>'close_price_raw' c from ingest.observation where natural_key like '%%2026-11-10%%raw' order by 1")
    assert [(x["observation_revision"], x["c"]) for x in r] == [(1, "11.86"), (2, "11.85")]  # prior revision kept
    # three snapshots exist, each immutable, version numbers gap-free
    assert [m["manifest_version"] for m in rows(svc, "select manifest_version from market.dataset_manifest where dataset_id='bars_raw_1d' order by 1")] == [1, 2, 3]


def test_tencent_daily_converts_lots_and_agrees_with_sina_within_a_lot(svc):
    svc.ingest_daily(SinaAdapter(client(mock_transport())), "sina", ["000001"], count=30)
    svc.ingest_daily(TencentAdapter(client(mock_transport())), "tencent", ["000001"], count=30)
    pair = rows(svc, "select o.canonical_payload->>'trade_date' d, r.source_id, (o.canonical_payload->>'volume_shares')::bigint v,"
                     " o.canonical_payload->>'close_price_raw' c from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.endpoint_id in ('kline_daily','kline_day')")
    by = {}
    for p in pair:
        by.setdefault(p["d"], {})[p["source_id"]] = p
    common = [v for v in by.values() if len(v) == 2]
    assert len(common) >= 20
    for v in common:
        assert v["sina"]["c"] == v["tencent"]["c"] and abs(v["sina"]["v"] - v["tencent"]["v"]) <= 100


def test_cross_source_reconciliation_is_stored_and_classified(svc):
    svc.ingest_daily(SinaAdapter(client(mock_transport())), "sina", CODES, count=30)
    svc.ingest_daily(TencentAdapter(client(mock_transport())), "tencent", CODES, count=30)
    out = svc.reconcile_daily(["000001", "600000"], last_n_dates=3)
    assert out.counts
    res = rows(svc, "select reconciliation_status, field_path, normalization_trace from ops.reconciliation_result")
    assert res and {r["reconciliation_status"] for r in res} <= {"matched", "disputed", "unverifiable"}
    vol = [r for r in res if r["field_path"].endswith("volume_shares") and r["normalization_trace"]["pair"] == ["sina", "tencent"]]
    assert vol and all(r["reconciliation_status"] == "matched" for r in vol)  # lots x100 vs exact shares inside one lot
    # legacy (forward adjusted) close vs raw: same basis only where no later ex-right exists, otherwise unverifiable (not a dispute)
    leg = [r for r in res if r["normalization_trace"]["pair"][0] == "legacy_warehouse" and r["field_path"].endswith("close_price")]
    assert leg and {r["reconciliation_status"] for r in leg} <= {"matched", "unverifiable"}
    assert rows(svc, "select count(*) n from ops.reconciliation_member")[0]["n"] >= 2 * len(res)


def test_minute_bars_are_interval_volume_on_the_regular_grid_and_final_only_after_close(svc):
    t = datetime(2026, 11, 11, 7, 20, tzinfo=timezone.utc)  # 15:20 CST
    svc.clock = clock_from(t)
    tc = TencentAdapter(client(mock_transport(), t - timedelta(seconds=30)))
    s = svc.ingest_minute(tc, "tencent", ["000001"], period_minutes=1, count=240)
    assert s.counts["observations_created"] > 100
    bars = rows(svc, "select * from market.bar_projection where timeframe='1m' order by bar_close_time")
    assert bars and all(b["bar_close_time"] - b["bar_open_time"] == timedelta(minutes=1) for b in bars)
    assert all(b["turnover_amount_cny"] is None and b["quality_status"] == "partial" for b in bars)
    assert all(b["volume_shares"] % 100 == 0 for b in bars)  # lots converted exactly, fractional lots never rounded away silently
    assert all(b["is_final"] for b in bars)  # captured > 60 s after each close
    five = svc.ingest_minute(SinaAdapter(client(mock_transport(), t - timedelta(seconds=30))), "sina", ["000001"], period_minutes=5, count=96)
    b5 = rows(svc, "select * from market.bar_projection where timeframe='5m' order by bar_close_time")
    assert len(b5) == five.counts["observations_created"] and b5[0]["bar_close_time"] - b5[0]["bar_open_time"] == timedelta(minutes=5)
    assert {m["dataset_id"] for m in rows(svc, "select dataset_id from market.dataset_manifest")} >= {"bars_raw_1m", "bars_raw_5m"}


def test_minute_bar_just_captured_is_not_final(svc):
    close = datetime(2026, 11, 11, 6, 45, tzinfo=timezone.utc)  # the latest bar in the m5 fixture is 15:00; use capture 20 s after 14:45 close
    t = datetime(2026, 11, 11, 6, 45, 20, tzinfo=timezone.utc)
    svc.clock = clock_from(t)
    svc.ingest_minute(SinaAdapter(client(mock_transport(), t - timedelta(seconds=1))), "sina", ["000001"], period_minutes=5, count=96)
    row = rows(svc, "select is_final from market.bar_projection where timeframe='5m' and bar_close_time=%s", close)
    assert row == [{"is_final": False}]


def test_quote_unit_mismatch_is_detected_and_recorded(svc):
    bad = recorded("sina_hq.txt").decode("gbk").replace("105703839,1245341421.540", "1057038,1245341421.540").encode("gbk")
    assert bad != recorded("sina_hq.txt")
    tr = mock_transport(overrides={r"hq\.sinajs\.cn": httpx.Response(200, content=bad)})
    s = svc.ingest_quotes(SinaAdapter(client(tr)), "sina", ["000001", "600000", "002415"])
    assert s.counts["quotes_unit_mismatch"] == 1
    issue = rows(svc, "select issue_code, issue_payload from ops.data_issue where issue_code='unit_mismatch'")[0]
    assert "lots_as_shares" in issue["issue_payload"]["detail"]
    q = rows(svc, "select quality_status from ingest.observation o join ingest.raw_object r using (raw_object_id) where r.endpoint_id='hq_quote' order by quality_status")
    assert "disputed" in {x["quality_status"] for x in q}


def test_block_stops_the_run_immediately_and_opens_health(svc):
    calls: list = []
    tr = mock_transport(overrides={r"sh600000": httpx.Response(403, content=b"forbidden")}, calls=calls)
    with pytest.raises(SourceBlocked):
        svc.ingest_daily(SinaAdapter(client(tr)), "sina", ["000001", "600000", "002415"], count=30)
    assert len(calls) == 2  # third symbol was never requested
    h = rows(svc, "select runtime_state, last_error_code, next_attempt_at from ingest.source_health where endpoint_id='kline_daily'")[0]
    assert h["runtime_state"] == "unavailable" and h["last_error_code"].startswith("blocked") and h["next_attempt_at"] > T0
    assert rows(svc, "select batch_state, failure_code from ingest.batch where endpoint_id='kline_daily'")[0] == {"batch_state": "FAILED", "failure_code": "blocked"}
    assert not rows(svc, "select 1 from market.dataset_manifest where dataset_id='bars_raw_1d'")  # nothing half-published


def test_login_page_and_schema_drift_are_not_data(svc):
    html = httpx.Response(200, content="<html>请登录 login captcha</html>".encode(), headers={"content-type": "text/html"})
    s = svc.ingest_daily(SinaAdapter(client(mock_transport(overrides={r"getKLineData.*sz000001": html}))), "sina", ["000001"], count=30)
    assert s.counts["captures_auth_required"] == 1 and not s.counts.get("observations_created")
    h = rows(svc, "select runtime_state from ingest.source_health where endpoint_id='kline_daily'")[0]
    assert h["runtime_state"] == "auth_required"  # a login page is never "healthy"
    assert rows(svc, "select count(*) n from ingest.raw_object where source_id='sina' and mime_type='text/html'")[0]["n"] == 1  # evidence kept
    renamed = recorded("sina_kline_240_sz000001.json").replace(b'"volume"', b'"vol"')
    s = svc.ingest_daily(SinaAdapter(client(mock_transport(overrides={r"getKLineData.*sz000001": httpx.Response(200, content=renamed)}))), "sina", ["000001"], count=30)
    assert s.counts.get("captures_schema_changed") == 1 and any("paused" in n for n in s.notes)
    h = rows(svc, "select runtime_state, last_error_code from ingest.source_health where endpoint_id='kline_daily'")[0]
    assert h["runtime_state"] == "degraded" and "missing_mapped_keys" in h["last_error_code"]
    assert not rows(svc, "select 1 from market.dataset_manifest where dataset_id='bars_raw_1d'")


# ---- announcements ---------------------------------------------------------------------------------------
def ann_svc(svc, transport, now=None):
    return CninfoAdapter(client(transport, now))


def test_announcements_preserve_original_ids_urls_events_and_are_idempotent(svc):
    svc.clock = clock_from(datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc))
    s = svc.ingest_announcements(ann_svc(svc, mock_transport()), days=1, page_size=30)
    assert s.counts["pages_fetched"] >= 3 and s.counts["documents_published"] > 40
    docs = rows(svc, "select * from intelligence.document_revision order by source_record_id")
    page1 = contracts.check  # noqa: F841
    import json
    first = json.loads(recorded("cninfo_page1.json"))["announcements"][0]
    d = [x for x in docs if x["source_record_id"] == str(first["announcementId"])][0]
    assert d["revision"] == 1 and d["document_status"] == "published" and d["origin_status"] == "confirmed_original"
    assert d["raw_object_id"] is not None and d["available_at"] >= d["first_seen_at"]
    wire = {"document_revision_id": str(d["document_revision_id"]), "document_id": str(d["document_id"]), "source_id": d["source_id"], "revision": 1,
            "title": d["title"], "origin_group": d["origin_group"], "origin_status": d["origin_status"],
            "published_at": d["published_at"].isoformat().replace("+00:00", "Z"), "published_time_precision": d["published_time_precision"],
            "first_seen_at": d["first_seen_at"].isoformat().replace("+00:00", "Z"), "available_at": d["available_at"].isoformat().replace("+00:00", "Z"),
            "quality_status": d["quality_status"], "document_status": d["document_status"], "raw_object_id": str(d["raw_object_id"]),
            "field_quality": {k: v for k, v in d["field_quality"].items() if k != "record_content_hash"}}
    for v in wire["field_quality"].values():
        v["raw_object_id"] = wire["raw_object_id"]
    contracts.check("EvidenceDocument", wire)
    claims = rows(svc, "select assertion_type, claim_status, predicate from intelligence.claim_revision where predicate='announcement.published' limit 1")[0]
    assert claims == {"assertion_type": "official_disclosure", "claim_status": "supported", "predicate": "announcement.published"}
    assert rows(svc, "select count(*) n from intelligence.claim_evidence")[0]["n"] >= len(docs)
    n_docs = len(docs)
    again = svc.ingest_announcements(ann_svc(svc, mock_transport()), days=1, page_size=30)
    assert again.counts["announcements_unchanged"] == n_docs and "documents_published" not in again.counts  # no repeated business conclusion
    assert rows(svc, "select count(*) n from intelligence.document_revision")[0]["n"] == n_docs
    cur = rows(svc, "select cursor_version, committed_cursor from ingest.source_cursor where endpoint_id='his_announcement_query'")[0]
    assert cur["cursor_version"] == 2 and cur["committed_cursor"]["complete"] is True


def test_announcement_correction_appends_a_revision_and_withdrawal_is_inferred_only_from_a_fully_paged_window(svc):
    import json
    svc.clock = clock_from(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))  # the fixture window is 2026-11-10
    svc.ingest_announcements(ann_svc(svc, mock_transport()), days=1)
    p1 = json.loads(recorded("cninfo_page1.json"))
    target = p1["announcements"][0]["announcementId"]
    # (a) the same id comes back with a changed attachment -> revision 2 'corrected'
    p1c = json.loads(recorded("cninfo_page1.json"))
    p1c["announcements"][0]["adjunctSize"] = (p1c["announcements"][0].get("adjunctSize") or 0) + 7
    tr = mock_transport(overrides={r"pageNum=1&": httpx.Response(200, content=json.dumps(p1c).encode())})
    s = svc.ingest_announcements(ann_svc(svc, tr), days=1)
    assert s.counts["documents_corrected"] == 1
    revs = rows(svc, "select revision, document_status from intelligence.document_revision where source_record_id=%s order by revision", str(target))
    assert [(r["revision"], r["document_status"]) for r in revs] == [(1, "published"), (2, "corrected")]
    # (b) a partially paged window (page cap) must NOT infer withdrawals
    only_first = mock_transport(overrides={r"pageNum=2&": httpx.Response(200, content=json.dumps({**json.loads(recorded("cninfo_page2.json")), "hasMore": True}).encode()),
                                           r"pageNum=3&": httpx.Response(200, content=json.dumps({**json.loads(recorded("cninfo_page2.json")), "hasMore": True}).encode())})
    s = svc.ingest_announcements(ann_svc(svc, only_first), days=1, page_cap=2)
    assert not s.counts.get("documents_deleted_at_source")
    # (c) fully paged window where one id vanished -> deleted_at_source revision
    p2 = json.loads(recorded("cninfo_page2.json"))
    gone = p2["announcements"].pop(0)
    tr = mock_transport(overrides={r"pageNum=2&": httpx.Response(200, content=json.dumps({**p2, "hasMore": False}).encode())})
    s = svc.ingest_announcements(ann_svc(svc, tr), days=1)
    assert s.counts["documents_deleted_at_source"] == 1
    last = rows(svc, "select revision, document_status from intelligence.document_revision where source_record_id=%s order by revision desc limit 1", str(gone["announcementId"]))[0]
    assert last["document_status"] == "deleted_at_source" and last["revision"] == 2


def test_announcement_login_page_and_block_do_not_produce_documents(svc):
    svc.clock = clock_from(datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc))
    html = httpx.Response(200, content="<html>login</html>".encode())
    s = svc.ingest_announcements(ann_svc(svc, mock_transport(overrides={r"pageNum=1&": html})), days=1)
    assert s.counts["pages_auth_required"] == 1
    assert rows(svc, "select count(*) n from intelligence.document_revision")[0]["n"] == 0
    h = rows(svc, "select runtime_state from ingest.source_health where endpoint_id='his_announcement_query'")[0]
    assert h["runtime_state"] == "auth_required"
    calls: list = []
    with pytest.raises(SourceBlocked):
        svc.ingest_announcements(ann_svc(svc, mock_transport(overrides={r"pageNum=1&": httpx.Response(501, content=b"x")}, calls=calls)), days=1)
    assert len(calls) == 1
    h = rows(svc, "select runtime_state, last_error_code from ingest.source_health where endpoint_id='his_announcement_query'")[0]
    assert h["runtime_state"] == "unavailable" and h["last_error_code"].startswith("blocked")


def test_partition_missing_is_an_explicit_error_not_silent_accumulation(svc):
    import psycopg
    with pytest.raises(psycopg.Error):
        svc.conn.execute(
            "insert into market.bar_projection(trade_date, instrument_id, timeframe, session_scope, bar_open_time, bar_close_time, snapshot_id,"
            " currency, is_final, available_at, quality_status) select '2040-01-02', instrument_id, '1d', 's', now(), now()+interval '1h',"
            " (select snapshot_id from market.dataset_manifest limit 1), 'CNY', false, now(), 'partial' from core.instrument limit 1")


def test_cli_commands_run_end_to_end_and_report_exit_codes(fresh_dsn, tmp_path, monkeypatch, capsys):
    from pulse.workers import ingest as cli

    def patched(**kw):
        return RateLimitedClient(min_interval_seconds=kw["min_interval_seconds"], transport=mock_transport(), now=clock_from(T0))

    monkeypatch.setattr(cli, "RateLimitedClient", patched)
    base = ["--dsn", fresh_dsn, "--lake", str(tmp_path / "lake"), "--min-interval", "0"]
    assert cli.run(base + ["legacy-import", "--since", "2026-10-01", "--warehouse", str(LEGACY_MINI)]) == 0
    assert cli.run(base + ["live-daily", "--codes", "000001", "600000", "--source", "both", "--count", "30"]) == 0
    assert cli.run(base + ["minute", "--codes", "000001", "--source", "tencent", "--period", "5", "--count", "96"]) == 0
    assert cli.run(base + ["announcements", "--days", "1"]) == 0
    assert cli.run(base + ["reconcile", "--codes", "000001"]) == 0
    assert cli.run(base + ["gc", "--min-age-seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "legacy-import" in out and "live-daily sina" in out and "live-daily tencent" in out and "http_requests:" in out

    def blocked(**kw):
        return RateLimitedClient(min_interval_seconds=0, transport=mock_transport(overrides={r"qt\.gtimg": httpx.Response(403, content=b"no")}), now=clock_from(T0))

    monkeypatch.setattr(cli, "RateLimitedClient", blocked)
    assert cli.run(base + ["quote", "--codes", "000001", "--source", "tencent"]) == 2  # blocked -> distinct exit code
