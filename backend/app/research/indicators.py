"""Technical indicators on daily bars, following the Chinese terminal conventions.

Pure Python on float lists: every recursion (EMA, KDJ, Wilder RSI/ATR) is
sequential anyway, and the pattern scanner reuses the same primitives so a
chart line and a signal can never disagree. Warm-up values are None; nothing is
back-filled. EMA/KDJ seeds follow 同花顺/通达信 (EMA from the first close, K/D
from 50), not TA-Lib's SMA seeding, so the lines match what traders see.
"""
from __future__ import annotations

from contracts.schema import IndicatorSeries, OHLCBar

Floats = list[float | None]


def sma(values: list[float], period: int) -> Floats:
    out: Floats = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first value: EMA_t = (2*x + (n-1)*EMA_{t-1}) / (n+1)."""
    out: list[float] = []
    alpha = 2 / (period + 1)
    for value in values:
        out.append(value if not out else alpha * value + (1 - alpha) * out[-1])
    return out


def boll(values: list[float], period: int = 20, width: float = 2.0) -> tuple[Floats, Floats]:
    """Bollinger bands with population standard deviation, as 通达信 STD does."""
    upper: Floats = [None] * len(values)
    lower: Floats = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mean = sum(window) / period
        deviation = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
        upper[index] = mean + width * deviation
        lower[index] = mean - width * deviation
    return upper, lower


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Floats, Floats, Floats]:
    """DIF, DEA and the 2*(DIF-DEA) histogram; blanked until each line is meaningful."""
    dif_full = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    dea_full = ema(dif_full, signal)
    dif: Floats = [v if i >= slow - 1 else None for i, v in enumerate(dif_full)]
    ready = slow - 1 + signal - 1
    dea: Floats = [v if i >= ready else None for i, v in enumerate(dea_full)]
    hist: Floats = [2 * (dif_full[i] - dea_full[i]) if i >= ready else None for i in range(len(closes))]
    return dif, dea, hist


def kdj(
    highs: list[float], lows: list[float], closes: list[float], period: int = 9
) -> tuple[Floats, Floats, Floats]:
    """K=2/3*K'+1/3*RSV, D=2/3*D'+1/3*K, J=3K-2D, both seeded at 50."""
    size = len(closes)
    k_line: Floats = [None] * size
    d_line: Floats = [None] * size
    j_line: Floats = [None] * size
    k, d = 50.0, 50.0
    for index in range(period - 1, size):
        top = max(highs[index - period + 1 : index + 1])
        bottom = min(lows[index - period + 1 : index + 1])
        rsv = 50.0 if top == bottom else (closes[index] - bottom) / (top - bottom) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        k_line[index], d_line[index], j_line[index] = k, d, 3 * k - 2 * d
    return k_line, d_line, j_line


def rsi(closes: list[float], period: int) -> Floats:
    """Wilder smoothing; the first value uses a simple average of `period` changes."""
    out: Floats = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(closes)):
        if index > period:
            avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        if avg_gain == 0 and avg_loss == 0:
            out[index] = 50.0
        elif avg_loss == 0:
            out[index] = 100.0
        else:
            out[index] = 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Floats:
    """Wilder ATR; true range of the first bar is its own high-low."""
    out: Floats = [None] * len(closes)
    if len(closes) < period:
        return out
    ranges = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    value = sum(ranges[:period]) / period
    out[period - 1] = value
    for index in range(period, len(closes)):
        value = (value * (period - 1) + ranges[index]) / period
        out[index] = value
    return out


def _rounded(values: Floats) -> Floats:
    return [None if v is None else round(v, 3) for v in values]


def compute(bars: list[OHLCBar]) -> list[IndicatorSeries]:
    """All chart indicators, each aligned index-for-index with `bars`."""
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    upper, lower = boll(closes)
    dif, dea, hist = macd(closes)
    k_line, d_line, j_line = kdj(highs, lows, closes)

    def line(key: str, label: str, pane: str, values: Floats, style: str = "line") -> IndicatorSeries:
        return IndicatorSeries(key=key, label=label, pane=pane, style=style, values=_rounded(values))

    return [
        *(line(f"ma{n}", f"MA{n}", "price", sma(closes, n)) for n in (5, 10, 20, 60)),
        line("boll_upper", "BOLL 上轨", "price", upper),
        line("boll_lower", "BOLL 下轨", "price", lower),
        line("vol_ma5", "成交量 MA5", "volume", sma(volumes, 5)),
        line("macd_dif", "DIF", "macd", dif),
        line("macd_dea", "DEA", "macd", dea),
        line("macd_hist", "MACD", "macd", hist, style="histogram"),
        line("kdj_k", "K", "kdj", k_line),
        line("kdj_d", "D", "kdj", d_line),
        line("kdj_j", "J", "kdj", j_line),
        line("rsi6", "RSI6", "rsi", rsi(closes, 6)),
        line("rsi14", "RSI14", "rsi", rsi(closes, 14)),
    ]
