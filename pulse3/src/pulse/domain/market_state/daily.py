"""Per-session market statistics from raw daily bars (PEX01/PEX09 market state, old-site overview parity).

Why: the overview dashboard, the regime timeline and the sentiment panel all need the same session
facts (limit-ups, broken boards, ladder, promotion, premium, breadth, volume). They are computed once
here, from the unified raw daily bars, so every consumer sees one definition (PEX09: "涨停、炸板、晋级
分母与研究/产品一致").

Definitions (version ``MARKET_STATE_DEFINITIONS``):
  * pct = close / previous close - 1 over the instrument's own previous bar. A move beyond the board's
    limit ratio + 1.1 points cannot be a price move (ex-rights or bad data): the day is *unverifiable* for
    pct statistics and is counted as such, never as 0.
  * limit-up: close >= limit price; limit price = previous close x (1 + ratio) rounded half-up to the tick
    (``PriceLimitRegistry``). broken board: high >= limit price and close < limit price. limit-down alike.
    Instruments whose ratio has no verified rule (e.g. SZSE risk-warning shares) are excluded from limit
    statistics and counted as ``limit_unverifiable``.
  * streak: consecutive limit-up closes in the instrument's own series (a suspended day does not break it).
    The window start truncates streaks that began before it; ``window_sessions`` is reported.
  * promotion rung k: yesterday's frozen limit-up set with streak k is the denominator; members without a
    bar today are ``missing`` and leave the evaluable denominator (never counted as failures).
  * premium: mean of today's close-to-previous-close (and open-to-previous-close) return over yesterday's
    limit-up set (previous bar on the previous session) that traded today.
  * volume: shares are exact; the money turnover is an *estimate* (shares x close) because the raw daily
    bars carry no turnover amount.
Not covered: intraday facts (first seal time, seal amount), IPO no-limit days, adjusted prices.
Pure: no IO. Decimal for prices and ratios.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from pulse.domain.market_rules import Board, Exchange, InstrumentSpec, PriceLimitRegistry, RuleNotVerified

MARKET_STATE_DEFINITIONS = "market-state-defs-v1"
D = Decimal
HUNDRED = D(100)
GUARD_MARGIN = D("0.011")                    # a move beyond ratio + 1.1 points is not a price move
DEFAULT_GUARD_RATIO = D("0.10")
HIST_LABELS = ("<-7", "-7~-5", "-5~-3", "-3~-1", "-1~0", "0", "0~1", "1~3", "3~5", "5~7", ">=7")
MA_WINDOW = 20


@dataclass(frozen=True)
class Bar:
    d: date
    o: Decimal | None
    h: Decimal | None
    l: Decimal | None
    c: Decimal | None
    v: int | None


@dataclass(frozen=True)
class Stock:
    iid: str
    code: str | None
    name: str
    exchange: Exchange
    board: Board
    is_risk_warning: bool

    @property
    def spec(self) -> InstrumentSpec:
        return InstrumentSpec(self.iid, self.exchange, self.board, self.is_risk_warning)


class RatioResolver:
    """Limit ratio per (stock, day) with one registry lookup per rule segment (registry rows are date intervals)."""

    def __init__(self, registry: PriceLimitRegistry):
        self.registry = registry
        self._cuts = sorted({x for r in registry.rules for x in (r.effective_from, r.effective_to) if x is not None})
        self._memo: dict[tuple[str, int], Decimal | None] = {}

    def ratio(self, stock: Stock, day: date) -> Decimal | None:
        key = (stock.iid, bisect_right(self._cuts, day))
        if key not in self._memo:
            try:
                self._memo[key] = self.registry.rule_for(stock.spec, day).ratio
            except RuleNotVerified:
                self._memo[key] = None
        return self._memo[key]


@dataclass
class SessionStats:
    d: date
    observed: int = 0                 # instruments with a bar this session
    pct_valid: int = 0                # previous bar available and the move plausible
    pct_unverifiable: int = 0         # previous bar missing or move beyond the board limit (ex-rights / bad data)
    up: int = 0
    down: int = 0
    flat: int = 0
    pct_sum: Decimal = D(0)
    hist: list[int] = field(default_factory=lambda: [0] * 11)
    limit_verified: int = 0           # pct_valid and a verified ratio
    limit_unverifiable: int = 0
    limit_up: int = 0
    broken: int = 0
    limit_down: int = 0
    ladder: dict[int, int] = field(default_factory=dict)         # streak -> instruments closing limit-up at that streak
    promo: dict[int, list[int]] = field(default_factory=dict)    # yesterday's rung k -> [frozen count, promoted, missing]
    prem_n: int = 0
    prem_close_sum: Decimal = D(0)
    prem_open_n: int = 0
    prem_open_sum: Decimal = D(0)
    volume_shares: int = 0
    volume_n: int = 0
    amount_estimate: Decimal = D(0)
    ma_above: int = 0
    ma_n: int = 0

    @property
    def max_height(self) -> int:
        return max(self.ladder, default=0)


@dataclass(frozen=True)
class InstrumentRow:
    """Per-instrument facts of the final session (ladder members, themes, sector sentiment)."""
    iid: str
    pct: Decimal | None
    close: Decimal | None
    limit_up: bool | None             # None = unverifiable
    broken: bool | None
    limit_down: bool | None
    streak: int
    growth_streak: int
    ratio: Decimal | None
    prev_lu_streak: int = 0           # streak the previous bar closed limit-up at (0 = it did not)
    consecutive: bool = False         # previous bar was on the previous session


@dataclass
class MarketHistory:
    sessions: list[SessionStats]
    last_rows: dict[str, InstrumentRow]
    window_sessions: int
    last_session: date | None


def _pct(close: Decimal, prev: Decimal) -> Decimal:
    return (close / prev - 1) * HUNDRED


def hist_bin(pct: Decimal) -> int:
    """11 left-closed bins: <-7, -7~-5, -5~-3, -3~-1, -1~0, exactly 0, 0~1, 1~3, 3~5, 5~7, >=7."""
    if pct == 0:
        return 5
    if pct < 0:
        for i, e in enumerate((D(-7), D(-5), D(-3), D(-1))):
            if pct < e:
                return i
        return 4
    for i, e in enumerate((D(1), D(3), D(5), D(7))):
        if pct < e:
            return 6 + i
    return 10


def limit_prices(prev_close: Decimal, ratio: Decimal, tick: Decimal = D("0.01")) -> tuple[Decimal, Decimal]:
    up = (prev_close * (1 + ratio) / tick).to_integral_value(ROUND_HALF_UP) * tick
    down = (prev_close * (1 - ratio) / tick).to_integral_value(ROUND_HALF_UP) * tick
    return up, down


def compute_history(stocks: Mapping[str, Stock], series: Mapping[str, Sequence[Bar]], registry: PriceLimitRegistry,
                    sessions: Sequence[date]) -> MarketHistory:
    """Statistics for every session in ``sessions`` from each instrument's own bar series (ascending dates)."""
    sess = sorted(set(sessions))
    if not sess:
        return MarketHistory([], {}, 0, None)
    acc = {d: SessionStats(d) for d in sess}
    prev_session = {d: (sess[i - 1] if i else None) for i, d in enumerate(sess)}
    last = sess[-1]
    resolver = RatioResolver(registry)
    rows: dict[str, InstrumentRow] = {}
    for iid, bars in series.items():
        stock = stocks.get(iid)
        if stock is None:
            continue
        prev: Bar | None = None
        streak = growth = 0
        prev_lu_streak = 0                      # streak when the previous bar closed limit-up, else 0
        closes: list[Decimal] = []
        roll = D(0)
        for b in bars:
            st = acc.get(b.d)
            if st is None or b.c is None or b.c <= 0:
                continue
            st.observed += 1
            ratio = resolver.ratio(stock, b.d)
            consecutive = prev is not None and prev.d == prev_session[b.d]
            pct: Decimal | None = None
            lu = brk = ld = None
            if prev is not None and prev.c is not None and prev.c > 0:
                p = _pct(b.c, prev.c)
                guard = ((ratio if ratio is not None else DEFAULT_GUARD_RATIO) + GUARD_MARGIN) * HUNDRED
                if abs(p) <= guard:
                    pct = p
            if pct is None:
                st.pct_unverifiable += 1
            else:
                st.pct_valid += 1
                st.pct_sum += pct
                st.hist[hist_bin(pct)] += 1
                if pct > 0:
                    st.up += 1
                elif pct < 0:
                    st.down += 1
                else:
                    st.flat += 1
                if ratio is None:
                    st.limit_unverifiable += 1
                else:
                    st.limit_verified += 1
                    up_px, down_px = limit_prices(prev.c, ratio)          # type: ignore[union-attr]
                    lu = b.c >= up_px
                    brk = b.h is not None and b.h >= up_px and b.c < up_px
                    ld = b.c <= down_px
                    st.limit_up += lu
                    st.broken += bool(brk)
                    st.limit_down += ld
                if consecutive and prev_lu_streak > 0:                    # yesterday's frozen limit-up set, traded today
                    st.prem_n += 1
                    st.prem_close_sum += pct
                    if b.o is not None:
                        st.prem_open_n += 1
                        st.prem_open_sum += _pct(b.o, prev.c)             # type: ignore[union-attr]
            # an unknown limit verdict leaves the streak unchanged; a known non-limit close ends it
            new_streak = streak + 1 if lu else 0 if lu is False else streak
            new_growth = growth + 1 if (pct is not None and pct > 0) else 0 if pct is not None else growth
            if consecutive and prev_lu_streak > 0:
                cell = st.promo.setdefault(prev_lu_streak, [0, 0, 0])
                if lu and new_streak == prev_lu_streak + 1:
                    cell[1] += 1
                cell[2] += 1                                              # temporarily: observed today
            if lu:
                st.ladder[new_streak] = st.ladder.get(new_streak, 0) + 1
            if b.v is not None:
                st.volume_shares += b.v
                st.volume_n += 1
                st.amount_estimate += b.c * b.v
            closes.append(b.c)
            roll += b.c
            if len(closes) > MA_WINDOW:
                roll -= closes[-MA_WINDOW - 1]
            if len(closes) >= MA_WINDOW:
                st.ma_n += 1
                st.ma_above += b.c > roll / MA_WINDOW
            if b.d == last:
                rows[iid] = InstrumentRow(iid, pct, b.c, lu, brk, ld, new_streak if lu else 0, new_growth, ratio, prev_lu_streak, consecutive)
            prev_lu_streak = new_streak if lu else 0
            streak, growth = new_streak, new_growth
            prev = b
    # promotion cells: [frozen count of yesterday's rung, promoted today, missing today]
    for d in sess:
        p = prev_session[d]
        if p is None:
            continue
        st = acc[d]
        for k, frozen in acc[p].ladder.items():
            cell = st.promo.setdefault(k, [0, 0, 0])
            observed = cell[2]
            cell[0], cell[2] = frozen, frozen - observed
    return MarketHistory([acc[d] for d in sess], rows, len(sess), last)


# --------------------------------------------------------------------------------------------------------
# derived rates (shared by every consumer)
# --------------------------------------------------------------------------------------------------------
def promotion_rate(cell: Sequence[int]) -> Decimal | None:
    """promoted / (frozen - missing); None when nothing was evaluable. Percent, 4 places."""
    frozen, promoted, missing = cell
    evaluable = frozen - missing
    if evaluable <= 0:
        return None
    return (D(promoted) * HUNDRED / D(evaluable)).quantize(D("0.0001"))


def mean_pct(total: Decimal, n: int) -> Decimal | None:
    return None if n == 0 else (total / n).quantize(D("0.0001"))


def seal_rate(limit_up: int, broken: int) -> Decimal | None:
    """limit-ups / (limit-ups + broken boards), percent; None when no board was attempted."""
    attempted = limit_up + broken
    return None if attempted == 0 else (D(limit_up) * HUNDRED / D(attempted)).quantize(D("0.0001"))
