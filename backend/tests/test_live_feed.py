"""Live adapter contract. A stub akshare proves the mapping without a network.

AkShare returns pandas objects in production; the stub returns polars frames so
the suite stays dependency-free. The pandas conversion path itself is covered
only by the `pl.from_pandas` branch and is not exercised here.
"""
from __future__ import annotations

import sys
import types

import polars as pl
import pytest

from backend.app.factors.engine import compute
from backend.app.market.live import AkShareFeed

SPOT = pl.DataFrame(
    {
        "代码": ["600000", "300750", "832491", "600519"],
        "名称": ["浦发银行", "宁德时代", "奥迪威", "*ST测试"],
        "最新价": [10.0, 200.0, 30.0, 5.0],
        "涨跌幅": [10.0, 20.0, 30.0, 2.0],
        "成交额": [1e9, 2e9, 3e8, 1e8],
        "换手率": [1.5, 2.5, 3.5, 0.5],
        "量比": [1.2, 2.0, 3.0, 0.8],
        "流通市值": [1e11, 5e11, 2e9, 1e10],
    }
)

SINA_SPOT = pl.DataFrame(
    {
        "代码": ["600000", "300750"],
        "名称": ["浦发银行", "宁德时代"],
        "最新价": [11.0, 240.0],
        "涨跌幅": [10.0, 20.0],
        "成交额": [1e9, 2e9],
        "昨收": [10.0, 200.0],
    }
)

POOL = pl.DataFrame(
    {
        "代码": ["600000", "300750"],
        "连板数": [3, 1],
        "首次封板时间": ["093100", "100500"],
        "最后封板时间": ["093100", "100500"],
        "封板资金": [5e8, 2e8],
        "炸板次数": [0, 1],
    }
)


def _module(**functions):
    module = types.ModuleType("akshare")
    for name, function in functions.items():
        setattr(module, name, function)
    return module


@pytest.fixture
def eastmoney(monkeypatch):
    monkeypatch.setattr("backend.app.market.live._tencent_quotes", lambda codes: {})
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(
            stock_zh_a_spot_em=lambda: SPOT,
            stock_zt_pool_em=lambda date: POOL,
            stock_zh_a_spot=lambda: SINA_SPOT,
        ),
    )


def test_maps_spot_and_pool_into_contract_columns(eastmoney) -> None:
    frame = AkShareFeed().refresh_now()
    assert frame is not None
    assert frame.height == 4

    rows = {row["code"]: row for row in frame.to_dicts()}
    assert rows["600000"]["board"] == "10cm"
    assert rows["300750"]["board"] == "20cm"
    assert rows["832491"]["board"] == "30cm"
    assert rows["600519"]["is_st"] is True

    # Amounts are yuan in the source and 亿元 in the contract.
    assert rows["600000"]["amount_today"] == pytest.approx(10.0)
    assert rows["600000"]["float_mkt_cap"] == pytest.approx(1000.0)

    # Limit price comes from the previous close, not from the reported change.
    assert rows["600000"]["limit_price"] == pytest.approx(10.0)
    assert rows["300750"]["limit_price"] == pytest.approx(200.0)

    # 连板数 counts today, while the factor adds today on top of yesterday.
    assert rows["600000"]["prev_limit"] == 2
    assert rows["300750"]["prev_limit"] == 0
    assert rows["832491"]["prev_limit"] == 0
    assert rows["600000"]["ever_limit_up_today"] is True
    assert rows["832491"]["ever_limit_up_today"] is False
    assert rows["600000"]["seal_order_amount"] == pytest.approx(5.0)
    # Eastmoney sends HHMMSS; the contract wants HH:MM:SS.
    assert rows["600000"]["first_seal_time"] == "09:31:00"


def test_sina_fallback_when_eastmoney_is_unreachable(monkeypatch) -> None:
    def boom():
        raise RuntimeError("proxy blocked")

    monkeypatch.setattr("backend.app.market.live._tencent_quotes", lambda codes: {})
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(stock_zh_a_spot_em=boom, stock_zh_a_spot=lambda: SINA_SPOT, stock_zt_pool_em=boom),
    )
    feed = AkShareFeed()
    frame = feed.refresh_now()
    assert frame is not None
    assert frame.height == 2
    rows = {row["code"]: row for row in frame.to_dicts()}
    assert rows["600000"]["previous_close"] == pytest.approx(10.0)
    assert rows["600000"]["limit_price"] == pytest.approx(11.0)
    assert "新浪" in feed.note
    # Sina has no float cap, and turnover stays null without Tencent.
    assert rows["600000"]["turnover_today"] is None
    assert rows["600000"]["float_mkt_cap"] is None


def test_live_frame_runs_through_the_factor_graph(eastmoney) -> None:
    frame = compute(AkShareFeed().refresh_now())
    rows = {
        row["code"]: row
        for row in frame.select(
            ["code", "is_limit_up", "limit_streak", "seal_to_amount_ratio", "turnover_ratio_yd"]
        ).to_dicts()
    }
    assert rows["600000"]["is_limit_up"] is True
    assert rows["600000"]["limit_streak"] == 3
    assert rows["300750"]["limit_streak"] == 1
    assert rows["832491"]["is_limit_up"] is True  # 30cm board at exactly +30%
    assert rows["600519"]["is_limit_up"] is False
    assert rows["600000"]["turnover_ratio_yd"] is None
    assert rows["600000"]["seal_to_amount_ratio"] is not None
    assert rows["832491"]["seal_to_amount_ratio"] is None


def test_failure_returns_none(monkeypatch) -> None:
    def boom():
        raise RuntimeError("upstream down")

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(stock_zh_a_spot_em=boom, stock_zh_a_spot=boom, stock_zt_pool_em=boom),
    )
    feed = AkShareFeed()
    assert feed.refresh_now() is None
    assert feed.error


def test_missing_columns_fall_through_to_none(monkeypatch) -> None:
    monkeypatch.setattr("backend.app.market.live._tencent_quotes", lambda codes: {})
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(
            stock_zh_a_spot_em=lambda: SPOT.drop("换手率"),
            stock_zh_a_spot=lambda: SINA_SPOT.drop("昨收"),
            stock_zt_pool_em=lambda date: POOL,
        ),
    )
    feed = AkShareFeed()
    assert feed.refresh_now() is None
    assert "不可用" in feed.error or "不完整" in feed.error


def test_turnover_is_enriched_from_tencent(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.market.live._tencent_quotes",
        lambda codes: {"600000": {"turnover": 1.25}, "300750": {"turnover": 0.75}},
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(stock_zh_a_spot_em=boom(), stock_zh_a_spot=lambda: SINA_SPOT, stock_zt_pool_em=boom()),
    )
    feed = AkShareFeed()
    frame = feed.refresh_now()
    rows = {row["code"]: row for row in frame.to_dicts()}
    assert rows["600000"]["turnover_today"] == pytest.approx(1.25)
    assert rows["300750"]["turnover_today"] == pytest.approx(0.75)


def boom():
    def inner():
        raise RuntimeError("blocked")

    return inner


def test_quotes_fill_turnover_volume_ratio_cap_and_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.market.live._tencent_quotes",
        lambda codes: {
            "600000": {"turnover": 1.25, "volume_ratio": 0.9, "float_cap": 3050.0, "limit_up": 11.05},
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        _module(stock_zh_a_spot_em=boom, stock_zh_a_spot=lambda: SINA_SPOT, stock_zt_pool_em=boom),
    )
    feed = AkShareFeed()
    frame = feed.refresh_now()
    rows = {row["code"]: row for row in frame.to_dicts()}
    assert rows["600000"]["turnover_today"] == pytest.approx(1.25)
    assert rows["600000"]["volume_ratio"] == pytest.approx(0.9)
    assert rows["600000"]["float_mkt_cap"] == pytest.approx(3050.0)
    assert rows["600000"]["free_float_mkt_cap"] == pytest.approx(1891.0)
    # A quoted limit beats a derived one: it already knows ST and board rules.
    assert rows["600000"]["limit_price"] == pytest.approx(11.05)
