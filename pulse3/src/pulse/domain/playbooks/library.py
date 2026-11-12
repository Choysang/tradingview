"""The 52-playbook library as strategy-definition candidates with pure signal functions over shared features.

Why: LEGACY inventory §5 - every old playbook family (limit relay 5, trend/breakout 7, academic factors 5, oversold 5,
sentiment cycle 9, crowd 5, factor composite 2, technical analysis 14) comes back as a definition with a pure signal
function. The old library files (backend/app/playbooks/library*.py) were read as historical evidence of the RULE
SEMANTICS (thresholds are stated in their own rule text); no code was copied, and NONE of their backtest numbers are carried
over. Every playbook starts in lifecycle ``specified`` with evidence label ``未检验`` and earns status only through the
unified experiment path (EvaluationContext + ExecutionKernel replay, user cost profile, x2/x3 stress, benchmark).
Invariants: a signal reads only indexes <= i of its view (see tests: truncation equals prefix); exits are CLOSE-based and
execute at the next open; ``requires`` names data pulse3 may not have - those playbooks have a signal that returns nothing
and an experiment on them is recorded as a failed trial ``data_not_available:<name>``. Differences from the old rules are
listed in each ``rule`` text (turnover/amount proxies, no ST screen, daily-close exits instead of intraday stops).
Not covered: ST exclusion (no reliable flag), turnover rate, sector/seat/name data, intraday fills.
"""
from __future__ import annotations

import math

from .panel import pearson, period_lines
from .spec import ExitSpec, Param, Playbook, XSpec
from .view import InstrumentView, ok

COMMUNITY = "社区经验（未经学术验证）"
REGISTRY: dict[str, Playbook] = {}
ORDER: list[str] = []
MIN_CROSS = 20


def P(key: str, label: str, value: float, low: float, high: float, step: float) -> Param:
    return Param(key, label, value, low, high, step)


def add(pb: Playbook) -> Playbook:
    if pb.id in REGISTRY:
        raise ValueError(f"duplicate playbook id {pb.id}")
    REGISTRY[pb.id] = pb
    ORDER.append(pb.id)
    return pb


def liquid(v: InstrumentView, i: int, cny: float = 1e8) -> bool:
    m = v.turnover_ma(20)[i]
    return m is not None and m >= cny


def tradable(v: InstrumentView, i: int, p: dict[str, float]) -> bool:
    return i >= 119 and v.close[i] is not None and v.close[i] >= p["min_price"] and liquid(v, i, p["min_amount"] * 1e8)


def cap_at(v: InstrumentView, i: int) -> float | None:
    return None if v.float_cap is None else v.float_cap[i]


def top_rank(xs, i: int, share: float, highest: bool) -> tuple[int, int] | None:
    """(rank within the chosen side, n) when the name is in the top ``share`` % (highest) or bottom (lowest)."""
    cell = xs[i]
    if cell is None:
        return None
    rank, n = cell
    take = math.ceil(n * share / 100.0)
    side = (n - rank + 1) if highest else rank
    return (side, n) if side <= take else None


def decile_score(v: InstrumentView, col: str, share: float, highest: bool, period_flag: tuple) -> list:
    xs = v.xs.get(col)
    if xs is None:
        return [None] * v.n
    out = []
    for i in range(v.n):
        hit = period_flag[i] and top_rank(xs, i, share, highest)
        out.append(None if not hit else 1.0 - (hit[0] - 1) / hit[1])
    return out


def _common(offer: float) -> tuple[Param, ...]:
    return (P("min_amount", "最低20日均成交额估算(亿)", 0.5, 0.1, 5, 0.1), P("min_price", "最低股价(元)", 2, 0, 10, 0.5),
            P("offer", "每日候选数", offer, 5, 100, 1))


NO_ST = "未剔除 ST（无可靠来源）；成交额以 收盘×成交量 估算（来源无成交额）；价格为前复权研究价。"

# ============================ 1. limit relay (5) =================================================================


def _first_board(v, p):
    ls, ow, vr, h60 = v.limit_streak(), v.one_word(), v.volume_ratio20(), v.prev_hhv("high", 60)
    return v.scan(lambda i: ls[i] == 1 and not ow[i] and ok(vr[i], h60[i]) and p["vol_min"] <= vr[i] <= p["vol_max"]
                  and v.close[i] >= h60[i] and vr[i])


add(Playbook("first_board_relay", "首板接力", "limit_relay", "sentiment", "short",
             "首次涨停（连板数=1）、非一字板；量比 ∈ [1.5, 6]；收盘 ≥ 前 60 日最高价。次日开盘买入，持有 3 日，收盘跌 7% 次日止损。" + NO_ST,
             COMMUNITY, (P("vol_min", "量比下限", 1.5, 0.5, 4, 0.1), P("vol_max", "量比上限", 6, 3, 15, 0.5)),
             ExitSpec(3, 7), 5, 2, _first_board))


def _two_to_three(v, p):
    ls, ow, t = v.limit_streak(), v.one_word(), v.turnover_cny()
    return v.scan(lambda i: ls[i] == int(p["streak"]) and not ow[i] and ok(t[i]) and t[i] / 1e8)


add(Playbook("two_to_three", "二进三", "limit_relay", "sentiment", "short",
             "连板数=2（可调）且非一字板；得分=估算成交额。次日开盘买入，持有 2 日，止损 8%。", COMMUNITY,
             (P("streak", "连板数", 2, 2, 4, 1),), ExitSpec(2, 8), 4, 2, _two_to_three))


def _broken_reflex(v, p):
    ls, lu, pc = v.limit_streak(), v.limit_up(), v.pct()
    return v.scan(lambda i: i >= 2 and ok(ls[i - 2], lu[i - 1], pc[i], v.close[i], v.high[i - 1]) and ls[i - 2] >= p["min_streak"]
                  and not lu[i - 1] and v.close[i] > v.high[i - 1] and pc[i] > p["min_pct"] and pc[i])


add(Playbook("broken_board_reflex", "断板反包", "limit_relay", "sentiment", "short",
             "前天连板数≥2 且昨日未涨停；今日收盘>昨日最高价且涨幅>3%。次日开盘买入，持有 3 日，止损 8%。", COMMUNITY,
             (P("min_streak", "断板前连板数", 2, 2, 5, 1), P("min_pct", "反包涨幅下限(%)", 3, 1, 9, 0.5)),
             ExitSpec(3, 8), 4, 2, _broken_reflex))


def _limit_pullback(v, p):
    lu, ma10, vol = v.limit_up(), v.ma(10), v.volume

    def fn(i):
        best = 0.0
        for k in range(3, 9):
            j = i - k
            if j < 0 or not (lu[j] and ok(v.low[j], vol[j], vol[i], ma10[i], v.close[i])):
                continue
            if v.low[j] <= v.close[i] <= v.low[j] * (1 + p["tolerance"] / 100) and vol[i] < p["max_volume"] * vol[j] and v.close[i] >= ma10[i]:
                best = max(best, vol[j] / vol[i])
        return best
    return v.scan(fn)


add(Playbook("limit_pullback", "涨停回踩", "limit_relay", "sentiment", "short",
             "3–8 日前有涨停；今日收盘∈[该日最低价, ×1.03]；成交量<0.6×涨停日；收盘≥MA10。次日开盘买入，持有 5 日，止损 6%。", COMMUNITY,
             (P("tolerance", "距最低价容忍(%)", 3, 1, 8, 0.5), P("max_volume", "缩量比上限", 0.6, 0.3, 1, 0.05)),
             ExitSpec(5, 6), 5, 2, _limit_pullback))


def _dragon_return(v, p):
    ls, pk, vma5, pc = v.limit_streak(), v.prev_hhv("high", 20), v.vol_ma(5), v.pct()

    def fn(i):
        if i < 21 or not ok(pk[i], vma5[i - 1], pc[i], v.volume[i], v.ma(5)[i], v.close[i - 1]):
            return None
        peak_streak = max((s for s in ls[i - 20:i] if s is not None), default=0)
        return (peak_streak >= p["min_streak"] and v.close[i - 1] <= pk[i] * (1 - p["pullback"] / 100) and pc[i] > 0
                and v.volume[i] > vma5[i - 1] and v.close[i] > v.ma(5)[i] and v.volume[i] / vma5[i - 1])
    return v.scan(fn)


add(Playbook("dragon_return", "龙回头", "limit_relay", "sentiment", "short",
             "前 20 日内最高连板数≥4；昨日收盘较期间最高价回撤≥20%；今日上涨、量>前 5 日均量、收盘>MA5。次日开盘买入，持有 5 日，止损 8%。", COMMUNITY,
             (P("min_streak", "峰值连板数", 4, 3, 7, 1), P("pullback", "回撤下限(%)", 20, 10, 40, 1)),
             ExitSpec(5, 8), 3, 1, _dragon_return))

# ============================ 2. trend and breakout (7) ============================================================


def _turtle(v, p):
    h = v.prev_hhv("high", 55)
    return v.scan(lambda i: ok(h[i], v.close[i]) and v.close[i] > h[i] * (1 + p["min_margin"] / 100) and (v.close[i] / h[i] - 1) * 100)


add(Playbook("turtle_55", "海龟突破", "trend_breakout", "trend", "swing",
             "收盘>前 55 日最高价。次日开盘买入，最长持有 60 日，止损 10%，追踪止损 10%（收盘判定）。", "Richard Dennis 海龟交易法则（Faith, 2007），A 股适用性未验证",
             (P("min_margin", "最小突破幅度(%)", 0, 0, 5, 0.5),), ExitSpec(60, 10, None, 10), 8, 2, _turtle))


def _platform(v, p):
    hi, lo, vr = v.prev_hhv("high", 20), v.prev_llv("low", 20), v.volume_ratio20()

    def fn(i):
        if not ok(hi[i], lo[i], vr[i], v.close[i]) or lo[i] <= 0:
            return None
        return (hi[i] - lo[i]) / lo[i] * 100 < p["max_range"] and v.close[i] > hi[i] and vr[i] > p["vol_mult"] and vr[i]
    return v.scan(fn)


add(Playbook("platform_breakout", "平台突破", "trend_breakout", "breakout", "swing",
             "前 20 日振幅<15%；收盘>前 20 日最高价；量>2×前 20 日均量。次日开盘买入，持有 15 日，止损 8%，追踪 10%。", "Sequoia/InStock 开源选股器思路（社区经验）",
             (P("max_range", "平台振幅上限(%)", 15, 5, 30, 1), P("vol_mult", "放量倍数", 2, 1.2, 5, 0.1)),
             ExitSpec(15, 8, None, 10), 6, 2, _platform))


def _ma_bull_pullback(v, p):
    m10, m20, m60, pc = v.ma(10), v.ma(20), v.ma(60), v.pct()
    return v.scan(lambda i: i >= 10 and ok(m10[i], m20[i], m60[i], m60[i - 10], pc[i], v.low[i], v.close[i]) and m10[i] > m20[i] > m60[i]
                  and m60[i] > m60[i - 10] and v.low[i] <= m20[i] * (1 + p["touch"] / 100) and v.close[i] >= m20[i] and pc[i] > 0
                  and (m20[i] / m60[i] - 1) * 100)


add(Playbook("ma_bull_pullback", "均线多头回踩", "trend_breakout", "trend", "swing",
             "MA10>MA20>MA60 且 MA60 较 10 日前上升；最低价≤MA20×1.015；收盘≥MA20 且收阳。次日开盘买入，持有 10 日，止损 6%，追踪 8%。", COMMUNITY,
             (P("touch", "触及 MA20 容忍(%)", 1.5, 0.5, 4, 0.5),), ExitSpec(10, 6, None, 8), 6, 2, _ma_bull_pullback))


def _golden_cross(v, p):
    m20, m60 = v.ma(20), v.ma(60)
    return v.scan(lambda i: i >= 1 and ok(m20[i], m60[i], m20[i - 1], m60[i - 1], v.close[i]) and m20[i] > m60[i]
                  and m20[i - 1] <= m60[i - 1] and v.close[i] > m60[i] and (m20[i] / m60[i] - 1) * 1000)


add(Playbook("golden_cross_20_60", "双均线金叉", "trend_breakout", "trend", "swing",
             "MA20 上穿 MA60 且收盘>MA60。次日开盘买入，持有 30 日，止损 8%，追踪 12%。", COMMUNITY, (),
             ExitSpec(30, 8, None, 12), 8, 2, _golden_cross))


def _macd_zero(v, p):
    m = v.macd()
    dif, dea, hist = m["macd"], m["signal"], m["hist"]
    return v.scan(lambda i: i >= 1 and ok(dif[i], dea[i], dif[i - 1], dea[i - 1], hist[i], v.close[i]) and dif[i] > 0 and dea[i] > 0
                  and dif[i] > dea[i] and dif[i - 1] <= dea[i - 1] and hist[i] / v.close[i] * 100)


add(Playbook("macd_above_zero_cross", "MACD 零轴上金叉", "trend_breakout", "trend", "swing",
             "DIF、DEA 均>0 时 DIF 上穿 DEA（12/26/9，v1 内核为 SMA 种子）。次日开盘买入，持有 15 日，止损 7%，追踪 10%。", COMMUNITY, (),
             ExitSpec(15, 7, None, 10), 8, 2, _macd_zero))


def _boll_squeeze(v, p):
    b = v.boll()
    up, mid, lo = b["upper"], b["mid"], b["lower"]
    width = [None if not ok(u, m, l_) or m <= 0 else (u - l_) / m for u, m, l_ in zip(up, mid, lo)]
    q = p["squeeze"] / 100.0

    def fn(i):
        if i < 121 or not ok(width[i - 1], up[i], v.close[i]):
            return None
        w = width[i - 120:i]
        if any(x is None for x in w):
            return None
        floor = sorted(w)[int(q * (len(w) - 1))]
        return width[i - 1] <= floor and v.close[i] > up[i] and (v.close[i] / up[i] - 1) * 100
    return v.scan(fn)


add(Playbook("boll_squeeze_breakout", "布林收口突破", "trend_breakout", "volatility", "swing",
             "昨日带宽处于最近 120 日分布最低 10%；今日收盘>布林上轨（20,2）。次日开盘买入，持有 15 日，止损 7%，追踪 10%。",
             "John Bollinger squeeze 概念（2001），阈值为社区经验", (P("squeeze", "带宽分位(%)", 10, 3, 25, 1),),
             ExitSpec(15, 7, None, 10), 6, 2, _boll_squeeze))


def _new_high_250(v, p):
    h = v.prev_hhv("high", 250)
    cd = int(p["cooldown"])

    def fn(i):
        if not ok(h[i], v.close[i]) or v.close[i] <= h[i]:
            return None
        if any(ok(h[k], v.close[k]) and v.close[k] > h[k] for k in range(max(0, i - cd), i)):
            return None
        return (v.close[i] / h[i] - 1) * 100
    return v.scan(fn)


add(Playbook("new_high_250", "一年新高", "trend_breakout", "momentum", "swing",
             "收盘>前 250 日最高价，且此前 5 日内无同样新高。次日开盘买入，持有 40 日，止损 10%，追踪 12%。",
             "George & Hwang (2004), The 52-Week High and Momentum Investing, Journal of Finance",
             (P("cooldown", "冷却期(交易日)", 5, 1, 20, 1),), ExitSpec(40, 10, None, 12), 8, 2, _new_high_250))

# ============================ 3. academic factors (5) ===============================================================


def _xs_signal(col: str, share_key: str, highest: bool, flag: str):
    def sig(v, p):
        period = v.first_of_month if flag == "m" else v.first_of_week
        return decile_score(v, col, p[share_key], highest, period or (False,) * v.n)
    return sig


MOM = XSpec("mom", {"m": lambda v, i: None if not ok(v.ret(250)[i], v.ret(20)[i]) else v.ret(250)[i] - v.ret(20)[i]},
            lambda v, i: liquid(v, i))
add(Playbook("momentum_12_1", "截面动量 12-1", "academic_factor", "momentum", "long",
             "每月第一个交易日；20 日均成交额估算≥1 亿；指标=ret_250−ret_20，取截面最高 10%。持有 20 日，无价格止损。" + NO_ST +
             "（研究序列仅约 1 年：250 日动量窗口多数日期无值。）",
             "Jegadeesh & Titman (1993), Journal of Finance",
             (P("top_pct", "入选比例(%)", 10, 5, 30, 1),), ExitSpec(20), 20, 20, _xs_signal("mom.m", "top_pct", True, "m"), xs=MOM))

LOWVOL = XSpec("lowvol", {"s": lambda v, i: v.ret_std(60)[i]}, lambda v, i: liquid(v, i))
add(Playbook("low_volatility", "低波动", "academic_factor", "volatility", "long",
             "每月第一个交易日；成交额估算≥1 亿；指标=近 60 日日收益标准差，取截面最低 10%。持有 20 日。" + NO_ST,
             "Ang, Hodrick, Xing & Zhang (2006), Journal of Finance", (P("top_pct", "入选比例(%)", 10, 5, 30, 1),),
             ExitSpec(20), 20, 20, _xs_signal("lowvol.s", "top_pct", False, "m"), xs=LOWVOL))

REV = XSpec("rev", {"r": lambda v, i: v.ret(5)[i]}, lambda v, i: liquid(v, i))
add(Playbook("short_reversal", "短期反转", "academic_factor", "reversal", "short",
             "每周第一个交易日；成交额估算≥1 亿；指标=ret_5，取截面最低 10%。持有 5 日。" + NO_ST,
             "Jegadeesh (1990), Journal of Finance", (P("top_pct", "入选比例(%)", 10, 5, 30, 1),),
             ExitSpec(5), 20, 20, _xs_signal("rev.r", "top_pct", False, "w"), xs=REV))

SMALL = XSpec("small", {"c": lambda v, i: cap_at(v, i)}, lambda v, i: i >= 249 and liquid(v, i, 0.5e8) and ok(cap_at(v, i)) and cap_at(v, i) > 0)
add(Playbook("small_cap", "小市值", "academic_factor", "value", "long",
             "每月第一个交易日；成交额估算≥0.5 亿；已有≥250 根日线；指标=当日流通市值（最新快照×当日收盘/最新收盘，股本固定近似），取截面最小 10%。持有 20 日。",
             "Banz (1981), Journal of Financial Economics", (P("top_pct", "入选比例(%)", 10, 5, 30, 1),),
             ExitSpec(20), 20, 20, _xs_signal("small.c", "top_pct", False, "m"), xs=SMALL, requires=("float_cap",)))

add(Playbook("value_pe_pb", "低估值 PE+PB", "academic_factor", "value", "long",
             "按 1/PE 与 1/PB 的截面排名之和选最便宜的 10%。需要历史时点基本面，pulse3 当前没有 PIT 财务数据，故无法检验。",
             "Fama & French (1992), Journal of Finance", (P("top_pct", "入选比例(%)", 10, 5, 30, 1),),
             ExitSpec(20), 20, 20, lambda v, p: [None] * v.n, requires=("fundamentals_pit",),
             unavailable_reason="没有按财报发布时点保存的 PE/PB（PIT 基本面）"))

# ============================ 4. oversold and oscillator (5) =========================================================


def _rsi_oversold(v, p):
    r = v.rsi(6)
    pc = v.pct()
    return v.scan(lambda i: i >= 1 and ok(r[i], r[i - 1], pc[i]) and r[i - 1] < p["threshold"] and r[i] > r[i - 1] and pc[i] > 0 and p["threshold"] - r[i - 1])


add(Playbook("rsi_oversold", "RSI 超卖反弹", "oversold", "reversal", "short",
             "昨日 RSI(6)<20；今日 RSI(6) 回升且收阳。次日开盘买入，持有 4 日，止损 6%。", "Wilder (1978)；阈值为社区经验",
             (P("threshold", "超卖阈值", 20, 10, 35, 1),), ExitSpec(4, 6), 6, 3, _rsi_oversold))


def _kdj_low(v, p):
    k = v.kdj()
    kk, dd = k["k"], k["d"]
    return v.scan(lambda i: i >= 1 and ok(kk[i], dd[i], kk[i - 1], dd[i - 1]) and kk[i] > dd[i] and kk[i - 1] <= dd[i - 1] and kk[i] < p["zone"] and p["zone"] - kk[i])


add(Playbook("kdj_golden_low", "KDJ 低位金叉", "oversold", "reversal", "short",
             "K 上穿 D 且 K<30（KDJ 9/3/3，K/D 以 50 为种子）。次日开盘买入，持有 5 日，止损 6%。", COMMUNITY,
             (P("zone", "低位区(K 上限)", 30, 15, 50, 1),), ExitSpec(5, 6), 6, 3, _kdj_low))


def _boll_rebound(v, p):
    lo = v.boll()["lower"]
    return v.scan(lambda i: i >= 1 and ok(lo[i], lo[i - 1], v.close[i - 1], v.close[i], v.open[i]) and v.close[i - 1] < lo[i - 1]
                  and v.close[i] > lo[i] and v.close[i] > v.open[i] and (v.close[i] / lo[i] - 1) * 100)


add(Playbook("boll_lower_rebound", "布林下轨反弹", "oversold", "reversal", "short",
             "昨日收盘<下轨；今日收盘>下轨且收阳。次日开盘买入，持有 5 日，止损 6%。", COMMUNITY, (), ExitSpec(5, 6), 6, 3, _boll_rebound))


def _dry_pullback(v, p):
    m20, m60, vma20 = v.ma(20), v.ma(60), v.vol_ma(20)

    def fn(i):
        if i < 3 or not ok(m20[i], m60[i], vma20[i - 1], v.volume[i], v.close[i], v.close[i - 3]) or v.volume[i] <= 0:
            return None
        drop3 = (v.close[i] / v.close[i - 3] - 1) * 100
        return m20[i] > m60[i] and v.close[i] > m60[i] and drop3 <= -p["pullback"] and v.volume[i] <= p["shrink"] * vma20[i - 1] and vma20[i - 1] / v.volume[i]
    return v.scan(fn)


add(Playbook("volume_dry_pullback", "缩量回调", "oversold", "volume", "short",
             "MA20>MA60 且收盘>MA60；3 日累计跌幅≥3%；成交量≤0.6×前 20 日均量。次日开盘买入，持有 5 日，止损 6%。", COMMUNITY,
             (P("pullback", "3 日跌幅下限(%)", 3, 1, 10, 0.5), P("shrink", "缩量比上限", 0.6, 0.3, 1, 0.05)), ExitSpec(5, 6), 6, 3, _dry_pullback))


def _low_surge(v, p):
    h, vr, pc = v.prev_hhv("high", 250), v.volume_ratio20(), v.pct()
    return v.scan(lambda i: ok(h[i], vr[i], pc[i], v.close[i]) and v.close[i] <= p["far"] * h[i] and vr[i] >= p["vol_mult"] and pc[i] > p["min_pct"] and vr[i])


add(Playbook("low_volume_surge", "底部放量", "oversold", "volume", "swing",
             "收盘≤0.7×前 250 日最高价（研究序列不足 250 日则无信号）；量≥3×前 20 日均量；涨幅>4%。次日开盘买入，持有 20 日，止损 8%，追踪 12%。", COMMUNITY,
             (P("far", "距高点比例上限", 0.7, 0.4, 0.9, 0.05), P("vol_mult", "放量倍数", 3, 1.5, 8, 0.5), P("min_pct", "涨幅下限(%)", 4, 2, 9, 0.5)),
             ExitSpec(20, 8, None, 12), 6, 2, _low_surge))

# ============================ 5. sentiment cycle and timing (9) ====================================================


def _ice(v, p):
    flag, ls, ow, t = v.xs.get("mkt.ice"), v.limit_streak(), v.one_word(), v.turnover_cny()
    if flag is None:
        return [None] * v.n
    return v.scan(lambda i: flag[i] and ls[i] == 1 and not ow[i] and ok(t[i]) and t[i] / 1e8)


add(Playbook("ice_point_reflex", "冰点反包", "sentiment_cycle", "sentiment", "short",
             "昨日全市场跌停家数≥其近 250 日（至少 60 日）90% 分位且>0，今日涨停家数>昨日；满足时当日首板且非一字板入选。次日开盘买入，持有 2 日，止损 6%。", COMMUNITY,
             (P("ice_pct", "跌停家数分位(%)", 90, 80, 99, 1), P("min_history", "最少历史天数", 60, 20, 250, 10)), ExitSpec(2, 6), 5, 2, _ice, needs_market=True))


def _divergence(v, p):
    lu, ow, vma5, ls = v.limit_up(), v.one_word(), v.vol_ma(5), v.limit_streak()
    return v.scan(lambda i: i >= 1 and lu[i - 1] and not ow[i - 1] and ok(vma5[i - 1], v.volume[i - 1], v.volume[i], ls[i])
                  and v.volume[i - 1] >= p["div_mult"] * vma5[i - 1] and lu[i] and v.volume[i] < p["shrink"] * v.volume[i - 1] and ls[i])


add(Playbook("divergence_to_consensus", "分歧转一致", "sentiment_cycle", "sentiment", "short",
             "昨日涨停非一字且量≥1.5×昨日 5 日均量；今日再涨停且量<0.7×昨日。次日开盘买入，持有 2 日，止损 7%。", COMMUNITY,
             (P("div_mult", "分歧放量倍数", 1.5, 1.1, 4, 0.1), P("shrink", "缩量比上限", 0.7, 0.3, 1, 0.05)), ExitSpec(2, 7), 4, 2, _divergence))


def _first_negative(v, p):
    ls, ld, m5 = v.limit_streak(), v.limit_down(), v.ma(5)
    return v.scan(lambda i: i >= 1 and ok(ls[i - 1], m5[i], ld[i], v.close[i], v.open[i]) and ls[i - 1] >= p["min_streak"] and v.close[i] < v.open[i]
                  and not ld[i] and v.close[i] >= m5[i] and ls[i - 1])


add(Playbook("first_negative", "首阴反抽", "sentiment_cycle", "sentiment", "short",
             "昨日连板数≥3；今日收阴、未跌停、收盘≥MA5。次日开盘买入，持有 3 日，止损 7%。", COMMUNITY,
             (P("min_streak", "连板数下限", 3, 2, 6, 1),), ExitSpec(3, 7), 4, 2, _first_negative))

HLS = XSpec("hls", {"c": lambda v, i: cap_at(v, i)},
            lambda v, i: bool(v.limit_streak()[i] == 1 and not v.one_word()[i] and ok(cap_at(v, i)) and cap_at(v, i) > 0), min_names=2)


def _high_low_switch(v, p):
    flag, xs = v.xs.get("mkt.switch"), v.xs.get("hls.c")
    if flag is None or xs is None:
        return [None] * v.n
    return v.scan(lambda i: flag[i] and xs[i] is not None and xs[i][0] <= math.ceil(xs[i][1] / 2) and 1.0 / max(cap_at(v, i), 1.0))


add(Playbook("high_low_switch", "高低切换", "sentiment_cycle", "sentiment", "short",
             "今日全市场最高连板<5 且昨日≥5；首板家数≥昨日；当日首板、非一字、流通市值≤同类首板中位数者入选。次日开盘买入，持有 2 日，止损 6%。", COMMUNITY,
             (P("high_streak", "高标连板数", 5, 3, 8, 1),), ExitSpec(2, 6), 5, 2, _high_low_switch, xs=HLS, needs_market=True, requires=("float_cap",)))


def _corr10(v, i):
    return pearson(v.close, v.volume, 10, i) if i >= 9 else None


VPD = XSpec("vpd", {"corr": _corr10, "r5": lambda v, i: v.ret(5)[i]}, lambda v, i: liquid(v, i))


def _vpd(v, p):
    c, r = v.xs.get("vpd.corr"), v.xs.get("vpd.r5")
    if c is None:
        return [None] * v.n
    out = []
    for i in range(v.n):
        a = top_rank(c, i, p["corr_pct"], False) if v.first_of_week[i] else None
        b = top_rank(r, i, p["ret_pct"], False) if a else None
        out.append(None if not (a and b) else 2.0 - pearson(v.close, v.volume, 10, i))
    return out


add(Playbook("volume_price_divergence", "量价背离反转", "sentiment_cycle", "reversal", "short",
             "每周第一个交易日；成交额估算≥1 亿；近 10 日收盘与成交量相关系数处截面最低 10% 且 ret_5 处最低 20%。持有 5 日。" + NO_ST, COMMUNITY,
             (P("corr_pct", "相关系数入选比例(%)", 10, 5, 30, 1), P("ret_pct", "ret_5 入选比例(%)", 20, 10, 50, 1)), ExitSpec(5), 20, 20, _vpd, xs=VPD))


def _amp10(v, i):
    if i < 10:
        return None
    pcs = v.prev_close()
    w = [(v.high[k] - v.low[k]) / pcs[k] if ok(v.high[k], v.low[k], pcs[k]) and pcs[k] > 0 else None for k in range(i - 9, i + 1)]
    return None if any(x is None for x in w) else sum(w) / 10


LOWAMP = XSpec("lowamp", {"amp": _amp10}, lambda v, i: liquid(v, i))


def _low_amp(v, p):
    xs = v.xs.get("lowamp.amp")
    if xs is None:
        return [None] * v.n
    hh, ll = v.hhv("high", 20), v.llv("low", 20)
    out = []
    for i in range(v.n):
        hit = top_rank(xs, i, p["top_pct"], False) if v.first_of_week[i] else None
        if not hit or not ok(hh[i], ll[i], v.close[i]) or hh[i] <= ll[i] or (v.close[i] - ll[i]) / (hh[i] - ll[i]) > p["pos_max"] / 100:
            out.append(None)
        else:
            out.append(1.0 / (1.0 + _amp10(v, i)))
    return out


add(Playbook("low_amplitude", "低振幅", "sentiment_cycle", "volatility", "short",
             "每周第一个交易日；成交额估算≥1 亿；近 10 日平均振幅处截面最低 10%，且收盘位于近 20 日区间最低 30% 以内。持有 5 日。" + NO_ST, COMMUNITY,
             (P("top_pct", "入选比例(%)", 10, 5, 30, 1), P("pos_max", "区间位置上限(%)", 30, 10, 60, 5)), ExitSpec(5), 20, 20, _low_amp, xs=LOWAMP))


def _ovn20(v, i):
    if i < 20:
        return None
    pcs = v.prev_close()
    w = [(v.open[k] / pcs[k] - 1.0) if ok(v.open[k], pcs[k]) and pcs[k] > 0 else None for k in range(i - 19, i + 1)]
    return None if any(x is None for x in w) else sum(w)


OVN = XSpec("ovn", {"o": _ovn20}, lambda v, i: liquid(v, i))
add(Playbook("overnight_gap_reversal", "隔夜收益反转", "sentiment_cycle", "reversal", "swing",
             "每周第一个交易日；成交额估算≥1 亿；隔夜收益(开盘/前收−1)近 20 日之和，取截面最低 10%。持有 10 日。" + NO_ST, COMMUNITY,
             (P("top_pct", "入选比例(%)", 10, 5, 30, 1),), ExitSpec(10), 20, 20, _xs_signal("ovn.o", "top_pct", False, "w"), xs=OVN))


def _turn20(v, i):
    cap = cap_at(v, i)
    if i < 20 or not ok(cap) or cap <= 0:
        return None
    w = [v.volume[k] * v.close[k] / cap if ok(v.volume[k], v.close[k], cap_at(v, k)) else None for k in range(i - 19, i + 1)]
    return None if any(x is None for x in w) else sum(w) / 20


LTLV = XSpec("ltlv", {"turn": _turn20, "vol": lambda v, i: v.ret_std(20)[i]}, lambda v, i: liquid(v, i) and ok(cap_at(v, i)) and cap_at(v, i) > 0)


def _ltlv(v, p):
    xs0, xs1 = v.xs.get("ltlv.turn"), v.xs.get("ltlv.vol")
    if xs0 is None:
        return [None] * v.n
    out = []
    for i in range(v.n):
        a = top_rank(xs0, i, p["top_pct"], False) if v.first_of_month[i] else None
        b = top_rank(xs1, i, p["top_pct"], False) if a else None
        out.append(None if not (a and b) else 1.0 - (xs0[i][0] + xs1[i][0] - 2) / (2 * xs0[i][1]))
    return [x if x and x > 0 else None for x in out]


add(Playbook("low_turnover_low_vol", "低换手低波动", "sentiment_cycle", "volatility", "long",
             "每月第一个交易日；换手代理=成交量×收盘/流通市值(近 20 日均值)处截面最低 30%，同时 20 日波动率处最低 30%。持有 20 日。" + NO_ST, COMMUNITY,
             (P("top_pct", "入选比例(%)", 30, 10, 50, 5),), ExitSpec(20), 20, 20, _ltlv, xs=LTLV, requires=("float_cap",)))


def _micro_flag(rows: list[dict]) -> bool | None:
    n = len(rows)
    if n < MIN_CROSS:
        return None
    k = max(1, math.ceil(n * 0.10))
    s = sorted(rows, key=lambda r: r["c"])
    return sum(r["r20"] for r in s[:k]) / k - sum(r["r20"] for r in s[-k:]) / k >= 0


MICRO = XSpec("micro", {"c": lambda v, i: cap_at(v, i), "r20": lambda v, i: v.ret(20)[i]},
              lambda v, i: i >= 119 and liquid(v, i, 0.5e8) and ok(cap_at(v, i)) and cap_at(v, i) > 0, date_flag=_micro_flag)


def _micro(v, p):
    xs, flag = v.xs.get("micro.c"), v.xs.get("micro.flag")
    if xs is None or flag is None:
        return [None] * v.n
    out = []
    for i in range(v.n):
        hit = top_rank(xs, i, p["top_pct"], False) if v.first_of_month[i] and flag[i] else None
        out.append(None if not hit else 1.0 / max(cap_at(v, i) / 1e8, 1e-6))
    return out


add(Playbook("microcap_timed", "微盘择时", "sentiment_cycle", "value", "long",
             "开关=当日流通市值最小 10% 的 ret_20 均值−最大 10% 的 ret_20 均值≥0（在可交易集合内计算）；仅当月第一个交易日开关为开时取最小 5%。持有 20 日。", COMMUNITY,
             (P("top_pct", "入选比例(%)", 5, 2, 20, 1),), ExitSpec(20), 20, 20, _micro, xs=MICRO, requires=("float_cap",)))

# ============================ 6. crowd and linkage (5): data not available ==========================================


def _none(v, p):
    return [None] * v.n


for pid, name, rule, req, why, days, stop in (
    ("industry_follower", "板块跟风", "当日所属行业涨停≥3 只；本股未涨停、涨幅≥涨停幅度的 50%、量≥5 日均量 1.5 倍。持有 2 日，止损 6%。", "sector_membership_history", "没有按时点保存的行业/板块成员关系", 2, 6),
    ("industry_first_board", "板块首板", "当日所属行业涨停≥3 只；本股首板、非一字。持有 2 日，止损 6%。", "sector_membership_history", "没有按时点保存的行业/板块成员关系", 2, 6),
    ("name_cluster_follower", "名称联动", "名称中少见汉字的同字股票当日已有≥2 只涨停；本股未涨停且涨幅≥3%。持有 2 日，止损 6%。", "instrument_name_history", "没有按时点保存的证券简称历史", 2, 6),
    ("smart_seat_follow", "强势席位合力", "当日上龙虎榜；买方前五席位中强势席位≥2 个且一日游席位为 0。持有 3 日，止损 7%。", "lhb_seat_trades_history", "龙虎榜席位成交未规范化入库", 3, 7),
    ("institution_net_buy", "机构净买入", "当日上龙虎榜；机构专用席位净买入≥成交额 3%；涨幅低于涨停幅度的 95%。持有 5 日，止损 7%。", "lhb_seat_trades_history", "龙虎榜席位成交未规范化入库", 5, 7),
):
    add(Playbook(pid, name, "crowd", "sentiment", "short" if days < 5 else "swing", rule, COMMUNITY, (), ExitSpec(days, stop), 5, 2, _none,
                 requires=(req,), unavailable_reason=why))

# ============================ 7. factor composites (2) ============================================================


def _tradable_xs(v, i):
    return i >= 119 and liquid(v, i, 0.5e8) and v.close[i] is not None and v.close[i] >= 2.0


def _max20(v, i):
    pc = v.pct()
    w = pc[i - 19:i + 1] if i >= 20 else None
    return None if not w or any(x is None for x in w) else -max(w)


def _lowturn5(v, i):
    cap = cap_at(v, i)
    t = v.turnover_cny()
    if i < 5 or not ok(cap) or cap <= 0:
        return None
    w = t[i - 4:i + 1]
    return None if any(x is None for x in w) else -(sum(w) / 5) / cap


LRC = XSpec("lrc", {"turn5": _lowturn5, "max20": _max20, "lowvol60": lambda v, i: None if v.ret_std(60)[i] is None else -v.ret_std(60)[i]},
            _tradable_xs, composite=("turn5", "max20", "lowvol60"))


def _composite_signal(key: str):
    def sig(v, p):
        xs = v.xs.get(f"{key}.score")
        if xs is None:
            return [None] * v.n
        return [None if c is None or c[0] > p["offer"] else 1.0 - (c[0] - 1) / c[1] for c in xs]
    return sig


add(Playbook("low_risk_combo", "低风险复合", "factor_composite", "volatility", "long",
             "三个因子逐日排百分位取平均：近 5 日换手(成交额/流通市值)越低、近 20 日最大单日涨幅越小、近 60 日波动越小越好；每天取得分前 offer 名补仓，持有 20 日。" +
             NO_ST, "Bali, Cakici & Whitelaw (2011) MAX 效应；Ang et al. (2006) 低波动；Liu, Stambaugh & Yuan (2019)",
             _common(40), ExitSpec(20), 20, 1, _composite_signal("lrc"), xs=LRC, requires=("float_cap",)))


def _shrink_ratio(v, i):
    t5, t60 = v.turnover_ma(5)[i], v.turnover_ma(60)[i]
    return None if not ok(t5, t60) or t60 <= 0 else -(t5 / t60)


def _bias60(v, i):
    m = v.ma(60)[i]
    return None if not ok(m, v.close[i]) or m <= 0 else -(v.close[i] / m - 1)


SHO = XSpec("sho", {"shrink": _shrink_ratio, "bias": _bias60, "rsi": lambda v, i: None if v.rsi(14)[i] is None else -v.rsi(14)[i]},
            _tradable_xs, composite=("shrink", "bias", "rsi"))
add(Playbook("shrink_oversold", "缩量超跌", "factor_composite", "reversal", "swing",
             "三个因子逐日排百分位取平均：5日/60日成交额之比越低、收盘相对 MA60 越负、RSI14 越低越好；每天取得分前 offer 名补仓，持有 10 日。" + NO_ST,
             "Jegadeesh (1990) 短期反转；缩量止跌为社区经验", _common(40), ExitSpec(10), 20, 2, _composite_signal("sho"), xs=SHO))

# ============================ 8. technical analysis (14) ==============================================================
TECH_PARAMS = _common(0)[:2]
NISON = "Steve Nison《日本蜡烛图技术》(1991) 常见定义量化为日线规则；阈值为社区经验"
APPEL = "MACD 由 Gerald Appel 提出；背离/零轴/多周期用法为社区经验"


def _candle(v: InstrumentView, i: int):
    o, h, l_, c = v.open[i], v.high[i], v.low[i], v.close[i]
    if not ok(o, h, l_, c):
        return None
    return {"body": abs(c - o), "range": h - l_, "upper": h - max(o, c), "lower": min(o, c) - l_, "green": c > o}


def _near_low(v, i) -> bool:
    return i >= 19 and ok(v.close[i]) and all(ok(x) for x in v.close[i - 19:i + 1]) and v.close[i] <= min(v.close[i - 19:i + 1]) * 1.03


def _down(v, i, n, thr) -> bool:
    return i >= n and ok(v.close[i], v.close[i - n]) and v.close[i] / v.close[i - n] - 1 <= thr


def _tech(pid, name, style, horizon, rule, hold, per_day, fn, source=COMMUNITY, family="technical"):
    def sig(v, p):
        return v.scan(lambda i: tradable(v, i, p) and fn(v, i))
    add(Playbook(pid, name, family, style, horizon, rule + f"次日开盘买入，持有 {hold} 日，无价格止损；最多 20 只，每天最多新开 {per_day} 只。股票池：成交额估算≥0.5 亿、已有≥120 根、股价≥2。" + NO_ST,
                 source, TECH_PARAMS, ExitSpec(hold), 20, per_day, sig))


def _hammer(v, i):
    k = _candle(v, i)
    return k and k["range"] > 0 and k["lower"] >= 2 * k["body"] and k["upper"] <= k["body"] + 0.001 * v.close[i] and _down(v, i, 5, -0.05) and _near_low(v, i) \
        and k["lower"] / max(k["body"], 0.001 * v.close[i])


def _engulf(v, i):
    k = _candle(v, i)
    if i < 6 or not k or not ok(v.open[i - 1], v.close[i - 1]):
        return None
    o1, c1 = v.open[i - 1], v.close[i - 1]
    return c1 < o1 and k["green"] and v.open[i] <= c1 and v.close[i] >= o1 and _down(v, i - 1, 5, -0.05) and k["body"] / max(o1 - c1, 0.001 * v.close[i])


def _morning(v, i):
    k = _candle(v, i)
    if i < 10 or not k or not all(ok(v.open[j], v.close[j]) for j in (i - 1, i - 2)) or not ok(v.close[i - 3]):
        return None
    o1, c1, o2, c2, pc2 = v.open[i - 1], v.close[i - 1], v.open[i - 2], v.close[i - 2], v.close[i - 3]
    mid = (o2 + c2) / 2
    return (c2 / pc2 - 1 <= -0.03 and abs(c1 - o1) <= abs(c2 - o2) * 0.3 and k["green"] and v.close[i] >= mid and _down(v, i, 10, -0.08)
            and (v.close[i] / mid - 1) * 100 + 1)


def _soldiers(v, i):
    if i < 13 or not all(ok(v.open[j], v.close[j]) for j in (i, i - 1, i - 2)) or not ok(v.close[i - 3]) or v.pct()[i] is None:
        return None
    o, c, o1, c1, o2, c2 = v.open[i], v.close[i], v.open[i - 1], v.close[i - 1], v.open[i - 2], v.close[i - 2]
    return (c > o and c1 > o1 and c2 > o2 and c > c1 > c2 and o >= o1 >= o2 and abs(v.pct()[i]) < 6 and _down(v, i - 3, 10, -0.08)
            and (c / v.close[i - 3] - 1) * 100)


def _doji(v, i):
    k = _candle(v, i)
    pc = v.prev_close()[i]
    return k and ok(pc) and k["range"] > 0.03 * pc and k["body"] <= 0.1 * k["range"] and _near_low(v, i) and _down(v, i, 5, -0.05) \
        and -(v.close[i] / v.close[i - 5] - 1) * 100


def _shadow_vol(v, i):
    k = _candle(v, i)
    pc, vma5 = v.prev_close()[i], v.vol_ma(5)[i]
    return k and ok(pc, vma5, v.volume[i]) and k["range"] > 0 and k["lower"] >= 0.6 * k["range"] and k["range"] > 0.04 * pc \
        and v.volume[i] >= 1.5 * vma5 and _near_low(v, i) and k["lower"] / k["range"] * v.volume[i] / vma5


def _macd_div(v, i):
    m = v.macd()
    dif, hist = m["macd"], m["hist"]
    if i < 41 or not ok(dif[i], hist[i], hist[i - 1], v.close[i]):
        return None
    cw, dw = v.close[i - 39:i - 9], dif[i - 39:i - 9]
    if any(x is None for x in cw) or any(x is None for x in dw):
        return None
    cmin, dmin = min(cw), min(dw)
    return v.close[i] < cmin and dif[i] > dmin and dif[i] < 0 and hist[i] > hist[i - 1] and dmin != 0 and (dif[i] - dmin) / abs(dmin)


def _macd_gold_below(v, i):
    m = v.macd()
    dif, dea = m["macd"], m["signal"]
    return i >= 1 and ok(dif[i], dea[i], dif[i - 1], dea[i - 1]) and dif[i] > dea[i] and dif[i - 1] <= dea[i - 1] and dif[i] < 0 and -dif[i] / v.close[i] * 100


def _kdj_j_turn(v, i):
    j = v.kdj()["j"]
    return i >= 1 and ok(j[i], j[i - 1]) and j[i] < 0 and j[i] > j[i - 1] and _near_low(v, i) and -j[i]


def _boll_rsi(v, i):
    lo, r6 = v.boll()["lower"][i], v.rsi(6)[i]
    k = _candle(v, i)
    return k and ok(lo, r6) and v.close[i] < lo and r6 < 20 and k["lower"] > k["body"] and (lo / v.close[i] - 1) * 100


def _pl(v: InstrumentView, kind: str):
    key = ("pl", kind)
    if key not in v._cache:
        v._cache[key] = period_lines(v, kind, 35 if kind == "w" else 12)
    return v._cache[key]


def _t20(v, i):
    t = v.turnover_ma(20)[i]
    return None if t is None else t / 1e8


def _w_macd(v, i):
    p = _pl(v, "w")
    m20 = v.ma(20)[i]
    return p["fresh"][i] and p["golden"][i] and ok(m20) and v.close[i] > m20 and _t20(v, i)


def _m_macd(v, i):
    p = _pl(v, "m")
    return p["fresh"][i] and p["golden"][i] and _t20(v, i)


def _m_ma(v, i):
    p = _pl(v, "m")
    return p["fresh"][i] and p["ma_golden"][i] and _t20(v, i)


def _mtf(v, i):
    m = v.macd()
    dif, dea = m["macd"], m["signal"]
    w, mo = _pl(v, "w"), _pl(v, "m")
    return i >= 1 and ok(dif[i], dea[i], dif[i - 1], dea[i - 1]) and dif[i] > dea[i] and dif[i - 1] <= dea[i - 1] and w["up"][i] and mo["up"][i] and _t20(v, i)


_tech("k_hammer", "锤子线", "reversal", "short", "近 5 日跌幅≥5%且收盘在 20 日最低收盘 3% 内；下影线≥2×实体，上影线不超过实体。", 5, 4, _hammer, NISON)
_tech("k_engulfing", "阳包阴", "reversal", "short", "昨日阴线且此前 5 日跌≥5%；今日阳线开盘≤昨收、收盘≥昨开。", 5, 4, _engulf, NISON)
_tech("k_morning_star", "早晨之星", "reversal", "short", "近 10 日跌≥8%；前天大跌≥3%，昨天小实体，今天阳线收复前天实体中点。", 5, 4, _morning, NISON)
_tech("k_three_soldiers", "红三兵", "reversal", "short", "此前 10 日跌≥8%；连续三根逐步抬高阳线，今日涨幅<6%。", 5, 4, _soldiers, NISON)
_tech("k_doji_low", "低位十字星", "reversal", "short", "近 5 日跌≥5%且在 20 日低位 3% 内；振幅>3%，实体≤振幅 10%。", 5, 4, _doji, NISON)
_tech("k_lower_shadow_volume", "低位放量长下影", "reversal", "short", "低位 3% 内；下影线≥振幅 60%，振幅>4%，量≥5 日均量 1.5 倍。", 5, 4, _shadow_vol, NISON)
_tech("macd_bottom_divergence", "MACD 底背离", "reversal", "swing", "收盘低于 10–40 日前最低收盘而 DIF 更高且<0，MACD 柱回升。", 10, 2, _macd_div, APPEL)
_tech("macd_golden_below_zero", "MACD 零轴下金叉", "reversal", "swing", "DIF 在零轴下上穿 DEA。", 10, 2, _macd_gold_below, APPEL)
_tech("kdj_j_oversold_turn", "KDJ J 值超卖拐头", "reversal", "short", "J<0 且高于昨日，收盘在 20 日低位 3% 内。", 5, 4, _kdj_j_turn)
_tech("boll_lower_rsi6", "下轨超卖下影", "reversal", "short", "收盘<布林下轨、RSI(6)<20、下影线长于实体。", 5, 4, _boll_rsi)
_tech("w_macd_golden", "周线 MACD 金叉", "trend", "swing", "上一根完成的周线 DIF 上穿 DEA（至少 35 周），下一周首个交易日且收盘>MA20 时发出。", 10, 2, _w_macd, APPEL)
_tech("m_macd_golden", "月线 MACD 金叉", "trend", "long", "上一根完成的月线 DIF 上穿 DEA（至少 12 月）；研究序列仅约一年，几乎不会有信号。", 20, 1, _m_macd, APPEL)
_tech("m_ma_golden", "月线均线金叉", "trend", "long", "上一根完成的月线 MA5 上穿 MA10；研究序列仅约一年，几乎不会有信号。", 20, 1, _m_ma)
_tech("mtf_resonance", "日周月共振", "trend", "swing", "日线 MACD 金叉，且最近完成的周线、月线 MACD 均多头。", 10, 2, _mtf, APPEL)

assert len(ORDER) == 52, len(ORDER)
