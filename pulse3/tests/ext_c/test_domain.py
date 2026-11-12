"""Pure-domain tests of group C (no database): alert tiers, calendar revisions, webhook payloads, forward-book rule, read models."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from pulse.adapters.notify import webhook
from pulse.adapters.sources import calendar_em, corp_events
from pulse.domain.alerts import tiers as T
from pulse.domain.calendar import events as CE
from pulse.domain.execution import LedgerReducer
from pulse.domain.market_rules.costs import scaled, user_account_2026
from pulse.domain.portfolio import forward_book as FB
from pulse.domain.portfolio import readmodels as RM

UTC = timezone.utc
A, B, C = (str(uuid.uuid4()) for _ in range(3))


def claim(**kw) -> T.ClaimView:
    base = dict(claim_id="c1", claim_revision_id="r1", revision=1, headline="某公司被证监会立案调查", category="company.ma_governance", polarity="-",
                claim_status="unverified", assertion_type="independently_reported", available_at="2026-11-12T01:00:00+00:00", event_time=None,
                published_lag_seconds=60.0, instrument_ids=(A,), sector_hints=())
    return T.ClaimView(**{**base, **kw})


CTX = T.Context(holdings=frozenset({A}), watch=frozenset({B}), main_board=frozenset({A, B, C}), holding_industries={"房地产": frozenset({A})})


# ---- alert tiers -----------------------------------------------------------------------------------------------------------------
def test_p0_negative_claim_naming_a_holding():
    hits = T.decide(claim(), CTX)
    assert [h.tier for h in hits] == ["P0"] and hits[0].instrument_ids == (A,)


def test_p1_negative_policy_in_a_holding_industry_names_no_company():
    c = claim(headline="多地收紧房地产限购政策", category="policy.industry", instrument_ids=())
    hits = T.decide(c, CTX)
    assert [h.tier for h in hits] == ["P1"] and hits[0].industries == ("房地产",) and hits[0].instrument_ids == ()
    # an industry keyword that matches no holding's industry gives nothing, and the keyword never names a company
    assert T.decide(claim(headline="新规影响半导体行业", category="policy.industry", instrument_ids=()), CTX) == []


def test_p2_watchlist_either_direction_and_positive_main_board_not_held():
    assert [h.tier for h in T.decide(claim(instrument_ids=(B,), polarity="+"), CTX)] == ["P2"]
    assert [h.tier for h in T.decide(claim(instrument_ids=(B,), polarity="-"), CTX)] == ["P2"]
    assert [h.tier for h in T.decide(claim(instrument_ids=(C,), polarity="+"), CTX)] == ["P2"]
    assert T.decide(claim(instrument_ids=(C,), polarity="-"), CTX) == []           # negative on an unrelated name: nothing
    assert T.decide(claim(instrument_ids=(A,), polarity="+"), CTX) == []           # positive on a holding is not an alert


def test_backfill_never_alerts_and_unverified_status_travels_in_the_evidence():
    assert T.decide(claim(published_lag_seconds=T.MAX_PUBLICATION_LAG_SECONDS + 1), CTX) == []
    hit = T.decide(claim(), CTX)[0]
    ev = T.evidence_body(hit, claim(), rule_id="r")
    assert ev["claim_status"] == "unverified" and "unverified" in ev["verification_note"] and ev["already_reacted"] is None


def test_dedup_key_changes_only_with_content_version():
    c1 = claim()
    assert T.dedup_key("P0", c1, A) == T.dedup_key("P0", claim(revision=2, claim_revision_id="r2"), A)        # an added document is not new content
    assert T.dedup_key("P0", c1, A) != T.dedup_key("P0", claim(claim_status="supported"), A)               # a status change is


# ---- calendar --------------------------------------------------------------------------------------------------------------------
def obs(**kw) -> CE.Observation:
    base = dict(source_id="s", source_event_key="k1", name="全国煤炭交易会", category="conference", region="CN",
                scheduled_at=None, scheduled_date=date(2026, 11, 20), time_precision="date")
    return CE.Observation(**{**base, **kw})


def stored(o: CE.Observation, rev: int, status: str) -> dict:
    f = CE.finalize(o, status)
    return {"revision": rev, "status": status, "content_hash": CE.content_hash(f), "scheduled_at": o.scheduled_at, "scheduled_date": o.scheduled_date,
            "name": o.name, "expected": o.expected, "actual": o.actual}


def test_date_only_event_cannot_carry_a_clock_time():
    with pytest.raises(ValueError):
        obs(scheduled_at=datetime(2026, 11, 20, 9, 30, tzinfo=UTC))
    with pytest.raises(ValueError):
        obs(scheduled_date=None, scheduled_at=None)


def test_postponed_then_unchanged_reread_keeps_one_revision_and_postponed_status():
    first = obs()
    kind, rev, status = CE.next_revision(None, first)
    assert (kind, rev, status) == ("new", 1, "scheduled")
    moved = obs(scheduled_date=date(2026, 11, 27))
    kind, rev, status = CE.next_revision(stored(first, 1, "scheduled"), moved)
    assert (kind, rev, status) == ("revised", 2, "postponed")
    again = CE.next_revision(stored(moved, 2, "postponed"), moved)           # same schedule read again later
    assert again[0] == "same" and again[1] == 2


def test_announcement_correction_and_cancel_and_actual_are_separate_statuses():
    base = obs()
    assert CE.next_revision(stored(base, 1, "scheduled"), obs(name="全国煤炭交易会（更正）"))[2] == "corrected"
    assert CE.next_revision(stored(base, 1, "scheduled"), obs(name="全国煤炭交易会取消"))[2] == "cancelled"
    macro = obs(category="macro_release", name="中国CPI", expected={"value": "2.0", "text_value": None, "unit": "%", "reference_period": "2026-10", "source_revision_id": str(uuid.uuid4())})
    revised = obs(category="macro_release", name="中国CPI", expected=macro.expected, actual={"value": "2.1", "text_value": None, "unit": "%", "reference_period": "2026-10", "source_revision_id": str(uuid.uuid4())})
    kind, rev, status = CE.next_revision(stored(macro, 1, "scheduled"), revised)
    assert (kind, rev, status) == ("revised", 2, "occurred")                    # expected and actual are different revisions, never merged


def test_visibility_is_point_in_time():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    cid = uuid.uuid4()
    rows = [{"calendar_event_id": cid, "revision": 1, "available_at": t0, "scheduled_at": None, "scheduled_date": date(2026, 11, 20)},
            {"calendar_event_id": cid, "revision": 2, "available_at": t0 + timedelta(days=5), "scheduled_at": None, "scheduled_date": date(2026, 11, 27)}]
    assert CE.visible(rows, t0 + timedelta(days=1))[0]["revision"] == 1
    assert CE.visible(rows, t0 + timedelta(days=6))[0]["revision"] == 2
    assert CE.visible(rows, t0 - timedelta(days=1)) == []


def test_rule_expiries_roll_over_holidays():
    holiday = date(2026, 12, 18)                                               # third Friday
    out = CE.expiries(2026, 12, lambda d: d.weekday() < 5 and d != holiday)
    assert out["index_futures_options"] == date(2026, 12, 21) and out["etf_options"] == date(2026, 12, 23)
    assert all(o.status == "rule_computed" for o in CE.expiry_observations(2026, 12, lambda d: d.weekday() < 5))


def test_links_skip_macro_and_market_rules_and_name_industries():
    assert CE.links_for(obs(name="第十六届半导体大会")) == [("电子器件", "半导体")]
    assert CE.links_for(obs(category="macro_release", name="半导体销售额")) == []


def test_eastmoney_rows_date_only_vs_minute_and_multi_minute():
    rows = [{"FE_CODE": "A1", "FE_NAME": "中国CPI年率", "FE_TYPE": "经济数据", "START_DATE": "2026-11-12 09:30:00", "END_DATE": "2026-11-12 09:30:00"},
            {"FE_CODE": "A1", "FE_NAME": "中国CPI年率", "FE_TYPE": "经济数据", "START_DATE": "2026-11-12 14:30:00", "END_DATE": "2026-11-12 14:30:00"},
            {"FE_CODE": "B2", "FE_NAME": "全国煤炭大会", "FE_TYPE": "其他会议", "START_DATE": "2026-11-20 00:00:00", "END_DATE": "2026-11-22 00:00:00", "CITY": "太原"}]
    out = {o.source_event_key: o for o in calendar_em.to_observations(rows)}
    assert out["A1:2026-11-12"].time_precision == "approx_minute" and out["A1:2026-11-12"].notes["all_times_listed"] == ["09:30", "14:30"]
    assert out["B2:2026-11-20"].time_precision == "date_range" and out["B2:2026-11-20"].scheduled_at is None
    body = json.dumps({"result": {"pages": 1, "data": rows}}).encode()
    assert calendar_em.parse_page(body)[1] == 1
    with pytest.raises(Exception):
        calendar_em.parse_page(b"<html>login</html>")


def test_company_events_need_an_explicit_date_in_the_title():
    rows = [{"issuer_code": "600000", "instrument_code": "600000", "title": "关于限售股上市流通的提示性公告", "announced_at": datetime(2026, 11, 10, tzinfo=UTC), "source_record_id": "1"},
            {"issuer_code": "600001", "instrument_code": "600001", "title": "关于首次公开发行限售股将于2026年11月20日上市流通的公告", "announced_at": datetime(2026, 11, 10, tzinfo=UTC), "source_record_id": "2"},
            {"issuer_code": "600002", "instrument_code": "600002", "title": "关于公司股票停牌的公告", "announced_at": datetime(2026, 11, 10, tzinfo=UTC), "source_record_id": "3"}]
    p = corp_events.to_observations(rows)
    assert p.date_unresolved == 2 and len(p.observations) == 1
    assert p.observations[0].scheduled_date == date(2026, 11, 20) and p.observations[0].category == "company_lockup"


# ---- webhook ---------------------------------------------------------------------------------------------------------------------
def test_webhook_is_off_without_config_and_url_never_leaks(tmp_path):
    assert webhook.load_config({}) is None and webhook.status({}) == {"configured": False, "kind": None, "host": None}
    cfg = webhook.load_config({"PULSE_PUSH_WEBHOOK": "https://qyapi.example.com/send?key=SECRET", "PULSE_PUSH_KIND": "wecom"})
    assert cfg is not None and "SECRET" not in repr(cfg) and "SECRET" not in json.dumps(webhook.status({"PULSE_PUSH_WEBHOOK": cfg.url}))
    f = tmp_path / "push.url"
    f.write_text("https://open.feishu.example.com/hook/abc\nfeishu\n", encoding="utf-8")
    assert webhook.load_config({"PULSE_PUSH_WEBHOOK_FILE": str(f)}).kind == "feishu"
    assert webhook.load_config({"PULSE_PUSH_WEBHOOK": "not a url"}) is None


def test_webhook_send_is_refused_under_pytest_without_a_transport():
    cfg = webhook.WebhookConfig("https://example.invalid/hook", "generic")
    with pytest.raises(webhook.SendDisabled):
        webhook.send(cfg, {"tier": "P0", "headline": "h"})


@pytest.mark.parametrize("kind,key", [("wecom", "markdown"), ("dingtalk", "markdown"), ("feishu", "content"), ("generic", "alert")])
def test_webhook_payload_shapes_and_provider_answers(kind, key):
    alert = {"tier": "P0", "headline": "某公司被立案", "instruments": [{"code": "600000", "name": "测试"}], "industries": [], "reason": "r", "claim_status": "unverified"}
    assert key in webhook.build_payload(kind, alert)
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 0}))
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"errcode": 93000}))
    down = httpx.MockTransport(lambda r: httpx.Response(500))
    def boom(request):
        raise httpx.ReadTimeout("t", request=request)
    cfg = webhook.WebhookConfig("https://example.invalid/hook", kind)
    assert webhook.send(cfg, alert, transport=ok).status == "accepted"
    assert webhook.send(cfg, alert, transport=bad).status == "failed"
    assert webhook.send(cfg, alert, transport=down).error_code == "HTTP_500"
    r = webhook.send(cfg, alert, transport=httpx.MockTransport(boom))
    assert r.status == "unknown" and "example.invalid" not in (r.error_code or "")
    assert "不构成投资建议" in webhook.message_text(alert)


# ---- forward book ----------------------------------------------------------------------------------------------------------------
def cal():
    return FB.HybridCalendar([d for d in (date(2026, 11, 2) + timedelta(days=i) for i in range(30)) if d.weekday() < 5])


def test_forward_book_rule_five_day_hold_four_new_from_top_forty():
    rule = FB.RULES[FB.MAIN_H5]
    ranking = [(f"i{n}", n) for n in range(1, 61)]
    plan = FB.plan_day(rule, date(2026, 11, 9), ranking, [], set(), cal())
    assert [i for i, _ in plan.buys] == ["i1", "i2", "i3", "i4"] and plan.sells == ()
    held = [FB.Holding("i1", 1000, date(2026, 11, 2)), FB.Holding("i2", 1000, date(2026, 11, 5))]
    plan = FB.plan_day(rule, date(2026, 11, 6), ranking, held, set(), cal())          # i1: entry Mon 11-02 -> 4 trading days later = Fri 11-06 -> exit decided
    assert plan.sells == ("i1",) and [i for i, _ in plan.buys][:2] == ["i3", "i4"]       # held names are skipped, offer stays top 40


def test_forward_book_shadow_takes_two_a_day_and_slots_cap():
    rule = FB.RULES[FB.SHADOW_H10]
    plan = FB.plan_day(rule, date(2026, 11, 9), [(f"i{n}", n) for n in range(1, 50)], [], set(), cal())
    assert len(plan.buys) == 2
    full = [FB.Holding(f"h{n}", 100, date(2026, 11, 9)) for n in range(20)]
    assert FB.plan_day(rule, date(2026, 11, 10), [(f"i{n}", n) for n in range(1, 50)], full, set(), cal()).buys == ()


def test_lot_quantity_and_hybrid_calendar():
    assert FB.lot_quantity(Decimal("50000"), Decimal("12.34")) == 4000 and FB.lot_quantity(Decimal("50000"), Decimal("600")) == 0
    c = cal()
    assert not c.is_trading_day(date(2026, 11, 7)) and c.trading_days_between(date(2026, 11, 2), date(2026, 11, 6)) == 4


# ---- read models -----------------------------------------------------------------------------------------------------------------
def ev(seq, kind, at, payload, eid=None):
    return {"event_id": eid or f"{kind}:{seq}", "sequence": seq, "kind": kind, "effective_at": at, "payload": payload}


def stream():
    d1, d2 = "2026-11-02T09:30:00+08:00", "2026-11-03T09:30:00+08:00"
    return [ev(1, "session.open", "2026-11-02T08:00:00+08:00", {"trading_day": "2026-11-02"}, "session:2026-11-02"),
            ev(2, "order.reserve", d1, {"order_id": "o1", "instrument_id": "AAA", "side": "buy", "quantity": 1000, "limit_price": "10", "fee_budget": "5"}),
            ev(3, "fill.booked", d1, {"fill_id": "f1", "order_id": "o1", "quantity": 400, "price": "10", "fee": "5", "eligible_on": "2026-11-03"}),
            ev(4, "session.open", "2026-11-03T08:00:00+08:00", {"trading_day": "2026-11-03"}, "session:2026-11-03"),
            ev(5, "order.reserve", d2, {"order_id": "o2", "instrument_id": "AAA", "side": "buy", "quantity": 100, "limit_price": "10", "fee_budget": "5"}),
            ev(6, "order.cancel_confirmed", d2, {"order_id": "o1", "cumulative_filled": 400})]


def test_order_views_states_and_knowledge_cutoff():
    ids = lambda kind, text: uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}:{text}")  # noqa: E731
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    views, cov = RM.orders_from_events(stream(), until_sequence=6, knowledge_as_of=end, window_start=start, window_end=end, id_of=ids)
    by = {v["original_quantity"]: v for v in views}
    assert by[1000]["state"] == "cancelled" and by[1000]["cumulative_filled_quantity"] == 400 and by[1000]["leaves_quantity"] == 0
    assert by[100]["state"] == "working" and by[100]["cumulative_filled_quantity"] == 0
    assert "cancel_request_in_flight_leaves_no_event" in cov["unverifiable_reasons"]
    early, _ = RM.orders_from_events(stream(), until_sequence=6, knowledge_as_of=datetime(2026, 11, 2, 2, 0, tzinfo=UTC), window_start=start, window_end=end, id_of=ids)
    assert [v["state"] for v in early] == ["partially_filled"]                      # the cancel and the second order are after the cutoff


def test_twr_cuts_at_flows_missing_mark_is_none_and_cost_stress_changes_nav():
    initial = {"trading_day": "2026-11-01", "opening_cash": "100000", "positions": {}, "sale_proceeds_reusable": True}
    events = [*stream()[:4], ev(5, "capital.deposit", "2026-11-03T10:00:00+08:00", {"amount": "100000"})]
    price = {"AAA": Decimal("11")}
    marks = lambda day, syms: {s: price.get(s) for s in syms}  # noqa: E731
    p = RM.compute_performance(initial, events, start=date(2026, 11, 2), end=date(2026, 11, 4), marks_at=marks)
    assert p.twr is not None and p.segments == 2 and p.net_capital == Decimal("200000")
    # the deposit is not income: TWR of the first segment is (nav_pre / nav_begin) only
    none = RM.compute_performance(initial, events, start=date(2026, 11, 2), end=date(2026, 11, 4), marks_at=lambda d, s: {x: None for x in s})
    assert none.twr is None and none.nav_end is None and none.missing == {"AAA"} and none.reasons
    x3 = RM.compute_performance(initial, events, start=date(2026, 11, 2), end=date(2026, 11, 4), marks_at=marks, profile=scaled(user_account_2026(), 3))
    assert x3.fees > p.fees and x3.nav_end < p.nav_end
    assert RM.recost(events, user_account_2026())[2]["payload"]["fee"] != "5"
