"""Pure tests: community sampling, seats (names, units, windows, PIT, hypotheses), fund flow, forecast claims.

Spec: FINAL §5, §5.2; sources_sectors §6-§8; acceptance SS-A11,A12,A14-A18,A23,A24,A27; fixtures.json lhb/community/amount_observations.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pulse import contracts
from pulse.domain.intelligence import community, feeds, forecast, funding, seats

from .helpers import SPEC_PKG, recorded

UTC = timezone.utc


def fx() -> dict:
    return json.loads((SPEC_PKG / "sources_sectors" / "fixtures.json").read_text(encoding="utf-8"))


def T(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---- community --------------------------------------------------------------------------------------------------------
def _posts():
    return feeds.parse_guba_list(recorded("guba_list_000001_f.html")).posts


def test_recorded_guba_page_parses_posts_with_cst_times_and_counts():
    p = feeds.parse_guba_list(recorded("guba_list_000001_f.html"))
    assert len(p.posts) > 20 and p.total_in_bar and not p.empty_success
    assert all(x.published_at.tzinfo is not None for x in p.posts)
    assert any(x.bar_code != "000001" for x in p.posts)  # the list mixes other bars: they must not be counted as the instrument's own


def test_coverage_is_complete_only_when_the_window_start_is_reached_in_publish_order():
    posts = _posts()
    newest = max(p.published_at for p in posts)
    oldest = min(p.published_at for p in posts if not p.pinned)
    assert community.coverage_status(posts, window_start=oldest - timedelta(hours=1), sorted_by_publish_time=True) == "partial"
    assert community.coverage_status(posts, window_start=oldest + timedelta(minutes=1), sorted_by_publish_time=True) == "complete_for_declared_scope"
    assert community.coverage_status(posts, window_start=oldest + timedelta(minutes=1), sorted_by_publish_time=False) == "partial"
    assert community.coverage_status([], window_start=newest, sorted_by_publish_time=True) == "unknown"


def test_summary_dedups_counts_authors_and_withholds_growth_without_coverage():
    posts = _posts()
    end = max(p.published_at for p in posts) + timedelta(minutes=5)
    oldest = min(p.published_at for p in posts if not p.pinned)
    full = community.Scope("e", "publish_time_desc", oldest + timedelta(minutes=1), end, 1, "complete_for_declared_scope")
    s = community.summarize(posts, "000001", full)
    assert s["basis"] == "attention_proxy_not_trading_flow" and s["posts_in_window"] > 0
    assert s["distinct_author_pseudonyms"] <= s["posts_in_window"] and s["growth"]["early_half_posts"] + s["growth"]["late_half_posts"] == s["posts_in_window"]
    part = community.Scope("e", "publish_time_desc", oldest - timedelta(hours=1), end, 1, "partial")
    s2 = community.summarize(posts, "000001", part)
    assert s2["growth"] is None and s2["growth_unavailable_reason"]
    community.assert_no_forbidden_labels(s)
    assert community.pseudonym("6055365723216738").startswith("gb:") and "6055365723216738" not in community.pseudonym("6055365723216738")
    with pytest.raises(ValueError):
        community.assert_no_forbidden_labels({"x": "retail_buying"})


def test_engagement_seen_three_days_later_is_not_usable_ten_minutes_after_posting():  # SS-A23
    c = fx()["community"]
    assert not community.visible_at(T(c["available_at"]), T(c["early_decision"]))
    assert community.visible_at(T(c["available_at"]), T(c["available_at"]) + timedelta(seconds=1))


# ---- seats ------------------------------------------------------------------------------------------------------------
def test_seat_name_resolution_rules():
    r = seats.resolve_seat_name("东方财富证券股份有限公司拉萨东环路第二证券营业部")
    assert r.status == "resolved" and r.city == "拉萨" and r.broker_legal_name == "东方财富证券股份有限公司" and r.seat_id
    assert seats.resolve_seat_name("国泰海通证券股份有限公司上海长宁区江苏路证券营业部").city == "上海"
    assert seats.resolve_seat_name("前东路").status == "unresolved" and seats.resolve_mention("前东路").seat_id is None  # SS-A17
    assert seats.resolve_seat_name("机构专用").status == "report_local_label"  # SS-A18
    assert seats.resolve_seat_name("机构专用1").seat_id is None
    assert seats.resolve_seat_name("拉萨天团").status == "ambiguous"
    assert seats.resolve_seat_name("国信证券股份有限公司浙江互联网分公司").status == "unresolved"  # no determinable city: not forced
    assert seats.resolve_seat_name("深股通专用").status == "unresolved"
    # regional names do not merge distinct branches
    a = seats.resolve_seat_name("东方财富证券股份有限公司拉萨东环路第一证券营业部")
    b = seats.resolve_seat_name("东方财富证券股份有限公司拉萨东环路第二证券营业部")
    assert a.seat_id != b.seat_id


def test_amount_units_convert_exactly():  # SS-A11
    for o in fx()["amount_observations"]:
        assert seats.to_cny(Decimal(o["raw_value"]), o["raw_unit"]) == Decimal(o["expected_cny"])
    assert seats.legacy_yi_to_wan(0.269411) == Decimal("2694.11")
    noisy = seats.legacy_yi_to_wan(0.06119884179999999)  # float noise below one yuan must not break canonical = raw x multiplier
    assert noisy == Decimal("611.988418") and seats.to_cny(noisy, "CNY_10000") == Decimal("6119884.18")
    assert seats.to_cny(seats.legacy_yi_to_wan(0.269411), "CNY_10000") == Decimal("26941100")
    with pytest.raises(ValueError):
        seats.to_cny(Decimal(1), "亿")


def _rep(rows, start="2026-11-09", end="2026-11-09", sid="r"):
    return seats.SeatReportRec("s", sid, "XSHE", "000530", date.fromisoformat(start), date.fromisoformat(end), "x", "unknown", "unknown", tuple(rows), "valid")


def _row(side, name, amt, rank=1):
    return seats.SeatRowRec(side, rank, name, Decimal(amt), "CNY", seats.resolve_seat_name(name), {})


def test_same_seat_on_both_sides_keeps_both_and_derives_net_inside_the_report():  # SS-A16
    f = fx()["lhb"]
    name = "东方财富证券股份有限公司拉萨东环路第二证券营业部"
    rep = _rep([_row("buy", name, f["same_report_rows"][0]["amount_cny"]), _row("sell", name, f["same_report_rows"][1]["amount_cny"])])
    sid = seats.resolve_seat_name(name).seat_id
    assert seats.net_in_report(rep, sid) == Decimal(f["expected_net_cny"])
    assert [r.side for r in rep.rows] == ["buy", "sell"]


def test_overlapping_windows_are_not_additive():  # SS-A15
    name = "东方财富证券股份有限公司拉萨东环路第二证券营业部"
    sid = seats.resolve_seat_name(name).seat_id
    one_day = _rep([_row("buy", name, "100")], "2026-11-09", "2026-11-09", "a")
    three_day = _rep([_row("buy", name, "300")], "2026-11-05", "2026-11-09", "b")
    with pytest.raises(seats.OverlappingWindows):
        seats.aggregate_seat_buy([one_day, three_day], sid)
    other = _rep([_row("buy", name, "50")], "2026-11-10", "2026-11-10", "c")
    assert seats.aggregate_seat_buy([one_day, other], sid) == Decimal(150)


def test_after_close_report_is_not_usable_intraday_but_is_next_day():  # SS-A14
    f = fx()["lhb"]
    rep = seats.SeatReportRec("s", "r", "XSHE", "000530", date(2026, 11, 9), date(2026, 11, 9), "x", "unknown", "unknown", (), "valid", available_at=T(f["available_at"]))
    assert seats.usable_reports([rep], T(f["intraday_decision"])) == []
    assert seats.usable_reports([rep], T(f["next_day_decision"])) == [rep]


def test_window_sessions_from_reason_text():
    assert seats.window_sessions("连续三个交易日内，涨幅偏离值累计达到20%的证券") == 3
    assert seats.window_sessions("有价格涨跌幅限制的连续10个交易日内收盘价格涨幅偏离值累计达到100%") == 10
    assert seats.window_sessions("日涨幅偏离值达到7%的前5只证券") == 1
    sessions = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10)]
    assert seats.window_start("连续三个交易日内", date(2026, 11, 10), sessions) == (date(2026, 11, 6), True)
    assert seats.window_start("连续三个交易日内", date(2026, 11, 3), sessions)[1] is False


def test_recorded_live_lhb_builds_reports_with_exact_amounts_and_no_forced_ids():
    lst = feeds.parse_lhb_list(recorded("lhb_daily_list_20261110.json")).rows
    buy = feeds.parse_lhb_seats(recorded("lhb_seat_buy_20261110.json")).rows
    sell = feeds.parse_lhb_seats(recorded("lhb_seat_sell_20261110.json")).rows
    reps = seats.reports_from_eastmoney(lst, buy, sell, [date(2026, 11, 10)], T("2026-11-10T11:00:00+00:00"))
    assert 0 < len(reps) <= len(lst) and all(seats.is_equity_code(r.code) for r in reps) and all(r.window_end == date(2026, 11, 10) for r in reps)
    rows = [r for rep in reps for r in rep.rows]
    assert rows and {r.resolution.status for r in rows} >= {"resolved", "report_local_label"}
    assert all(r.resolution.seat_id is None for r in rows if r.resolution.status != "resolved")
    for rep in reps:
        for side in ("buy", "sell"):
            ranks = [r.rank for r in rep.rows if r.side == side]
            assert ranks == list(range(1, len(ranks) + 1))
    one = next(r for r in reps if r.code == "000530")
    br = [r for r in one.rows if r.side == "buy" and "拉萨东环路第二" in r.raw_seat_name][0]
    assert br.canonical_amount_cny == Decimal("16785186")  # vendor yuan, no float arithmetic


def test_legacy_rows_become_reports_available_at_their_ingest_time():
    ing = datetime(2026, 10, 31, 3, 41, tzinfo=UTC)
    stocks = [{"code": "000530", "date": date(2026, 10, 26), "reason": "日涨幅偏离值达到7%的前5只证券", "ingested_at": ing}]
    srows = [{"code": "000530", "date": date(2026, 10, 26), "reason": "日涨幅偏离值达到7%的前5只证券", "side": "buy", "rank": 1,
              "seat": "国信证券股份有限公司浙江互联网分公司", "seat_code": "10025080", "buy": 0.269411, "sell": 0.21256, "net": 0.056851, "ingested_at": ing}]
    (rep,) = seats.reports_from_legacy(stocks, srows, [date(2026, 10, 26)])
    assert rep.available_at == ing and rep.quality_status == "partial" and rep.rows[0].raw_unit == "CNY_10000"
    assert rep.rows[0].canonical_amount_cny == Decimal("26941100") and Decimal(rep.rows[0].source_amounts["net_cny"]) == Decimal(5685100)


def _obs(i, value, avail="2026-11-05T11:00:00+00:00", entry="2026-11-06T01:30:00+00:00", out="2026-11-06T07:10:00+00:00", ctx=None):
    return seats.StyleObservation(f"r{i}", T(avail), T(entry), T(out), Decimal(value), ctx or {"board": "first"})


def test_style_hypothesis_uses_only_information_available_at_as_of():
    h = seats.SeatStyleHypothesis("raw_name_literal", "某营业部", "buy", {"board": "first"}, 1, "next_open_gap", "claim", min_sample=3)
    obs = [_obs(i, "0.01") for i in range(4)] + [_obs(9, "0.5", out="2026-11-20T07:10:00+00:00")]
    r = seats.evaluate_style(h, obs, T("2026-11-10T00:00:00+00:00"))
    assert r["n"] == 4 and r["excluded_not_yet_available"] == 1 and r["outcome"]["status"] == "estimated"
    r2 = seats.evaluate_style(h, obs[:2], T("2026-11-10T00:00:00+00:00"))
    assert r2["outcome"]["status"] == "insufficient_sample"
    assert seats.evaluate_style(h, obs, T("2026-11-10T00:00:00+00:00"), trials_in_family=50)["outcome"]["multiple_comparison"] == "bonferroni_z"


def test_style_observation_entering_before_disclosure_is_rejected_as_lookahead():
    h = seats.SeatStyleHypothesis("seat_id", "x", "buy", {}, 1, "next_open_gap", "c", min_sample=1)
    with pytest.raises(seats.LookaheadError):
        seats.evaluate_style(h, [_obs(1, "0.01", avail="2026-11-05T11:00:00+00:00", entry="2026-11-05T05:00:00+00:00")], T("2026-12-01T00:00:00+00:00"))


def test_hypothesis_id_is_deterministic_and_condition_sensitive():
    a = seats.SeatStyleHypothesis("seat_id", "k", "buy", {"board": "first"}, 1, "next_open_gap", "c")
    b = seats.SeatStyleHypothesis("seat_id", "k", "buy", {"board": "second"}, 1, "next_open_gap", "c")
    assert a.hypothesis_id() == seats.SeatStyleHypothesis("seat_id", "k", "buy", {"board": "first"}, 1, "next_open_gap", "c").hypothesis_id() != b.hypothesis_id()


# ---- funding ------------------------------------------------------------------------------------------------------------
def _fo(provider="eastmoney", method="m1", value="10"):
    return funding.FundingObs(provider, "ns", "main_net_inflow", method, "1", "provider_derived", "net", Decimal(value), "CNY", datetime(2026, 11, 9, tzinfo=UTC),
                              datetime(2026, 11, 10, tzinfo=UTC))


def test_different_providers_or_methods_are_never_combined():  # SS-A12
    spec = fx()["funding_metrics"]
    a = _fo(spec[0]["provider"], spec[0]["methodology"])
    b = _fo(spec[1]["provider"], spec[1]["methodology"])
    assert a.series_key != b.series_key and len(funding.split_series([a, b])) == 2
    with pytest.raises(funding.MethodMismatch):
        funding.combine([a, b])
    assert funding.combine([_fo(value="10"), _fo(value="5")]) == Decimal(15)


def test_missing_value_is_not_zero_and_gross_turnover_has_no_net():  # SS-A27
    with pytest.raises(ValueError):
        funding.combine([_fo(value="10"), funding.FundingObs("eastmoney", "ns", "main_net_inflow", "m1", "1", "provider_derived", "net", None, "CNY",
                                                              datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC))])
    with pytest.raises(funding.NoDirection):
        funding.net_from_gross(funding.FundingObs("sse", "stock_connect", "turnover", "x", "1", "disclosed_observation", "gross_total", Decimal(1), "CNY",
                                                  datetime(2026, 11, 9, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC), market_direction="northbound"))


def test_recorded_fundflow_becomes_provider_derived_series_with_finality_rule():
    p = feeds.parse_fundflow(recorded("fundflow_000001.json"))
    obs = funding.observations_from_days(p.days, "000001")
    assert len(obs) == 10 * 10 and {o.measurement_class for o in obs} == {"provider_derived"}
    assert len({o.series_key for o in obs}) == 10  # ten metrics, one methodology: ten separate series
    assert funding.is_final("2026-11-10", T("2026-11-10T09:00:00+00:00")) and not funding.is_final("2026-11-11", T("2026-11-11T06:00:00+00:00"))


# ---- community forecast claim --------------------------------------------------------------------------------------------
def _payload(**kw):
    p = {"instrument_id": str(uuid.uuid4()), "target_spec_ref": {"ref": "target/up1d", "content_hash": "a" * 64, "contract_id": "x", "schema_version": "1"},
         "forecast_as_of": "2026-11-10T02:00:00Z", "horizon_start_at": "2026-11-10T02:00:00Z", "horizon_end_at": "2026-11-11T07:00:00Z",
         "session_id": "XSHG-2026-11-10", "prediction_probability": "0.7", "probability_origin": "source_explicit",
         "source_document_revision_id": str(uuid.uuid4()), "source_evidence_ordinal": 1, "invalidation_condition": "gap down below prior close"}
    p.update(kw)
    return p


def test_forecast_payload_passes_contract_and_semantic_rules():
    p = _payload()
    forecast.validate_forecast(p)
    contracts.check("CommunityForecastPayload", p, schema_id=contracts.SUPPLEMENTAL_ID)
    forecast.validate_bound_claim(p, claim_schema_version=forecast.TOKEN, subject_kind="instrument", subject_key=p["instrument_id"],
                                  predicate="community_binary_forecast", assertion_type="forecast")
    with pytest.raises(ValueError):
        forecast.validate_forecast(_payload(probability_origin="not_provided"))  # cannot infer a probability the source never quoted
    with pytest.raises(ValueError):
        forecast.validate_forecast(_payload(horizon_end_at="2026-11-10T02:00:00Z"))
    with pytest.raises(ValueError):
        forecast.validate_bound_claim(p, claim_schema_version="pulse.community_forecast@2.0", subject_kind="instrument", subject_key=p["instrument_id"],
                                      predicate="community_binary_forecast", assertion_type="forecast")
    with pytest.raises(contracts.ContractViolation):
        contracts.check("CommunityForecastPayload", {**p, "extra": 1}, schema_id=contracts.SUPPLEMENTAL_ID)


def test_late_discovered_forecast_is_known_only_from_actual_availability():
    p = _payload()
    pub = T("2026-11-10T02:00:00+00:00")
    late = T("2026-11-12T01:00:00+00:00")
    assert not forecast.known_at(p, source_published_at=pub, document_available_at=late, claim_available_at=late, decision_as_of=T("2026-11-10T03:00:00+00:00"))
    assert forecast.known_at(p, source_published_at=pub, document_available_at=late, claim_available_at=late, decision_as_of=late)
