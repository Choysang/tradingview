"""Metrics derived from real daily bars.

Only what a bar series can prove: the 60-day high and the number of limit-up
days in the past year. Turnover-based fields need share counts a quote does not
carry, so they stay null rather than being guessed.
"""
from __future__ import annotations

from contracts.schema import OHLCBar

LOOKBACK_HIGH = 60
LIMIT_LOOKBACK = 250
LIMITS = {"10cm": 0.095, "20cm": 0.195, "30cm": 0.295}


def board_of(code: str) -> str:
    if code.startswith(("30", "68")):
        return "20cm"
    if code.startswith(("4", "8", "92")):
        return "30cm"
    return "10cm"


def high_60d(bars: list[OHLCBar]) -> float | None:
    window = bars[-LOOKBACK_HIGH:]
    if not window:
        return None
    return round(max(bar.high for bar in window), 2)


def limit_up_count_1y(bars: list[OHLCBar], code: str) -> int | None:
    """A day counts when its close-to-close gain reaches the board limit."""
    if len(bars) < 2:
        return None
    threshold = LIMITS[board_of(code)]
    window = bars[-(LIMIT_LOOKBACK + 1):] if len(bars) > LIMIT_LOOKBACK else bars
    count = 0
    for previous, bar in zip(window, window[1:]):
        if previous.close <= 0:
            continue
        if bar.close / previous.close - 1 >= threshold:
            count += 1
    return count


def dist_to_60d_high(price: float | None, high: float | None) -> float | None:
    if not price or not high or high <= 0:
        return None
    return round((price / high - 1) * 100, 1)


def derive(code: str, bars: list[OHLCBar], price: float | None = None) -> dict[str, float | int | None]:
    high = high_60d(bars)
    return {
        "high_60d": high,
        "zt_count_1y_bar": limit_up_count_1y(bars, code),
        "dist_to_60d_high": dist_to_60d_high(price, high),
    }
