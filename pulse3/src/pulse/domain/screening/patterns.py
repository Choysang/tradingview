"""The 12 pattern-radar rules, evaluated on the latest completed bar of an InstrumentView.

Why: LEGACY_FEATURE_INVENTORY §5 "Pattern rules (research/patterns, 12)" must come back as shared, PIT, testable
rules. The old file (read as historical evidence of the rule SEMANTICS only; no code was copied) states every threshold
in words; those words are the spec here. Rules are strict on purpose: zero hits on a quiet day is an honest answer.
Invariants: ``evaluate(view, i)`` reads only indexes <= i (tests truncate the view and compare); a rule that lacks the
history it needs returns ``INSUFFICIENT`` (screen value UNKNOWN), never "no hit"; indicators come from the shared kernels
(MACD uses the sma-seeded EMA of definition v1, so values differ slightly from 通达信's first-close seed in the first
bars; KDJ/BOLL/ATR are the shared kernels). Original rules that needed turnover amount or Dragon-Tiger seats omit that
condition (the source has neither) and say so in ``omitted``.
Not covered: turnover/amount conditions, seats, ST handling.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..playbooks.view import InstrumentView, ok

INSUFFICIENT = object()


@dataclass(frozen=True)
class PatternDef:
    id: str
    name: str
    family: str
    rule: str
    min_bars: int
    omitted: str = ""


@dataclass(frozen=True)
class PatternHit:
    strength: float
    note: str


def _lim(v: InstrumentView) -> float:
    return v.limit_pct - 0.5


def _ma_bull(v: InstrumentView, i: int):
    m5, m10, m20, m60 = (v.ma(n) for n in (5, 10, 20, 60))
    a, b, d, e, earlier = m5[i], m10[i], m20[i], m60[i], m20[i - 5]
    if not ok(a, b, d, e, earlier) or not (a > b > d > e) or not d > earlier:
        return None
    return PatternHit((a / e - 1) * 100, f"MA5 {a:.2f} > MA10 {b:.2f} > MA20 {d:.2f} > MA60 {e:.2f}，MA20 较 5 根前 {earlier:.2f} 上行")


def _vr5(v: InstrumentView, i: int) -> float | None:
    return v.volume_ratio5()[i]


def _volume_surge(v: InstrumentView, i: int):
    p, r = v.pct()[i], _vr5(v, i)
    if not ok(p, r) or p < 2 or v.close[i] < v.open[i] or r < 2:
        return None
    return PatternHit(r, f"涨幅 {p:.2f}%，收盘不低于开盘，成交量为前 5 日均量的 {r:.2f} 倍")


def _climax_limitdown(v: InstrumentView, i: int):
    p, r = v.pct()[i], _vr5(v, i)
    if not ok(p, r) or p > -_lim(v) or r < 4:
        return None
    return PatternHit(r, f"跌幅 {p:.2f}% 触及跌停线，成交量为前 5 日均量的 {r:.2f} 倍")


def _steady(v: InstrumentView, k: int, floor: float) -> bool:
    o, c = v.open[k], v.close[k]
    return ok(o, c) and o > floor and c > floor and 0.97 < c / o < 1.03


def _parking_apron(v: InstrumentView, i: int):
    pct = v.pct()
    for d in range(i - 3, max(i - 6, 0), -1):
        if not ok(pct[d]) or pct[d] <= _lim(v) or v.close[d] < max(v.close[max(0, d - 14):d + 1]):
            continue
        floor = v.close[d]
        if not all(_steady(v, k, floor) for k in (d + 1, d + 2, d + 3)):
            continue
        if not all(ok(pct[k]) and -5 < pct[k] < 5 for k in (d + 2, d + 3)):
            continue
        return PatternHit(max((v.close[d + 3] / floor - 1) * 100, 0.0), f"{v.dates[d]} 涨停后连续 3 日开收均高于涨停日收盘且波幅小于 3%")
    return None


def _yearline_pullback(v: InstrumentView, i: int):
    line = v.ma(250)
    lo = max(i - 59, 249)
    rng = range(lo, i + 1)
    if not all(ok(v.close[k], line[k]) for k in rng):
        return INSUFFICIENT
    peak = max(rng, key=lambda k: v.close[k])
    before, after = range(lo, peak), range(peak + 1, i + 1)
    if not before or len(after) < 10:
        return None
    if not (v.close[before[0]] < line[before[0]] and v.close[before[-1]] > line[before[-1]]):
        return None
    if any(v.close[k] < line[k] for k in after):
        return None
    low = min(after, key=lambda k: v.close[k])
    if not 10 <= low - peak <= 50 or not ok(v.volume[low], v.volume[peak]) or v.volume[low] <= 0:
        return None
    vr, drop = v.volume[peak] / v.volume[low], v.close[low] / v.close[peak]
    if vr <= 2 or drop >= 0.8:
        return None
    return PatternHit((1 - drop) * 100, f"{v.dates[peak]} 高点回落 {(drop - 1) * 100:.1f}% 仍在 MA250 之上，峰谷量比 {vr:.2f}")


def _platform_breakout(v: InstrumentView, i: int):
    ma60, vr = v.ma(60), v.volume_ratio5()
    start = max(i - 59, 59)
    for b in range(i, start, -1):
        line, r = ma60[b], vr[b]
        if not ok(line, r, v.open[b], v.close[b]) or not (v.open[b] < line <= v.close[b]) or r < 2:
            continue
        if all(ok(ma60[k], v.close[k]) and -0.05 < (ma60[k] - v.close[k]) / ma60[k] < 0.2 for k in range(start, b)):
            return PatternHit(r, f"{v.dates[b]} 收盘上穿 MA60（开盘在其下），量比 {r:.2f}，此前整理在 MA60 附近")
    return None


def _low_drawdown(v: InstrumentView, i: int):
    gain = v.ret(60)[i]
    if not ok(gain):
        return INSUFFICIENT
    if gain < 60:
        return None
    pct = v.pct()
    days = range(i - 59, i + 1)
    if not all(ok(pct[k], v.open[k], v.close[k]) for k in days):
        return INSUFFICIENT
    day_drop = min(pct[k] for k in days)
    intraday = min((v.close[k] / v.open[k] - 1) * 100 for k in days)
    two_day = min((v.close[k] / v.close[k - 2] - 1) * 100 for k in days)
    two_open = min((v.close[k] / v.open[k - 1] - 1) * 100 for k in days)
    if day_drop < -7 or intraday < -7 or two_day < -10 or two_open < -10:
        return None
    return PatternHit(gain, f"近 60 根涨幅 {gain:.1f}%，最大单日跌幅 {day_drop:.2f}%，最大两日累计 {two_day:.2f}%")


def _turtle_breakout(v: InstrumentView, i: int):
    w = v.close[i - 59:i + 1]
    if not all(ok(x) for x in w):
        return INSUFFICIENT
    if v.close[i] < max(w):
        return None
    ceil = max(v.close[i - 60:i])
    return PatternHit(max((v.close[i] / ceil - 1) * 100, 0.0), f"收盘不低于近 60 根最高收盘（此前最高 {ceil:.2f}）")


def _high_tight_flag(v: InstrumentView, i: int):
    window = range(i - 24, i - 9)
    lows = [v.low[k] for k in window]
    closes = [v.close[k] for k in window]
    if not all(ok(x) for x in lows + closes):
        return INSUFFICIENT
    lowest = min(lows)
    ratio = max(closes) / lowest if lowest > 0 else 0.0
    if ratio < 1.9:
        return None
    pct = v.pct()
    pair = [k for k in window if k + 1 in window and ok(pct[k], pct[k + 1]) and pct[k] >= _lim(v) and pct[k + 1] >= _lim(v)]
    if not pair:
        return None
    return PatternHit(ratio, f"窗口内最高收盘/最低价 {ratio:.2f} 倍，{v.dates[pair[-1]]} 起连续两个涨停")


def _low_atr_growth(v: InstrumentView, i: int):
    a, line, c, gain = v.atr(14)[i], v.ma(20)[i], v.close[i], v.ret(60)[i]
    if not ok(a, line, c, gain) or c <= 0:
        return INSUFFICIENT
    ratio = a / c * 100
    if ratio > 2.5 or gain < 15 or c < line:
        return None
    return PatternHit(gain / max(ratio, 0.1), f"ATR14 占收盘 {ratio:.2f}%，近 60 根涨幅 {gain:.1f}%，收盘不低于 MA20")


def _macd_bottom_cross(v: InstrumentView, i: int):
    m = v.macd()
    a0, b0, a1, b1 = m["macd"][i - 1], m["signal"][i - 1], m["macd"][i], m["signal"][i]
    if not ok(a0, b0, a1, b1):
        return INSUFFICIENT
    if not (a0 <= b0 and a1 > b1 and a1 < 0 and b1 < 0):
        return None
    return PatternHit((a1 - b1) / v.close[i] * 100, f"DIF {a1:.3f} 上穿 DEA {b1:.3f}，均在零轴下")


def _kdj_oversold(v: InstrumentView, i: int):
    k = v.kdj()
    k0, d0, k1, d1, j0 = k["k"][i - 1], k["d"][i - 1], k["k"][i], k["d"][i], k["j"][i - 1]
    if not ok(k0, d0, k1, d1, j0):
        return INSUFFICIENT
    if not (k0 <= d0 and k1 > d1) or not (d1 < 25 or j0 < 0):
        return None
    return PatternHit(k1 - d1, f"K {k1:.1f} 上穿 D {d1:.1f}，前一根 J {j0:.1f}")


DEFINITIONS: tuple[PatternDef, ...] = (
    PatternDef("ma_bull", "均线多头", "trend", "最新一根 MA5 > MA10 > MA20 > MA60，且 MA20 高于 5 根前的 MA20。", 66),
    PatternDef("volume_surge", "放量上涨", "volume", "涨幅 ≥ 2%，收盘不低于开盘，成交量 ≥ 前 5 日均量 2 倍。", 7, "原规则另需成交额 ≥ 2 亿：日线无成交额字段，省略"),
    PatternDef("climax_limitdown", "放量跌停", "volume", "跌幅达到跌停线（板块涨跌幅 - 0.5pp），成交量 ≥ 前 5 日均量 4 倍。", 7, "成交额条件省略"),
    PatternDef("parking_apron", "停机坪", "breakout", "近 15 根内涨停日 d 且 d 收盘不低于其前 15 根最高收盘；d+1..d+3 开收均高于 d 收盘、波幅 < 3%，d+2、d+3 涨跌幅在 ±5% 内；命中日 d+3 在最近 3 根内。", 20),
    PatternDef("yearline_pullback", "回踩年线", "reversal", "至少 250 根；最近 60 根中峰前首根收盘 < MA250 且末根 > MA250，峰后收盘均 ≥ MA250，最低收盘在峰后 10–50 根，峰量/谷量 > 2，谷/峰收盘 < 0.8。", 250),
    PatternDef("platform_breakout", "突破平台", "breakout", "最近 60 根内某日 b：开盘 < MA60 ≤ 收盘，量比 ≥ 2；窗口内 b 之前每日 -0.05 < (MA60-收盘)/MA60 < 0.2。", 66),
    PatternDef("low_drawdown", "无大幅回撤", "trend", "近 60 根涨幅 ≥ 60%；其间无单日 < -7%、无收开 < -7%、无连续两日累计 < -10%（收盘或开到收）。", 61),
    PatternDef("turtle_breakout", "海龟突破", "breakout", "收盘 ≥ 最近 60 根（含当日）最高收盘。", 61),
    PatternDef("high_tight_flag", "高而窄旗形", "breakout", "24 根前至 10 根前窗口：最高收盘/最低价 ≥ 1.9 且出现连续两日涨停。", 25, "原规则另需龙虎榜机构席位，省略"),
    PatternDef("low_atr_growth", "低波成长", "volatility", "ATR14/收盘 ≤ 2.5%，近 60 根涨幅 ≥ 15%，收盘 ≥ MA20。", 61),
    PatternDef("macd_bottom_cross", "MACD 零轴下金叉", "reversal", "DIF 上穿 DEA（前一根 DIF ≤ DEA），且 DIF 与 DEA 均 < 0。", 36),
    PatternDef("kdj_oversold", "KDJ 超卖金叉", "reversal", "K 上穿 D（前一根 K ≤ D），且 D < 25 或前一根 J < 0。", 11),
)
RULES: dict[str, Callable] = {
    "ma_bull": _ma_bull, "volume_surge": _volume_surge, "climax_limitdown": _climax_limitdown,
    "parking_apron": _parking_apron, "yearline_pullback": _yearline_pullback, "platform_breakout": _platform_breakout,
    "low_drawdown": _low_drawdown, "turtle_breakout": _turtle_breakout, "high_tight_flag": _high_tight_flag,
    "low_atr_growth": _low_atr_growth, "macd_bottom_cross": _macd_bottom_cross, "kdj_oversold": _kdj_oversold,
}
BY_ID = {d.id: d for d in DEFINITIONS}


def evaluate(view: InstrumentView, pattern_id: str, i: int | None = None):
    """PatternHit | None (rule says no) | INSUFFICIENT (history too short or an input missing: unknown, not no)."""
    d = BY_ID[pattern_id]
    i = view.n - 1 if i is None else i
    if i + 1 < d.min_bars:
        return INSUFFICIENT
    return RULES[pattern_id](view, i)
