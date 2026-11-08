"""Ladder members, seal timeline, event tape and the natural-language screener."""
from __future__ import annotations

import time

import polars as pl
import pytest
from fastapi.testclient import TestClient

from backend.app.core.engine import MarketEngine, engine
from backend.app.core.events import EventTape, SpeedGate, diff
from backend.app.main import app
from backend.app.query.nlq import parse
from contracts.schema import ScreenerFilterRequest

THEMES = ["AI算力", "算力", "低空经济", "机器人"]
STREAK = lambda low, high=20: list(range(low, high + 1))  # noqa: E731

# --- natural-language parser ---------------------------------------------------------

PARSE_CASES = [
    ("首板", {"limit_streak_exact": [1], "pool_type": "limit_up"}),
    ("二连板", {"limit_streak_exact": [2], "pool_type": "limit_up"}),
    ("2板", {"limit_streak_exact": [2]}),
    ("3连板以上", {"limit_streak_exact": STREAK(3)}),
    ("连板≥3", {"limit_streak_exact": STREAK(3)}),
    ("三板以上", {"limit_streak_exact": STREAK(3)}),
    ("高标", {"limit_streak_exact": STREAK(5), "pool_type": "limit_up"}),
    ("涨停", {"pool_type": "limit_up"}),
    ("炸板", {"pool_type": "broken_attack"}),
    ("冲板", {"pool_type": "broken_attack"}),
    ("烂板", {"pool_type": "broken_attack"}),
    ("反包", {"pool_type": "n_days_m_boards"}),
    ("5天4板", {"pool_type": "n_days_m_boards", "limit_streak_exact": None}),
    ("5连阳", {"pool_type": "growth_streak", "min_growth_streak": 5}),
    ("连阳5天以上", {"pool_type": "growth_streak", "min_growth_streak": 5}),
    ("连阳", {"pool_type": "growth_streak", "min_growth_streak": 3}),
    ("假阴", {"allow_doji_tolerance": True}),
    ("换手率大于10%", {"turnover_today_min": 10}),
    ("换手>10", {"turnover_today_min": 10}),
    ("换手10%-30%", {"turnover_today_min": 10, "turnover_today_max": 30}),
    ("换手低于15", {"turnover_today_max": 15}),
    ("昨日换手大于5%", {"turnover_yd_min": 5, "turnover_today_min": 0}),
    ("缩量", {"turnover_regime": "shrink"}),
    ("平量", {"turnover_regime": "normal"}),
    ("放量", {"turnover_regime": "expand"}),
    ("昨换比0.8-1.5", {"turnover_ratio_min": 0.8, "turnover_ratio_max": 1.5}),
    ("自由流通市值小于100亿", {"free_cap_max": 100}),
    ("流通市值50亿以下", {"free_cap_max": 50}),
    ("市值50-200亿", {"free_cap_min": 50, "free_cap_max": 200}),
    ("自由流通市值大于2000万", {"free_cap_min": 0.2}),
    ("小盘", {"free_cap_max": 80}),
    ("中盘", {"free_cap_min": 80, "free_cap_max": 300}),
    ("大盘", {"free_cap_min": 500}),
    ("股价低于20元", {"price_max": 20}),
    ("价格10到30", {"price_min": 10, "price_max": 30}),
    ("涨幅大于5%", {"pct_chg_min": 5}),
    ("涨幅3%-7%", {"pct_chg_min": 3, "pct_chg_max": 7}),
    ("上涨", {"pct_chg_min": 0.01}),
    ("下跌", {"pct_chg_max": -0.01}),
    ("成交额大于5亿", {"min_amount_today": 5}),
    ("封成比大于10%", {"min_seal_to_amount_ratio": 10}),
    ("一字板", {"max_first_seal_time": "09:25:00", "pool_type": "limit_up"}),
    ("秒板", {"max_first_seal_time": "09:31:00"}),
    ("早盘封板", {"max_first_seal_time": "10:00:00"}),
    ("10点前封板", {"max_first_seal_time": "10:00:00"}),
    ("9:35前封板", {"max_first_seal_time": "09:35:00"}),
    ("十点前", {"max_first_seal_time": "10:00:00"}),
    ("硬板", {"max_open_board_count": 0, "pool_type": "limit_up"}),
    ("没开板", {"max_open_board_count": 0}),
    ("创业板", {"boards": ["20cm"]}),
    ("主板", {"boards": ["10cm"]}),
    ("北交所 科创板", {"boards": ["20cm", "30cm"]}),
    ("含ST", {"exclude_st": False}),
    ("次新", {"exclude_new": False}),
    ("近一年涨停5次以上", {"min_zt_count_1y": 5}),
    ("封板率大于60%", {"min_seal_success_rate_1y": 60}),
    ("接近60日新高", {"only_near_60d_high": True}),
    ("累计涨幅大于15%", {"min_streak_cum_return": 15}),
    ("AI算力", {"selected_themes": ["AI算力"]}),
    ("#低空经济", {"selected_themes": ["低空经济"]}),
    ("按涨幅排序", {"sort_by": "pct_chg", "sort_desc": True}),
    ("按成交额", {"sort_by": "amount_today"}),
    ("按换手", {"sort_by": "turnover_today"}),
    ("按连板", {"sort_by": "limit_streak"}),
    ("按综合分", {"sort_by": "composite_score"}),
    (
        "2连板以上 换手低于15% 自由流通小于100亿 AI算力",
        {
            "limit_streak_exact": STREAK(2),
            "pool_type": "limit_up",
            "turnover_today_max": 15,
            "free_cap_max": 100,
            "selected_themes": ["AI算力"],
        },
    ),
    (
        "首板，创业板、涨幅大于5%；成交额大于5亿 且 不炸板",
        {"limit_streak_exact": [1], "boards": ["20cm"], "pct_chg_min": 5, "min_amount_today": 5, "max_open_board_count": 0},
    ),
    # drop depth, total cap and valuation caps
    ("跌幅大于3%", {"pct_chg_max": -3}),
    ("跌幅小于2%", {"pct_chg_min": -2, "pct_chg_max": -0.01}),
    ("跌幅2-5%", {"pct_chg_min": -5, "pct_chg_max": -2}),
    ("总市值大于100亿", {"total_cap_min": 100, "free_cap_min": 0}),
    ("总市值50-300亿", {"total_cap_min": 50, "total_cap_max": 300, "free_cap_max": 20000}),
    ("市盈率低于20", {"pe_max": 20}),
    ("PE<30", {"pe_max": 30}),
    ("市净率小于2", {"pb_max": 2}),
    ("低估值", {"pe_max": 20, "pb_max": 2}),
    ("低估值 市盈率小于15", {"pe_max": 15, "pb_max": 2}),
]


def _field(filters: ScreenerFilterRequest, name: str):
    value = getattr(filters, name)
    return [item.value for item in value] if name == "boards" else value


@pytest.mark.parametrize(("text", "expected"), PARSE_CASES, ids=[case[0] for case in PARSE_CASES])
def test_parse_sets_expected_fields(text: str, expected: dict) -> None:
    result = parse(text, THEMES)
    for name, value in expected.items():
        assert _field(result.filters, name) == value, (text, name)
    assert result.tokens, text
    assert result.unmatched == [], text


def test_parse_covers_at_least_25_cases() -> None:
    assert len(PARSE_CASES) >= 25


def test_parse_labels_and_fields() -> None:
    result = parse("3连板以上 换手10%-30% 自由流通小于100亿", THEMES)
    assert [token.label for token in result.tokens] == ["连板 ≥ 3", "换手 10–30%", "自由流通 ≤ 100 亿"]
    assert [token.field for token in result.tokens] == [
        "limit_streak_exact",
        "turnover_today_min,turnover_today_max",
        "free_cap_max",
    ]
    assert result.tokens[0].text == "3连板以上"
    assert result.summary == "连板 ≥ 3 · 换手 10–30% · 自由流通 ≤ 100 亿"


def test_rising_and_falling_are_strict() -> None:
    rising, falling = parse("上涨", THEMES), parse("下跌", THEMES)
    assert rising.filters.pct_chg_min > 0 and rising.tokens[0].label == "上涨 (涨幅 > 0)"
    assert falling.filters.pct_chg_max < 0 and falling.tokens[0].label == "下跌 (涨幅 < 0)"
    # A flat stock (0.00%) satisfies neither bound.
    assert 0.0 < rising.filters.pct_chg_min and falling.filters.pct_chg_max < 0.0
    both = parse("上涨 下跌", THEMES)
    assert both.filters.pct_chg_min == 0.01 and both.filters.pct_chg_max is None
    assert any("矛盾" in note for note in both.unmatched)


def test_yesterday_turnover_is_not_read_as_today() -> None:
    result = parse("昨日换手大于5%", THEMES)
    assert [token.field for token in result.tokens] == ["turnover_yd_min"]


def test_longest_theme_wins_and_hash_is_optional() -> None:
    assert parse("ai算力", THEMES).filters.selected_themes == ["AI算力"]
    assert parse("#算力 机器人", THEMES).filters.selected_themes == ["算力", "机器人"]


def test_unmatched_text_is_reported_not_dropped() -> None:
    result = parse("AI算力 火星基地 换手低于15%", THEMES)
    assert result.unmatched == ["火星基地"]
    assert result.filters.turnover_today_max == 15
    assert "火星基地" not in result.summary


def test_nothing_recognised_gives_empty_summary() -> None:
    result = parse("随便说说", THEMES)
    assert result.tokens == []
    assert result.summary == "未识别到筛选条件"
    assert result.filters == ScreenerFilterRequest()


def test_contradictions_are_reported_and_filters_stay_valid() -> None:
    result = parse("换手大于30 换手小于10", THEMES)
    assert result.filters.turnover_today_min == 30
    assert result.filters.turnover_today_max == 100
    assert any("矛盾" in note for note in result.unmatched)
    ScreenerFilterRequest.model_validate(result.filters.model_dump())


def test_conflicting_pools_keep_the_first_and_report_the_second() -> None:
    result = parse("炸板 涨停", THEMES)
    assert result.filters.pool_type.value == "broken_attack"
    assert any("涨停" in note and "冲突" in note for note in result.unmatched)


def test_upper_bound_on_lower_only_field_is_rejected() -> None:
    result = parse("成交额小于5亿", THEMES)
    assert result.filters.min_amount_today == 0
    assert result.unmatched and "下限" in result.unmatched[0]


def test_valuation_labels_say_what_is_filtered() -> None:
    result = parse("总市值50-300亿 市盈率低于20 市净率小于2 跌幅大于3%", THEMES)
    assert [token.label for token in result.tokens] == [
        "总市值 50–300 亿",
        "市盈率(仅盈利) ≤ 20 倍",
        "市净率 ≤ 2 倍",
        "跌幅 ≥ 3% (涨幅 ≤ -3%)",
    ]
    assert [token.field for token in result.tokens] == [
        "total_cap_min,total_cap_max", "pe_max", "pb_max", "pct_chg_max",
    ]


def test_lower_bound_on_valuation_cap_is_rejected() -> None:
    result = parse("市盈率大于20", THEMES)
    assert result.filters.pe_max is None
    assert result.unmatched and "上限" in result.unmatched[0]


def test_total_cap_is_not_read_as_float_cap() -> None:
    result = parse("总市值大于100亿", THEMES)
    assert result.filters.free_cap_min == 0 and result.filters.total_cap_min == 100


def test_clock_outside_session_is_rejected() -> None:
    result = parse("8点前封板", THEMES)
    assert result.filters.max_first_seal_time is None
    assert result.unmatched


def test_parse_is_fast() -> None:
    parse("warm up", THEMES)
    started = time.perf_counter()
    for _ in range(200):
        parse("2连板以上 换手低于15% 自由流通小于100亿 AI算力 按涨幅排序", THEMES)
    assert (time.perf_counter() - started) / 200 < 0.01


# --- event diff -------------------------------------------------------------------------


def _frame(*rows: dict) -> pl.DataFrame:
    base = {
        "code": "000001", "name": "测试", "price": 10.0, "pct_chg": 0.0,
        "is_limit_up": False, "ever_limit_up_today": False, "limit_streak": 0,
        "open_board_count": 0, "seal_order_amount": 0.0,
        "theme_tags": ["低空经济", "A股全市场"], "is_suspended": False,
    }
    return pl.DataFrame([{**base, **row} for row in rows], schema_overrides={"limit_streak": pl.Int64})


def _kinds(events) -> list[str]:
    return [event.kind for event in events]


def test_seal_event() -> None:
    prev = _frame({"pct_chg": 9.0})
    curr = _frame({"pct_chg": 10.0, "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 3, "seal_order_amount": 2.31})
    events = diff(prev, curr, 7, "09:41:00")
    seal = next(event for event in events if event.kind == "seal")
    assert seal.detail == "封涨停 · 3 连板 · 封单 2.31 亿"
    assert seal.id == "7-000001-seal"
    assert seal.sequence == 7 and seal.time == "09:41:00"
    assert seal.themes == ["低空经济"]


def test_reseal_event() -> None:
    prev = _frame({"pct_chg": 8.0, "ever_limit_up_today": True})
    curr = _frame({"pct_chg": 10.0, "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 2})
    events = diff(prev, curr, 3, "10:00:00")
    assert _kinds(events) == ["reseal"]
    assert events[0].detail == "回封涨停"


def test_break_event() -> None:
    prev = _frame({"pct_chg": 10.0, "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 2})
    curr = _frame({"pct_chg": 8.5, "ever_limit_up_today": True, "open_board_count": 2})
    events = diff(prev, curr, 4, "10:30:00")
    assert _kinds(events) == ["break"]
    assert events[0].detail == "炸板 · 第 2 次开板"


def test_surge_and_plunge_cross_thresholds_only_once() -> None:
    low, high = _frame({"pct_chg": 4.8}), _frame({"pct_chg": 5.3})
    assert _kinds(diff(low, high, 1, "09:40:00")) == ["surge"]
    assert diff(high, _frame({"pct_chg": 5.9}), 2, "09:41:00") == []
    drop = diff(_frame({"pct_chg": -4.8}), _frame({"pct_chg": -5.2}), 3, "09:42:00")
    assert _kinds(drop) == ["plunge"]
    assert drop[0].detail == "跌幅扩大至 -5.2%"


def _pct(value: float) -> pl.DataFrame:
    return _frame({"pct_chg": value})


def test_surge_fires_again_only_after_rearm_below_three_percent() -> None:
    gate = SpeedGate(cooldown=0)
    assert _kinds(diff(_pct(4.8), _pct(5.3), 1, "09:40:00", gate)) == ["surge"]
    # Flickering around the threshold stays silent while disarmed.
    assert diff(_pct(5.3), _pct(4.8), 2, "09:41:00", gate) == []
    assert diff(_pct(4.8), _pct(5.4), 3, "09:42:00", gate) == []
    # Falling back under +3% re-arms the code.
    assert diff(_pct(4.8), _pct(2.9), 4, "09:43:00", gate) == []
    assert _kinds(diff(_pct(4.8), _pct(5.1), 5, "09:44:00", gate)) == ["surge"]


def test_plunge_rearms_above_minus_three_percent() -> None:
    gate = SpeedGate(cooldown=0)
    assert _kinds(diff(_pct(-4.8), _pct(-5.2), 1, "09:40:00", gate)) == ["plunge"]
    assert diff(_pct(-4.8), _pct(-5.3), 2, "09:41:00", gate) == []
    diff(_pct(-5.3), _pct(-2.5), 3, "09:42:00", gate)
    assert _kinds(diff(_pct(-4.9), _pct(-5.1), 4, "09:43:00", gate)) == ["plunge"]


def test_cooldown_blocks_a_rearmed_code_for_fifteen_sequences() -> None:
    gate = SpeedGate()
    assert _kinds(diff(_pct(4.8), _pct(5.3), 10, "09:40:00", gate)) == ["surge"]
    diff(_pct(5.3), _pct(2.0), 11, "09:41:00", gate)  # re-armed
    assert diff(_pct(4.8), _pct(5.3), 20, "09:42:00", gate) == []  # still cooling down
    assert _kinds(diff(_pct(4.8), _pct(5.3), 25, "09:43:00", gate)) == ["surge"]


def test_gate_is_per_code_and_cleared_with_the_tape() -> None:
    gate = SpeedGate()
    first = _frame({"code": "000001", "pct_chg": 4.8}, {"code": "000002", "pct_chg": 4.8})
    second = _frame({"code": "000001", "pct_chg": 5.3}, {"code": "000002", "pct_chg": 5.4})
    assert len(diff(first, second, 1, "09:40:00", gate)) == 2
    tape = EventTape()
    tape.gate.fire("000001", "surge", 1)
    assert not tape.gate.allow("000001", "surge", 2)
    tape.clear()
    assert tape.gate.allow("000001", "surge", 2)


def test_new_high_needs_to_beat_previous_market_height() -> None:
    prev = _frame(
        {"code": "000001", "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 5, "pct_chg": 10.0},
        {"code": "000002", "pct_chg": 9.0},
    )
    curr = _frame(
        {"code": "000001", "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 5, "pct_chg": 10.0},
        {"code": "000002", "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 6, "pct_chg": 10.0},
    )
    events = diff(prev, curr, 9, "11:00:00")
    assert sorted(_kinds(events)) == ["new_high", "seal"]
    high = next(event for event in events if event.kind == "new_high")
    assert high.code == "000002" and high.detail == "晋级 6 板 · 市场新高度"
    # Equalling the previous height is not a new height.
    level = _frame(
        {"code": "000001", "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 5, "pct_chg": 10.0},
        {"code": "000002", "is_limit_up": True, "ever_limit_up_today": True, "limit_streak": 5, "pct_chg": 10.0},
    )
    assert "new_high" not in _kinds(diff(prev, level, 10, "11:01:00"))


def test_suspended_and_unchanged_rows_produce_nothing() -> None:
    prev = _frame({"pct_chg": 4.8, "is_suspended": True})
    curr = _frame({"pct_chg": 5.3, "is_suspended": True})
    assert diff(prev, curr, 1, "09:40:00") == []
    steady = _frame({"pct_chg": 2.0})
    assert diff(steady, steady, 2, "09:41:00") == []
    assert diff(pl.DataFrame(), steady, 3, "09:42:00") == []


def test_tape_is_a_bounded_newest_first_ring() -> None:
    tape = EventTape(maxlen=3)
    for sequence in range(1, 6):
        tape.extend(diff(_frame({"pct_chg": 4.8}), _frame({"pct_chg": 5.3}), sequence, "09:40:00"))
    assert len(tape) == 3
    assert [event.sequence for event in tape.latest(None, 10)] == [5, 4, 3]
    assert [event.sequence for event in tape.latest(3, 10)] == [5, 4]
    assert len(tape.latest(None, 1)) == 1


# --- engine integration -----------------------------------------------------------------


@pytest.fixture(scope="module")
def market() -> MarketEngine:
    return MarketEngine()


def test_no_events_on_first_frame_or_after_mode_switch() -> None:
    fresh = MarketEngine()
    assert fresh.last_event_count == 0
    assert fresh.events(None, 300).events == []
    for _ in range(60):
        fresh.refresh()
    assert fresh.events(None, 300).events
    # Pretend the previous frame came from the other source.
    fresh._frame_mode = "LIVE"
    assert fresh.refresh() == 0
    assert fresh.events(None, 300).events == []


def test_sim_session_produces_seal_or_break_events() -> None:
    fresh = MarketEngine()
    produced = sum(fresh.refresh() for _ in range(60))
    events = fresh.events(None, 300).events
    assert produced >= len(events) > 0
    assert any(event.kind in {"seal", "break", "reseal"} for event in events)
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences, reverse=True)
    assert len({event.id for event in events}) == len(events)


def test_sim_surge_and_plunge_are_reported_once_per_code() -> None:
    fresh = MarketEngine()
    for _ in range(60):
        fresh.refresh()
    fast = [e for e in fresh.events(None, 300).events if e.kind in {"surge", "plunge"}]
    keys = [(e.code, e.kind) for e in fast]
    # Without hysteresis this session produced ~160 surge/plunge events.
    assert len(keys) == len(set(keys))
    assert len(fast) < 100


def test_events_since_filters_by_sequence(market: MarketEngine) -> None:
    for _ in range(40):
        market.refresh()
    everything = market.events(None, 300).events
    assert everything
    cutoff = everything[len(everything) // 2].sequence
    newer = market.events(cutoff, 300).events
    assert all(event.sequence > cutoff for event in newer)
    assert len(market.events(None, 2).events) <= 2


def test_overview_ladder_members_are_ordered(market: MarketEngine) -> None:
    overview = market.overview()
    for tier in overview.limit_ladder_tiers:
        assert len(tier.members) == min(tier.count, 24)
        assert all(member.is_limit_up for member in tier.members)
        times = [member.first_seal_time for member in tier.members if member.first_seal_time]
        assert times == sorted(times)
        assert "A股全市场" not in {member.theme for member in tier.members}
    for tier in overview.growth_streak_tiers:
        assert 0 < len(tier.members) <= 12


def test_members_put_unknown_seal_time_last() -> None:
    from backend.app.core.engine import _members

    group = pl.DataFrame(
        {
            "code": ["a", "b", "c"], "name": ["A", "B", "C"], "pct_chg": [10.0, 10.0, 10.0],
            "is_limit_up": [True, True, True], "first_seal_time": [None, "09:40:00", "09:30:00"],
            "open_board_count": [0, 0, 0], "seal_order_amount": [1.0, 1.0, 1.0],
            "theme_tags": [["A股全市场", "机器人"], [], ["AI算力"]], "amount_today": [9.0, 1.0, 2.0],
        }
    )
    members = _members(group, ["is_limit_up", "first_seal_time", "amount_today"], [True, False, True], 24)
    assert [member.code for member in members] == ["c", "b", "a"]
    assert [member.theme for member in members] == ["AI算力", None, "机器人"]


def test_seal_timeline_is_sorted_and_only_sealed(market: MarketEngine) -> None:
    timeline = market.overview().seal_timeline
    assert timeline
    times = [point.first_seal_time for point in timeline]
    assert times == sorted(times)
    assert len(timeline) <= 400
    overview = market.overview()
    assert sum(point.is_limit_up for point in timeline) >= overview.total_limit_up


def test_pct_filter_bounds(market: MarketEngine) -> None:
    both = market.query(ScreenerFilterRequest(pct_chg_min=3, pct_chg_max=7, limit=6000))
    assert both.total_matched > 0
    assert all(3 <= record.pct_chg <= 7 for record in both.records)
    only_min = market.query(ScreenerFilterRequest(pct_chg_min=9, limit=6000))
    assert all(record.pct_chg >= 9 for record in only_min.records)
    unbounded = market.query(ScreenerFilterRequest(limit=6000))
    assert only_min.total_matched < unbounded.total_matched
    only_max = market.query(ScreenerFilterRequest(pct_chg_max=-1, limit=6000))
    assert all(record.pct_chg <= -1 for record in only_max.records)


# --- HTTP ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_events_endpoint(client: TestClient) -> None:
    for _ in range(30):
        engine().refresh()
    body = client.get("/api/v1/market/events", params={"limit": 5}).json()
    assert len(body["events"]) <= 5
    assert body["provenance"]["mode"] in {"LIVE", "SIM"}
    sequences = [event["sequence"] for event in body["events"]]
    assert sequences == sorted(sequences, reverse=True)
    if body["events"]:
        newest = body["events"][0]["sequence"]
        assert client.get("/api/v1/market/events", params={"since": newest}).json()["events"] == []


def test_events_endpoint_validates_limit(client: TestClient) -> None:
    assert client.get("/api/v1/market/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/market/events", params={"limit": 301}).status_code == 422
    assert client.get("/api/v1/market/events", params={"since": -1}).status_code == 422


def test_parse_endpoint_uses_live_theme_names(client: TestClient) -> None:
    theme = engine().theme_names()[0]
    body = client.post("/api/v1/screener/parse", json={"text": f"首板 换手低于15% #{theme}"}).json()
    assert body["filters"]["limit_streak_exact"] == [1]
    assert body["filters"]["turnover_today_max"] == 15
    assert body["filters"]["selected_themes"] == [theme]
    assert body["unmatched"] == []
    assert "首板" in body["summary"]
    # The parsed filters must be directly usable as a query body.
    assert client.post("/api/v1/screener/query", json=body["filters"]).status_code == 200


def test_parse_endpoint_rejects_empty_text(client: TestClient) -> None:
    assert client.post("/api/v1/screener/parse", json={"text": ""}).status_code == 422


def test_overview_endpoint_carries_members_and_timeline(client: TestClient) -> None:
    body = client.get("/api/v1/market/overview").json()
    assert body["seal_timeline"]
    assert any(tier["members"] for tier in body["limit_ladder_tiers"])
