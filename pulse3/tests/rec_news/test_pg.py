"""Native news recorder on real PostgreSQL: same document store as the legacy import, revisions, restart safety, claims, alerts, blocks, CLI."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from pulse.adapters.sources import legacy_recordings as LR
from pulse.adapters.sources import native_news
from pulse.adapters.sources.http import RateLimitedClient
from pulse.application import alert_service as AS
from pulse.application import portfolio_service as PS
from pulse.application.account_service import open_paper_account
from pulse.application.bf_live_service import LegacyNewsImporter
from pulse.application.native_news_recorder import NewsRecorder, default_feeds
from pulse.application.native_news_service import NativeNewsWriter
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code

from .support import CLS, EM, THS, Clock, World

pytestmark = pytest.mark.pg
UTC = timezone.utc
NATIVE = native_news.SOURCE_ID


def _n(conn, sql, *a):
    return conn.execute(sql, a).fetchone()["n"]


def ths_body(items: list[dict]) -> bytes:
    return json.dumps({"code": "200", "msg": "成功", "data": {"list": items}}, ensure_ascii=False).encode("utf-8")


def ths_item(item_id: str, title: str, digest: str, published: datetime, codes: list[str] = ()) -> dict:
    return {"id": item_id, "title": title, "digest": digest, "url": f"https://news.10jqka.com.cn/{item_id}.shtml", "ctime": str(int(published.timestamp())),
            "stock": [{"name": "x", "stockCode": c, "stockMarket": "33"} for c in codes], "import": "1"}


def quiet_world(items: list[dict]) -> World:
    """Only Tonghuashun has news; Eastmoney and CLS answer with valid empty lists."""
    w = World()
    w.bodies[THS] = ths_body(items)
    w.bodies[EM] = b'{"code": "1", "data": {"fastNewsList": []}}'
    w.bodies[CLS] = b'{"errno": 0, "data": {"roll_data": []}}'
    return w


def start(conn, lake, clock, tmp_path, world, *, claims=True, sub="day"):
    writer = NativeNewsWriter(conn, lake, clock=clock)
    writer.bootstrap()
    rec = NewsRecorder(tmp_path / sub, writer, default_feeds(world.clients(clock)), clock=clock, sleep=clock.sleep, run_claims=claims)
    return writer, rec, {s.feed.origin: s for s in rec.states}


def test_native_documents_have_the_same_shape_as_the_legacy_import(conn, lake, clock, tmp_path):
    from bf_live import support as LT

    pub, recv = datetime(2026, 11, 12, 7, 55), datetime(2026, 11, 12, 7, 56)
    clock.t = recv.replace(tzinfo=UTC)
    body = "平安银行(000001)今日被证监会立案调查，股价承压。"
    # pulse3's own recorder polls the message first (a legacy-stored item is not stored again by it: see the cutover test below)
    world = quiet_world([ths_item("n1", "平安银行被证监会立案调查", body, pub.replace(tzinfo=UTC), ["000001"])])
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    assert rec.poll(st[THS]) == 1
    # the old recorder's file for the same message, imported by the legacy importer afterwards
    root = tmp_path / "legacy"
    LT.write_news(root, "part-0001.parquet", [LT.news_row("n1", "r1", "平安银行被证监会立案调查", body, pub, recv, ["000001"], source="ths.724")])
    legacy = LegacyNewsImporter(conn, lake, clock=Clock(datetime(2026, 11, 12, 8, 0, tzinfo=UTC)))
    legacy.bootstrap()
    legacy.import_news(LR.LegacyRecordings(root), LT.DAY)
    old, new = [conn.execute("SELECT * FROM intelligence.document_revision WHERE source_id=%s", (s,)).fetchone() for s in (LR.NEWS_SOURCE_ID, NATIVE)]
    assert old["source_record_id"] == new["source_record_id"] == "ths.724:n1"        # same record id: <origin>:<item id>
    for col in ("revision", "document_status", "origin_status", "title", "language_code", "published_at", "published_time_precision", "first_seen_at",
                "quality_status", "event_time"):
        assert old[col] == new[col], col
    assert new["published_at"] == pub.replace(tzinfo=UTC) and new["first_seen_at"] == recv.replace(tzinfo=UTC) and new["available_at"] == clock.t
    assert set(old) == set(new), set(old) ^ set(new)
    assert old["origin_group"] == new["origin_group"]
    strip = lambda fq: {k: {a: b for a, b in v.items() if a != "raw_object_id"} for k, v in fq.items()}      # noqa: E731
    assert strip(old["field_quality"]) == strip(new["field_quality"])
    raws = [json.loads(lake.read_object(conn.execute("SELECT content_hash FROM ingest.raw_object WHERE raw_object_id=%s", (d["raw_object_id"],)).fetchone()["content_hash"]))
            for d in (old, new)]
    assert set(raws[0]) == set(raws[1]) and raws[1]["format"] == native_news.RAW_FORMAT == "legacy-news-revision-v1"
    same = [k for k in raws[0] if k not in ("revision_id", "content_hash", "part", "part_sha256", "source_time_zone", "url", "comments", "level", "source_modified_at")]
    assert {k: raws[0][k] for k in same} == {k: raws[1][k] for k in same}
    # lineage: the native source id, never the legacy ones
    assert new["source_id"] == NATIVE and raws[1]["part"].startswith("raw/ths.724.jsonl.gz#") and len(raws[1]["part_sha256"]) == 64
    assert _n(conn, "SELECT count(*) n FROM ingest.raw_object WHERE source_id=%s", NATIVE) == 1


def test_revisions_restart_and_reimport_add_nothing(conn, lake, clock, tmp_path):
    t0 = clock.t
    items = [ths_item("a1", "某公司拟回购股份", "某公司公告拟回购股份。", t0 - timedelta(minutes=5), ["000002"]), ths_item("a2", "丽人丽妆中标订单", "丽人丽妆(605136)中标订单。", t0 - timedelta(minutes=4), ["605136"])]
    world = quiet_world(items)
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    assert rec.poll(st[THS]) == 2 and rec.poll(st[THS]) == 0
    docs = lambda: conn.execute("SELECT source_record_id, revision, document_status, title FROM intelligence.document_revision WHERE source_id=%s ORDER BY 1,2", (NATIVE,)).fetchall()  # noqa: E731
    assert [(d["source_record_id"], d["revision"], d["document_status"]) for d in docs()] == [("ths.724:a1", 1, "published"), ("ths.724:a2", 1, "published")]
    counts = lambda: (_n(conn, "SELECT count(*) n FROM ingest.raw_object WHERE source_id=%s", NATIVE), _n(conn, "SELECT count(*) n FROM ingest.batch WHERE source_id=%s", NATIVE))  # noqa: E731
    before = counts()
    # a NEW process (new writer, new recorder, empty memory) over the same store: the stored states are found, nothing is written
    clock.advance(minutes=2)
    writer2, rec2, st2 = start(conn, lake, clock, tmp_path, world, sub="restarted")
    assert rec2.poll(st2[THS]) == 0 and counts() == before
    # a correction appends revision 2 of the same document and the raw object chain points back
    items[0] = ths_item("a1", "某公司拟回购股份（更正：金额）", "某公司公告拟回购股份，金额更正。", t0 - timedelta(minutes=5), ["000002"])
    world.bodies[THS] = ths_body(items)
    clock.advance(minutes=1)
    assert rec2.poll(st2[THS]) == 1
    a1 = [d for d in docs() if d["source_record_id"] == "ths.724:a1"]
    assert [(d["revision"], d["document_status"]) for d in a1] == [(1, "published"), (2, "corrected")] and a1[1]["title"].endswith("（更正：金额）")
    ids = {d["document_id"] for d in conn.execute("SELECT document_id FROM intelligence.document_revision WHERE source_record_id='ths.724:a1'").fetchall()}
    assert len(ids) == 1
    raw2 = json.loads(lake.read_object(conn.execute("SELECT r.content_hash FROM intelligence.document_revision d JOIN ingest.raw_object r USING (raw_object_id)"
                                                    " WHERE d.source_record_id='ths.724:a1' AND d.revision=2").fetchone()["content_hash"]))
    raw1 = json.loads(lake.read_object(conn.execute("SELECT r.content_hash FROM intelligence.document_revision d JOIN ingest.raw_object r USING (raw_object_id)"
                                                    " WHERE d.source_record_id='ths.724:a1' AND d.revision=1").fetchone()["content_hash"]))
    assert raw2["supersedes"] == raw1["revision_id"] and raw1["supersedes"] is None
    assert counts() == (before[0] + 1, before[1] + 1)
    # another restart after the correction: still nothing new
    writer3, rec3, st3 = start(conn, lake, clock, tmp_path, world, sub="restarted2")
    assert rec3.poll(st3[THS]) == 0


def test_claims_run_after_the_poll_and_the_alert_scan_reads_them_unchanged(conn, lake, clock, tmp_path):
    now = clock.t
    world = quiet_world([ths_item("n1", "平安银行被证监会立案调查", "平安银行(000001)今日被证监会立案调查，股价承压。", now - timedelta(minutes=2), ["000001"]),
                         ths_item("n2", "丽人丽妆中标大额订单", "丽人丽妆(605136)中标大额订单。", now - timedelta(minutes=3), ["605136"])])
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    assert rec.poll(st[THS]) == 2
    assert rec.claim_totals.get("claims_new", 0) >= 2
    claim = conn.execute("SELECT subject_key, object_payload FROM intelligence.claim_revision WHERE subject_key='000001'").fetchone()
    assert claim is not None and claim["object_payload"]["polarity_hint"] == "-"
    assert _n(conn, "SELECT count(*) n FROM intelligence.claim_evidence ce JOIN intelligence.document_revision d USING (document_revision_id) WHERE d.source_id=%s", NATIVE) >= 2
    assert not writer.claims().get("claims_new")                       # a second claim step finds nothing left to do
    principal, acct = uuid.uuid4(), uuid.uuid4()
    held = instrument_uuid(venue_for_code("000001"), "000001")
    open_paper_account(conn, trading_day=date(2026, 11, 2), account_id=acct, initial_cash="1000000", positions={str(held): {"quantity": 1000, "cost": "10000"}})
    PS.grant(conn, principal, acct, "read", now - timedelta(days=1))
    AS.init_default_rules(conn, principal, now - timedelta(days=1))
    out = AS.scan(conn, now + timedelta(minutes=1), principals=[principal], since=now - timedelta(hours=1))
    assert out["intents"] >= 1
    rows = conn.execute("SELECT i.tier, a.body FROM notification.alert_intent i JOIN ops.ext_artifact a ON a.ref=i.evidence_artifact WHERE i.principal_id=%s AND i.tier='P0'",
                        (principal,)).fetchall()
    assert len(rows) == 1 and rows[0]["body"]["headline"].startswith("平安银行") and rows[0]["body"]["instruments"][0]["instrument_id"] == str(held)
    assert AS.scan(conn, now + timedelta(minutes=1), principals=[principal], since=now - timedelta(hours=1))["intents"] == 0


def test_recorded_answers_of_all_three_feeds_land_in_the_store(conn, lake, clock, tmp_path):
    world = World()
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    rec.once()
    n = lambda origin: _n(conn, "SELECT count(*) n FROM intelligence.document_revision WHERE source_id=%s AND source_record_id LIKE %s", NATIVE, origin + ":%")   # noqa: E731
    assert (n(THS), n(EM), n(CLS)) == (30, 50, 50)
    assert _n(conn, "SELECT count(*) n FROM intelligence.document_revision WHERE source_id<>%s", NATIVE) == 0
    assert rec.claim_totals.get("documents_to_process", 0) >= 130 and _n(conn, "SELECT count(*) n FROM intelligence.claim_revision") > 0
    health = conn.execute("SELECT partition_key, runtime_state FROM ingest.source_health WHERE source_id=%s ORDER BY 1", (NATIVE,)).fetchall()
    assert [h["partition_key"] for h in health] == ["poll:cls.telegraph", "poll:em.724", "poll:ths.724"] and {h["runtime_state"] for h in health} == {"healthy"}
    summary = json.loads((tmp_path / "day" / "summary.json").read_text(encoding="utf-8"))
    assert summary["feeds"][THS]["new"] == 30 and summary["end"] == "once"


def test_a_block_opens_the_circuit_in_the_store_and_a_restart_leaves_that_host_alone(conn, lake, clock, tmp_path):
    world = World()
    world.status[CLS] = 429
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    rec.once()
    assert st[CLS].stopped.startswith("blocked") and rec.blocked and st[THS].new == 30 and st[EM].new == 50
    row = conn.execute("SELECT runtime_state, last_error_code FROM ingest.source_health WHERE source_id=%s AND partition_key='poll:cls.telegraph'", (NATIVE,)).fetchone()
    assert row["runtime_state"] == "unavailable" and row["last_error_code"].startswith("blocked")
    assert writer.circuit_open(CLS) and writer.circuit_open(THS) is None
    # a new day directory (no blocked.json) and a new process: the store's open circuit still keeps the host stopped
    clock.advance(minutes=5)
    world2 = World()
    writer2, rec2, st2 = start(conn, lake, clock, tmp_path, world2, sub="other-day")
    assert st2[CLS].stopped.startswith("circuit open")
    rec2.once()
    assert world2.requests[CLS] == 0 and world2.requests[THS] == 1


def test_cli_once_stores_documents_and_exit_code_two_means_blocked(fresh_dsn, tmp_path, monkeypatch, capsys):
    from pulse.workers import rec_news

    world = World()

    def fake_clients():
        origins = iter(native_news.POLL_EVERY_SECONDS)                       # the CLI builds one client per origin in this order

        def make(**kw):
            return RateLimitedClient(min_interval_seconds=0.0, transport=httpx.MockTransport(world.handler_for(next(origins))), sleep=lambda s: None)
        return make

    args = ["--dsn", fresh_dsn, "--lake", str(tmp_path / "lake"), "--news-dir", str(tmp_path / "news"), "--no-claims", "once"]
    monkeypatch.setattr(rec_news, "RateLimitedClient", fake_clients())
    assert rec_news.run(args) == 0
    out = capsys.readouterr().out
    assert "ths.724: polls=1 new=30" in out and "em.724: polls=1 new=50" in out and "cls.telegraph: polls=1 new=50" in out
    day = datetime.now(UTC).astimezone(rec_news.CST).date().isoformat()
    assert (tmp_path / "news" / day / "summary.json").is_file()
    monkeypatch.setattr(rec_news, "RateLimitedClient", fake_clients())       # second run the same day: nothing new, still exit 0
    assert rec_news.run(args) == 0
    assert out.count("new=") == 3 and capsys.readouterr().out.count("new=0") == 3
    monkeypatch.setattr(rec_news, "RateLimitedClient", fake_clients())       # a block: exit code 2, the other feeds still ran
    world.status[THS] = 403
    assert rec_news.run(args) == 2
    assert "ths.724: polls=1 new=0 revisions=0 stopped=blocked" in capsys.readouterr().out


def _legacy_import(conn, lake, tmp_path, rows):
    from bf_live import support as LT

    root = tmp_path / "legacy"
    LT.write_news(root, "part-0001.parquet", rows)
    imp = LegacyNewsImporter(conn, lake, clock=Clock(datetime(2026, 11, 12, 8, 0, tzinfo=UTC)))
    imp.bootstrap()
    imp.import_news(LR.LegacyRecordings(root), LT.DAY)
    return imp


def test_items_already_imported_from_the_legacy_recorder_are_not_stored_again_after_the_cutover(conn, lake, clock, tmp_path):
    from bf_live import support as LT

    pub, recv = datetime(2026, 11, 12, 7, 50), datetime(2026, 11, 12, 7, 51)
    title, body = "平安银行被证监会立案调查", "平安银行(000001)今日被证监会立案调查，股价承压。"
    imp = _legacy_import(conn, lake, tmp_path, [LT.news_row("n1", "r1", title, body, pub, recv, ["000001"], source="ths.724")])   # content_hash in the file is not the real formula
    imp.process_new_claims()
    clock.t = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    counts = lambda: tuple(_n(conn, q) for q in (                                                                                    # noqa: E731
        "SELECT count(*) n FROM intelligence.document_revision", "SELECT count(*) n FROM ingest.raw_object", "SELECT count(*) n FROM intelligence.claim_revision"))
    before = counts()
    world = quiet_world([ths_item("n1", title, body, pub.replace(tzinfo=UTC), ["000001"])])
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    assert rec.poll(st[THS]) == 0 and counts() == before                       # unchanged: no new document, no raw object, no new claim
    assert _n(conn, "SELECT count(*) n FROM intelligence.document_revision WHERE source_id=%s", NATIVE) == 0
    # the item is corrected after the cutover: exactly one new document revision, native source, pointing back at the legacy revision
    world.bodies[THS] = ths_body([ths_item("n1", title + "（更正）", body, pub.replace(tzinfo=UTC), ["000001"])])
    clock.advance(minutes=1)
    assert rec.poll(st[THS]) == 1
    assert _n(conn, "SELECT count(*) n FROM intelligence.document_revision") == before[0] + 1
    d = conn.execute("SELECT r.content_hash, d.source_record_id FROM intelligence.document_revision d JOIN ingest.raw_object r USING (raw_object_id) WHERE d.source_id=%s", (NATIVE,)).fetchone()
    new_raw = json.loads(lake.read_object(d["content_hash"]))
    assert d["source_record_id"] == "ths.724:n1" and new_raw["supersedes"] == "r1" and new_raw["title"].endswith("（更正）")
    # the same restart-proof state: a new process sees the native correction as the latest state
    writer2, rec2, st2 = start(conn, lake, clock, tmp_path, world, sub="again")
    assert rec2.poll(st2[THS]) == 0


def test_the_origin_key_of_an_own_dispatch_is_the_same_whichever_recorder_stored_it(conn, lake, clock, tmp_path):
    from bf_live import support as LT

    pub = datetime(2026, 11, 12, 7, 50)
    title, body = "某公司拟回购股份", "财联社11月12日电，某公司公告拟回购股份。"
    _legacy_import(conn, lake, tmp_path, [LT.news_row("L1", "r1", title, body, pub, pub + timedelta(minutes=1), ["000002"], source="cls.telegraph")])
    clock.t = datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    world = quiet_world([ths_item("N1", title, body, pub.replace(tzinfo=UTC), ["000002"])])         # another item with the same title and day, stored natively
    writer, rec, st = start(conn, lake, clock, tmp_path, world)
    assert rec.poll(st[THS]) == 1
    groups = {r["source_id"]: r["origin_group"] for r in conn.execute("SELECT source_id, origin_group FROM intelligence.document_revision").fetchall()}
    assert groups[NATIVE] == groups[LR.NEWS_SOURCE_ID] and groups[NATIVE].startswith("own:legacy_news_recorder:2026-11-12:")
