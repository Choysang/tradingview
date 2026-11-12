"""Screen field catalog ``screen-fields-v1``: the whitelist of fields a ScreenAst may reference, with their units.

Why: PEX10 - "字段目录固定", units are never guessed, a screen cannot run arbitrary SQL/scripts. Only fields that EXIST in
pulse3 data are listed (daily bars, limit-up class, optional float-cap facts, instrument board); the old filter-rail
fields that need turnover, order-book seal data, valuation or themes are listed in ``UNSUPPORTED`` so the parser can
say "unsupported" instead of dropping them silently.
Invariants: every field has a unit string; operand units must equal the field unit (no conversion); a field value is
``None`` (UNKNOWN in the three-valued evaluation) when it cannot be computed - never 0. Values are computed at the
latest completed bar <= decision_as_of by :func:`field_values` from an InstrumentView (shared kernels).
Not covered: sector/theme membership, valuation, turnover rate, seal quality (no PIT source in pulse3 today).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..playbooks.view import InstrumentView, ok
from . import patterns as P

CATALOG_VERSION = "screen-fields-v1"
FIELD_VERSION = "v1"
SUBJECT_ID = "00000000-0000-0000-0000-000000000000"   # FeatureOperand.instrument_id placeholder = "each candidate"
SESSION_POLICY = "regular_continuous_v1"
RESOLUTION_POLICY = "latest-completed-bar-v1"


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    unit: str
    description: str
    group: str
    needs: str = "bars"          # bars | float_cap | instrument


_F = Field
FIELDS: tuple[Field, ...] = (
    _F("close", "最新价", "CNY", "最新完成交易日收盘（前复权研究序列；最新一根等于不复权价，历史价位为复权值）", "price"),
    _F("pct_chg", "涨幅", "pct", "当日涨跌幅（%）", "price"),
    _F("ret_5d", "5日涨幅", "pct", "收盘较 5 根前（%）", "price"),
    _F("ret_20d", "20日涨幅", "pct", "收盘较 20 根前（%）", "price"),
    _F("ret_60d", "60日涨幅", "pct", "收盘较 60 根前（%）", "price"),
    _F("ret_250d", "250日涨幅", "pct", "收盘较 250 根前（%）", "price"),
    _F("volume_ratio_5", "量比", "ratio", "当日成交量 / 前 5 日均量", "volume"),
    _F("turnover_proxy_20", "估算日均成交额", "100m_cny", "近 20 日 收盘×成交量 均值（亿元，估算：来源无成交额，收盘为复权价）", "volume"),
    _F("limit_streak", "连板数", "count", "当前连续涨停收盘天数（按板块涨跌幅，容差 0.5pp）", "limit"),
    _F("growth_streak", "连阳天数", "count", "当前连续上涨收盘天数", "limit"),
    _F("limit_count_250", "近一年涨停次数", "count", "近 250 根内涨停收盘次数", "limit"),
    _F("dist_60d_high", "距60日新高", "pct", "收盘 / 近 60 日最高价 - 1（%）；≥0 即创 60 日新高", "trend"),
    _F("ma_gap_20", "20日乖离", "pct", "收盘 / MA20 - 1（%）", "trend"),
    _F("rsi_14", "RSI14", "index", "Wilder RSI(14)", "trend"),
    _F("kdj_j", "KDJ-J", "index", "KDJ(9,3,3) 的 J 值", "trend"),
    _F("atr_ratio_14", "ATR占比", "pct", "ATR14 / 收盘（%）", "trend"),
    _F("float_cap", "流通市值", "100m_cny", "流通市值（亿元）：最新快照流通市值 × 当日收盘 / 最新收盘（股本固定近似）", "cap", "float_cap"),
    _F("listed_sessions", "已交易天数", "count", "研究序列中已有的交易日数（下限）", "instrument"),
    _F("is_new", "次新股", "flag", "已交易天数 < 60 为 1", "instrument", "instrument"),
    _F("is_st", "ST", "flag", "ST/*ST 为 1（取实例状态 is_risk_warning）", "instrument", "instrument"),
    _F("board_main", "沪深主板", "flag", "主板为 1", "instrument", "instrument"),
    _F("board_chinext", "创业板", "flag", "创业板为 1", "instrument", "instrument"),
    _F("board_star", "科创板", "flag", "科创板为 1", "instrument", "instrument"),
    _F("board_bse", "北交所", "flag", "北交所为 1", "instrument", "instrument"),
    *[_F(f"pattern.{d.id}", d.name, "flag", d.rule, "pattern") for d in P.DEFINITIONS],
)
BY_ID: dict[str, Field] = {f.id: f for f in FIELDS}

#: phrases of the old filter rail that pulse3 data cannot answer (-> ScreenParseIssue reason "unsupported")
UNSUPPORTED = (
    "换手", "换手率", "昨换比", "成交额", "封单", "封成比", "封流比", "封板", "炸板", "首封", "秒板", "一字板",
    "市盈率", "市净率", "pe", "pb", "估值", "题材", "概念", "板块", "行业", "龙虎榜", "机构", "席位", "自由流通",
)


@dataclass(frozen=True)
class InstrumentFacts:
    """Instrument-level inputs of the flag fields (None = unknown)."""
    board_code: str | None = None          # main | chinext | star | bse
    is_risk_warning: bool | None = None
    float_cap_cny: float | None = None     # latest snapshot float cap in CNY at the evaluation bar (scaled), if any


def field_values(view: InstrumentView, facts: InstrumentFacts, i: int | None = None) -> dict[str, float | None]:
    """All catalog fields at bar ``i`` (default last). Insufficient history -> None, never 0."""
    i = view.n - 1 if i is None else i
    if i < 0:
        return {f.id: None for f in FIELDS}
    c = view.close[i]
    h60 = view.hhv("high", 60)[i]
    ma20 = view.ma(20)[i]
    atr = view.atr(14)[i]
    t20 = view.turnover_ma(20)[i]
    v: dict[str, float | None] = {
        "close": c,
        "pct_chg": view.pct()[i],
        "ret_5d": view.ret(5)[i], "ret_20d": view.ret(20)[i], "ret_60d": view.ret(60)[i], "ret_250d": view.ret(250)[i],
        "volume_ratio_5": view.volume_ratio5()[i],
        "turnover_proxy_20": None if t20 is None else t20 / 1e8,
        "limit_streak": view.limit_streak()[i],
        "growth_streak": view.growth_streak()[i],
        "limit_count_250": view.limit_count(250)[i],
        "dist_60d_high": None if not ok(c, h60) or h60 <= 0 else (c / h60 - 1) * 100,
        "ma_gap_20": None if not ok(c, ma20) or ma20 <= 0 else (c / ma20 - 1) * 100,
        "rsi_14": view.rsi(14)[i],
        "kdj_j": view.kdj()["j"][i],
        "atr_ratio_14": None if not ok(atr, c) or c <= 0 else atr / c * 100,
        "float_cap": None if facts.float_cap_cny is None else facts.float_cap_cny / 1e8,
        "listed_sessions": float(i + 1),
        "is_new": 1.0 if i + 1 < 60 else 0.0,
        "is_st": None if facts.is_risk_warning is None else (1.0 if facts.is_risk_warning else 0.0),
    }
    for b in ("main", "chinext", "star", "bse"):
        v[f"board_{b}"] = None if facts.board_code is None else (1.0 if facts.board_code == b else 0.0)
    for d in P.DEFINITIONS:
        r = P.evaluate(view, d.id, i)
        v[f"pattern.{d.id}"] = None if r is P.INSUFFICIENT else (1.0 if r is not None else 0.0)
    return v
