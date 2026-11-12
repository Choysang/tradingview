"""Technical indicator kernels with fixed windows, warm-up and null policy (spec FINAL §4.1/§5).

Why: MA/EMA/MACD/RSI/ATR/volume-ratio/relative-strength must be one
implementation for charts, ranking, plans and backtests.

Policy (definition_version ``v1``, numeric policy ``float64-v1``):
* inputs are sequences of ``float | None`` ordered by time; outputs have the same
  length; positions inside the warm-up are ``None``;
* a ``None`` inside a window yields ``None`` at that position (never skipped,
  never zero-filled); recursive indicators (EMA, Wilder RSI/ATR) RESET their
  state after a ``None`` and re-warm, so a gap cannot be bridged silently;
* EMA alpha = 2/(n+1); seed ``sma_seed`` (first value at index n-1 is the SMA of
  the first n inputs) or ``first_value`` (first valid input);
* MACD = EMA(fast) - EMA(slow); signal = EMA(MACD, signal); histogram =
  (MACD - signal) * ``hist_multiplier`` (1 or 2); first valid index of MACD is
  slow-1 and of signal slow+signal-2; callers may require
  ``min_completed_bars >= slow + signal - 1``.
* RSI / ATR use Wilder smoothing seeded by the SMA of the first n values;
  ATR true range needs the previous close so its first value is at index n.
Not covered: Decimal-exact variants, intraday session resets.
"""
from __future__ import annotations

from typing import Sequence

Series = Sequence["float | None"]


def ma(values: Series, n: int) -> list[float | None]:
    if n < 1:
        raise ValueError("n must be >= 1")
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        out[i] = sum(w) / n  # type: ignore[arg-type]
    return out


def ema(values: Series, n: int, seed: str = "sma_seed") -> list[float | None]:
    if n < 1:
        raise ValueError("n must be >= 1")
    if seed not in ("sma_seed", "first_value"):
        raise ValueError("seed must be sma_seed or first_value")
    alpha = 2.0 / (n + 1)
    out: list[float | None] = [None] * len(values)
    run = 0
    prev: float | None = None
    for i, v in enumerate(values):
        if v is None:
            run, prev = 0, None
            continue
        run += 1
        if prev is None:
            if seed == "first_value":
                prev = v
                out[i] = prev
            elif run >= n:
                w = values[i - n + 1:i + 1]
                prev = sum(w) / n  # type: ignore[arg-type]
                out[i] = prev
        else:
            prev = prev + alpha * (v - prev)
            out[i] = prev
    return out


def macd(close: Series, fast: int = 12, slow: int = 26, signal: int = 9, ema_seed: str = "sma_seed",
         hist_multiplier: int = 1) -> dict[str, list[float | None]]:
    if fast >= slow:
        raise ValueError("fast must be < slow")
    if hist_multiplier not in (1, 2):
        raise ValueError("hist_multiplier must be 1 or 2")
    f, s = ema(close, fast, ema_seed), ema(close, slow, ema_seed)
    dif = [None if a is None or b is None else a - b for a, b in zip(f, s)]
    dea = ema(dif, signal, ema_seed)
    hist = [None if a is None or b is None else (a - b) * hist_multiplier for a, b in zip(dif, dea)]
    return {"macd": dif, "signal": dea, "hist": hist}


def _wilder(values: Series, n: int, start: int) -> list[float | None]:
    """Wilder smoothing from ``start`` index with an SMA seed of n valid values; resets on None."""
    out: list[float | None] = [None] * len(values)
    prev: float | None = None
    run = 0
    for i in range(start, len(values)):
        v = values[i]
        if v is None:
            prev, run = None, 0
            continue
        run += 1
        if prev is None:
            if run >= n:
                w = values[i - n + 1:i + 1]
                if any(x is None for x in w):
                    continue
                prev = sum(w) / n  # type: ignore[arg-type]
                out[i] = prev
        else:
            prev = (prev * (n - 1) + v) / n
            out[i] = prev
    return out


def rsi(close: Series, n: int = 14) -> list[float | None]:
    gains: list[float | None] = [None] * len(close)
    losses: list[float | None] = [None] * len(close)
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        if a is None or b is None:
            continue
        d = b - a
        gains[i], losses[i] = max(d, 0.0), max(-d, 0.0)
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    out: list[float | None] = [None] * len(close)
    for i, (g, l) in enumerate(zip(ag, al)):
        if g is None or l is None:
            continue
        out[i] = 100.0 if l == 0 and g > 0 else (50.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l))
    return out


def true_range(high: Series, low: Series, close: Series) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    for i in range(1, len(close)):
        h, l, pc = high[i], low[i], close[i - 1]
        if h is None or l is None or pc is None:
            continue
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def atr(high: Series, low: Series, close: Series, n: int = 14) -> list[float | None]:
    return _wilder(true_range(high, low, close), n, 1)


def volume_ratio(volume: Series, n: int = 5) -> list[float | None]:
    """volume_t / mean(volume over the n sessions BEFORE t); None when that mean is 0 or any input is None."""
    out: list[float | None] = [None] * len(volume)
    for i in range(n, len(volume)):
        w, v = volume[i - n:i], volume[i]
        if v is None or any(x is None for x in w):
            continue
        mean = sum(w) / n  # type: ignore[arg-type]
        if mean > 0:
            out[i] = v / mean
    return out


def relative_strength(close: Series, benchmark: Series, n: int = 20) -> list[float | None]:
    """(close_t/close_{t-n}) / (bench_t/bench_{t-n}); series must be aligned by position (same dates)."""
    if len(close) != len(benchmark):
        raise ValueError("close and benchmark must be aligned to the same dates")
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        c0, c1, b0, b1 = close[i - n], close[i], benchmark[i - n], benchmark[i]
        if None in (c0, c1, b0, b1) or c0 == 0 or b0 == 0 or b1 == 0:
            continue
        out[i] = (c1 / c0) / (b1 / b0)  # type: ignore[operator]
    return out


# ---- ext group B kernels (BOLL, KDJ and the rolling helpers the playbook/pattern/screen layers share) ------------
# definition_version ``v1``; same null policy as above: a None inside a window gives None, recursions reset.
def boll(close: Series, n: int = 20, k: float = 2.0) -> dict[str, list[float | None]]:
    """Bollinger bands: mid = SMA(n); upper/lower = mid +- k * population standard deviation (as 通达信 STD)."""
    if n < 2:
        raise ValueError("n must be >= 2")
    mid: list[float | None] = [None] * len(close)
    up: list[float | None] = [None] * len(close)
    lo: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        m = sum(w) / n  # type: ignore[arg-type]
        sd = (sum((v - m) ** 2 for v in w) / n) ** 0.5  # type: ignore[operator]
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return {"mid": mid, "upper": up, "lower": lo}


def kdj(high: Series, low: Series, close: Series, n: int = 9) -> dict[str, list[float | None]]:
    """KDJ(n,3,3): RSV over n bars; K=2/3*K'+1/3*RSV, D=2/3*D'+1/3*K, J=3K-2D; K and D are seeded at 50 on the
    first valid bar (index n-1) and the recursion RESETS to that seed after any missing input."""
    size = len(close)
    kk: list[float | None] = [None] * size
    dd: list[float | None] = [None] * size
    jj: list[float | None] = [None] * size
    k = d = 50.0
    for i in range(n - 1, size):
        hw, lw, c = high[i - n + 1:i + 1], low[i - n + 1:i + 1], close[i]
        if c is None or any(v is None for v in hw) or any(v is None for v in lw):
            k = d = 50.0
            continue
        top, bottom = max(hw), min(lw)  # type: ignore[type-var]
        rsv = 50.0 if top == bottom else (c - bottom) / (top - bottom) * 100.0
        k = 2.0 / 3.0 * k + rsv / 3.0
        d = 2.0 / 3.0 * d + k / 3.0
        kk[i], dd[i], jj[i] = k, d, 3.0 * k - 2.0 * d
    return {"k": kk, "d": dd, "j": jj}


def roc(values: Series, n: int) -> list[float | None]:
    """Simple return over n bars: v_t / v_{t-n} - 1 (None when either is missing or the base is not positive)."""
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        a, b = values[i - n], values[i]
        if a is None or b is None or a <= 0:
            continue
        out[i] = b / a - 1.0
    return out


def hhv(values: Series, n: int) -> list[float | None]:
    """Highest value of the last n bars including the current one."""
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        if all(v is not None for v in w):
            out[i] = max(w)  # type: ignore[type-var]
    return out


def llv(values: Series, n: int) -> list[float | None]:
    """Lowest value of the last n bars including the current one."""
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        if all(v is not None for v in w):
            out[i] = min(w)  # type: ignore[type-var]
    return out


def ret_std(close: Series, n: int) -> list[float | None]:
    """Sample standard deviation (ddof=1) of the n simple daily returns ending at t."""
    r = roc(close, 1)
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        w = r[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        m = sum(w) / n  # type: ignore[arg-type]
        out[i] = (sum((v - m) ** 2 for v in w) / (n - 1)) ** 0.5  # type: ignore[operator]
    return out


def up_streak(close: Series) -> list[float | None]:
    """Consecutive up-closes ending at t (close_t > close_{t-1}); 0 on a flat/down close; None before data."""
    out: list[float | None] = [None] * len(close)
    run = 0
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        if a is None or b is None:
            run = 0
            continue
        run = run + 1 if b > a else 0
        out[i] = float(run)
    return out


def limit_up_flags(close: Series, limit_pct: float) -> list[float | None]:
    """1.0 when the close rose by at least ``limit_pct - 0.5`` percent (the board limit less a 0.5pp rounding /
    adjustment tolerance), else 0.0; None without a previous close. Forward-adjusted series keep the ratio, so this
    is usable on them."""
    out: list[float | None] = [None] * len(close)
    thr = limit_pct - 0.5
    for i in range(1, len(close)):
        a, b = close[i - 1], close[i]
        if a is None or b is None or a <= 0:
            continue
        out[i] = 1.0 if (b / a - 1.0) * 100.0 >= thr else 0.0
    return out


def limit_streak(close: Series, limit_pct: float) -> list[float | None]:
    """Consecutive limit-up closes ending at t (see :func:`limit_up_flags`)."""
    flags = limit_up_flags(close, limit_pct)
    out: list[float | None] = [None] * len(close)
    run = 0
    for i, f in enumerate(flags):
        if f is None:
            run = 0
            continue
        run = run + 1 if f == 1.0 else 0
        out[i] = float(run)
    return out


def limit_count(close: Series, limit_pct: float, n: int) -> list[float | None]:
    """Number of limit-up closes among the last n bars (None while any flag in the window is missing)."""
    flags = limit_up_flags(close, limit_pct)
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        w = flags[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        out[i] = float(sum(w))  # type: ignore[arg-type]
    return out
