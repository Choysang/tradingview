"""PEX07 / PEX08 / PEX09 on real PostgreSQL: calendar revisions and links, event clusters, seats, community opinions."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse.application import calendar_service as CS
from pulse.contracts import utc_z
from pulse.domain.calendar import events as CE

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc
ZERO = str(uuid.UUID(int=0))


def obs(key, **kw):
    base = dict(source_id="cal-test", source_event_key=key, name="全国半导体产业大会", category="conference", region="CN", scheduled_at=None,
                scheduled_date=date(2026, 12, 3), time_precision="date")
    return CE.Observation(**{**base, **kw})


def q_events(api, asof, start="2026-12-01T00:00:00Z", end="2026-12-31T00:00:00Z", snapshot=ZERO):
    r = api.get("query-calendar-events", snapshot_id=snapshot, decision_as_of=utc_z(asof), window={"start": start, "end": end}, page={"limit": 50})
    assert r.status_code == 200, r.text
    return r.json()["data"]["events"]


def test_calendar_revisions_postponement_correction_and_macro_actual_are_point_in_time(make_api, conn, principal):
    api = make_api(principal)
    t1, t2, t3 = (datetime(2026, 11, 20 + i, 8, 0, tzinfo=UTC) for i in range(3))
    key = f"meet-{uuid.uuid4().hex[:6]}"
    assert CS.ingest_observations(conn, [obs(key)], available_at=t1)["new"] == 1
    assert CS.ingest_observations(conn, [obs(key)], available_at=t1)["same"] == 1                       # a re-read adds nothing
    assert CS.ingest_observations(conn, [obs(key, scheduled_date=date(2026, 12, 10))], available_at=t2)["revised"] == 1
    cid = str(CE.event_id("cal-test", key))
    ev = lambda asof, **kw: [e for e in q_events(api, asof, **kw) if e["calendar_event_id"] == cid]    # noqa: E731
    assert ev(t1 + timedelta(hours=1))[0]["scheduled_date"] == "2026-12-03" and ev(t1 + timedelta(hours=1))[0]["revision"] == 1
    after = ev(t2 + timedelta(hours=1))
    assert after[0]["revision"] == 2 and after[0]["status"] == "postponed" and after[0]["scheduled_date"] == "2026-12-10"
    # a window that only covers the OLD date does not resurrect the old revision once it is postponed
    assert ev(t2 + timedelta(hours=1), start="2026-12-01T00:00:00Z", end="2026-12-05T00:00:00Z") == []
    assert ev(t1 - timedelta(days=1)) == []                                                              # not known yet
    assert after[0]["time_precision"] == "date" and after[0]["scheduled_at"] is None                     # date-only stays date-only
    CS.ingest_observations(conn, [obs(key, scheduled_date=date(2026, 12, 10), name="全国半导体产业大会（更正）")], available_at=t3)
    assert ev(t3 + timedelta(hours=1))[0]["status"] == "corrected"
    # macro: expected first, the actual is a later revision; nothing is merged
    mk = f"cpi-{uuid.uuid4().hex[:6]}"
    meas = lambda v: {"value": v, "text_value": None, "unit": "%", "reference_period": "2026-11", "source_revision_id": str(uuid.uuid4())}   # noqa: E731
    CS.ingest_observations(conn, [obs(mk, name="中国CPI", category="macro_release", scheduled_at=datetime(2026, 12, 9, 1, 30, tzinfo=UTC), scheduled_date=None, time_precision="minute", expected=meas("2.0"))], available_at=t1)
    CS.ingest_observations(conn, [obs(mk, name="中国CPI", category="macro_release", scheduled_at=datetime(2026, 12, 9, 1, 30, tzinfo=UTC), scheduled_date=None, time_precision="minute", expected=meas("2.0"), actual=meas("2.2"))], available_at=t3)
    mid = [e for e in q_events(api, t2) if e["calendar_event_id"] == str(CE.event_id("cal-test", mk))][0]
    late = [e for e in q_events(api, t3 + timedelta(hours=1)) if e["calendar_event_id"] == str(CE.event_id("cal-test", mk))][0]
    assert mid["actual"] is None and mid["expected"]["value"] == "2.0" and late["actual"]["value"] == "2.2" and late["status"] == "occurred" and late["revision"] == 2
    # snapshot pinning: a snapshot taken before the later revisions shows the earlier world
    snaps = conn.execute("SELECT snapshot_id FROM calendar.snapshot ORDER BY created_at").fetchall()
    assert snaps
    assert api.get("query-calendar-events", snapshot_id=uuid.uuid4(), decision_as_of=utc_z(t3), window={"start": "2026-12-01T00:00:00Z", "end": "2026-12-31T00:00:00Z"}, page={"limit": 5}).status_code == 404


def test_event_links_resolve_stocks_point_in_time(make_api, conn, principal):
    api = make_api(principal)
    iid = S.seed_instrument(conn, "600961", "半导体股")
    conn.execute("INSERT INTO core.taxonomy(taxonomy_id,taxonomy_version,taxonomy_kind,definition,available_at) VALUES ('legacy_boards_industry',1,'sector','{}',now()) ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO core.taxonomy_node(taxonomy_id,taxonomy_version,node_id,parent_node_id,display_name,definition) VALUES ('legacy_boards_industry',1,'new_dzqj',NULL,'电子器件','{}') ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO core.instrument_tag(assignment_id,instrument_id,taxonomy_id,taxonomy_version,node_id,effective_from,available_at,assertion_kind,evidence_ref)"
                 " VALUES (%s,%s,'legacy_boards_industry',1,'new_dzqj','2026-11-25Z','2026-11-25Z','observed','t')", (uuid.uuid4(), iid))
    key = f"link-{uuid.uuid4().hex[:6]}"
    CS.ingest_observations(conn, [obs(key, name="第十五届半导体大会")], available_at=datetime(2026, 11, 1, tzinfo=UTC))
    cid = CE.event_id("cal-test", key)
    links = lambda asof: api.c.get("/v1/x/c/calendar-links", params={"calendar_event_id": str(cid), "decision_as_of": utc_z(asof)}, cookies=api._cookies()).json()["data"]["links"]   # noqa: E731
    assert [l["industry_name"] for l in links(datetime(2026, 12, 1, tzinfo=UTC))] == ["电子器件"]
    assert iid and str(iid) in {i["instrument_id"] for i in links(datetime(2026, 12, 1, tzinfo=UTC))[0]["instruments"]}
    assert str(iid) not in {i["instrument_id"] for i in links(datetime(2026, 11, 10, tzinfo=UTC))[0]["instruments"]}        # membership did not exist yet
    assert links(datetime(2026, 10, 1, tzinfo=UTC)) == []                                                                   # the event itself was unknown
    names = api.c.get("/v1/x/c/calendar-names", params={"ids": str(cid)}, cookies=api._cookies()).json()["data"]["names"]
    assert names[str(cid)]["name"] == "第十五届半导体大会"
    macro = f"m-{uuid.uuid4().hex[:6]}"
    CS.ingest_observations(conn, [obs(macro, category="macro_release", name="半导体销售额")], available_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert conn.execute("SELECT count(*) n FROM calendar.event_link WHERE calendar_event_id=%s", (CE.event_id("cal-test", macro),)).fetchone()["n"] == 0


def test_company_events_only_from_explicit_title_dates(conn):
    day = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
    iid = S.seed_instrument(conn, "600962", "解禁股")
    code = conn.execute("SELECT source_symbol FROM core.instrument_alias WHERE instrument_id=%s", (iid,)).fetchone()["source_symbol"]
    for title in (f"{code}关于首次公开发行限售股将于2026年12月20日上市流通的公告", f"{code}关于限售股上市流通的提示性公告"):
        raw = S.raw_object(conn, "cninfo_announcements", title)
        crid = uuid.uuid4()
        conn.execute("INSERT INTO intelligence.claim_revision(claim_revision_id,claim_id,revision,subject_kind,subject_key,predicate,object_payload,claim_schema_version,assertion_type,claim_status,"
                     "verification_policy_version,available_at,recorded_at,quality_status) VALUES (%s,%s,1,'issuer',%s,'announcement.published',%s,'announcement-claim-v1','official_disclosure','supported','v',%s,%s,'valid')",
                     (crid, uuid.uuid4(), code, __import__("psycopg").types.json.Jsonb({"title": title, "source_record_id": "x"}), day, day))
    out = CS.derive_company_events(conn, day)
    assert out["events"] >= 1 and out["date_unresolved"] >= 1
    row = conn.execute("SELECT category, scheduled_date, available_at, instrument_id FROM calendar.event_revision WHERE source_id='cninfo_title_events' AND source_event_key LIKE %s", (f"{code}:%",)).fetchone()
    assert row["category"] == "company_lockup" and row["scheduled_date"] == date(2026, 12, 20) and row["available_at"] == day and row["instrument_id"] == iid
    assert CS.derive_company_events(conn, day)["new"] == 0                                              # idempotent


def test_rule_expiries_are_rule_computed(conn):
    out = CS.add_rule_expiries(conn, 2027, 3)
    assert out["new"] == 2
    st = {r["status"] for r in conn.execute("SELECT status FROM calendar.event_revision WHERE source_id='pulse_rules' AND source_event_key LIKE 'expiry:%%2027-03'")}
    assert st == {"rule_computed"}


# ---- clusters ---------------------------------------------------------------------------------------------------------------------
def test_clusters_reposts_are_one_cluster_membership_changes_append_revisions_and_macro_is_kept(make_api, conn, principal):
    api = make_api(principal)
    inst = S.seed_instrument(conn, "600971", "聚类股")
    base = datetime(2026, 12, 1, 3, 0, tzinfo=UTC)
    rel = S.publish(conn, as_of=base + timedelta(hours=6))
    c1 = S.add_claim(conn, headline="聚类股签订大额订单", category="company.contract_order", polarity="+", instruments=[inst], available_at=base)
    S.add_claim(conn, headline="央行下调存款准备金率", category="policy.monetary", polarity="+", instruments=[], available_at=base + timedelta(minutes=1))
    S.add_claim(conn, headline="迟到的新闻", category="company.contract_order", polarity="+", instruments=[inst], available_at=base + timedelta(hours=8))   # after the release
    win = {"start": utc_z(base - timedelta(hours=1)), "end": utc_z(base + timedelta(days=1))}
    q = lambda scope, **kw: api.get("query-event-clusters", release_id=rel, scope=scope, window=win, page={"limit": 20}, **kw)   # noqa: E731
    r = q({"scope_kind": "market", "scope_key": "all", "membership_snapshot_id": None})
    assert r.status_code == 200, r.text
    clusters = r.json()["data"]["clusters"]
    assert len(clusters) == 2 and all(c["revision"] == 1 and c["clustering_policy_version"] == "claim-cluster-v1" for c in clusters)     # macro event not dropped, post-release claim invisible
    only = q({"scope_kind": "instrument", "scope_key": str(inst), "membership_snapshot_id": None}).json()["data"]["clusters"]
    assert len(only) == 1 and only[0]["claim_revision_ids"] == [str(c1["claim_revision_id"])]
    # a second document joins the same claim: a new cluster revision; revision 1 stays readable
    S.add_claim(conn, headline="聚类股签订大额订单", category="company.contract_order", polarity="+", instruments=[inst], available_at=base + timedelta(minutes=30), claim_id=c1["claim_id"], revision=2)
    again = {c["cluster_id"]: c for c in q({"scope_kind": "instrument", "scope_key": str(inst), "membership_snapshot_id": None}).json()["data"]["clusters"]}
    cl = again[str(c1["claim_id"])]
    assert cl["revision"] == 2 and cl["claim_revision_ids"] != [str(c1["claim_revision_id"])]
    old = api.get("query-event-clusters", release_id=rel, scope={"scope_kind": "instrument", "scope_key": str(inst), "membership_snapshot_id": None}, window=win,
                  cluster_id=str(c1["claim_id"]), cluster_revision=1, page={"limit": 5})
    assert old.json()["data"]["clusters"][0]["revision"] == 1 and old.json()["data"]["clusters"][0]["claim_revision_ids"] == [str(c1["claim_revision_id"])]
    assert api.get("query-event-clusters", release_id=uuid.uuid4(), scope={"scope_kind": "market", "scope_key": "a", "membership_snapshot_id": None}, window=win, page={"limit": 5}).status_code == 404


# ---- seats ------------------------------------------------------------------------------------------------------------------------
def test_seat_directory_never_merges_labels_or_versions_and_reports_respect_the_knowledge_cutoff(make_api, conn, principal):
    api = make_api(principal)
    seat = S.seat_entity(conn, f"测试证券拉萨东环路营业部{uuid.uuid4().hex[:4]}", "XZ")
    conn.execute("INSERT INTO intelligence.seat_entity_version(seat_id,seat_version,entity_kind,canonical_name,broker_legal_name,city_code,effective_from,evidence_raw_object_id,alias_evidence,available_at,recorded_at)"
                 " SELECT seat_id,2,entity_kind,canonical_name,broker_legal_name,'SH',effective_from,evidence_raw_object_id,alias_evidence,%s,%s FROM intelligence.seat_entity_version WHERE seat_id=%s",
                 (datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC), seat))
    name = conn.execute("SELECT canonical_name FROM intelligence.seat_entity_version WHERE seat_id=%s AND seat_version=1", (seat,)).fetchone()["canonical_name"]
    inst = S.seed_instrument(conn, "600981", "席位股")
    day = date(2026, 11, 6)
    close = datetime(2026, 11, 6, 7, 0, tzinfo=UTC)
    rep = S.seat_report(conn, inst, day, [("buy", 1, name, Decimal("5000000"), seat), ("buy", 2, "机构专用", Decimal("3000000"), None), ("sell", 1, name, Decimal("1000000"), seat),
                                         ("sell", 2, "机构专用", Decimal("500000"), None)], available_at=close + timedelta(hours=3), source_report_id="R-SEAT")
    S.seat_report(conn, inst, day, [("buy", 1, name, Decimal("9999999"), seat)], available_at=close + timedelta(days=2), revision=2, source_report_id="R-SEAT")      # later correction
    d = api.get("query-seat-directory", snapshot_id=ZERO, name_query="拉萨东环路", page={"limit": 20}).json()["data"]["seats"]
    mine = [s for s in d if s["seat_id"] == str(seat)]
    assert sorted(s["seat_version"] for s in mine) == [1, 2] and all(s["resolution_status"] == "resolved" for s in mine)       # relocation = two versions, not merged
    lab = api.get("query-seat-directory", snapshot_id=ZERO, name_query="机构专用", page={"limit": 20}).json()["data"]["seats"]
    assert lab and all(s["seat_id"] is None and s["resolution_status"] != "resolved" and s["report_local_ref"] for s in lab)
    base = dict(snapshot_id=ZERO, trade_date="2026-11-06", page={"limit": 10})
    intraday = api.get("query-seat-reports", decision_as_of=utc_z(close - timedelta(hours=2)), **base).json()["data"]["reports"]
    assert [r for r in intraday if r["report_revision_id"] == str(rep)] == []                                       # an after-close report is not intraday knowledge
    nextday = [r for r in api.get("query-seat-reports", decision_as_of=utc_z(close + timedelta(hours=4)), **base).json()["data"]["reports"] if r["window_end"] == "2026-11-06"]
    mine_r = [r for r in nextday if r["report_revision_id"] == str(rep)]
    assert mine_r and [row["side"] for row in mine_r[0]["rows"]] == ["buy", "buy", "sell", "sell"]                   # both sides of the same seat stay as reported
    later = api.get("query-seat-reports", decision_as_of=utc_z(close + timedelta(days=3)), **base).json()["data"]["reports"]
    assert any(r["revision"] == 2 for r in later) and not any(r["report_revision_id"] == str(rep) for r in later)    # the correction is read only after it exists
    # the stock-level view (same table) agrees on report ids
    assert conn.execute("SELECT count(*) n FROM intelligence.seat_report WHERE report_revision_id=%s AND instrument_id=%s", (rep, inst)).fetchone()["n"] == 1


def test_seat_profile_is_conditional_on_disclosure_has_a_sample_floor_and_nets_inside_a_report(make_api, conn, principal):
    api = make_api(principal)
    seat = S.seat_entity(conn, f"测试证券上海分公司{uuid.uuid4().hex[:4]}", "SH")
    inst = S.seed_instrument(conn, "600982", "画像股")
    snap = S.seed_manifest(conn)
    days = S.weekdays(date(2026, 9, 1), 60)
    S.seed_bars(conn, inst, snap, days, base=Decimal("10"), step=Decimal("0.1"))
    few = lambda n: [days[i] for i in range(2, 2 + n)]   # noqa: E731
    def build(n):
        for d in few(n):
            close = datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=7)
            S.seat_report(conn, inst, d, [("buy", 1, "席", Decimal("2000000"), seat)], available_at=close + timedelta(hours=3))
    build(3)
    req = dict(seat_id=seat, seat_version=1, decision_as_of="2027-01-01T00:00:00Z", window={"start": "2026-09-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}, policy_version="seat-profile-v1")
    r = api.get("query-seat-profile", **req)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["sample_count"] == 3 and any("样本不足" in l for l in d["limitations"])
    body = api.c.get("/v1/x/c/artifact", params={"ref": d["profile"]["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    mean = [m for m in body["metrics"] if m["name"] == "net_buy_forward_1d_mean_return.single_day"][0]
    assert mean["value"] is None and mean["missing_reason"] == "insufficient_sample" and mean["sample_count"] == 3          # not a fake 0 return
    assert api.get("query-seat-profile", **{**req, "policy_version": "mystery"}).status_code == 400
    assert api.get("query-seat-profile", **{**req, "seat_version": 9}).status_code == 404
    # enough samples: the number appears and equals the hand calculation (entry = next session open after the report)
    seat2 = S.seat_entity(conn, f"测试证券北京分公司{uuid.uuid4().hex[:4]}", "BJ")
    for d_ in few(12):
        close = datetime.combine(d_, datetime.min.time(), UTC) + timedelta(hours=7)
        S.seat_report(conn, inst, d_, [("buy", 1, "席", Decimal("2000000"), seat2), ("sell", 1, "席", Decimal("500000"), seat2)], available_at=close + timedelta(hours=3))
    extra_net_neg = days[30]
    S.seat_report(conn, inst, extra_net_neg, [("buy", 1, "席", Decimal("100"), seat2), ("sell", 1, "席", Decimal("300"), seat2)], available_at=datetime.combine(extra_net_neg, datetime.min.time(), UTC) + timedelta(hours=10))
    r2 = api.get("query-seat-profile", **{**req, "seat_id": seat2}).json()["data"]
    body2 = api.c.get("/v1/x/c/artifact", params={"ref": r2["profile"]["ref"]}, cookies=api._cookies()).json()["data"]["body"]
    m1 = [m for m in body2["metrics"] if m["name"] == "net_buy_forward_1d_mean_return.single_day"][0]
    expected = [Decimal("10") + Decimal("0.1") * (days.index(d_) + 1 + 1) for d_ in few(12)]
    entries = [Decimal("10") + Decimal("0.1") * (days.index(d_) + 1) for d_ in few(12)]
    mean_expected = sum(e / n - 1 for e, n in zip(expected, entries)) / 12
    assert m1["sample_count"] == 12 and abs(Decimal(m1["value"]) - mean_expected) < Decimal("0.0000001")
    gb = [m for m in body2["metrics"] if m["name"] == "gross_buy_cny"][0]
    assert Decimal(gb["value"]) == Decimal("2000000") * 12 + Decimal("100")          # the net-negative report counts gross on both sides, never a double net
    assert r2["sample_count"] == 13


# ---- community --------------------------------------------------------------------------------------------------------------------
def test_community_opinions_only_bound_forecasts_and_outcome_is_a_separate_artifact(make_api, conn, principal):
    api = make_api(principal)
    inst = S.seed_instrument(conn, "600991", "社区股")
    t0 = datetime(2026, 12, 2, 3, 0, tzinfo=UTC)
    rel = S.publish(conn, as_of=t0 + timedelta(days=1))
    scope = {"scope_kind": "instrument", "scope_key": str(inst), "membership_snapshot_id": None}
    q = lambda: api.get("query-community-opinions", release_id=rel, scope=scope, outcome_as_of=utc_z(t0 + timedelta(days=5)), page={"limit": 10})   # noqa: E731
    assert q().json()["data"]["opinions"] == []                                                         # no extractor, no data: nothing invented
    doc = S.add_claim(conn, headline="社区帖", category="social.attention_sentiment", polarity="none", instruments=[inst], available_at=t0)
    payload = {"instrument_id": str(inst), "target_spec_ref": {"ref": "target/up1d", "content_hash": "a" * 64, "contract_id": "x", "schema_version": "1"},
               "forecast_as_of": "2026-12-02T02:00:00Z", "horizon_start_at": "2026-12-02T02:00:00Z", "horizon_end_at": "2026-12-03T07:00:00Z", "session_id": "XSHG-2026-12-02",
               "prediction_probability": "0.7", "probability_origin": "source_explicit", "source_document_revision_id": str(doc["document_revision_id"]),
               "source_evidence_ordinal": 1, "invalidation_condition": "gap down"}
    crid = uuid.uuid4()
    from psycopg.types.json import Jsonb
    conn.execute("INSERT INTO intelligence.claim_revision(claim_revision_id,claim_id,revision,subject_kind,subject_key,predicate,object_payload,claim_schema_version,assertion_type,claim_status,"
                 "verification_policy_version,available_at,recorded_at,quality_status) VALUES (%s,%s,1,'instrument',%s,'community_binary_forecast',%s,'pulse.community_forecast@1.0','forecast','not_falsifiable_yet','v',%s,%s,'valid')",
                 (crid, uuid.uuid4(), str(inst), Jsonb(payload), t0, t0))
    conn.execute("INSERT INTO intelligence.claim_evidence(claim_revision_id,document_revision_id,evidence_ordinal,relation,evidence_span,extraction_version,available_at,recorded_at) VALUES (%s,%s,1,'supports','{}','t',%s,%s)",
                 (crid, doc["document_revision_id"], t0, t0))
    ops = q().json()["data"]["opinions"]
    assert len(ops) == 1 and ops[0]["forecast"]["prediction_probability"] == "0.7" and ops[0]["outcome"] is None
    out = {"claim_revision_id": str(crid), "forecast_payload_hash": "b" * 64, "target_spec_hash": "c" * 64, "evaluation_snapshot_id": str(uuid.uuid4()), "scoring_policy_version": "brier_binary_decimal12_v1",
           "horizon_end_at": "2026-12-03T07:00:00Z", "computed_at": "2026-12-04T00:00:00Z", "label_state": "resolved", "label_available_at": "2026-12-03T08:00:00Z", "label": 1, "brier_error": "0.09", "reason": None}
    from pulse.application import ops_service
    ops_service.put_artifact(conn, kind="community-outcome", contract_id=CS.OUTCOME_CONTRACT, schema_version="1", body=out)
    assert q().json()["data"]["opinions"][0]["outcome"]["label"] == 1
    early = api.get("query-community-opinions", release_id=rel, scope=scope, outcome_as_of=utc_z(t0 + timedelta(days=1)), page={"limit": 10}).json()["data"]["opinions"]
    assert early[0]["outcome"] is None                                                                  # the result was not known yet at that cutoff
