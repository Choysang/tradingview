"""Four-dimension market regime timeline: trend x liquidity x sentiment cycle x size style (old-site 市场周期).

Why: LEGACY_FEATURE_INVENTORY §1 keeps the regime banner and timeline. The rules are the old site's
(thresholds are engineering parameters, uncalibrated, ``REGIME_VERSION``); what changes is honesty about
history: every label uses only data up to its own day (trailing windows, trailing 5-day mode), and every
dimension reports its ``basis`` - ``ok`` or ``insufficient_history`` / ``missing_index`` - so a default label
is never presented as a measurement.
Rules: trend bull = index > MA60 and 20-bar MA60 slope > +2% and breadth_ma20 > 55; bear is the mirror
(< -2%, < 45); else range; then a trailing 5-day mode. liquidity = mean(amount,20)/mean(amount,120) > 1.15
expanding, < 0.85 contracting. sentiment = freezing (limit-up percentile < 20 and premium <= 0) | climax
(percentile > 80 and height >= its 80th percentile) | repair (5-day mean of limit-ups rose) | ebb.
style = 20-day return of the small-cap index minus the large-cap index > +3 small, < -3 large, else balanced;
a missing index gives ``unknown`` (the old site silently said balanced).
Market amount is an estimate (shares x close): the daily bars carry no turnover amount.
Not covered: calibration, sector-level regimes, intraday regimes.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping, Sequence

from .daily import SessionStats

REGIME_VERSION = "regime-v1-old-site-rules-uncalibrated"
PRIMARY_INDEX, LARGE_INDEX, SMALL_INDEX = "000985", "000300", "000852"
INDEX_NAMES = {"000985": "中证全指", "000300": "沪深300", "000852": "中证1000", "000905": "中证500", "000001": "上证指数", "399006": "创业板指"}
SLOPE_BARS, MA_BARS = 20, 60
BULL_SLOPE, BEAR_SLOPE = 2.0, -2.0
BULL_BREADTH, BEAR_BREADTH = 55.0, 45.0
SMOOTH_DAYS = 5
EXPANDING, CONTRACTING = 1.15, 0.85
AMOUNT_SHORT, AMOUNT_LONG = 20, 120
PERCENTILE_WINDOW, MIN_PERCENTILE_OBS = 250, 30
FREEZING_PCT, CLIMAX_PCT = 20.0, 80.0
STYLE_SPREAD = 3.0
LABELS = {
    "trend": {"bull": "牛市", "bear": "熊市", "range": "震荡"},
    "liquidity": {"expanding": "增量", "steady": "存量", "contracting": "缩量"},
    "sentiment": {"freezing": "冰点", "repair": "修复", "climax": "高潮", "ebb": "退潮"},
    "style": {"small": "小盘占优", "large": "大盘占优", "balanced": "风格均衡", "unknown": "风格未知"},
}
RULES = (
    "趋势：牛市 = 指数收盘 > MA60 且 MA60 的 20 日斜率 > +2% 且收盘站上 20 日线的个股占比 > 55%；熊市 = 收盘 < MA60 且斜率 < -2% 且占比 < 45%；其余为震荡；再取最近 5 个交易日的众数（并列取最近者）。",
    "流动性：全市场成交额估算（股数×收盘价）20 日均值 / 120 日均值 > 1.15 为增量，< 0.85 为缩量，其余为存量。",
    "情绪：涨停家数在最近 250 日内的分位 p；冰点 = p < 20 且昨日涨停股今日平均涨幅 ≤ 0；高潮 = p > 80 且最高连板 ≥ 其 250 日 80 分位；否则涨停家数 5 日均值较上一日上升为修复，不升为退潮。",
    "风格：中证1000 近 20 日涨幅 − 沪深300 近 20 日涨幅 > +3 个百分点为小盘占优，< −3 为大盘占优，其余为均衡；缺少任一指数时标记为风格未知。",
    "历史不足时：MA60/斜率、120 日均量、分位数样本不足的日子沿用旧站默认标签（震荡 / 存量 / 不判冰点高潮），并在 basis 中标明 insufficient_history，页面应淡化显示。",
    "指标版本未经校准，仅描述当日市场结构，不构成预测。",
)


def _dec(x: float | None, places: int = 4) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, places))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def mode_smooth(raw: Sequence[str], window: int = SMOOTH_DAYS) -> list[str]:
    """Trailing mode of the last ``window`` labels; ties go to the most recent label."""
    out: list[str] = []
    for i in range(len(raw)):
        recent = raw[max(0, i - window + 1): i + 1]
        counts: dict[str, int] = {}
        for lab in recent:
            counts[lab] = counts.get(lab, 0) + 1
        best = max(counts.values())
        out.append(next(lab for lab in reversed(recent) if counts[lab] == best))
    return out


def _rolling_mean(values: Sequence[float | None], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        win = values[i - n + 1: i + 1] if i + 1 >= n else []
        out.append(None if len(win) < n or any(v is None for v in win) else sum(win) / n)   # type: ignore[arg-type]
    return out


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    k = (len(sorted_vals) - 1) * q / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def classify_sentiment(limit_up: Sequence[float], height: Sequence[float], premium: Sequence[float | None]
                       ) -> tuple[list[str], list[float | None]]:
    mean5 = _rolling_mean(limit_up, 5)
    phases, pcts = [], []
    for i in range(len(limit_up)):
        win = range(max(0, i - PERCENTILE_WINDOW + 1), i + 1)
        if i + 1 < MIN_PERCENTILE_OBS:
            p, cut = None, None
        else:
            p = sum(1 for j in win if limit_up[j] <= limit_up[i]) / len(win) * 100
            cut = _percentile(sorted(height[j] for j in win), 80)
        pcts.append(p)
        gain = premium[i] if premium[i] is not None else 0.0
        if p is not None and p < FREEZING_PCT and gain <= 0:
            phases.append("freezing")
        elif p is not None and p > CLIMAX_PCT and cut is not None and height[i] >= cut:
            phases.append("climax")
        elif i >= 5 and mean5[i] is not None and mean5[i - 1] is not None and mean5[i] > mean5[i - 1]:   # type: ignore[operator]
            phases.append("repair")
        else:
            phases.append("ebb")
    return phases, pcts


def _index_series(indices: Mapping[str, Mapping[date, float]]) -> tuple[str | None, dict[date, float]]:
    if PRIMARY_INDEX in indices and indices[PRIMARY_INDEX]:
        return PRIMARY_INDEX, dict(indices[PRIMARY_INDEX])
    for sym in sorted(indices):
        if indices[sym]:
            return sym, dict(indices[sym])
    return None, {}


def _spread(indices: Mapping[str, Mapping[date, float]], dates: Sequence[date]) -> list[float | None]:
    small, large = indices.get(SMALL_INDEX), indices.get(LARGE_INDEX)
    if not small or not large:
        return [None] * len(dates)

    def ret(series: Mapping[date, float]) -> dict[date, float]:
        ds = sorted(series)
        return {ds[i]: (series[ds[i]] / series[ds[i - SLOPE_BARS]] - 1) * 100 for i in range(SLOPE_BARS, len(ds))}

    rs, rl = ret(small), ret(large)
    return [(rs[d] - rl[d]) if d in rs and d in rl else None for d in dates]


def build_regime(stats: Sequence[SessionStats], indices: Mapping[str, Mapping[date, float]]) -> dict[str, Any]:
    """Timeline over the sessions that have both stock statistics and a primary-index close."""
    symbol, closes = _index_series(indices)
    through = max(closes) if closes else None
    if symbol is None:
        return {"version": REGIME_VERSION, "available": False, "reason": "NO_INDEX_BARS", "index": None, "points": [],
                "current": None, "rules": list(RULES)}
    idx_dates = sorted(closes)
    idx_ma = _rolling_mean([closes[d] for d in idx_dates], MA_BARS)
    ma_by = dict(zip(idx_dates, idx_ma))
    slope_by = {idx_dates[i]: ((idx_ma[i] / idx_ma[i - SLOPE_BARS] - 1) * 100) for i in range(len(idx_dates))
                if i >= SLOPE_BARS and idx_ma[i] is not None and idx_ma[i - SLOPE_BARS] is not None}
    rows = [s for s in sorted(stats, key=lambda s: s.d) if s.d in closes]
    if not rows:
        return {"version": REGIME_VERSION, "available": False, "reason": "NO_OVERLAP_STOCK_INDEX", "index": {"symbol": symbol}, "points": [],
                "current": None, "rules": list(RULES)}
    dates = [s.d for s in rows]
    amount = [float(s.amount_estimate) if s.volume_n else None for s in rows]
    a20, a120 = _rolling_mean(amount, AMOUNT_SHORT), _rolling_mean(amount, AMOUNT_LONG)
    ratio = [None if a20[i] is None or a120[i] is None or not a120[i] else a20[i] / a120[i] for i in range(len(rows))]   # type: ignore[operator]
    breadth = [None if s.ma_n == 0 else s.ma_above / s.ma_n * 100 for s in rows]
    raw_trend, trend_ok = [], []
    for i, s in enumerate(rows):
        d, c, ma, sl, br = s.d, closes[s.d], ma_by.get(s.d), slope_by.get(s.d), breadth[i]
        if ma is None or sl is None or br is None:
            raw_trend.append("range")
            trend_ok.append(False)
        else:
            trend_ok.append(True)
            raw_trend.append("bull" if c > ma and sl > BULL_SLOPE and br > BULL_BREADTH
                             else "bear" if c < ma and sl < BEAR_SLOPE and br < BEAR_BREADTH else "range")
    trend = mode_smooth(raw_trend)
    liquidity = ["steady" if r is None else "expanding" if r > EXPANDING else "contracting" if r < CONTRACTING else "steady" for r in ratio]
    lus = [float(s.limit_up) for s in rows]
    heights = [float(s.max_height) for s in rows]
    prem = [None if s.prem_n == 0 else float(s.prem_close_sum / s.prem_n) for s in rows]
    sentiment, pcts = classify_sentiment(lus, heights, prem)
    spread = _spread(indices, dates)
    style = ["unknown" if v is None else "small" if v > STYLE_SPREAD else "large" if v < -STYLE_SPREAD else "balanced" for v in spread]
    points = []
    for i, s in enumerate(rows):
        points.append({
            "date": s.d, "index_close": _dec(closes[s.d], 2), "trend": trend[i], "liquidity": liquidity[i],
            "sentiment": sentiment[i], "style": style[i], "breadth_ma20": _dec(breadth[i], 1),
            "limit_up_count": s.limit_up if s.limit_verified else None, "broken_rate": _dec(None if (s.limit_up + s.broken) == 0 else s.broken / (s.limit_up + s.broken) * 100, 1),
            "max_height": s.max_height if s.limit_verified else None, "amount_ratio": _dec(ratio[i], 3), "premium": _dec(prem[i], 2),
            "lu_percentile": _dec(pcts[i], 1), "index_ma60": _dec(ma_by.get(s.d), 2), "ma60_slope": _dec(slope_by.get(s.d), 2),
            "style_spread": _dec(spread[i], 2),
            "basis": {"trend": "ok" if trend_ok[i] else "insufficient_history", "liquidity": "ok" if ratio[i] is not None else "insufficient_history",
                      "sentiment": "ok" if pcts[i] is not None else "insufficient_history", "style": "ok" if spread[i] is not None else "missing_index"},
        })
    cur = points[-1]
    label = " · ".join(LABELS[k][cur[k]] for k in ("trend", "liquidity", "sentiment", "style"))
    return {"version": REGIME_VERSION, "available": True, "reason": None,
            "index": {"symbol": symbol, "name": INDEX_NAMES.get(symbol, symbol), "through": through, "fallback": symbol != PRIMARY_INDEX},
            "stock_sessions_through": max(s.d for s in stats), "points": points,
            "current": {**cur, "label": label, "explanation": explain(cur, INDEX_NAMES.get(symbol, symbol))}, "rules": list(RULES),
            "labels": LABELS}


def explain(p: Mapping[str, Any], index_name: str) -> list[str]:
    def f(v: Any, pat: str) -> str:
        return "无数据" if v is None else format(float(v), pat)

    out = [
        f"趋势：{index_name}收盘 {f(p['index_close'], '.2f')}，MA60 为 {f(p['index_ma60'], '.2f')}（20 日斜率 {f(p['ma60_slope'], '+.1f')}%），"
        f"站上 20 日线的个股占 {f(p['breadth_ma20'], '.0f')}%，判定为{LABELS['trend'][p['trend']]}。",
        f"量能：全市场成交额估算 20 日均值 / 120 日均值 = {f(p['amount_ratio'], '.2f')}，判定为{LABELS['liquidity'][p['liquidity']]}。",
        f"情绪：涨停 {p['limit_up_count'] if p['limit_up_count'] is not None else '无数据'} 家（近 250 日分位 {f(p['lu_percentile'], '.0f')}%），炸板率 {f(p['broken_rate'], '.0f')}%，"
        f"最高 {p['max_height'] if p['max_height'] is not None else '无数据'} 板，昨日涨停股今日平均涨幅 {f(p['premium'], '+.1f')}%，处于{LABELS['sentiment'][p['sentiment']]}阶段。",
        ("风格：缺少中证1000 或沪深300 指数，风格未知。" if p["style"] == "unknown"
         else f"风格：中证1000 较沪深300 近 20 日涨幅差 {f(p['style_spread'], '+.1f')} 个百分点，{LABELS['style'][p['style']]}。"),
    ]
    bad = [k for k, v in p["basis"].items() if v != "ok"]
    if bad:
        out.append("以下维度历史样本不足或缺指数，标签为默认值：" + "、".join(bad) + "。")
    return out
