"""Recorded 7x24 news -> documents (revisions kept, idempotent) -> claims -> pulse3's own alert scan, on real PostgreSQL."""
from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

import pytest

from pulse.adapters.sources import legacy_recordings as LR
from pulse.application import alert_service as AS
from pulse.application import portfolio_service as PS
from pulse.application.account_service import open_paper_account
from pulse.application.bf_live_service import LegacyNewsImporter
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code

from . import support as T

pytestmark = pytest.mark.pg
UTC = timezone.utc
PUB = datetime(2026, 11, 12, 7, 55)          # naive UTC, 5 minutes before the import clock (08:00 UTC)
RECV = datetime(2026, 11, 12, 7, 56)


def _n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()["n"]


def _importer(conn, lake, clock):
    imp = LegacyNewsImporter(conn, lake, clock=clock)
    imp.bootstrap()
    return imp


def _rows():
    return [T.news_row("n1", "r1", "平安银行被证监会立案调查", "平安银行(000001)今日被证监会立案调查，股价承压。", PUB, RECV, ["000001"]),
            T.news_row("n2", "r1", "丽人丽妆中标大额订单", "丽人丽妆(605136)中标大额订单。", PUB, RECV, ["605136"]),
            T.news_row("n3", "r1", "某公司拟回购股份", "某公司公告拟回购股份。", PUB, RECV, ["000002"]),
            T.news_row("n3", "r2", "某公司拟回购股份（更正：金额）", "某公司公告拟回购股份，金额更正。", PUB, RECV + timedelta(minutes=1), ["000002"], supersedes="r1")]


def test_documents_revisions_and_reimport_idempotence(conn, lake, legacy_root, clock):
    T.write_news(legacy_root, "part-0001.parquet", _rows())
    rec = LR.LegacyRecordings(legacy_root)
    imp = _importer(conn, lake, clock)
    s = imp.import_news(rec, T.DAY)
    assert s.counts["documents_published"] == 3 and s.counts["documents_corrected"] == 1
    docs = conn.execute("SELECT * FROM intelligence.document_revision WHERE source_id=%s ORDER BY source_record_id, revision", (LR.NEWS_SOURCE_ID,)).fetchall()
    assert [(d["source_record_id"], d["revision"], d["document_status"]) for d in docs] == [
        ("ths_724:n1", 1, "published"), ("ths_724:n2", 1, "published"), ("ths_724:n3", 1, "published"), ("ths_724:n3", 2, "corrected")]
    n3 = [d for d in docs if d["source_record_id"] == "ths_724:n3"]
    assert n3[0]["document_id"] == n3[1]["document_id"] and n3[1]["title"].endswith("（更正：金额）")
    # PIT: the source publication time is kept; first seen = the recorder's received_at; available = import time
    d1 = docs[0]
    assert d1["published_at"] == PUB.replace(tzinfo=UTC) and d1["first_seen_at"] == RECV.replace(tzinfo=UTC)
    assert d1["available_at"] == clock.t and d1["published_time_precision"] == "second"
    # raw object per message revision: lossless, keeps the recorder's ids and the zone decision
    raw = conn.execute("SELECT content_hash FROM ingest.raw_object WHERE raw_object_id=%s", (d1["raw_object_id"],)).fetchone()
    body = lake.read_object(raw["content_hash"]).decode("utf-8")
    assert '"revision_id":"r1"' in body and '"source_time_zone":"UTC"' in body
    batches = _n(conn, "SELECT count(*) n FROM ingest.batch WHERE source_id=%s", LR.NEWS_SOURCE_ID)
    # re-import adds nothing (no batch either)
    s2 = imp.import_news(rec, T.DAY)
    assert s2.counts["documents_unchanged"] == 3 and "documents_published" not in s2.counts
    assert _n(conn, "SELECT count(*) n FROM intelligence.document_revision WHERE source_id=%s", LR.NEWS_SOURCE_ID) == 4
    assert _n(conn, "SELECT count(*) n FROM ingest.batch WHERE source_id=%s", LR.NEWS_SOURCE_ID) == batches
    # a later part with a third state of n3 and a duplicate-content revision: exactly one new revision
    clock.advance(minutes=10)
    T.write_news(legacy_root, "part-0002.parquet", [
        T.news_row("n3", "r3", "某公司拟回购股份（再次更正）", "某公司公告拟回购股份，再次更正。", PUB, RECV + timedelta(minutes=5), ["000002"], supersedes="r2"),
        T.news_row("n2", "r1b", "丽人丽妆中标大额订单", "丽人丽妆(605136)中标大额订单。", PUB, RECV + timedelta(minutes=6), ["605136"], supersedes="r1")])
    s3 = imp.import_news(rec, T.DAY)
    assert s3.counts["documents_corrected"] == 1 and "documents_published" not in s3.counts
    latest = conn.execute("SELECT revision, available_at FROM intelligence.document_revision WHERE source_record_id='ths_724:n3' ORDER BY revision DESC LIMIT 1").fetchone()
    assert latest["revision"] == 3 and latest["available_at"] == clock.t                 # the new revision is available at ITS import
    assert _n(conn, "SELECT count(*) n FROM intelligence.document_revision WHERE source_record_id='ths_724:n2'") == 1   # same content: no revision
    assert imp.import_news(rec, T.DAY).counts["documents_unchanged"] == 3


def test_claims_then_pulse3_alert_for_a_held_code_and_no_copy_of_old_alerts(conn, lake, legacy_root, clock):
    T.write_news(legacy_root, "part-0001.parquet", _rows())
    (legacy_root / "news" / T.DAY.isoformat() / "alerts.jsonl").write_text('{"level": "P0", "code": "000001"}\n', encoding="utf-8")
    rec = LR.LegacyRecordings(legacy_root)
    imp = _importer(conn, lake, clock)
    imp.import_news(rec, T.DAY)
    c = imp.process_new_claims()
    assert c.counts["claims_new"] >= 2
    claim = conn.execute("SELECT subject_key, claim_status, object_payload FROM intelligence.claim_revision WHERE subject_key='000001'").fetchone()
    assert claim is not None and claim["object_payload"]["polarity_hint"] == "-"
    # the user holds 000001 and watches nothing else
    principal = uuid.uuid4()
    held = instrument_uuid(venue_for_code("000001"), "000001")
    acct = uuid.uuid4()
    open_paper_account(conn, trading_day=T.DAY.replace(day=2), account_id=acct, initial_cash="1000000", positions={str(held): {"quantity": 1000, "cost": "10000"}})
    PS.grant(conn, principal, acct, "read", clock.t - timedelta(days=1))
    AS.init_default_rules(conn, principal, clock.t - timedelta(days=1))
    now = clock.t + timedelta(minutes=2)
    out = AS.scan(conn, now, principals=[principal], since=clock.t - timedelta(hours=1))
    assert out["intents"] >= 1
    rows = conn.execute("SELECT i.tier, a.body FROM notification.alert_intent i JOIN ops.ext_artifact a ON a.ref=i.evidence_artifact"
                        " WHERE i.principal_id=%s AND i.tier='P0'", (principal,)).fetchall()
    assert len(rows) == 1 and rows[0]["body"]["headline"].startswith("平安银行") and rows[0]["body"]["instruments"][0]["instrument_id"] == str(held)
    assert AS.scan(conn, now, principals=[principal], since=clock.t - timedelta(hours=1))["intents"] == 0        # re-scan: nothing new
    # the old system's alerts.jsonl is importable as a comparison dataset but never becomes a pulse3 alert
    from pulse.application.bf_live_service import LiveService

    live = LiveService(conn, lake, rec, clock=clock)
    live.bootstrap()
    assert live.import_alerts(T.DAY).counts["rows_imported"] == 1
    assert _n(conn, "SELECT count(*) n FROM notification.alert_intent WHERE source_claim_revision_id IS NULL") == 0     # every alert comes from a pulse3 claim


def test_history_imported_late_never_alerts(conn, lake, legacy_root, clock):
    """The first import of an old day: items published hours before the import are backfill, not breaking news."""
    old_pub, old_recv = datetime(2026, 11, 12, 1, 0), datetime(2026, 11, 12, 1, 1)
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("o1", "r1", "平安银行被证监会立案调查", "平安银行(000001)被立案。", old_pub, old_recv, ["000001"])])
    imp = _importer(conn, lake, clock)
    imp.import_news(LR.LegacyRecordings(legacy_root), T.DAY)
    imp.process_new_claims()
    principal = uuid.uuid4()
    held = instrument_uuid(venue_for_code("000001"), "000001")
    acct = uuid.uuid4()
    open_paper_account(conn, trading_day=T.DAY.replace(day=2), account_id=acct, initial_cash="1000000", positions={str(held): {"quantity": 1000, "cost": "10000"}})
    PS.grant(conn, principal, acct, "read", clock.t - timedelta(days=1))
    AS.init_default_rules(conn, principal, clock.t - timedelta(days=1))
    out = AS.scan(conn, clock.t + timedelta(minutes=1), principals=[principal], since=clock.t - timedelta(hours=1))
    assert out["intents"] == 0 and out["skipped_backfill"] >= 1


def test_cli_news_import_then_alert_scan_is_idempotent(fresh_dsn, tmp_path, legacy_root, capsys):
    """The scheduled sequence: ``ingest legacy-news`` (documents + claims) then ``alerts scan``; a second run changes nothing."""
    from pulse.adapters.postgres import db, instrument_repo
    from pulse.domain.instruments.identity import InstrumentRecord
    from pulse.workers import alerts, ingest

    c = db.connect(fresh_dsn, autocommit=True)
    with c.transaction():
        instrument_repo.upsert_instruments(c, [InstrumentRecord.build("000001", "平安银行")], datetime(2026, 11, 1, tzinfo=UTC), (), "cli")
    now = datetime.now(UTC).replace(microsecond=0)
    pub = (now - timedelta(minutes=3)).replace(tzinfo=None)
    T.write_news(legacy_root, "part-0001.parquet", [T.news_row("n1", "r1", "平安银行被证监会立案调查", "平安银行(000001)被证监会立案调查。", pub, pub + timedelta(minutes=1), ["000001"])])
    principal = uuid.uuid4()
    held = instrument_uuid(venue_for_code("000001"), "000001")
    acct = uuid.uuid4()
    open_paper_account(c, trading_day=T.DAY.replace(day=2), account_id=acct, initial_cash="1000000", positions={str(held): {"quantity": 1000, "cost": "10000"}})
    PS.grant(c, principal, acct, "read", now - timedelta(days=1))
    AS.init_default_rules(c, principal, now - timedelta(days=1))
    base = ["--dsn", fresh_dsn, "--lake", str(tmp_path / "cli-lake")]
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    args = [*base, "legacy-news", "--date", day, "--legacy-data", str(legacy_root)]
    (legacy_root / "news" / T.DAY.isoformat()).rename(legacy_root / "news" / day)
    assert ingest.run(args) == 0
    out = capsys.readouterr().out
    assert "documents_published: 1" in out and "claims_new: 1" in out
    assert alerts.run([*base, "scan"]) == 0
    assert _n(c, "SELECT count(*) n FROM notification.alert_intent WHERE principal_id=%s AND tier='P0'", principal) == 1
    assert ingest.run(args) == 0                                           # second run: nothing new
    assert "documents_published" not in capsys.readouterr().out
    assert alerts.run([*base, "scan"]) == 0
    assert _n(c, "SELECT count(*) n FROM notification.alert_intent WHERE principal_id=%s", principal) == 1
    c.close()
