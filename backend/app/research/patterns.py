"""Classic A-share selection patterns evaluated on daily bars.

The rules follow the well-known open-source selectors (sngyai/Sequoia and
myhhub/stock "InStock") but are implemented here from their written definitions.
Each rule states its exact thresholds in `PatternDefinition.rule` and the numbers
that fired in `PatternHit.note`. Rules are strict on purpose: zero hits on a quiet
day is an honest answer, so thresholds are never loosened to manufacture signals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import polars as pl

from contracts.schema import (
    OHLCBar,
    PatternDefinition,
    PatternFamily,
    PatternHit,
    PatternSummary,
)
from . import indicators
from .metrics import LIMITS, board_of

SEQUOIA = ("Sequoia（sngyai）", "https://github.com/sngyai/Sequoia/tree/195cccb8e6/strategy")
INSTOCK = ("InStock（myhhub）", "https://github.com/myhhub/stock")
ADAPTED_MA = ("Pulse 改编（通用均线多头口径）", "")
ADAPTED_ATR = ("Pulse 改编（参考 Sequoia low_atr）", SEQUOIA[1])
ADAPTED_TDX = ("Pulse 改编（通达信/同花顺常用口径）", "")
NO_AMOUNT = "原规则另需成交额 ≥ 2 亿，日线无成交额字段，此条件省略。"


def _definition(id: str, name: str, family: PatternFamily, rule: str, origin: tuple[str, str]) -> PatternDefinition:
    return PatternDefinition(id=id, name=name, family=family, rule=rule, source=origin[0], source_url=origin[1])


DEFINITIONS: list[PatternDefinition] = [
    _definition("ma_bull", "均线多头", PatternFamily.TREND,
               "最新一根 MA5 > MA10 > MA20 > MA60，且 MA20 高于 5 根前的 MA20。", ADAPTED_MA),
    _definition("volume_surge", "放量上涨", PatternFamily.VOLUME,
               "最新一根涨幅 ≥ 2%，收盘不低于开盘，成交量 ≥ 前 5 日均量的 2 倍。" + NO_AMOUNT, INSTOCK),
    _definition("climax_limitdown", "放量跌停", PatternFamily.VOLUME,
               "最新一根跌幅达到跌停线（主板 -9.5%、创业板科创板 -19.5%、北交所 -29.5%），成交量 ≥ 前 5 日均量的 4 倍。" + NO_AMOUNT, INSTOCK),
    _definition("parking_apron", "停机坪", PatternFamily.BREAKOUT,
               "近 15 根内有涨停日 d（涨幅超过涨停线：主板 9.5%、创业板科创板 19.5%、北交所 29.5%），且 d 日收盘不低于截至 d 的 15 根最高收盘；"
               "d+1 日开盘与收盘均高于 d 日收盘且 0.97 < 收盘/开盘 < 1.03；d+2、d+3 日同样满足价格条件，且涨跌幅在 -5% 至 5% 之间；"
               "命中日为 d+3，须落在最近 3 根之内。", SEQUOIA),
    _definition("yearline_pullback", "回踩年线", PatternFamily.REVERSAL,
               "至少 250 根日线；在最近 60 根（仅 MA250 有定义的部分）中以最高收盘为界：峰前段首根收盘 < MA250 且末根收盘 > MA250；"
               "峰后段收盘均 ≥ MA250，最低收盘出现在峰后 10 至 50 根，峰日成交量 / 最低日成交量 > 2，最低收盘 / 峰日收盘 < 0.8。", SEQUOIA),
    _definition("platform_breakout", "突破平台", PatternFamily.BREAKOUT,
               "最近 60 根内存在某日 b：开盘 < 当日 MA60 ≤ 收盘，成交量 ≥ 前 5 日均量的 2 倍（InStock 口径）；"
               "窗口内 b 之前每日满足 -0.05 < (MA60 - 收盘)/MA60 < 0.2；取最近的 b，命中日为 b。", SEQUOIA),
    _definition("low_drawdown", "无大幅回撤", PatternFamily.TREND,
               "近 60 根涨幅 ≥ 60%；其间任一日涨跌幅 < -7%、任一日 (收盘-开盘)/开盘 < -7%（高开低走）、"
               "连续两日累计涨跌幅 < -10%、连续两日开盘到收盘累计跌幅 < -10% 均排除。", SEQUOIA),
    _definition("turtle_breakout", "海龟突破", PatternFamily.BREAKOUT,
               "最新收盘价 ≥ 最近 60 根（含当日）的最高收盘价（海龟入场 check_enter）。", SEQUOIA),
    _definition("high_tight_flag", "高而窄旗形", PatternFamily.BREAKOUT,
               "取 24 根前至 10 根前的窗口：最高收盘 / 最低价 ≥ 1.9，且窗口内出现连续两日涨幅达到涨停线（主板 9.5%、创业板科创板 19.5%、北交所 29.5%）。"
               "原规则另需龙虎榜机构席位，此条件省略。", INSTOCK),
    _definition("low_atr_growth", "低波成长", PatternFamily.VOLATILITY,
               "ATR14 / 收盘价 ≤ 2.5%，近 60 根涨幅 ≥ 15%，收盘不低于 MA20。", ADAPTED_ATR),
    _definition("macd_bottom_cross", "MACD 零轴下金叉", PatternFamily.REVERSAL,
               "最新一根 DIF 上穿 DEA（前一根 DIF ≤ DEA），且 DIF 与 DEA 均小于 0。", ADAPTED_TDX),
    _definition("kdj_oversold", "KDJ 超卖金叉", PatternFamily.REVERSAL,
               "最新一根 K 上穿 D（前一根 K ≤ D），且最新 D < 25 或前一根 J < 0。", ADAPTED_TDX),
]
MIN_BARS = 7


@dataclass(frozen=True)
class _Signal:
    index: int
    strength: float
    note: str


class _Ctx:
    """One stock's bars as plain lists, with indicators computed once on demand."""

    def __init__(
        self, code: str, name: str, dates: list[str], opens: list[float], highs: list[float],
        lows: list[float], closes: list[float], volumes: list[float],
    ) -> None:
        self.code = code
        self.name = name
        self.dates, self.opens, self.highs = dates, opens, highs
        self.lows, self.closes, self.volumes = lows, closes, volumes
        self.n = len(closes)
        self.last = self.n - 1
        self.limit = LIMITS[board_of(code)] * 100
        self._cache: dict[str, object] = {}

    @classmethod
    def from_bars(cls, code: str, name: str, bars: list[OHLCBar]) -> "_Ctx":
        return cls(
            code, name, [b.date for b in bars], [b.open for b in bars], [b.high for b in bars],
            [b.low for b in bars], [b.close for b in bars], [b.volume for b in bars],
        )

    def pct(self, index: int) -> float:
        previous = self.closes[index - 1]
        return (self.closes[index] / previous - 1) * 100 if previous > 0 else 0.0

    def is_limit_up(self, index: int) -> bool:
        return index > 0 and self.pct(index) >= self.limit

    def ma(self, period: int) -> indicators.Floats:
        key = f"ma{period}"
        if key not in self._cache:
            self._cache[key] = indicators.sma(self.closes, period)
        return self._cache[key]  # type: ignore[return-value]

    def macd(self):
        if "macd" not in self._cache:
            self._cache["macd"] = indicators.macd(self.closes)
        return self._cache["macd"]

    def kdj(self):
        if "kdj" not in self._cache:
            self._cache["kdj"] = indicators.kdj(self.highs, self.lows, self.closes)
        return self._cache["kdj"]

    def mean_volume(self, start: int, stop: int) -> float:
        window = self.volumes[start:stop]
        return sum(window) / len(window) if window else 0.0

    def ret(self, bars_back: int) -> float:
        return (self.closes[self.last] / self.closes[self.last - bars_back] - 1) * 100


def _ma_bull(c: _Ctx) -> _Signal | None:
    if c.n < 66:
        return None
    ma5, ma10, ma20, ma60 = (c.ma(p) for p in (5, 10, 20, 60))
    a, b, d, e = ma5[c.last], ma10[c.last], ma20[c.last], ma60[c.last]
    earlier = ma20[c.last - 5]
    if None in (a, b, d, e, earlier) or not (a > b > d > e) or not d > earlier:
        return None
    strength = (a / e - 1) * 100
    return _Signal(c.last, strength, f"MA5 {a:.2f} > MA10 {b:.2f} > MA20 {d:.2f} > MA60 {e:.2f}，MA20 较 5 根前 {earlier:.2f} 上行")


def _volume_ratio(c: _Ctx, index: int) -> float:
    base = c.mean_volume(index - 5, index)
    return c.volumes[index] / base if base > 0 else 0.0


def _volume_surge(c: _Ctx) -> _Signal | None:
    if c.n < MIN_BARS:
        return None
    ratio = _volume_ratio(c, c.last)
    pct = c.pct(c.last)
    if pct < 2 or c.closes[c.last] < c.opens[c.last] or ratio < 2:
        return None
    return _Signal(c.last, ratio, f"涨幅 {pct:.2f}%，收盘 {c.closes[c.last]:.2f} 不低于开盘，成交量为前 5 日均量的 {ratio:.2f} 倍")


def _climax_limitdown(c: _Ctx) -> _Signal | None:
    if c.n < MIN_BARS:
        return None
    ratio = _volume_ratio(c, c.last)
    pct = c.pct(c.last)
    if pct > -c.limit or ratio < 4:
        return None
    return _Signal(c.last, ratio, f"跌幅 {pct:.2f}% 触及跌停线 -{c.limit:.1f}%，成交量为前 5 日均量的 {ratio:.2f} 倍")


def _steady_body(c: _Ctx, index: int, floor: float) -> bool:
    """Open and close both above `floor`, and the body within 3% of the open."""
    opening, close = c.opens[index], c.closes[index]
    return opening > floor and close > floor and 0.97 < close / opening < 1.03


def _parking_apron(c: _Ctx) -> _Signal | None:
    if c.n < 20:
        return None
    for d in range(c.last - 3, max(c.last - 6, 0), -1):  # d + 3 must be one of the last 3 bars
        if c.pct(d) <= c.limit or c.closes[d] < max(c.closes[max(0, d - 14) : d + 1]):
            continue
        floor = c.closes[d]
        if not all(_steady_body(c, i, floor) for i in (d + 1, d + 2, d + 3)):
            continue
        if not all(-5 < c.pct(i) < 5 for i in (d + 2, d + 3)):
            continue
        lift = (c.closes[d + 3] / floor - 1) * 100
        note = f"{c.dates[d]} 涨停 {c.pct(d):.2f}% 收 {floor:.2f}，随后 3 日开收均高于涨停日收盘且波幅小于 3%"
        return _Signal(d + 3, max(lift, 0.0), note)
    return None


def _yearline_pullback(c: _Ctx) -> _Signal | None:
    if c.n < 250:
        return None
    line = c.ma(250)
    window = range(max(c.last - 59, 249), c.last + 1)
    peak = max(window, key=lambda i: c.closes[i])
    before, after = range(window.start, peak), range(peak + 1, c.last + 1)
    if not before or len(after) < 10:
        return None
    if not (c.closes[before[0]] < line[before[0]] and c.closes[before[-1]] > line[before[-1]]):
        return None
    if any(c.closes[i] < line[i] for i in after):
        return None
    low = min(after, key=lambda i: c.closes[i])
    if not 10 <= low - peak <= 50 or c.volumes[low] <= 0:
        return None
    volume_ratio = c.volumes[peak] / c.volumes[low]
    drop = c.closes[low] / c.closes[peak]
    if volume_ratio <= 2 or drop >= 0.8:
        return None
    note = f"{c.dates[peak]} 高点 {c.closes[peak]:.2f} 回落至 {c.dates[low]} 的 {c.closes[low]:.2f}（{(drop - 1) * 100:.1f}%），仍在 MA250 {line[low]:.2f} 之上，峰谷量比 {volume_ratio:.2f}"
    return _Signal(c.last, (1 - drop) * 100, note)


def _platform_breakout(c: _Ctx) -> _Signal | None:
    if c.n < 66:
        return None
    ma60 = c.ma(60)
    start = max(c.last - 59, 59)
    for b in range(c.last, start, -1):
        line = ma60[b]
        ratio = _volume_ratio(c, b)
        if not (c.opens[b] < line <= c.closes[b]) or ratio < 2:
            continue
        if all(-0.05 < (ma60[i] - c.closes[i]) / ma60[i] < 0.2 for i in range(start, b)):
            note = f"{c.dates[b]} 收盘 {c.closes[b]:.2f} 上穿 MA60 {line:.2f}（开盘 {c.opens[b]:.2f}），量比 {ratio:.2f}，此前整理在 MA60 附近"
            return _Signal(b, ratio, note)
    return None


def _low_drawdown(c: _Ctx) -> _Signal | None:
    if c.n < 61:
        return None
    gain = c.ret(60)
    if gain < 60:
        return None
    days = range(c.last - 59, c.last + 1)
    day_drop = min(c.pct(i) for i in days)
    intraday = min((c.closes[i] / c.opens[i] - 1) * 100 for i in days)
    two_day = min((c.closes[i] / c.closes[i - 2] - 1) * 100 for i in days)
    two_open = min((c.closes[i] / c.opens[i - 1] - 1) * 100 for i in days)
    if day_drop < -7 or intraday < -7 or two_day < -10 or two_open < -10:
        return None
    return _Signal(c.last, gain, f"近 60 根涨幅 {gain:.1f}%，最大单日跌幅 {day_drop:.2f}%，最大两日累计 {two_day:.2f}%")


def _turtle_breakout(c: _Ctx) -> _Signal | None:
    if c.n < 61:
        return None
    close = c.closes[c.last]
    if close < max(c.closes[c.last - 59 :]):
        return None
    ceiling = max(c.closes[c.last - 60 : c.last])
    return _Signal(c.last, max((close / ceiling - 1) * 100, 0.0), f"收盘 {close:.2f} 不低于近 60 根最高收盘（此前最高 {ceiling:.2f}）")


def _high_tight_flag(c: _Ctx) -> _Signal | None:
    if c.n < 25:
        return None
    window = range(c.last - 24, c.last - 9)
    lowest = min(c.lows[i] for i in window)
    ratio = max(c.closes[i] for i in window) / lowest if lowest > 0 else 0.0
    if ratio < 1.9:
        return None
    pair = [i for i in window if i + 1 in window and c.pct(i) >= c.limit and c.pct(i + 1) >= c.limit]
    if not pair:
        return None
    return _Signal(c.last, ratio, f"窗口内最高收盘/最低价 {ratio:.2f} 倍，{c.dates[pair[-1]]} 起连续两个涨停")


def _low_atr_growth(c: _Ctx) -> _Signal | None:
    if c.n < 61:
        return None
    value = indicators.atr(c.highs, c.lows, c.closes)[c.last]
    line = c.ma(20)[c.last]
    close = c.closes[c.last]
    if value is None or line is None or close <= 0:
        return None
    ratio = value / close * 100
    gain = c.ret(60)
    if ratio > 2.5 or gain < 15 or close < line:
        return None
    return _Signal(c.last, gain / max(ratio, 0.1), f"ATR14 占收盘 {ratio:.2f}%，近 60 根涨幅 {gain:.1f}%，收盘 {close:.2f} ≥ MA20 {line:.2f}")


def _macd_bottom_cross(c: _Ctx) -> _Signal | None:
    if c.n < 36:
        return None
    dif, dea, _ = c.macd()
    a0, b0, a1, b1 = dif[c.last - 1], dea[c.last - 1], dif[c.last], dea[c.last]
    if None in (a0, b0, a1, b1) or not (a0 <= b0 and a1 > b1 and a1 < 0 and b1 < 0):
        return None
    return _Signal(c.last, (a1 - b1) / c.closes[c.last] * 100, f"DIF {a1:.3f} 上穿 DEA {b1:.3f}，均在零轴下")


def _kdj_oversold(c: _Ctx) -> _Signal | None:
    if c.n < 11:
        return None
    k_line, d_line, j_line = c.kdj()
    k0, d0, k1, d1, j0 = k_line[c.last - 1], d_line[c.last - 1], k_line[c.last], d_line[c.last], j_line[c.last - 1]
    if None in (k0, d0, k1, d1, j0) or not (k0 <= d0 and k1 > d1) or not (d1 < 25 or j0 < 0):
        return None
    return _Signal(c.last, k1 - d1, f"K {k1:.1f} 上穿 D {d1:.1f}，前一根 J {j0:.1f}")


_RULES: dict[str, Callable[[_Ctx], _Signal | None]] = {
    "ma_bull": _ma_bull,
    "volume_surge": _volume_surge,
    "climax_limitdown": _climax_limitdown,
    "parking_apron": _parking_apron,
    "yearline_pullback": _yearline_pullback,
    "platform_breakout": _platform_breakout,
    "low_drawdown": _low_drawdown,
    "turtle_breakout": _turtle_breakout,
    "high_tight_flag": _high_tight_flag,
    "low_atr_growth": _low_atr_growth,
    "macd_bottom_cross": _macd_bottom_cross,
    "kdj_oversold": _kdj_oversold,
}


def _evaluate(c: _Ctx) -> list[PatternHit]:
    hits: list[PatternHit] = []
    for definition in DEFINITIONS:
        signal = _RULES[definition.id](c)
        if signal is None:
            continue
        hits.append(
            PatternHit(
                pattern_id=definition.id,
                code=c.code,
                name=c.name,
                date=c.dates[signal.index],
                price=round(c.closes[c.last], 2),
                pct_chg=round(c.pct(c.last), 2) if c.n > 1 else None,
                strength=round(signal.strength, 3),
                note=signal.note,
            )
        )
    return hits


def match(code: str, name: str, bars: list[OHLCBar]) -> list[PatternHit]:
    """Every pattern the stock triggers on its latest bar."""
    if len(bars) < MIN_BARS:
        return []
    return _evaluate(_Ctx.from_bars(code, name, bars))


def scan(panel: pl.DataFrame, names: dict[str, str] | None = None, limit: int = 30) -> list[PatternSummary]:
    """Run all rules over a long (code, date, OHLCV) panel; hits sorted by strength."""
    labels = names or {}
    found: dict[str, list[PatternHit]] = {definition.id: [] for definition in DEFINITIONS}
    for key, frame in panel.sort("date").partition_by("code", as_dict=True, maintain_order=True).items():
        code = key[0] if isinstance(key, tuple) else key
        ctx = _Ctx(code, labels.get(code, code), *(frame[c].to_list() for c in ("date", "open", "high", "low", "close", "volume")))
        if ctx.n < MIN_BARS:
            continue
        for hit in _evaluate(ctx):
            found[hit.pattern_id].append(hit)
    return [
        PatternSummary(
            pattern=definition,
            hit_count=len(found[definition.id]),
            hits=sorted(found[definition.id], key=lambda h: h.strength or 0, reverse=True)[:limit],
        )
        for definition in DEFINITIONS
    ]
