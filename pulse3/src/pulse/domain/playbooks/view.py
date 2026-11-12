"""InstrumentView: one instrument's aligned daily series plus lazily computed SHARED-kernel features.

Why: screening fields, the 12 pattern rules, the factor lab, analogs and the 52 playbooks must all read the same
definition of MA / MACD / KDJ / BOLL / RSI / ATR / limit-up (FINAL_SYSTEM_SPEC §4.1, FEATURE_STRATEGY_PROTOCOLS: one
indicator, one definition). Every derived array comes from ``pulse.domain.features.indicators`` (the kernels
registered in ``DEFINITIONS``); nothing is re-implemented here except trivial list algebra (lag, scan, percent).
Invariants: arrays are index-aligned with ``dates`` (oldest first); a value is ``None`` when it cannot be computed
(warm-up, a missing input) and is NEVER filled with 0; a view never reads index > i when a rule is evaluated at i
(``truncate`` exists so tests can prove it). ``limit_pct`` is the instrument's price-limit class in percent (10/20/30).
Not covered: ST detection (the research series has no reliable ST flag), turnover rate (the source has none), intraday
paths. Prices are whatever series the caller passes (the research series is forward adjusted: relative rules only).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pulse.domain.features import indicators as ind

Num = "float | None"


def ok(*values: Any) -> bool:
    """True when every value is present (not None)."""
    return all(v is not None for v in values)


def lag(values: Sequence[Any], k: int = 1) -> list[Any]:
    """Value k bars ago (None for the first k positions) - a positive shift only."""
    if k < 0:
        raise ValueError("negative lag would read the future")
    return [None] * min(k, len(values)) + list(values[:max(len(values) - k, 0)])


@dataclass
class InstrumentView:
    instrument_id: str
    limit_pct: float
    dates: tuple[str, ...]
    open: tuple
    high: tuple
    low: tuple
    close: tuple
    volume: tuple
    code: str | None = None
    #: per-date calendar flags from the PANEL calendar (not the instrument's own bars)
    first_of_month: tuple[bool, ...] = ()
    first_of_week: tuple[bool, ...] = ()
    #: cross-sectional columns attached by the panel builder (name -> per-date list); PIT: same-date data only
    xs: dict[str, list] = field(default_factory=dict)
    #: optional per-date float-cap estimate in CNY (needs the cap fact dataset); None => cap rules unavailable
    float_cap: list | None = None
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        n = len(self.dates)
        for name in ("open", "high", "low", "close", "volume"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} length differs from dates")

    @property
    def n(self) -> int:
        return len(self.dates)

    # ---- PIT test helper ------------------------------------------------------------------------------------
    def truncate(self, k: int) -> "InstrumentView":
        """The same instrument as it looked after the first k bars (for no-look-ahead tests)."""
        return InstrumentView(
            self.instrument_id, self.limit_pct, self.dates[:k], self.open[:k], self.high[:k], self.low[:k],
            self.close[:k], self.volume[:k], self.code, self.first_of_month[:k], self.first_of_week[:k],
            {name: col[:k] for name, col in self.xs.items()}, None if self.float_cap is None else self.float_cap[:k])

    def _memo(self, key: Any, build: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def scan(self, fn: Callable[[int], Any]) -> list[float | None]:
        """Per-index score: the truthy numeric result of ``fn(i)`` when positive, else None. ``fn`` only reads index <= i."""
        out: list[float | None] = []
        for i in range(self.n):
            r = fn(i)
            out.append(float(r) if r and not isinstance(r, bool) and r > 0 else (1.0 if r is True else None))
        return out

    # ---- derived arrays (shared kernels) --------------------------------------------------------------------
    def pct(self) -> list[float | None]:
        """Daily change in percent."""
        return self._memo("pct", lambda: [None if r is None else r * 100.0 for r in ind.roc(self.close, 1)])

    def ret(self, n: int) -> list[float | None]:
        """n-bar return in percent."""
        return self._memo(("ret", n), lambda: [None if r is None else r * 100.0 for r in ind.roc(self.close, n)])

    def ma(self, n: int) -> list[float | None]:
        return self._memo(("ma", n), lambda: ind.ma(self.close, n))

    def vol_ma(self, n: int) -> list[float | None]:
        return self._memo(("vma", n), lambda: ind.ma(self.volume, n))

    def turnover_cny(self) -> list[float | None]:
        """close * volume: a CNY turnover PROXY (the source has no amount; the research close is adjusted)."""
        return self._memo("tov", lambda: [None if not ok(c, v) else c * v for c, v in zip(self.close, self.volume)])

    def turnover_ma(self, n: int) -> list[float | None]:
        return self._memo(("tma", n), lambda: ind.ma(self.turnover_cny(), n))

    def macd(self) -> dict[str, list[float | None]]:
        return self._memo("macd", lambda: ind.macd(self.close, 12, 26, 9, "sma_seed", 2))

    def kdj(self) -> dict[str, list[float | None]]:
        return self._memo("kdj", lambda: ind.kdj(self.high, self.low, self.close, 9))

    def boll(self) -> dict[str, list[float | None]]:
        return self._memo("boll", lambda: ind.boll(self.close, 20, 2.0))

    def rsi(self, n: int = 14) -> list[float | None]:
        return self._memo(("rsi", n), lambda: ind.rsi(self.close, n))

    def atr(self, n: int = 14) -> list[float | None]:
        return self._memo(("atr", n), lambda: ind.atr(self.high, self.low, self.close, n))

    def hhv(self, field_name: str, n: int) -> list[float | None]:
        return self._memo(("hhv", field_name, n), lambda: ind.hhv(getattr(self, field_name), n))

    def llv(self, field_name: str, n: int) -> list[float | None]:
        return self._memo(("llv", field_name, n), lambda: ind.llv(getattr(self, field_name), n))

    def prev_hhv(self, field_name: str, n: int) -> list[float | None]:
        """Highest value over the n bars BEFORE today (today excluded)."""
        return self._memo(("phhv", field_name, n), lambda: lag(self.hhv(field_name, n), 1))

    def prev_llv(self, field_name: str, n: int) -> list[float | None]:
        return self._memo(("pllv", field_name, n), lambda: lag(self.llv(field_name, n), 1))

    def ret_std(self, n: int) -> list[float | None]:
        """Std of daily returns (ratio, ddof=1)."""
        return self._memo(("rstd", n), lambda: ind.ret_std(self.close, n))

    def volume_ratio20(self) -> list[float | None]:
        """Volume over the prior-20-bar mean volume (today does not dilute its own baseline)."""
        return self._memo("vr20", lambda: [None if not (ok(v, m) and m > 0) else v / m
                                           for v, m in zip(self.volume, lag(self.vol_ma(20), 1))])

    def volume_ratio5(self) -> list[float | None]:
        return self._memo("vr5", lambda: ind.volume_ratio(self.volume, 5))

    def prev_close(self) -> list[float | None]:
        return self._memo("pc", lambda: lag(self.close, 1))

    # ---- limit-up family (board limit class) ----------------------------------------------------------------
    def limit_up(self) -> list[bool | None]:
        return self._memo("lu", lambda: [None if f is None else f == 1.0 for f in ind.limit_up_flags(self.close, self.limit_pct)])

    def limit_down(self) -> list[bool | None]:
        thr = -(self.limit_pct - 0.5)
        return self._memo("ld", lambda: [None if p is None else p <= thr for p in self.pct()])

    def limit_streak(self) -> list[float | None]:
        return self._memo("ls", lambda: ind.limit_streak(self.close, self.limit_pct))

    def touched_limit(self) -> list[bool | None]:
        thr = self.limit_pct - 0.5
        return self._memo("tl", lambda: [None if not (ok(h, p) and p > 0) else (h / p - 1.0) * 100.0 >= thr
                                         for h, p in zip(self.high, self.prev_close())])

    def one_word(self) -> list[bool | None]:
        """Opens, trades and closes at one price on a limit-up day: cannot be bought."""
        return self._memo("ow", lambda: [None if lu is None else bool(lu and o == h == l_ == c)
                                         for lu, o, h, l_, c in zip(self.limit_up(), self.open, self.high, self.low, self.close)])

    def growth_streak(self) -> list[float | None]:
        return self._memo("gs", lambda: ind.up_streak(self.close))

    def limit_count(self, n: int) -> list[float | None]:
        return self._memo(("lc", n), lambda: ind.limit_count(self.close, self.limit_pct, n))


def calendar_flags(sessions: Sequence[date]) -> tuple[dict[str, bool], dict[str, bool]]:
    """First-trading-day-of-month / of-ISO-week flags keyed by iso date, from the PANEL calendar (sorted sessions)."""
    month: dict[str, bool] = {}
    week: dict[str, bool] = {}
    prev: date | None = None
    for d in sessions:
        month[d.isoformat()] = prev is None or (prev.year, prev.month) != (d.year, d.month)
        week[d.isoformat()] = prev is None or prev.isocalendar()[:2] != d.isocalendar()[:2]
        prev = d
    return month, week


def limit_class_pct(board_code: str) -> float:
    """Price-limit class in percent by board (main 10, chinext/star 20, bse 30)."""
    return {"main": 10.0, "mainboard": 10.0, "chinext": 20.0, "star": 20.0, "bse": 30.0}.get(board_code, 10.0)
