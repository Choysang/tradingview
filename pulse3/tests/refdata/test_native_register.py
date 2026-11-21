"""Native instrument register, short-name / ST versions, cap snapshot and the code helper behind PULSE_REFDATA_SOURCE (legacy stays unchanged)."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from pulse.adapters.postgres import instrument_repo
from pulse.adapters.sources import legacy_recordings as LR
from pulse.application import bindings as B
from pulse.application import intraday_rows as IR
from pulse.application import native_register as NR
from pulse.application import refdata_core as C
from pulse.application.refdata_service import instrument_id_of
from pulse.domain.instruments.identity import InstrumentRecord
from pulse.domain.ranking.universe import apply_universe

from .support import World
from .test_pg_flow import build

UTC = timezone.utc
LEGACY_SOURCES = ("legacy_warehouse", "sina", "tencent", "cninfo_announcements")


def quote_row(code: str, name: str, day: date, *, float_cap: float | None = 100.0, total_cap: float | None = 120.0, price: float = 10.0) -> dict:
    row = {c: 0.0 for c in LR.QUOTE_REQUIRED}
    t = datetime(day.year, day.month, day.day, 15, 0)                                       # naive Asia/Shanghai, like the old files
    row.update({"code": code, "name": name, "kind": "full", "poll_ts": t, "quote_ts": t, "price": price, "prev_close": price, "open": price,
                "high": price, "low": price, "float_cap": float_cap, "total_cap": total_cap})
    return row


def publish_session(svc, day: date, rows: list[dict]) -> dict:
    frame = IR.rows_to_frame(rows, stream="quotes", part="p1", day=day)
    return B.publish_derived(svc, NR.DATASET_QUOTES, frame, {"dataset": NR.DATASET_QUOTES, "session_date": day.isoformat()}, contract_id="pulse.analysis_artifact")


def publish_listing(svc, rows: list[tuple]) -> dict:
    """rows: (code, listed_on, name, source)."""
    now = svc.clock()
    frame = pl.DataFrame([{"instrument_id": instrument_id_of(c), "code": c, "listed_on": d, "exchange": "XSHE" if c[0] in "03" else "XSHG", "available_at": now,
                           "source": s, "name": n} for c, d, n, s in rows], schema=C.LISTING_SCHEMA)
    return B.publish_derived(svc, C.LISTING_DATASET, frame, {"dataset": C.LISTING_DATASET}, contract_id="EvidenceDocument")


def register_legacy_style(conn, when: datetime, rows: list[tuple]) -> None:
    """Instruments as ``register_universe`` leaves them: the legacy alias (plus the vendor aliases) and a first state."""
    with conn.transaction():
        for code, name, listed in rows:
            rec = InstrumentRecord.build(code, name, listed)
            instrument_repo.upsert_instruments(conn, [rec], when, LEGACY_SOURCES, "test_legacy")
            instrument_repo.append_state_if_changed(conn, rec, date(2026, 11, 1), when, "test_legacy")


def aliases(conn, code: str) -> dict[str, str]:
    return {r["source_id"]: r["source_symbol"] for r in conn.execute("SELECT source_id, source_symbol FROM core.instrument_alias WHERE instrument_id=%s", (instrument_id_of(code),))}


def states(conn, code: str) -> list[dict]:
    return conn.execute("SELECT state_version, display_name, is_risk_warning, board_code, effective_from, evidence_ref FROM core.instrument_state"
                        " WHERE instrument_id=%s ORDER BY state_version", (instrument_id_of(code),)).fetchall()


# ---- offline -------------------------------------------------------------------------------------------------------------------------------
def test_the_native_alias_source_is_registered_and_the_record_carries_it():
    from pulse.adapters.sources import registry

    assert "native" in registry.SOURCES and InstrumentRecord.build("603999", "新股份").symbols["native"] == "603999"
    assert instrument_repo.CODE_SOURCES == ("legacy_warehouse", "native") and "'native'" in instrument_repo.CODE_SOURCES_SQL


def test_no_listed_module_hardcodes_the_legacy_alias_for_the_stock_code_any_more():
    root = Path(__file__).resolve().parents[2] / "src" / "pulse"
    for rel in ("api/routes_ext_b.py", "application/bf_board_service.py", "application/bf_fillm_crowd.py", "application/bf_fusion_read.py",
                "workers/research.py", "workers/ingest.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert not re.search(r"source_id\s*=\s*'legacy_warehouse'", text), rel
        assert "CODE_SOURCES_SQL" in text, rel
    assert "src == \"legacy_warehouse\"" not in (root / "application/bindings.py").read_text(encoding="utf-8")


def test_cli_has_register_native_and_refuses_without_the_switch():
    from pulse.workers import refdata as W

    a = W._parser().parse_args(["register-native", "--no-caps"])
    assert a.cmd == "register-native" and a.no_caps and not a.force


# ---- register: new listings, state versions -------------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_a_stock_listed_after_the_switch_gets_id_code_name_state_and_aliases_without_the_legacy_warehouse(conn, lake, monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    world = World()
    svc, rs = build(conn, lake, world)
    register_legacy_style(conn, world.now(), [("000001", "平安银行", date(1991, 4, 3))])
    d = date(2026, 11, 18)
    publish_listing(svc, [("000001", date(1991, 4, 3), "平安银行", "szse"), ("603999", date(2024, 1, 2), "新股份", "sse"), ("000002", date(1991, 1, 29), "ＳＴ万  科Ａ", "szse")])
    publish_session(svc, d, [quote_row("000001", "平安银行", d), quote_row("603999", "新股份", d, float_cap=12.5, total_cap=20.0, price=20.5)])
    out = NR.register_native(svc, lake)
    assert out.counts["instruments_new"] == 2 and out.counts["instruments_seen"] == 3
    iid = instrument_id_of("603999")
    assert aliases(conn, "603999") == {"native": "603999", "sina": "sh603999", "tencent": "sh603999", "cninfo_announcements": "603999"}   # no legacy alias
    assert instrument_repo.code_of(conn, [iid]) == {uuid.UUID(iid): "603999"}
    row = conn.execute("SELECT instrument_type, venue_code, listed_on FROM core.instrument WHERE instrument_id=%s", (iid,)).fetchone()
    assert (row["instrument_type"], row["venue_code"], row["listed_on"]) == ("equity", "XSHG", date(2024, 1, 2))
    st = states(conn, "603999")
    assert [(s["display_name"], s["is_risk_warning"], s["board_code"]) for s in st] == [("新股份", False, "main")]
    assert st[0]["effective_from"] == datetime(2026, 11, 18, tzinfo=UTC) and st[0]["evidence_ref"].startswith("native_close:2026-11-18:")      # from its FIRST OBSERVATION
    # a stock the legacy import registered keeps its single (legacy) code alias and its state: nothing is duplicated or rewritten
    assert set(aliases(conn, "000001")) == set(LEGACY_SOURCES) and len(states(conn, "000001")) == 1
    # a code only the listing knows is registered from the listing name; the full-width ST prefix is normalised and flagged
    only_listing = states(conn, "000002")
    assert [(s["display_name"], s["is_risk_warning"]) for s in only_listing] == [("ST万科A", True)] and only_listing[0]["evidence_ref"].startswith("native_listing:")
    again = NR.register_native(svc, lake)
    assert again.counts.get("instruments_new", 0) == 0 and again.counts.get("state_versions_appended", 0) == 0                            # idempotent


@pytest.mark.pg
def test_a_name_and_a_risk_warning_change_are_new_versions_from_their_first_observation(conn, lake, monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    world = World()
    svc, rs = build(conn, lake, world)
    days = [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)]
    names = ["新股份", "新股份", "*ST新股", "新股份科技"]                                  # ST flag on day 3, removed with a new name on day 4
    marks = []
    for day, name in zip(days, names):
        world.advance(hours=24)
        publish_session(svc, day, [quote_row("603999", name, day)])
        out = NR.register_native(svc, lake)
        marks.append((world.now(), dict(out.counts)))
    st = states(conn, "603999")
    assert [(s["state_version"], s["display_name"], s["is_risk_warning"]) for s in st] == [(1, "新股份", False), (2, "*ST新股", True), (3, "新股份科技", False)]
    assert [s["effective_from"].date() for s in st] == [days[0], days[2], days[3]]                    # a change is effective from the session it was first quoted in
    assert marks[0][1]["instruments_new"] == 1 and "state_versions_appended" not in marks[1][1]
    assert marks[2][1]["risk_warning_changes"] == 1 and marks[3][1]["risk_warning_changes"] == 1 and "name_changes" not in marks[3][1]
    # point in time: a reader before the ST day sees the old state
    for as_of, want in ((marks[1][0], ("新股份", False)), (marks[2][0], ("*ST新股", True)), (marks[3][0], ("新股份科技", False))):
        meta = B.load_instrument_meta(conn, as_of)[0][uuid.UUID(instrument_id_of("603999"))]
        assert (meta.display_name, meta.is_risk_warning) == want and meta.code == "603999"


@pytest.mark.pg
def test_a_code_without_a_name_is_not_registered_and_an_unsupported_code_is_skipped(conn, lake):
    world = World()
    svc, rs = build(conn, lake, world)
    day = date(2026, 11, 18)
    publish_listing(svc, [("603998", date(2024, 1, 2), None, "sse"), ("000003", date(2020, 1, 2), "PT金田A", "szse")])
    publish_session(svc, day, [quote_row("123456", "可转债", day)])
    out = NR.register_native(svc, lake)
    assert out.counts["skipped_no_name"] == 1 and out.counts["skipped_unsupported_code"] == 1 and out.counts["instruments_new"] == 1
    assert conn.execute("SELECT count(*) AS n FROM core.instrument WHERE instrument_id=%s", (instrument_id_of("603998"),)).fetchone()["n"] == 0


# ---- caps -----------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.pg
def test_native_caps_are_cny_from_the_close_rows_continue_the_legacy_history_and_follow_the_switch(conn, lake, monkeypatch):
    world = World()
    svc, rs = build(conn, lake, world)
    register_legacy_style(conn, world.now(), [("000001", "平安银行", date(1991, 4, 3))])
    legacy = pl.DataFrame([{"instrument_id": instrument_id_of("000001"), "as_of": "2026-11-10", "float_cap_cny": Decimal("2300000000.00"), "total_cap_cny": Decimal("2400000000.00"),
                            "available_at": datetime(2026, 11, 10, 8, tzinfo=UTC)}], schema=NR._CAP_SCHEMA)
    B.publish_derived(svc, B.LEGACY_CAP_DATASET, legacy, {"dataset": B.LEGACY_CAP_DATASET}, contract_id="pulse.instrument_cap_fact")
    d = date(2026, 11, 18)
    publish_listing(svc, [("603999", date(2024, 1, 2), "新股份", "sse")])
    publish_session(svc, d, [quote_row("000001", "平安银行", d, float_cap=2310.5, total_cap=2410.0), quote_row("603999", "新股份", d, float_cap=12.5, total_cap=None),
                             quote_row("603998", "另一只", d, float_cap=9.0), quote_row("000003", "无市值", d, float_cap=None), quote_row("000004", "零市值", d, float_cap=0.0)])
    NR.register_native(svc, lake)
    man = NR.publish_native_caps(svc, lake)
    assert man["dataset_id"] == "instrument_circulating_cap_native" and man["contract_id"] == "pulse.instrument_cap_fact"
    frame = lake.read_snapshot_frames(man["content_hash"])
    assert dict(frame.schema) == NR._CAP_SCHEMA
    rows = {(r["instrument_id"], r["as_of"]): r for r in frame.iter_rows(named=True)}
    assert rows[(instrument_id_of("000001"), "2026-11-10")]["float_cap_cny"] == Decimal("2300000000.00")                       # the legacy history is the seed
    new = rows[(instrument_id_of("603999"), "2026-11-18")]
    assert (new["float_cap_cny"], new["total_cap_cny"]) == (Decimal("1250000000.00"), None)                                   # 12.5 x 100 million CNY; an unknown total stays NULL
    assert rows[(instrument_id_of("000001"), "2026-11-18")]["float_cap_cny"] == Decimal("231050000000.00")
    assert (instrument_id_of("603998"), "2026-11-18") in rows                                                                   # registered by its close row, so it has its cap
    assert not any(iid in (instrument_id_of("000003"), instrument_id_of("000004")) for iid, _ in rows)                          # no cap / a zero cap: no fact (unknown, never 0)
    assert new["available_at"] == datetime(2026, 11, 18, 7, tzinfo=UTC)                                                           # the poll that carried it (15:00 CST)
    assert NR.publish_native_caps(svc, lake)["snapshot_id"] == man["snapshot_id"]                                               # nothing new: the same manifest
    # the switch picks the dataset; the cap of the new stock is visible at a later cutoff and not at an earlier one
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    assert B.CAP_DATASET == "instrument_circulating_cap_native"
    caps, cman = B.load_caps(conn, lake, world.now() + timedelta(hours=1))
    assert cman["dataset_id"] == "instrument_circulating_cap_native" and caps[uuid.UUID(instrument_id_of("603999"))] == Decimal("1250000000.00")
    monkeypatch.delenv("PULSE_REFDATA_SOURCE")
    assert B.CAP_DATASET == "instrument_circulating_cap_legacy" and B.load_caps(conn, lake, world.now() + timedelta(hours=1))[0] != caps    # legacy: its own dataset


@pytest.mark.pg
def test_the_new_stock_is_in_the_release_universe_with_code_name_cap_and_listing_age(conn, lake, monkeypatch):
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    world = World()
    svc, rs = build(conn, lake, world)
    d = date(2026, 11, 18)
    publish_listing(svc, [("603999", date(2024, 1, 2), "新股份", "sse")])
    publish_session(svc, d, [quote_row("603999", "新股份", d, float_cap=12.5, total_cap=20.0, price=20.5)])
    s, cap_man = NR.refresh(svc, lake)
    assert s.counts["instruments_new"] == 1 and cap_man["dataset_id"] == "instrument_circulating_cap_native"
    as_of = world.now() + timedelta(hours=1)
    metas, listed = B.load_instrument_meta(conn, as_of)
    uid = uuid.UUID(instrument_id_of("603999"))
    assert metas[uid].code == "603999" and metas[uid].display_name == "新股份" and listed[uid] == date(2024, 1, 2)
    caps, _ = B.load_caps(conn, lake, as_of)
    sessions = [date(2026, 1, 1) + timedelta(days=i) for i in range(200)]
    facts = B.build_facts(metas, listed, sessions, {uid: Decimal("20.5")}, caps)
    from pulse.application.release_service import universe_policy_for

    res = apply_universe(universe_policy_for(True), facts)
    assert uid in {f.instrument_id for f in res.included} and not res.exclusions


@pytest.mark.pg
def test_resolve_inputs_runs_the_native_refresh_under_the_switch_and_the_legacy_cap_snapshot_otherwise(conn, lake, monkeypatch):
    from pulse.application import release_service as RS

    world = World()
    svc, rs = build(conn, lake, world)
    calls: list[str] = []
    monkeypatch.setattr(NR, "refresh", lambda s, l, **k: (calls.append("native") or (None, None)))
    monkeypatch.setattr(B, "publish_cap_snapshot", lambda s, w: calls.append("legacy") or None)
    cfg = RS.BuildConfig(legacy_warehouse="C:/nowhere")
    monkeypatch.setenv("PULSE_REFDATA_SOURCE", "native")
    with pytest.raises(LookupError):                                                   # nothing is published in this database: the build stops right after the hook
        RS.resolve_inputs(svc, lake, cfg, None)
    monkeypatch.delenv("PULSE_REFDATA_SOURCE")
    with pytest.raises(LookupError):
        RS.resolve_inputs(svc, lake, cfg, None)
    assert calls == ["native", "legacy"]


# ---- regression under legacy: the replaced queries return what they returned ------------------------------------------------------------------------
@pytest.mark.pg
def test_every_replaced_query_returns_the_same_under_legacy_and_also_finds_a_native_only_stock(conn, lake, monkeypatch):
    from pulse.application import bf_board_service as BS
    from pulse.application import bf_fusion_read as FR

    monkeypatch.delenv("PULSE_REFDATA_SOURCE", raising=False)
    world = World()
    svc, rs = build(conn, lake, world)
    register_legacy_style(conn, world.now(), [("000001", "平安银行", date(1991, 4, 3)), ("600000", "浦发银行", date(1999, 11, 10)), ("600519", "ST茅台", date(2001, 8, 27))])
    ids = [instrument_id_of(c) for c in ("000001", "600000", "600519")]

    old_names = {r["code"]: r["display_name"] for r in conn.execute(
        "SELECT a.source_symbol AS code, s.display_name FROM core.instrument_alias a JOIN LATERAL (SELECT display_name FROM core.instrument_state st"
        " WHERE st.instrument_id=a.instrument_id ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true WHERE a.source_id='legacy_warehouse'")}
    assert FR._names(conn) == old_names and set(old_names) == {"000001", "600000", "600519"}

    old_search = [r["code"] for r in conn.execute(
        "SELECT a.source_symbol AS code FROM core.instrument i JOIN core.instrument_alias a ON a.instrument_id=i.instrument_id AND a.source_id='legacy_warehouse'"
        " WHERE i.instrument_type='equity' AND a.source_symbol LIKE '6%%' ORDER BY a.source_symbol")]
    assert [x["code"] for x in BS.search_instruments(conn, "6")["items"]] == old_search

    assert instrument_repo.code_of(conn, ids) == {uuid.UUID(i): c for i, c in zip(ids, ("000001", "600000", "600519"))}
    assert instrument_repo.instrument_ids_of_codes(conn, ["600000", "999999"]) == {"600000": uuid.UUID(ids[1])}
    metas, _ = B.load_instrument_meta(conn, world.now() + timedelta(days=1))
    assert {m.code for m in metas.values()} == {"000001", "600000", "600519"}

    # a native-only stock appears in every one of them
    with conn.transaction():
        rec = InstrumentRecord.build("603999", "新股份", date(2024, 1, 2))
        instrument_repo.upsert_instruments(conn, [rec], world.now(), NR.NEW_ALIAS_SOURCES, "native_register")
        instrument_repo.append_state_if_changed(conn, rec, date(2026, 11, 2), world.now(), "t")
    assert FR._names(conn)["603999"] == "新股份"
    assert "603999" in [x["code"] for x in BS.search_instruments(conn, "603")["items"]]
    assert {m.code for m in B.load_instrument_meta(conn, world.now() + timedelta(days=1))[0].values()} == {"000001", "600000", "600519", "603999"}
    assert set(instrument_repo.code_of(conn, [instrument_id_of("603999")]).values()) == {"603999"}
    assert [r["source_symbol"] for r in conn.execute("SELECT source_symbol FROM core.instrument_alias a WHERE a.source_id " + instrument_repo.CODE_SOURCES_SQL
                                                      + " AND a.instrument_id = %s", (instrument_id_of("603999"),))] == ["603999"]            # one row per stock
