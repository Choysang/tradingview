"""TradingView Pro Multi-Timeframe Technical Ratings, Pivot Points, VPVR & Volume/Divergence Engine.

Implements exact, deterministic, zero-lookahead mathematical specifications for:
1. Multi-timeframe OHLCV aggregation: Daily (``1d``), Weekly (``1w``), Monthly (``1mo``), Yearly (``1y``).
2. TradingView 26-Indicator Technical Ratings Gauge:
   - 15 Moving Averages: SMA(10,20,30,50,100,200), EMA(10,20,30,50,100,200), HullMA(9), VWMA(20), Ichimoku Base(26).
   - 11 Oscillators: RSI(14), Stoch %K(14,3,3), CCI(20), Wilder RMA ADX(14), AO, Mom(10), MACD(12,26,9),
     StochRSI(3,3,14,14), Williams %R(14), BullBearPower(13), Ultimate Oscillator(7,14,28).
   - Summary score ``(R_MA + R_Osc) / 2`` mapped via ``[-0.5, -0.1, +0.1, +0.5]`` thresholds.
3. 6 Pivot Point Systems: Traditional, Classic, Fibonacci, Camarilla, Woodie, DeMark.
4. Volume Profile Visible Range (VPVR) & Chip Profile: 24-bin range overlap allocation with
   per-bar chip turnover decay, POC, greedy 70% Value Area (VAH / VAL), avg chip cost,
   winner chip ratio, 90% concentration, and HVN / LVN node classification.
5. SuperTrend(10, 3.0), BB-Keltner Volatility Squeeze + LazyBear linreg momentum, Anchored VWAP(20),
   causal multi-point MACD/RSI top/bottom divergence, KDJ/MACD crosses, volume breakouts, and
   A-share volume patterns (倍量阳柱/缩量地量/放量滞涨/缩量回调).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence


@dataclass(frozen=True)
class SimpleBar:
    d: date
    o: float
    h: float
    l: float
    c: float
    v: float


def resample_bars(daily: Sequence[SimpleBar], timeframe: str) -> list[SimpleBar]:
    """Aggregate ascending daily bars into ``1d``, ``1w``, ``1mo``, or ``1y`` bars."""
    if timeframe == "1d" or not daily:
        return list(daily)
    buckets: list[list[SimpleBar]] = []
    cur_key: tuple[int, ...] | None = None
    for b in daily:
        if timeframe == "1w":
            iso = b.d.isocalendar()
            key = (iso.year, iso.week)
        elif timeframe == "1mo":
            key = (b.d.year, b.d.month)
        elif timeframe == "1y":
            key = (b.d.year,)
        else:
            key = (b.d.year, b.d.month, b.d.day)
        if key != cur_key:
            buckets.append([b])
            cur_key = key
        else:
            buckets[-1].append(b)
    return [
        SimpleBar(
            d=grp[-1].d,
            o=grp[0].o,
            h=max(x.h for x in grp),
            l=min(x.l for x in grp),
            c=grp[-1].c,
            v=sum(x.v for x in grp),
        )
        for grp in buckets
    ]


# ---------------------------------------------------------------------------------------------------------
# Core Technical Primitives
# ---------------------------------------------------------------------------------------------------------
def _sma(vals: Sequence[float], n: int) -> float | None:
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def _ema_series(vals: Sequence[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (n + 1.0)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def _adx_series(
    bars: Sequence[SimpleBar], n: int = 14
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Standard Wilder RMA smoothed (+DI, -DI, ADX) aligned 1:1 with ``bars``."""
    m = len(bars)
    if m < n + 1 or n <= 0:
        return [None] * m, [None] * m, [None] * m

    trs: list[float] = []
    pdms: list[float] = []
    mdms: list[float] = []
    for i in range(1, m):
        up = bars[i].h - bars[i - 1].h
        dn = bars[i - 1].l - bars[i].l
        pdms.append(up if (up > dn and up > 0.0) else 0.0)
        mdms.append(dn if (dn > up and dn > 0.0) else 0.0)
        trs.append(max(bars[i].h - bars[i].l, abs(bars[i].h - bars[i - 1].c), abs(bars[i].l - bars[i - 1].c)))

    adx_out: list[float | None] = [None] * m
    pdi_out: list[float | None] = [None] * m
    mdi_out: list[float | None] = [None] * m

    tr_s = sum(trs[:n])
    pdm_s = sum(pdms[:n])
    mdm_s = sum(mdms[:n])
    dx_buf: list[float] = []
    adx_val: float | None = None

    for k in range(n - 1, len(trs)):
        if k >= n:
            tr_s = tr_s - (tr_s / n) + trs[k]
            pdm_s = pdm_s - (pdm_s / n) + pdms[k]
            mdm_s = mdm_s - (mdm_s / n) + mdms[k]
        bar_idx = k + 1
        pdi = 0.0 if tr_s <= 0.0 else 100.0 * pdm_s / tr_s
        mdi = 0.0 if tr_s <= 0.0 else 100.0 * mdm_s / tr_s
        pdi_out[bar_idx] = pdi
        mdi_out[bar_idx] = mdi
        dx = 0.0 if (pdi + mdi) <= 0.0 else 100.0 * abs(pdi - mdi) / (pdi + mdi)
        dx_buf.append(dx)
        if len(dx_buf) == n:
            adx_val = sum(dx_buf) / n
            adx_out[bar_idx] = adx_val
        elif len(dx_buf) > n and adx_val is not None:
            adx_val = (adx_val * (n - 1) + dx) / n
            adx_out[bar_idx] = adx_val

    return adx_out, pdi_out, mdi_out


def _linreg_last(vals: Sequence[float]) -> float | None:
    """Linear regression value at the final index of ``vals`` (for Squeeze Momentum)."""
    m = len(vals)
    if m == 0:
        return None
    if m == 1:
        return vals[0]
    x_mean = 0.5 * (m - 1)
    y_mean = sum(vals) / m
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(vals))
    den = sum((i - x_mean) ** 2 for i in range(m))
    slope = (num / den) if den > 0.0 else 0.0
    intercept = y_mean - slope * x_mean
    return intercept + slope * (m - 1)


def _wma(vals: Sequence[float], n: int) -> float | None:
    if len(vals) < n or n <= 0:
        return None
    sub = vals[-n:]
    denom = n * (n + 1) / 2.0
    return sum((i + 1) * x for i, x in enumerate(sub)) / denom


def _hma(vals: Sequence[float], n: int = 9) -> float | None:
    half = max(1, n // 2)
    sq = max(1, int(round(math.sqrt(n))))
    if len(vals) < n + sq:
        return None
    diff_seq: list[float] = []
    for end in range(len(vals) - sq + 1, len(vals) + 1):
        sub = vals[:end]
        w_half = _wma(sub, half)
        w_full = _wma(sub, n)
        if w_half is None or w_full is None:
            return None
        diff_seq.append(2.0 * w_half - w_full)
    return _wma(diff_seq, sq)


def _rsi_series(closes: Sequence[float], n: int = 14) -> list[float | None]:
    if len(closes) < n + 1:
        return [None] * len(closes)
    out: list[float | None] = [None] * n
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    avg_g = sum(gains) / n
    avg_l = sum(losses) / n
    out.append(100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))
    for i in range(n + 1, len(closes)):
        chg = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(0.0, chg)) / n
        avg_l = (avg_l * (n - 1) + max(0.0, -chg)) / n
        out.append(100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))
    return out


def _atr_series(bars: Sequence[SimpleBar], n: int = 14) -> list[float | None]:
    if len(bars) < 2:
        return [None] * len(bars)
    trs = [bars[0].h - bars[0].l]
    for i in range(1, len(bars)):
        pc = bars[i - 1].c
        trs.append(max(bars[i].h - bars[i].l, abs(bars[i].h - pc), abs(bars[i].l - pc)))
    if len(trs) < n:
        return [None] * len(bars)
    out: list[float | None] = [None] * (n - 1)
    atr = sum(trs[:n]) / n
    out.append(atr)
    for i in range(n, len(trs)):
        atr = (atr * (n - 1) + trs[i]) / n
        out.append(atr)
    return out


def _rating_label(score: float | None) -> str:
    if score is None:
        return "unavailable"
    if score < -0.5:
        return "strong_sell"
    if score < -0.1:
        return "sell"
    if score <= 0.1:
        return "neutral"
    if score <= 0.5:
        return "buy"
    return "strong_buy"


def _summarize_votes(items: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [x for x in items if x["signal"] != "unavailable"]
    buys = sum(1 for x in valid if x["signal"] == "buy")
    sells = sum(1 for x in valid if x["signal"] == "sell")
    neuts = sum(1 for x in valid if x["signal"] == "neutral")
    if not valid:
        return {
            "label": "unavailable", "score": None,
            "buy_count": 0, "neutral_count": 0, "sell_count": 0, "total_count": 0, "items": items,
        }
    sc = (buys - sells) / len(valid)
    return {
        "label": _rating_label(sc),
        "score": f"{sc:.3f}",
        "buy_count": buys,
        "neutral_count": neuts,
        "sell_count": sells,
        "total_count": len(valid),
        "items": items,
    }


def compute_tv_technical_rating(bars: Sequence[SimpleBar], timeframe: str) -> dict[str, Any]:
    """Compute exact TradingView 26-Indicator Technical Rating (15 MAs + 11 Oscillators)."""
    n = len(bars)
    if n == 0:
        empty = _summarize_votes([])
        return {
            "timeframe": timeframe, "bar_count": 0, "last_bar_date": None,
            "summary": empty, "moving_averages": empty, "oscillators": empty,
        }
    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    vols = [b.v for b in bars]
    price = closes[-1]

    # 1. 15 Moving Averages
    ma_items: list[dict[str, Any]] = []
    for p in (10, 20, 30, 50, 100, 200):
        sv = _sma(closes, p)
        sig = "unavailable" if sv is None else ("buy" if price > sv else ("sell" if price < sv else "neutral"))
        ma_items.append({"name": f"SMA({p})", "value": f"{sv:.2f}" if sv is not None else None, "signal": sig})
    for p in (10, 20, 30, 50, 100, 200):
        if n >= p:
            ev = _ema_series(closes, p)[-1]
            sig = "buy" if price > ev else ("sell" if price < ev else "neutral")
            ma_items.append({"name": f"EMA({p})", "value": f"{ev:.2f}", "signal": sig})
        else:
            ma_items.append({"name": f"EMA({p})", "value": None, "signal": "unavailable"})

    # VWMA(20)
    if n >= 20 and sum(vols[-20:]) > 0:
        vwma = sum(closes[-20 + i] * vols[-20 + i] for i in range(20)) / sum(vols[-20:])
        ma_items.append({"name": "VWMA(20)", "value": f"{vwma:.2f}", "signal": "buy" if price > vwma else ("sell" if price < vwma else "neutral")})
    else:
        ma_items.append({"name": "VWMA(20)", "value": None, "signal": "unavailable"})

    # Hull MA(9)
    hma9 = _hma(closes, 9)
    ma_items.append({
        "name": "HullMA(9)",
        "value": f"{hma9:.2f}" if hma9 is not None else None,
        "signal": "unavailable" if hma9 is None else ("buy" if price > hma9 else ("sell" if price < hma9 else "neutral")),
    })

    # Ichimoku Base Line (26)
    if n >= 26:
        ichi_base = (max(highs[-26:]) + min(lows[-26:])) / 2.0
        ma_items.append({"name": "IchimokuBase(26)", "value": f"{ichi_base:.2f}", "signal": "buy" if price > ichi_base else ("sell" if price < ichi_base else "neutral")})
    else:
        ma_items.append({"name": "IchimokuBase(26)", "value": None, "signal": "unavailable"})

    # 2. 11 Oscillators
    osc_items: list[dict[str, Any]] = []

    # RSI(14)
    rsis = _rsi_series(closes, 14)
    r_now = rsis[-1] if rsis else None
    r_prev = rsis[-2] if len(rsis) >= 2 else None
    if r_now is not None and r_prev is not None:
        sig = "buy" if (r_now < 30 and r_now > r_prev) else ("sell" if (r_now > 70 and r_now < r_prev) else "neutral")
        osc_items.append({"name": "RSI(14)", "value": f"{r_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "RSI(14)", "value": None, "signal": "unavailable"})

    # Stochastic %K(14, 3, 3)
    if n >= 18:
        raw_k = []
        for idx in range(13, n):
            hh, ll = max(highs[idx - 13 : idx + 1]), min(lows[idx - 13 : idx + 1])
            raw_k.append(50.0 if hh <= ll else 100.0 * (closes[idx] - ll) / (hh - ll))
        k_smooth = [sum(raw_k[i - 2 : i + 1]) / 3.0 for i in range(2, len(raw_k))]
        if len(k_smooth) >= 3:
            k_val = k_smooth[-1]
            d_val = sum(k_smooth[-3:]) / 3.0
            sig = "buy" if (k_val < 20 and d_val < 20 and k_val > d_val) else ("sell" if (k_val > 80 and d_val > 80 and k_val < d_val) else "neutral")
            osc_items.append({"name": "Stoch%K(14,3,3)", "value": f"{k_val:.2f}", "signal": sig})
        else:
            osc_items.append({"name": "Stoch%K(14,3,3)", "value": None, "signal": "unavailable"})
    else:
        osc_items.append({"name": "Stoch%K(14,3,3)", "value": None, "signal": "unavailable"})

    # CCI(20)
    if n >= 21:
        tps = [(bars[i].h + bars[i].l + bars[i].c) / 3.0 for i in range(n)]
        def _cci_at(idx: int) -> float:
            win = tps[idx - 19 : idx + 1]
            m = sum(win) / 20.0
            md = sum(abs(x - m) for x in win) / 20.0
            return 0.0 if md == 0 else (tps[idx] - m) / (0.015 * md)
        c_now, c_prev = _cci_at(n - 1), _cci_at(n - 2)
        sig = "buy" if (c_now < -100 and c_now > c_prev) else ("sell" if (c_now > 100 and c_now < c_prev) else "neutral")
        osc_items.append({"name": "CCI(20)", "value": f"{c_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "CCI(20)", "value": None, "signal": "unavailable"})

    # ADX(14) — exact Wilder RMA smoothing
    adx_ser, pdi_ser, mdi_ser = _adx_series(bars, 14)
    adx_now = adx_ser[-1] if adx_ser else None
    adx_prev = adx_ser[-2] if len(adx_ser) >= 2 else None
    p_di = pdi_ser[-1] if pdi_ser else None
    m_di = mdi_ser[-1] if mdi_ser else None
    if adx_now is not None and adx_prev is not None and p_di is not None and m_di is not None:
        sig = "buy" if (p_di > m_di and adx_now > 20.0 and adx_now > adx_prev) else (
            "sell" if (p_di < m_di and adx_now > 20.0 and adx_now > adx_prev) else "neutral"
        )
        osc_items.append({"name": "ADX(14)", "value": f"{adx_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "ADX(14)", "value": None, "signal": "unavailable"})

    # Awesome Oscillator (AO = SMA(hl2, 5) - SMA(hl2, 34))
    if n >= 36:
        hl2 = [(b.h + b.l) / 2.0 for b in bars]
        ao_now = sum(hl2[-5:]) / 5.0 - sum(hl2[-34:]) / 34.0
        ao_prev = sum(hl2[-6:-1]) / 5.0 - sum(hl2[-35:-1]) / 34.0
        sig = "buy" if (ao_now > 0 and ao_now > ao_prev) or (ao_prev <= 0 < ao_now) else (
            "sell" if (ao_now < 0 and ao_now < ao_prev) or (ao_prev >= 0 > ao_now) else "neutral"
        )
        osc_items.append({"name": "AO(5,34)", "value": f"{ao_now:.3f}", "signal": sig})
    else:
        osc_items.append({"name": "AO(5,34)", "value": None, "signal": "unavailable"})

    # Momentum(10)
    if n >= 12:
        mom_now = closes[-1] - closes[-11]
        mom_prev = closes[-2] - closes[-12]
        sig = "buy" if mom_now > mom_prev else ("sell" if mom_now < mom_prev else "neutral")
        osc_items.append({"name": "Momentum(10)", "value": f"{mom_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "Momentum(10)", "value": None, "signal": "unavailable"})

    # MACD(12, 26, 9)
    if n >= 35:
        e12 = _ema_series(closes, 12)
        e26 = _ema_series(closes, 26)
        dif = [e12[i] - e26[i] for i in range(n)]
        dea = _ema_series(dif, 9)
        sig = "buy" if dif[-1] > dea[-1] else ("sell" if dif[-1] < dea[-1] else "neutral")
        osc_items.append({"name": "MACD(12,26,9)", "value": f"{dif[-1] - dea[-1]:.3f}", "signal": sig})
    else:
        osc_items.append({"name": "MACD(12,26,9)", "value": None, "signal": "unavailable"})

    # StochRSI(3, 3, 14, 14)
    valid_rsi = [x for x in rsis if x is not None]
    if len(valid_rsi) >= 18:
        sr_raw = []
        for i in range(13, len(valid_rsi)):
            w = valid_rsi[i - 13 : i + 1]
            lo_r, hi_r = min(w), max(w)
            sr_raw.append(50.0 if hi_r <= lo_r else 100.0 * (valid_rsi[i] - lo_r) / (hi_r - lo_r))
        sr_k = [sum(sr_raw[i - 2 : i + 1]) / 3.0 for i in range(2, len(sr_raw))]
        sr_d = sum(sr_k[-3:]) / 3.0
        sig = "buy" if (sr_k[-1] < 20 and sr_d < 20 and sr_k[-1] > sr_d) else (
            "sell" if (sr_k[-1] > 80 and sr_d > 80 and sr_k[-1] < sr_d) else "neutral"
        )
        osc_items.append({"name": "StochRSI(14)", "value": f"{sr_k[-1]:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "StochRSI(14)", "value": None, "signal": "unavailable"})

    # Williams %R(14)
    if n >= 15:
        def _wr(idx: int) -> float:
            hh, ll = max(highs[idx - 13 : idx + 1]), min(lows[idx - 13 : idx + 1])
            return -50.0 if hh <= ll else -100.0 * (hh - closes[idx]) / (hh - ll)
        w_now, w_prev = _wr(n - 1), _wr(n - 2)
        sig = "buy" if (w_now < -80 and w_now > w_prev) else ("sell" if (w_now > -20 and w_now < w_prev) else "neutral")
        osc_items.append({"name": "Williams%R(14)", "value": f"{w_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "Williams%R(14)", "value": None, "signal": "unavailable"})

    # Bull Bear Power (13)
    if n >= 15:
        e13 = _ema_series(closes, 13)
        bull_now, bull_prev = highs[-1] - e13[-1], highs[-2] - e13[-2]
        bear_now, bear_prev = lows[-1] - e13[-1], lows[-2] - e13[-2]
        sig = "buy" if (e13[-1] > e13[-2] and bear_now < 0 and bear_now > bear_prev) else (
            "sell" if (e13[-1] < e13[-2] and bull_now > 0 and bull_now < bull_prev) else "neutral"
        )
        osc_items.append({"name": "BullBearPower(13)", "value": f"{bull_now + bear_now:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "BullBearPower(13)", "value": None, "signal": "unavailable"})

    # Ultimate Oscillator (7, 14, 28)
    if n >= 29:
        bp, tr_u = [], []
        for i in range(1, n):
            min_l = min(lows[i], closes[i - 1])
            max_h = max(highs[i], closes[i - 1])
            bp.append(closes[i] - min_l)
            tr_u.append(max(max_h - min_l, 1e-9))
        a7 = sum(bp[-7:]) / sum(tr_u[-7:])
        a14 = sum(bp[-14:]) / sum(tr_u[-14:])
        a28 = sum(bp[-28:]) / sum(tr_u[-28:])
        uo = 100.0 * (4.0 * a7 + 2.0 * a14 + a28) / 7.0
        sig = "buy" if uo > 70.0 else ("sell" if uo < 30.0 else "neutral")
        osc_items.append({"name": "UltimateOsc(7,14,28)", "value": f"{uo:.2f}", "signal": sig})
    else:
        osc_items.append({"name": "UltimateOsc(7,14,28)", "value": None, "signal": "unavailable"})

    ma_grp = _summarize_votes(ma_items)
    osc_grp = _summarize_votes(osc_items)

    # TradingView Summary = 50% R_MA + 50% R_Osc
    s_ma = float(ma_grp["score"]) if ma_grp["score"] is not None else None
    s_osc = float(osc_grp["score"]) if osc_grp["score"] is not None else None
    if s_ma is not None and s_osc is not None:
        s_all: float | None = 0.5 * (s_ma + s_osc)
    else:
        s_all = s_ma if s_ma is not None else s_osc

    summary_grp = {
        "label": _rating_label(s_all),
        "score": f"{s_all:.3f}" if s_all is not None else None,
        "buy_count": ma_grp["buy_count"] + osc_grp["buy_count"],
        "neutral_count": ma_grp["neutral_count"] + osc_grp["neutral_count"],
        "sell_count": ma_grp["sell_count"] + osc_grp["sell_count"],
        "total_count": ma_grp["total_count"] + osc_grp["total_count"],
        "items": [],
    }
    return {
        "timeframe": timeframe,
        "bar_count": n,
        "last_bar_date": bars[-1].d.isoformat(),
        "summary": summary_grp,
        "moving_averages": ma_grp,
        "oscillators": osc_grp,
    }


# ---------------------------------------------------------------------------------------------------------
# 6 Pivot Point Systems (Traditional / Classic / Fibonacci / Camarilla / Woodie / DeMark)
# ---------------------------------------------------------------------------------------------------------
def _f(v: float | None) -> str | None:
    return f"{v:.2f}" if v is not None and math.isfinite(v) else None


def compute_all_pivot_points(bars: Sequence[SimpleBar], timeframe: str = "1d") -> list[dict[str, str | None]]:
    """Compute all 6 TradingView Pivot Point systems.

    For completed daily bars (``timeframe == '1d'``), uses ``bars[-1]`` to project pivots for
    the next trading session (with Woodie pivot anchored to ``bars[-1].c`` as next-open proxy).
    For active/unfinished higher-timeframe bars (``1w``, ``1mo``, ``1y``), uses the last
    completed prior bar ``bars[-2]`` together with the current period's open ``bars[-1].o``.
    """
    if len(bars) < 2:
        return []
    if timeframe == "1d":
        ref = bars[-1]
        curr_o = ref.c
    else:
        ref = bars[-2]
        curr_o = bars[-1].o
    h, l, c, o = ref.h, ref.l, ref.c, ref.o
    rng = max(0.0, h - l)
    p = (h + l + c) / 3.0

    # Woodie uses current period open (or prior close for un-opened next daily session)
    pw = (h + l + 2.0 * curr_o) / 4.0
    # DeMark conditional X
    if c < o:
        x_dm = h + 2.0 * l + c
    elif c > o:
        x_dm = 2.0 * h + l + c
    else:
        x_dm = h + l + 2.0 * c

    return [
        {
            "method": "traditional",
            "p": _f(p), "r1": _f(2 * p - l), "r2": _f(p + rng), "r3": _f(h + 2 * (p - l)), "r4": _f(h + 3 * (p - l)),
            "s1": _f(2 * p - h), "s2": _f(p - rng), "s3": _f(l - 2 * (h - p)), "s4": _f(l - 3 * (h - p)),
        },
        {
            "method": "classic",
            "p": _f(p), "r1": _f(2 * p - l), "r2": _f(p + rng), "r3": _f(p + 2 * rng), "r4": _f(p + 3 * rng),
            "s1": _f(2 * p - h), "s2": _f(p - rng), "s3": _f(p - 2 * rng), "s4": _f(p - 3 * rng),
        },
        {
            "method": "fibonacci",
            "p": _f(p), "r1": _f(p + 0.382 * rng), "r2": _f(p + 0.618 * rng), "r3": _f(p + rng), "r4": None,
            "s1": _f(p - 0.382 * rng), "s2": _f(p - 0.618 * rng), "s3": _f(p - rng), "s4": None,
        },
        {
            "method": "camarilla",
            "p": _f(p),
            "r1": _f(c + 1.1 * rng / 12.0), "r2": _f(c + 1.1 * rng / 6.0),
            "r3": _f(c + 1.1 * rng / 4.0), "r4": _f(c + 1.1 * rng / 2.0),
            "s1": _f(c - 1.1 * rng / 12.0), "s2": _f(c - 1.1 * rng / 6.0),
            "s3": _f(c - 1.1 * rng / 4.0), "s4": _f(c - 1.1 * rng / 2.0),
        },
        {
            "method": "woodie",
            "p": _f(pw), "r1": _f(2 * pw - l), "r2": _f(pw + rng), "r3": _f(h + 2 * (pw - l)), "r4": _f(pw + 2 * rng + (h - l)),
            "s1": _f(2 * pw - h), "s2": _f(pw - rng), "s3": _f(l - 2 * (h - pw)), "s4": _f(pw - 2 * rng - (h - l)),
        },
        {
            "method": "demark",
            "p": _f(x_dm / 4.0), "r1": _f(x_dm / 2.0 - l), "r2": None, "r3": None, "r4": None,
            "s1": _f(x_dm / 2.0 - h), "s2": None, "s3": None, "s4": None,
        },
    ]


# ---------------------------------------------------------------------------------------------------------
# Volume Profile Visible Range (VPVR) & Chip Distribution (POC / 70% VAH-VAL / Winner Ratio)
# ---------------------------------------------------------------------------------------------------------
def compute_vpvr_profile(
    bars: Sequence[SimpleBar],
    *,
    num_bins: int = 24,
    lookback: int = 120,
    decay_per_bar: float = 0.985,
) -> dict[str, Any]:
    """TradingView exact 70% Value Area greedy two-pointer expansion + A-share chip winner ratio."""
    num_bins = max(4, int(num_bins))
    decay = min(1.0, max(0.5, float(decay_per_bar)))
    sub = list(bars[-lookback:]) if bars else []
    if len(sub) < 5 or sum(b.v for b in sub) <= 0:
        return {
            "available": False, "poc": None, "vah": None, "val": None,
            "winner_ratio": None, "concentration_90": None,
            "lookback_bars": len(sub), "avg_chip_cost": None,
            "hvn_zones": [], "lvn_zones": [], "bins": [],
        }
    p_min = min(b.l for b in sub)
    p_max = max(b.h for b in sub)
    if p_max <= p_min:
        p_max = p_min + 0.01
    step = (p_max - p_min) / num_bins
    bin_vol = [0.0] * num_bins
    bin_up = [0.0] * num_bins

    n_sub = len(sub)
    for idx_b, b in enumerate(sub):
        age = n_sub - 1 - idx_b
        w = decay ** age
        w_vol = max(0.0, b.v) * w
        is_up = b.c >= b.o
        lo_b, hi_b = b.l, b.h
        if hi_b <= lo_b + 1e-6:
            target_idx = min(num_bins - 1, max(0, int((b.c - p_min) / step)))
            bin_vol[target_idx] += w_vol
            if is_up:
                bin_up[target_idx] += w_vol
            continue
        bar_span = hi_b - lo_b
        for idx in range(num_bins):
            b_lo = p_min + idx * step
            b_hi = b_lo + step
            overlap = max(0.0, min(hi_b, b_hi) - max(lo_b, b_lo))
            if overlap > 0:
                alloc = w_vol * (overlap / bar_span)
                bin_vol[idx] += alloc
                if is_up:
                    bin_up[idx] += alloc

    total_vol = sum(bin_vol) or 1.0
    poc_idx = max(range(num_bins), key=lambda i: bin_vol[i])

    # Greedy 70% Value Area expansion around POC
    target_va = 0.70 * total_vol
    va_vol = bin_vol[poc_idx]
    up_ptr, dn_ptr = poc_idx, poc_idx
    while va_vol < target_va and (up_ptr < num_bins - 1 or dn_ptr > 0):
        v_above = bin_vol[up_ptr + 1] if up_ptr + 1 < num_bins else -1.0
        v_below = bin_vol[dn_ptr - 1] if dn_ptr - 1 >= 0 else -1.0
        if v_above >= v_below:
            up_ptr += 1
            va_vol += bin_vol[up_ptr]
        else:
            dn_ptr -= 1
            va_vol += bin_vol[dn_ptr]

    poc_price = p_min + (poc_idx + 0.5) * step
    vah_price = p_min + (up_ptr + 1.0) * step
    val_price = p_min + dn_ptr * step
    avg_chip_cost = sum((p_min + (idx + 0.5) * step) * bin_vol[idx] for idx in range(num_bins)) / total_vol
    curr_c = sub[-1].c

    # Winner chip ratio (proportion of volume below current close) & 90% chip concentration
    win_vol = 0.0
    cum = 0.0
    p05, p95 = p_min, p_max
    hit05, hit95 = False, False
    for idx in range(num_bins):
        b_lo = p_min + idx * step
        b_hi = b_lo + step
        if curr_c >= b_hi:
            win_vol += bin_vol[idx]
        elif curr_c > b_lo:
            win_vol += bin_vol[idx] * ((curr_c - b_lo) / step)
        cum += bin_vol[idx] / total_vol
        if not hit05 and cum >= 0.05:
            p05, hit05 = b_lo, True
        if not hit95 and cum >= 0.95:
            p95, hit95 = b_hi, True
    conc_90 = (p95 - p05) / max(p95 + p05, 0.01)

    avg_bin = total_vol / num_bins
    hvn_zones: list[str] = []
    lvn_zones: list[str] = []
    bins_wire: list[dict[str, Any]] = []
    for idx in range(num_bins):
        b_lo = p_min + idx * step
        b_hi = b_lo + step
        b_mid = 0.5 * (b_lo + b_hi)
        v_s = bin_vol[idx] / total_vol
        u_s = (bin_up[idx] / bin_vol[idx]) if bin_vol[idx] > 0 else 0.5
        is_peak = (idx == 0 or bin_vol[idx] >= bin_vol[idx - 1]) and (idx == num_bins - 1 or bin_vol[idx] >= bin_vol[idx + 1])
        is_trough = (idx == 0 or bin_vol[idx] <= bin_vol[idx - 1]) and (idx == num_bins - 1 or bin_vol[idx] <= bin_vol[idx + 1])

        if idx == poc_idx:
            node_kind = "poc"
        elif is_peak and bin_vol[idx] >= 1.35 * avg_bin:
            node_kind = "hvn"
            hvn_zones.append(f"{b_lo:.2f}~{b_hi:.2f}")
        elif is_trough and bin_vol[idx] <= 0.55 * avg_bin:
            node_kind = "lvn"
            lvn_zones.append(f"{b_lo:.2f}~{b_hi:.2f}")
        else:
            node_kind = "normal"

        bins_wire.append({
            "low": f"{b_lo:.2f}",
            "high": f"{b_hi:.2f}",
            "mid": f"{b_mid:.2f}",
            "volume_share": f"{v_s:.4f}",
            "up_share": f"{u_s:.3f}",
            "is_value_area": dn_ptr <= idx <= up_ptr,
            "is_poc": idx == poc_idx,
            "node_kind": node_kind,
        })

    return {
        "available": True,
        "poc": f"{poc_price:.2f}",
        "vah": f"{vah_price:.2f}",
        "val": f"{val_price:.2f}",
        "winner_ratio": f"{win_vol / total_vol:.3f}",
        "concentration_90": f"{conc_90:.3f}",
        "lookback_bars": n_sub,
        "avg_chip_cost": f"{avg_chip_cost:.2f}",
        "hvn_zones": hvn_zones[:4],
        "lvn_zones": lvn_zones[:4],
        "bins": bins_wire,
    }


# ---------------------------------------------------------------------------------------------------------
# SuperTrend, BB/KC Volatility Squeeze, Anchored VWAP, Divergences & A-Share Volume Patterns
# ---------------------------------------------------------------------------------------------------------
def compute_pro_signals_and_markers(bars: Sequence[SimpleBar]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute advanced TradingView overlays, causal divergences, volume patterns, and chart markers."""
    n = len(bars)
    if n < 20:
        return (
            {
                "supertrend_dir": "unavailable", "supertrend_level": None,
                "volatility_squeeze": "unavailable", "bb_kc_squeeze_on": None,
                "squeeze_momentum": None, "adx_14": None,
                "anchored_vwap_20d": None, "rvol_20d": None,
                "macd_divergence": "none", "rsi_divergence": "none",
                "volume_patterns": [], "atr_14": None,
            },
            [],
        )

    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    vols = [b.v for b in bars]
    atrs = _atr_series(bars, 14)
    atr_now = atrs[-1]
    adx_ser, _, _ = _adx_series(bars, 14)
    adx_now = adx_ser[-1] if adx_ser else None

    # 1. SuperTrend (10, 3.0)
    atrs10 = _atr_series(bars, 10)
    st_dir = "bullish"
    st_lvl = closes[-1]
    final_up = 0.0
    final_dn = 0.0
    raw_markers: list[tuple[int, dict[str, Any]]] = []
    for i in range(10, n):
        a = atrs10[i] or 0.0
        hl2 = 0.5 * (highs[i] + lows[i])
        basic_up = hl2 + 3.0 * a
        basic_dn = hl2 - 3.0 * a
        if i == 10:
            final_up, final_dn = basic_up, basic_dn
            st_dir = "bullish" if closes[i] >= hl2 else "bearish"
            continue
        prev_up, prev_dn, prev_dir = final_up, final_dn, st_dir
        final_up = basic_up if (basic_up < prev_up or closes[i - 1] > prev_up) else prev_up
        final_dn = basic_dn if (basic_dn > prev_dn or closes[i - 1] < prev_dn) else prev_dn
        if prev_dir == "bearish" and closes[i] > final_up:
            st_dir = "bullish"
            if i >= n - 60:
                raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "belowBar", "shape": "arrowUp", "tone": "buy", "text": "SuperTrend转多"}))
        elif prev_dir == "bullish" and closes[i] < final_dn:
            st_dir = "bearish"
            if i >= n - 60:
                raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "aboveBar", "shape": "arrowDown", "tone": "sell", "text": "SuperTrend破位"}))
        st_lvl = final_dn if st_dir == "bullish" else final_up

    # 2. Bollinger Bands (20, 2.0) vs Keltner Channels (20, 1.5) Squeeze & LazyBear Momentum
    atrs20 = _atr_series(bars, 20)
    sq_state = "normal"
    bb_kc_squeeze_on: bool | None = None
    squeeze_mom_str: str | None = None
    if n >= 22 and atrs20[-1] is not None and atrs20[-2] is not None:
        def _in_sq(idx: int) -> bool:
            win = closes[idx - 19 : idx + 1]
            m = sum(win) / 20.0
            sd = math.sqrt(sum((x - m) ** 2 for x in win) / 20.0)
            kc_w = 1.5 * (atrs20[idx] or 0.0)
            return 2.0 * sd <= kc_w

        now_sq = _in_sq(n - 1)
        prev_sq = _in_sq(n - 2)
        bb_kc_squeeze_on = now_sq
        if now_sq:
            sq_state = "squeeze_on"
        elif prev_sq and not now_sq:
            sq_state = "squeeze_fired_up" if closes[-1] > sum(closes[-20:]) / 20.0 else "squeeze_fired_down"
            raw_markers.append((n - 1, {
                "time": bars[-1].d.isoformat(),
                "position": "belowBar" if sq_state == "squeeze_fired_up" else "aboveBar",
                "shape": "circle",
                "tone": "buy" if sq_state == "squeeze_fired_up" else "warn",
                "text": "波动挤压突破",
            }))

    # LazyBear center-deviation linear regression momentum
    if n >= 24:
        dev_vals: list[float] = []
        start_j = max(19, n - 20)
        for j in range(start_j, n):
            hh20 = max(highs[j - 19 : j + 1])
            ll20 = min(lows[j - 19 : j + 1])
            sma20 = sum(closes[j - 19 : j + 1]) / 20.0
            midline = 0.5 * (0.5 * (hh20 + ll20) + sma20)
            dev_vals.append(closes[j] - midline)
        sq_mom = _linreg_last(dev_vals)
        if sq_mom is not None and math.isfinite(sq_mom):
            squeeze_mom_str = f"{sq_mom:.3f}"

    # 3. Anchored VWAP (20-bar)
    anchor_sub = bars[-20:]
    v_sum20 = sum(b.v for b in anchor_sub)
    avwap20 = (sum(((b.h + b.l + b.c) / 3.0) * b.v for b in anchor_sub) / v_sum20) if v_sum20 > 0 else None

    # 4. RVOL 20d
    prev_v19 = sum(vols[-20:-1]) / 19.0 if len(vols) >= 20 else 0.0
    rvol = (vols[-1] / prev_v19) if prev_v19 > 0 else None

    # 5. Causal Multi-Point MACD & RSI Divergence + MACD Crosses + KDJ Crosses (zero lookahead)
    macd_div = "none"
    rsi_div = "none"
    if n >= 40:
        e12 = _ema_series(closes, 12)
        e26 = _ema_series(closes, 26)
        dif = [e12[i] - e26[i] for i in range(n)]
        dea = _ema_series(dif, 9)
        hist = [dif[i] - dea[i] for i in range(n)]
        rsis = _rsi_series(closes, 14)

        last_bot_mark = -99
        last_top_mark = -99
        last_rsi_bot = -99
        last_rsi_top = -99
        scan_start = max(40, n - 60)
        for t in range(scan_start, n):
            rec_lo_i = min(range(t - 7, t + 1), key=lambda k: lows[k])
            pri_lo_i = min(range(t - 31, t - 8), key=lambda k: lows[k])
            if lows[rec_lo_i] < lows[pri_lo_i] * 0.995 and hist[rec_lo_i] > hist[pri_lo_i] and hist[t] > hist[t - 1]:
                if t >= n - 6:
                    macd_div = "bullish_bottom_div"
                if rec_lo_i - last_bot_mark >= 5:
                    last_bot_mark = rec_lo_i
                    raw_markers.append((rec_lo_i, {"time": bars[rec_lo_i].d.isoformat(), "position": "belowBar", "shape": "arrowUp", "tone": "buy", "text": "MACD底背离(E060)"}))

            rec_hi_i = max(range(t - 7, t + 1), key=lambda k: highs[k])
            pri_hi_i = max(range(t - 31, t - 8), key=lambda k: highs[k])
            if highs[rec_hi_i] > highs[pri_hi_i] * 1.005 and hist[rec_hi_i] < hist[pri_hi_i] and hist[t] < hist[t - 1]:
                if t >= n - 6:
                    macd_div = "bearish_top_div"
                if rec_hi_i - last_top_mark >= 5:
                    last_top_mark = rec_hi_i
                    raw_markers.append((rec_hi_i, {"time": bars[rec_hi_i].d.isoformat(), "position": "aboveBar", "shape": "arrowDown", "tone": "sell", "text": "MACD顶背离"}))

            if rsis[rec_lo_i] is not None and rsis[pri_lo_i] is not None:
                if lows[rec_lo_i] < lows[pri_lo_i] * 0.995 and (rsis[rec_lo_i] or 50.0) > (rsis[pri_lo_i] or 50.0) + 2.0:
                    if t >= n - 6:
                        rsi_div = "bullish_bottom_div"
                    if rec_lo_i - last_rsi_bot >= 6:
                        last_rsi_bot = rec_lo_i
                        raw_markers.append((rec_lo_i, {"time": bars[rec_lo_i].d.isoformat(), "position": "belowBar", "shape": "circle", "tone": "buy", "text": "RSI底背离"}))

            if rsis[rec_hi_i] is not None and rsis[pri_hi_i] is not None:
                if highs[rec_hi_i] > highs[pri_hi_i] * 1.005 and (rsis[rec_hi_i] or 50.0) < (rsis[pri_hi_i] or 50.0) - 2.0:
                    if t >= n - 6:
                        rsi_div = "bearish_top_div"
                    if rec_hi_i - last_rsi_top >= 6:
                        last_rsi_top = rec_hi_i
                        raw_markers.append((rec_hi_i, {"time": bars[rec_hi_i].d.isoformat(), "position": "aboveBar", "shape": "circle", "tone": "sell", "text": "RSI顶背离"}))

        # MACD Golden / Death Crosses in recent 45 sessions
        for i in range(max(35, n - 45), n):
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "belowBar", "shape": "arrowUp", "tone": "buy", "text": "MACD金叉"}))
            elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "aboveBar", "shape": "arrowDown", "tone": "sell", "text": "MACD死叉"}))

    # KDJ(9, 3, 3) Low Golden Cross & High Death Cross
    if n >= 20:
        k_val, d_val = 50.0, 50.0
        for i in range(8, n):
            hh9 = max(highs[i - 8 : i + 1])
            ll9 = min(lows[i - 8 : i + 1])
            rsv = 50.0 if hh9 <= ll9 else 100.0 * (closes[i] - ll9) / (hh9 - ll9)
            k_prev, d_prev = k_val, d_val
            k_val = (2.0 * k_prev + rsv) / 3.0
            d_val = (2.0 * d_prev + k_val) / 3.0
            if i >= max(12, n - 45):
                if k_prev <= d_prev and k_val > d_val and k_val < 35.0:
                    raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "belowBar", "shape": "circle", "tone": "buy", "text": "KDJ低位金叉"}))
                elif k_prev >= d_prev and k_val < d_val and k_val > 75.0:
                    raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "aboveBar", "shape": "circle", "tone": "sell", "text": "KDJ高位死叉"}))

    # Volume Breakout markers in recent 45 sessions
    last_bo = -99
    for i in range(max(20, n - 45), n):
        avg_v20 = sum(vols[i - 20 : i]) / 20.0
        prev_hi20 = max(closes[i - 20 : i])
        if avg_v20 > 0 and vols[i] >= 1.8 * avg_v20 and closes[i] > prev_hi20 and closes[i] > bars[i].o:
            if i - last_bo >= 4:
                last_bo = i
                raw_markers.append((i, {"time": bars[i].d.isoformat(), "position": "belowBar", "shape": "arrowUp", "tone": "buy", "text": "放量突破"}))

    # 6. A-Share Volume Structure Patterns
    vol_pats: list[str] = []
    ma5_v = sum(vols[-5:]) / 5.0
    ma20_c = sum(closes[-20:]) / 20.0
    ret1 = (closes[-1] / closes[-2] - 1.0) if closes[-2] > 0 else 0.0
    if vols[-2] > 0 and vols[-1] >= 1.9 * vols[-2] and closes[-1] > bars[-1].o:
        vol_pats.append("倍量阳柱启动 (V>=1.9x昨量)")
        raw_markers.append((n - 1, {"time": bars[-1].d.isoformat(), "position": "belowBar", "shape": "square", "tone": "info", "text": "倍量阳"}))
    if n >= 30 and vols[-1] <= min(vols[-30:]) * 1.05:
        vol_pats.append("30日缩量地量柱 (筹码沉淀)")
    if rvol is not None and rvol >= 2.0 and -0.01 < ret1 < 0.02:
        vol_pats.append("高位放量滞涨 (RVOL>=2.0但涨幅停滞)")
    if ret1 < 0 and vols[-1] < 0.70 * ma5_v and closes[-1] > ma20_c:
        vol_pats.append("20日线上缩量良性回调 (V<0.7xMA5量)")
    if not vol_pats:
        vol_pats.append("常态换手量能结构")

    # Sort chronologically, deduplicate by (time, text), keep at most 12 recent significant markers
    raw_markers.sort(key=lambda pair: pair[0])
    seen_keys: set[tuple[str, str]] = set()
    markers: list[dict[str, Any]] = []
    for _, mk in raw_markers:
        key = (mk["time"], mk["text"])
        if key not in seen_keys:
            seen_keys.add(key)
            markers.append(mk)

    return (
        {
            "supertrend_dir": st_dir,
            "supertrend_level": _f(st_lvl),
            "volatility_squeeze": sq_state,
            "bb_kc_squeeze_on": bb_kc_squeeze_on,
            "squeeze_momentum": squeeze_mom_str,
            "adx_14": _f(adx_now),
            "anchored_vwap_20d": _f(avwap20),
            "rvol_20d": f"{rvol:.2f}" if rvol is not None else None,
            "macd_divergence": macd_div,
            "rsi_divergence": rsi_div,
            "volume_patterns": vol_pats,
            "atr_14": _f(atr_now),
        },
        markers[-12:],
    )
