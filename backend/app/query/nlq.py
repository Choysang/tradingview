"""Deterministic natural-language screener (iWencai style, no model involved).

Text is consumed rule by rule: every rule finds its pattern, writes the matching
`ScreenerFilterRequest` fields, and masks the matched span so later, looser rules
cannot read it twice ("昨日换手" must not also become "换手"). Whatever is left
after all rules is reported as `unmatched`; nothing is dropped silently.

Rule order therefore matters: specific phrases first, generic metrics last.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math
import re
from typing import Callable
import unicodedata

from pydantic import ValidationError

from contracts.schema import (
    BoardType,
    MacroPoolType,
    ParsedToken,
    ParseResponse,
    ScreenerFilterRequest,
    TurnoverRegime,
)

MASK = "\x00"
MAX_STREAK = 20
MAX_RULE_HITS = 20
SMALL_CAP_MAX = 80.0
MID_CAP_RANGE = (80.0, 300.0)
LARGE_CAP_MIN = 500.0
HIGH_STREAK = 5
DIRECTION_EPSILON = 0.01
EMPTY_SUMMARY = "未识别到筛选条件"

_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN = r"[一二两三四五六七八九十]{1,3}"
NUM = r"\d+(?:\.\d+)?"
SNUM = r"-?\d+(?:\.\d+)?"
INT = rf"(?:\d+|{CN})"
GT = "大于等于|高于等于|不低于|不小于|至少|大于|高于|超过|多于|>=|≥|>"
LT = "小于等于|低于等于|不超过|不高于|不大于|小于|低于|少于|<=|≤|<"
RANGE_SEP = "-|~|到|至|—|–"
SUFFIX_UP = {"以上", "及以上", "之上"}
STOP_WORDS = re.compile("的|股票|个股|筛选|选出|帮我|找出|请|找|查|排序|排名|股|票")
SEPARATORS = re.compile(r"[,;、，；]|并且|而且|以及|同时|且|和")


def _cn_int(text: str) -> int | None:
    """Chinese numerals up to 99: 三, 十, 十五, 二十, 二十五."""
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN.get(left, 0) if left else 1
        ones = _CN.get(right, 0) if right else 0
        if (left and not tens) or (right and not ones):
            return None
        return tens * 10 + ones
    return _CN.get(text) if len(text) == 1 else None


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        value = _cn_int(text)
        return float(value) if value is not None else None


def _fmt(value: float) -> str:
    return f"{value:g}"


# --- parse context ------------------------------------------------------------


@dataclass
class _Ctx:
    raw: str
    low: str
    state: dict
    found: list[tuple[int, ParsedToken]] = field(default_factory=list)
    dropped: list[tuple[int, str]] = field(default_factory=list)
    boards: list[BoardType] = field(default_factory=list)
    themes: list[tuple[int, str]] = field(default_factory=list)
    pool_explicit: bool = False

    def _mask(self, start: int, end: int) -> str:
        shown = self.raw[start:end].strip()
        blank = MASK * (end - start)
        self.raw = self.raw[:start] + blank + self.raw[end:]
        self.low = self.low[:start] + blank + self.low[end:]
        return shown

    def hit(self, start: int, end: int, field_name: str, label: str) -> bool:
        shown = self._mask(start, end)
        self.found.append((start, ParsedToken(text=shown, field=field_name, label=label)))
        return True

    def reject(self, start: int, end: int, note: str) -> bool:
        shown = self._mask(start, end)
        self.dropped.append((start, f"{shown}（{note}）"))
        return True

    def pool(self, pool: MacroPoolType, explicit: bool) -> bool:
        """Pool is single-valued. Explicit keywords beat implied ones; two
        different explicit keywords conflict and the later one is reported."""
        current = self.state["pool_type"]
        if current is MacroPoolType.ALL or (explicit and not self.pool_explicit):
            self.state["pool_type"] = pool
            self.pool_explicit = explicit
            return True
        if current is pool:
            self.pool_explicit = self.pool_explicit or explicit
            return True
        return not explicit


Rule = Callable[[_Ctx], bool]


def _once(pattern: re.Pattern[str], apply: Callable[[_Ctx, re.Match[str]], bool]) -> Rule:
    def rule(ctx: _Ctx) -> bool:
        match = pattern.search(ctx.low)
        return apply(ctx, match) if match else False

    return rule


# --- numeric bounds ---------------------------------------------------------------


@dataclass(frozen=True)
class _Bounds:
    start: int
    end: int
    lo: float | None
    hi: float | None
    unit: str | None


@lru_cache(maxsize=None)
def _bound_patterns(metric: str, unit: str, signed: bool) -> tuple[re.Pattern[str], ...]:
    num = SNUM if signed else NUM
    tail = lambda name: rf"\s*(?P<{name}>{unit})?" if unit else ""  # noqa: E731
    lead = rf"(?:{metric})\s*(?:为|在|介于|是)?\s*"
    return (
        re.compile(rf"{lead}(?P<a>{num}){tail('ua')}\s*(?:{RANGE_SEP})\s*(?P<b>{num}){tail('ub')}"),
        re.compile(rf"{lead}(?:(?P<gt>{GT})|(?P<lt>{LT}))\s*(?P<n>{num}){tail('u')}"),
        re.compile(rf"{lead}(?P<n>{num}){tail('u')}\s*(?P<suf>以上|及以上|之上|以下|及以下|之下|以内)"),
    )


def _find_bounds(text: str, metric: str, unit: str, signed: bool) -> _Bounds | None:
    """Range ("10-30%"), comparator prefix ("大于10") or suffix ("10以上")."""
    range_p, compare_p, suffix_p = _bound_patterns(metric, unit, signed)
    if m := range_p.search(text):
        low, high = float(m["a"]), float(m["b"])
        return _Bounds(m.start(), m.end(), min(low, high), max(low, high), m["ub"] or m["ua"] if unit else None)
    if m := compare_p.search(text):
        value, up = float(m["n"]), m["gt"] is not None
        return _Bounds(m.start(), m.end(), value if up else None, None if up else value, m["u"] if unit else None)
    if m := suffix_p.search(text):
        value, up = float(m["n"]), m["suf"] in SUFFIX_UP
        return _Bounds(m.start(), m.end(), value if up else None, None if up else value, m["u"] if unit else None)
    return None


def _set_range(
    state: dict,
    lo_field: str | None,
    hi_field: str | None,
    lo: float | None,
    hi: float | None,
    clamp: tuple[float, float] | None,
) -> bool:
    """Write a bound pair unless it contradicts what the request already holds.

    The contract rejects inverted ranges, so a contradiction must be reported
    here rather than surface as a 422 from a parse call.
    """
    if clamp:
        lo = None if lo is None else min(max(lo, clamp[0]), clamp[1])
        hi = None if hi is None else min(max(hi, clamp[0]), clamp[1])
    new_lo = lo if lo is not None else (state[lo_field] if lo_field else None)
    new_hi = hi if hi is not None else (state[hi_field] if hi_field else None)
    if new_lo is not None and new_hi is not None and new_lo > new_hi:
        return False
    if lo is not None and lo_field:
        state[lo_field] = lo
    if hi is not None and hi_field:
        state[hi_field] = hi
    return True


@dataclass(frozen=True)
class _Metric:
    name: str
    metric: str
    lo_field: str | None
    hi_field: str | None
    unit: str = ""
    suffix: str = ""
    integer: bool = False
    wan: bool = False  # unit 万 is divided by 10000 to get 亿
    signed: bool = False
    clamp: tuple[float, float] | None = None


def _range_label(name: str, lo: float | None, hi: float | None, suffix: str) -> str:
    if lo is not None and hi is not None:
        return f"{name} {_fmt(lo)}–{_fmt(hi)}{suffix}"
    if lo is not None:
        return f"{name} ≥ {_fmt(lo)}{suffix}"
    return f"{name} ≤ {_fmt(hi or 0)}{suffix}"


def _metric_rule(spec: _Metric) -> Rule:
    fields = [name for name in (spec.lo_field, spec.hi_field) if name]

    def rule(ctx: _Ctx) -> bool:
        found = _find_bounds(ctx.low, spec.metric, spec.unit, spec.signed)
        if found is None:
            return False
        lo, hi = found.lo, found.hi
        if spec.wan and found.unit and found.unit.startswith("万"):
            lo = None if lo is None else lo / 10000
            hi = None if hi is None else hi / 10000
        if spec.integer:
            lo = None if lo is None else math.ceil(lo)
            hi = None if hi is None else math.floor(hi)
        # Fields that only have a lower bound in the contract cannot express "≤".
        if spec.hi_field is None:
            if lo is None:
                return ctx.reject(found.start, found.end, "该条件只支持下限")
            if hi is not None:
                ctx.dropped.append((found.start, f"{ctx.raw[found.start:found.end].strip()}（上限不支持，已忽略）"))
                hi = None
        # Valuation caps (PE / PB) only exist as upper bounds in the contract.
        if spec.lo_field is None:
            if hi is None:
                return ctx.reject(found.start, found.end, "该条件只支持上限")
            if lo is not None:
                ctx.dropped.append((found.start, f"{ctx.raw[found.start:found.end].strip()}（下限不支持，已忽略）"))
                lo = None
        if not _set_range(ctx.state, spec.lo_field, spec.hi_field, lo, hi, spec.clamp):
            return ctx.reject(found.start, found.end, "与其他条件矛盾或超出范围")
        applied = [name for name, value in ((spec.lo_field, lo), (spec.hi_field, hi)) if name and value is not None]
        label = _range_label(spec.name, lo, hi, spec.suffix)
        return ctx.hit(found.start, found.end, ",".join(applied or fields), label)

    return rule


_METRICS: tuple[_Metric, ...] = (
    _Metric("近一年涨停", r"(?:近一年|近1年|一年内|近一年内|年内)涨停(?:次数)?", "min_zt_count_1y", None, unit="次", suffix=" 次", integer=True),
    _Metric("封板率", r"封板(?:成功)?率", "min_seal_success_rate_1y", None, unit="%", suffix="%", clamp=(0, 100)),
    _Metric("昨日换手", r"昨日?换手率?", "turnover_yd_min", "turnover_yd_max", unit="%", suffix="%"),
    _Metric("昨换比", r"昨换比", "turnover_ratio_min", "turnover_ratio_max"),
    _Metric("换手", r"(?:今日|今天|今)?换手率?", "turnover_today_min", "turnover_today_max", unit="%", suffix="%"),
    _Metric("总市值", r"总市值", "total_cap_min", "total_cap_max", unit="亿元|万元|亿|万", suffix=" 亿", wan=True),
    _Metric("自由流通", r"自由流通市值|自由流通|流通市值|流通盘|市值", "free_cap_min", "free_cap_max", unit="亿元|万元|亿|万", suffix=" 亿", wan=True),
    _Metric("股价", r"股价|价格|现价|最新价", "price_min", "price_max", unit="元|块", suffix=" 元"),
    _Metric("累计涨幅", r"连涨累计涨幅|区间涨幅|累计涨幅", "min_streak_cum_return", None, unit="%", suffix="%", signed=True),
    _Metric("涨幅", r"涨跌幅|涨幅", "pct_chg_min", "pct_chg_max", unit="%", suffix="%", signed=True, clamp=(-100, 100)),
    # Nulls and loss-makers never satisfy these two caps (see the engine filter),
    # so the labels say so instead of implying a plain "≤".
    _Metric("市盈率(仅盈利)", r"市盈率(?:\s*\(?ttm\)?)?|(?<![a-z])pe(?:[-_ ]?ttm)?(?![a-z])", None, "pe_max", unit="倍", suffix=" 倍"),
    _Metric("市净率", r"市净率|(?<![a-z])pb(?![a-z])", None, "pb_max", unit="倍", suffix=" 倍"),
    _Metric("成交额", r"成交额|成交金额", "min_amount_today", None, unit="亿元|万元|亿|万", suffix=" 亿", wan=True),
    _Metric("封成比", r"封成比", "min_seal_to_amount_ratio", None, unit="%", suffix="%"),
)
_METRIC_BY_NAME = {spec.name: _metric_rule(spec) for spec in _METRICS}


# --- keyword rules ------------------------------------------------------------------


def _streak(
    ctx: _Ctx, start: int, end: int, lo: int | None, hi: int | None, label: str | None = None
) -> bool:
    low, high = lo or 1, hi or MAX_STREAK
    if low > high or low < 1 or low > MAX_STREAK:
        return ctx.reject(start, end, "连板数超出范围")
    ctx.state["limit_streak_exact"] = list(range(low, min(high, MAX_STREAK) + 1))
    ctx.pool(MacroPoolType.LIMIT_UP_LADDER, explicit=False)
    if label is None:
        if low == high:
            label = f"连板 = {low}"
        elif high >= MAX_STREAK:
            label = f"连板 ≥ {low}"
        elif low == 1:
            label = f"连板 ≤ {high}"
        else:
            label = f"连板 {low}–{high}"
    return ctx.hit(start, end, "limit_streak_exact", label)


def _first_board(ctx: _Ctx, m: re.Match[str]) -> bool:
    return _streak(ctx, m.start(), m.end(), 1, 1, "首板 (连板 = 1)")


def _high_board(ctx: _Ctx, m: re.Match[str]) -> bool:
    return _streak(ctx, m.start(), m.end(), HIGH_STREAK, None, f"高标 (连板 ≥ {HIGH_STREAK})")


_STREAK_METRIC, _STREAK_UNIT = r"连板数?|板数", "板|个"


def _streak_bounds(ctx: _Ctx) -> bool:
    found = _find_bounds(ctx.low, _STREAK_METRIC, _STREAK_UNIT, False)
    if found is None:
        return False
    lo = None if found.lo is None else math.ceil(found.lo)
    hi = None if found.hi is None else math.floor(found.hi)
    return _streak(ctx, found.start, found.end, lo, hi)


_STREAK_FORWARD = re.compile(rf"(?<![新\d.])(?P<n>\d+|{CN})\s*(?:连板|板)\s*(?P<q>以上|及以上|以下|及以下|以内)?")


def _streak_forward(ctx: _Ctx, m: re.Match[str]) -> bool:
    value = _number(m["n"])
    if value is None:
        return ctx.reject(m.start(), m.end(), "无法识别连板数")
    n, qualifier = int(value), m["q"]
    if qualifier in SUFFIX_UP:
        return _streak(ctx, m.start(), m.end(), n, None)
    if qualifier:
        return _streak(ctx, m.start(), m.end(), None, n)
    return _streak(ctx, m.start(), m.end(), n, n)


def _bare_streak(ctx: _Ctx, m: re.Match[str]) -> bool:
    return _streak(ctx, m.start(), m.end(), 2, None)


def _pool_rule(pool: MacroPoolType, label: Callable[[re.Match[str]], str]) -> Callable[[_Ctx, re.Match[str]], bool]:
    def apply(ctx: _Ctx, m: re.Match[str]) -> bool:
        if not ctx.pool(pool, explicit=True):
            return ctx.reject(m.start(), m.end(), "与已选股票池冲突")
        return ctx.hit(m.start(), m.end(), "pool_type", label(m))

    return apply


def _n_days_label(m: re.Match[str]) -> str:
    return f"{m['n']}天{m['m']}板池" if m["n"] else "反包池"


def _growth(ctx: _Ctx, start: int, end: int, days: int | None) -> bool:
    value = 3 if days is None else days
    if not 1 <= value <= 100:
        return ctx.reject(start, end, "连阳天数超出范围")
    ctx.state["min_growth_streak"] = value
    ctx.pool(MacroPoolType.CONTINUOUS_GROWTH, explicit=False)
    return ctx.hit(start, end, "min_growth_streak", f"连阳 ≥ {value}")


_GROWTH_METRIC = ("连阳(?:天数)?", "天|日")
_GROWTH_FORWARD = re.compile(rf"(?P<n>\d+|{CN})\s*连[阳涨]\s*(?:天|日)?\s*(?:以上|及以上)?")
_GROWTH_BARE = re.compile(r"连阳|连涨(?!停)|连续上涨")


def _growth_bounds(ctx: _Ctx) -> bool:
    found = _find_bounds(ctx.low, _GROWTH_METRIC[0], _GROWTH_METRIC[1], False)
    if found is None:
        return False
    if found.lo is None:
        return ctx.reject(found.start, found.end, "连阳只支持下限")
    return _growth(ctx, found.start, found.end, math.ceil(found.lo))


def _growth_forward(ctx: _Ctx, m: re.Match[str]) -> bool:
    value = _number(m["n"])
    if value is None:
        return ctx.reject(m.start(), m.end(), "无法识别连阳天数")
    return _growth(ctx, m.start(), m.end(), int(value))


def _doji(ctx: _Ctx, m: re.Match[str]) -> bool:
    ctx.state["allow_doji_tolerance"] = True
    return ctx.hit(m.start(), m.end(), "allow_doji_tolerance", "允许假阴容错")


_REGIME = {
    "缩": (TurnoverRegime.SHRINK_ACCEL, "缩量 (昨换比 < 0.75)"),
    "平": (TurnoverRegime.NORMAL_HAND, "平量 (昨换比 0.75–1.35)"),
    "放": (TurnoverRegime.EXPAND_DIV, "放量 (昨换比 > 1.35)"),
}


def _regime(ctx: _Ctx, m: re.Match[str]) -> bool:
    regime, label = _REGIME[m["k"]]
    ctx.state["turnover_regime"] = regime
    return ctx.hit(m.start(), m.end(), "turnover_regime", label)


def _cap_class(ctx: _Ctx, m: re.Match[str]) -> bool:
    lo, hi = {
        "小盘": (None, SMALL_CAP_MAX),
        "中盘": MID_CAP_RANGE,
        "大盘": (LARGE_CAP_MIN, None),
    }[m["k"]]
    if not _set_range(ctx.state, "free_cap_min", "free_cap_max", lo, hi, None):
        return ctx.reject(m.start(), m.end(), "与其他条件矛盾或超出范围")
    label = {"小盘": f"小盘 (自由流通 ≤ {SMALL_CAP_MAX:g} 亿)", "中盘": "中盘 (自由流通 80–300 亿)", "大盘": f"大盘 (自由流通 ≥ {LARGE_CAP_MIN:g} 亿)"}[m["k"]]
    fields = ",".join(name for name, value in (("free_cap_min", lo), ("free_cap_max", hi)) if value is not None)
    return ctx.hit(m.start(), m.end(), fields, label)


def _direction(ctx: _Ctx, m: re.Match[str]) -> bool:
    rising = m["k"] == "上涨"
    # The filter bounds are inclusive; one display step (0.01%) keeps flat names out.
    edge = DIRECTION_EPSILON if rising else -DIRECTION_EPSILON
    if not _set_range(ctx.state, "pct_chg_min", "pct_chg_max", edge if rising else None, None if rising else edge, None):
        return ctx.reject(m.start(), m.end(), "与其他条件矛盾或超出范围")
    label = "上涨 (涨幅 > 0)" if rising else "下跌 (涨幅 < 0)"
    return ctx.hit(m.start(), m.end(), "pct_chg_min" if rising else "pct_chg_max", label)


LOW_VALUATION_PE = 20.0
LOW_VALUATION_PB = 2.0


def _low_valuation(ctx: _Ctx, m: re.Match[str]) -> bool:
    """Shorthand for PE <= 20 and PB <= 2; never loosens an explicit tighter cap."""
    for name, cap in (("pe_max", LOW_VALUATION_PE), ("pb_max", LOW_VALUATION_PB)):
        current = ctx.state[name]
        ctx.state[name] = cap if current is None else min(current, cap)
    return ctx.hit(
        m.start(), m.end(), "pe_max,pb_max",
        f"低估值 (市盈率 ≤ {LOW_VALUATION_PE:g} 且市净率 ≤ {LOW_VALUATION_PB:g}，仅统计盈利)",
    )


_DROP_METRIC = r"(?<!涨)跌幅"


def _drop(ctx: _Ctx) -> bool:
    """"跌幅大于3%" means the stock fell by more than 3%: pct_chg <= -3.

    "跌幅小于2%" means it is falling, but by less than 2%: -2 <= pct_chg < 0
    (a flat or rising stock has no 跌幅 to compare).
    """
    found = _find_bounds(ctx.low, _DROP_METRIC, "%", False)
    if found is None:
        return False
    lo, hi = found.lo, found.hi  # magnitudes of the drop
    chg_min = None if hi is None else -hi
    chg_max = -lo if lo is not None else -DIRECTION_EPSILON
    if not _set_range(ctx.state, "pct_chg_min", "pct_chg_max", chg_min, chg_max, (-100, 100)):
        return ctx.reject(found.start, found.end, "与其他条件矛盾或超出范围")
    if lo is not None and hi is not None:
        label = f"跌幅 {_fmt(lo)}–{_fmt(hi)}%"
    elif lo is not None:
        label = f"跌幅 ≥ {_fmt(lo)}% (涨幅 ≤ -{_fmt(lo)}%)"
    else:
        label = f"跌幅 ≤ {_fmt(hi or 0)}% (涨幅 -{_fmt(hi or 0)}% ~ 0)"
    applied = ",".join(name for name, value in (("pct_chg_min", chg_min), ("pct_chg_max", chg_max)) if value is not None)
    return ctx.hit(found.start, found.end, applied, label)


# Seal-time and open-board filters only constrain stocks that have a seal time /
# open-board count; without a limit-up pool they would let every unsealed name through.
def _seal_time(ctx: _Ctx, m: re.Match[str], clock: str, label: str) -> bool:
    current = ctx.state["max_first_seal_time"]
    ctx.state["max_first_seal_time"] = clock if current is None else min(current, clock)
    ctx.pool(MacroPoolType.LIMIT_UP_LADDER, explicit=False)
    return ctx.hit(m.start(), m.end(), "max_first_seal_time", f"{label} (首封 ≤ {clock[:5]})")


_FIXED_SEAL = (
    (re.compile(r"一字板|一字涨停|一字"), "09:25:00", "一字板"),
    (re.compile(r"秒板|秒封"), "09:31:00", "秒板"),
    (re.compile(r"早盘(?:封板|封停|涨停|板)(?!率)"), "10:00:00", "早盘封板"),
)
_CLOCK = re.compile(
    rf"(?P<h>\d{{1,2}}|{CN})\s*[:点时]\s*(?:(?P<m>\d{{1,2}})\s*分?|(?P<half>半))?\s*(?:之?前|以前)\s*(?:(?:封板|封停|封死|涨停|首封)(?!率))?"
)


def _fixed_seal(pattern: re.Pattern[str], clock: str, label: str) -> Rule:
    return _once(pattern, lambda ctx, m: _seal_time(ctx, m, clock, label))


def _clock(ctx: _Ctx, m: re.Match[str]) -> bool:
    hour = _number(m["h"])
    minute = 30 if m["half"] else int(m["m"] or 0)
    if hour is None or not 9 <= hour <= 15 or minute > 59:
        return ctx.reject(m.start(), m.end(), "时间不在交易时段")
    return _seal_time(ctx, m, f"{int(hour):02d}:{minute:02d}:00", "封板时间")


def _hard_board(ctx: _Ctx, m: re.Match[str]) -> bool:
    current = ctx.state["max_open_board_count"]
    ctx.state["max_open_board_count"] = 0 if current is None else min(current, 0)
    ctx.pool(MacroPoolType.LIMIT_UP_LADDER, explicit=False)
    return ctx.hit(m.start(), m.end(), "max_open_board_count", "不炸板 (开板 0 次)")


_BOARD_WORDS = (
    (re.compile(r"沪深主板|主板|10cm"), BoardType.MAIN_10CM),
    (re.compile(r"创业板|科创板|双创|20cm"), BoardType.CHINEXT_20CM),
    (re.compile(r"北交所|北交|30cm"), BoardType.BSE_30CM),
)


def _board_rule(pattern: re.Pattern[str], board: BoardType) -> Rule:
    def apply(ctx: _Ctx, m: re.Match[str]) -> bool:
        if board not in ctx.boards:
            ctx.boards.append(board)
        return ctx.hit(m.start(), m.end(), "boards", f"制式 {board.value}")

    return _once(pattern, apply)


def _flag(field_name: str, value: bool, label: str) -> Callable[[_Ctx, re.Match[str]], bool]:
    def apply(ctx: _Ctx, m: re.Match[str]) -> bool:
        ctx.state[field_name] = value
        return ctx.hit(m.start(), m.end(), field_name, label)

    return apply


_SORT_KEYS = {
    "综合评分": "composite_score", "综合得分": "composite_score", "综合分": "composite_score", "得分": "composite_score",
    "涨跌幅": "pct_chg", "涨幅": "pct_chg",
    "成交额": "amount_today",
    "换手率": "turnover_today", "换手": "turnover_today",
    "连板数": "limit_streak", "连板": "limit_streak",
    "连阳": "growth_streak",
    "自由流通市值": "free_float_mkt_cap", "流通市值": "free_float_mkt_cap", "市值": "free_float_mkt_cap",
    "股价": "price", "价格": "price",
    "封成比": "seal_to_amount_ratio",
    "昨换比": "turnover_ratio_yd",
}
_SORT_ALT = "|".join(sorted(_SORT_KEYS, key=len, reverse=True))
_SORT = re.compile(
    rf"(?:按\s*(?P<k>{_SORT_ALT})|(?P<k2>{_SORT_ALT})(?=排序|排名|排列))"
    r"\s*(?P<d>从低到高|由低到高|升序|正序|从高到低|由高到低|降序|倒序)?\s*(?:排序|排名|排列)?"
)


def _sort(ctx: _Ctx, m: re.Match[str]) -> bool:
    key = m["k"] or m["k2"]
    ascending = (m["d"] or "") in {"从低到高", "由低到高", "升序", "正序"}
    ctx.state["sort_by"] = _SORT_KEYS[key]
    ctx.state["sort_desc"] = not ascending
    return ctx.hit(m.start(), m.end(), "sort_by", f"按{key}{'升序' if ascending else '降序'}")


_RULES: tuple[Rule, ...] = (
    _once(_SORT, _sort),
    # time of seal / hard board before anything that reads "板"
    *(_fixed_seal(pattern, clock, label) for pattern, clock, label in _FIXED_SEAL),
    _once(_CLOCK, _clock),
    _once(re.compile(r"硬板|[不未没]炸板|[不未没]开板"), _hard_board),
    _once(re.compile(r"(?P<n>\d+|" + CN + r")\s*天\s*(?P<m>\d+|" + CN + r")\s*板|反包"), _pool_rule(MacroPoolType.N_DAYS_M_BOARDS, _n_days_label)),
    _METRIC_BY_NAME["近一年涨停"],
    _METRIC_BY_NAME["封板率"],
    _once(re.compile(r"炸板|冲板|烂板|冲击涨停|冲刺涨停|冲刺"), _pool_rule(MacroPoolType.BROKEN_OR_ATTACK, lambda m: "炸板/冲板池")),
    _once(re.compile(r"首板"), _first_board),
    _once(re.compile(r"高标"), _high_board),
    _streak_bounds,
    _once(_STREAK_FORWARD, _streak_forward),
    _once(re.compile(r"连续涨停|连板"), _bare_streak),
    _once(re.compile(r"涨停(?:板|股|池)?"), _pool_rule(MacroPoolType.LIMIT_UP_LADDER, lambda m: "涨停池")),
    _METRIC_BY_NAME["累计涨幅"],
    _growth_bounds,
    _once(_GROWTH_FORWARD, _growth_forward),
    _once(_GROWTH_BARE, lambda ctx, m: _growth(ctx, m.start(), m.end(), None)),
    _once(re.compile(r"假阴十字星|假阴|十字星|容错"), _doji),
    _METRIC_BY_NAME["昨日换手"],
    _METRIC_BY_NAME["昨换比"],
    _METRIC_BY_NAME["换手"],
    _once(re.compile(r"(?P<k>[缩平放])量(?:加速|良性|分歧)?"), _regime),
    _METRIC_BY_NAME["总市值"],
    _METRIC_BY_NAME["自由流通"],
    _once(re.compile(r"(?P<k>小盘|中盘|大盘)"), _cap_class),
    _METRIC_BY_NAME["股价"],
    _METRIC_BY_NAME["涨幅"],
    _drop,
    _once(re.compile(r"(?P<k>上涨|下跌)"), _direction),
    _METRIC_BY_NAME["市盈率(仅盈利)"],
    _METRIC_BY_NAME["市净率"],
    _once(re.compile(r"低估值|低估"), _low_valuation),
    _METRIC_BY_NAME["成交额"],
    _METRIC_BY_NAME["封成比"],
    *(_board_rule(pattern, board) for pattern, board in _BOARD_WORDS),
    _once(re.compile(r"(?:不含|剔除|排除|非|去掉|不要|不带|不包含)\s*\*?st"), _flag("exclude_st", True, "剔除 ST")),
    _once(re.compile(r"(?:包含|包括|含|带)\s*\*?st"), _flag("exclude_st", False, "含 ST")),
    _once(re.compile(r"(?:不含|剔除|排除|不要|去掉)次新(?:股)?"), _flag("exclude_new", True, "剔除次新")),
    _once(re.compile(r"次新(?:股)?"), _flag("exclude_new", False, "含次新")),
    _once(re.compile(r"(?:接近|逼近|临近|突破|创)?(?:60日|六十日)?新高(?!度)"), _flag("only_near_60d_high", True, "接近 60 日新高")),
)


def _extract_themes(ctx: _Ctx, themes: list[str]) -> None:
    """Known theme names, longest first so "AI算力" is not eaten by "算力"."""
    for name in sorted({t for t in themes if len(t) >= 2}, key=len, reverse=True):
        match = re.search(r"#?" + re.escape(name.lower()), ctx.low)
        if match:
            ctx.themes.append((match.start(), name))
            ctx.hit(match.start(), match.end(), "selected_themes", f"题材 {name}")


def parse(text: str, themes: list[str]) -> ParseResponse:
    normal = unicodedata.normalize("NFKC", text)
    lowered = normal.lower()
    if len(lowered) != len(normal):
        normal = lowered
    ctx = _Ctx(raw=normal, low=lowered, state=ScreenerFilterRequest().model_dump())
    _extract_themes(ctx, themes)
    # Separators become blanks of equal length so spans keep pointing at the input.
    blank = lambda m: " " * len(m.group())  # noqa: E731
    ctx.raw, ctx.low = SEPARATORS.sub(blank, ctx.raw), SEPARATORS.sub(blank, ctx.low)
    for rule in _RULES:
        for _ in range(MAX_RULE_HITS):
            if not rule(ctx):
                break
    for match in re.finditer(rf"[^\s{MASK}]+", ctx.raw):
        residue = STOP_WORDS.sub("", match.group())
        if residue:
            ctx.dropped.append((match.start(), residue))
    if ctx.themes:
        ctx.state["selected_themes"] = [name for _, name in sorted(ctx.themes)]
    if ctx.boards:
        ctx.state["boards"] = [board for board in BoardType if board in ctx.boards]
    try:
        filters = ScreenerFilterRequest.model_validate(ctx.state)
    except ValidationError as error:  # a rule bypassed the range checks: say so, apply nothing
        return ParseResponse(
            filters=ScreenerFilterRequest(),
            tokens=[],
            unmatched=[text.strip(), f"解析结果未通过校验：{error.errors()[0]['msg']}"],
            summary=EMPTY_SUMMARY,
        )
    tokens = [token for _, token in sorted(ctx.found, key=lambda item: item[0])]
    unmatched = [note for _, note in sorted(ctx.dropped, key=lambda item: item[0])]
    return ParseResponse(
        filters=filters,
        tokens=tokens,
        unmatched=unmatched,
        summary=" · ".join(token.label for token in tokens) or EMPTY_SUMMARY,
    )
