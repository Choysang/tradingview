"""Tear sheet of an equity series and a trade list (pure: no IO, no clock, no float).

Why: LEGACY_TRIAGE rows #160 / #161. A replay or a paper account is judged by more than its total return: how deep and how long it fell,
how much it earned per unit of risk, how the winners compare with the losers, how often it was invested. This module computes those
figures from a daily NAV series and a trade list; the server computes them, the browser only draws them (BACKFILL_BRIEF "Data rules").
Definitions (version ``TEARSHEET_VERSION``; every figure is a ``Metric``: a value or ``None`` with a reason, never 0 for "unknown"):
  * Series: ``(session, nav)`` in session order, ``nav`` may be ``None`` (a session without a mark). ``initial`` (optional) is the capital
    before the first session; with it the first session has a return. Without it the first valid NAV is the base.
  * Daily return r_t = nav_t / nav_(t-1) - 1, only between two ADJACENT sessions that both have a NAV. A return across a missing mark is
    dropped, never spread over the gap (it would be one multi-day figure counted as one day). ``missing_nav_sessions`` counts the gaps.
  * total_return = last valid NAV / base - 1.  annualised_return = (1 + total_return) ** (ppy / span) - 1, ``span`` = sessions elapsed from
    the base to the last valid NAV (the gaps count as elapsed), ``ppy`` = 252 sessions a year by default.
  * annualised_volatility = sample standard deviation (n - 1) of the daily returns * sqrt(ppy); needs at least 2 returns.
  * sharpe = mean(r) / sample std(r) * sqrt(ppy), risk-free rate 0; ``None`` when the standard deviation is 0.
  * sortino = mean(r) / DD * sqrt(ppy) with the downside deviation DD = sqrt(sum(min(r, 0) ** 2) / n), POPULATION form over all n returns
    (target 0: a return above 0 contributes 0, the divisor stays n); ``None`` when no return is below 0.
  * max_drawdown = min(nav / running peak - 1) <= 0, the running peak starts at the base.  calmar = annualised_return / |max_drawdown|;
    ``None`` when there was no drawdown.
  * longest_drawdown_sessions = the longest stretch (in sessions) from a peak to the first session whose NAV is back at or above that peak,
    or to the last valid NAV when it never recovered (then ``recovered`` is False and the figure is a lower bound).
  * Trades: only CLOSED trades (a ``pnl``) count; an open trade is counted separately and carries no result. A trade is a win when
    pnl > 0, a loss when pnl < 0, a break-even when pnl == 0 (in the win-rate denominator, in neither side of the other ratios).
    profit_factor = gross wins / |gross losses| (``None`` without a loss); win_rate = wins / closed trades; avg_win / avg_loss = mean pnl
    of the wins / of the losses (avg_loss is negative); payoff_ratio = avg_win / |avg_loss|. ``avg_win_return`` / ``avg_loss_return`` are
    the same averages of the trades' own return ratios when given. pnl is whatever basis the caller supplies (the replay: quantity x
    (exit - entry) price, gross of fees; the fees are inside the NAV).
  * exposure = share of the series' sessions on which at least one trade is held at the close: entry_day <= session < exit_day (the exit
    fills at the open of its exit day; an open trade is held to the end). ``None`` when no trade list exists.
Invariants: Decimal arithmetic at precision 50 (ROUND_HALF_EVEN); the input is never mutated; the result depends on nothing but the inputs.
Monthly returns (``monthly_returns``, LEGACY_TRIAGE #35) are a separate function below; ``compute`` and every figure above are unchanged by it.
Not covered: benchmark-relative figures (alpha, beta, information ratio), per-year tables, fees per trade. Short samples are NOT hidden
(``sessions`` and ``returns`` say how many points support a figure; reading a 10-session Sharpe as evidence is the reader's error).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

TEARSHEET_VERSION = "tearsheet-v1"
PERIODS_PER_YEAR = 252
D = Decimal

METRIC_NAMES = ("total_return", "annualised_return", "annualised_volatility", "sharpe", "sortino", "max_drawdown", "calmar",
                "longest_drawdown_sessions", "profit_factor", "win_rate", "avg_win", "avg_loss", "payoff_ratio", "avg_win_return",
                "avg_loss_return", "exposure")


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    reason: str | None = None


@dataclass(frozen=True)
class TradeIn:
    """One trade. ``pnl`` None = still open. ``ret`` is the trade's own return ratio (optional)."""
    entry_day: str
    exit_day: str | None
    pnl: Decimal | None
    ret: Decimal | None = None


@dataclass(frozen=True)
class DrawdownSpan:
    sessions: int
    peak_session: str | None            # None = the initial capital before the first session
    recovery_session: str | None        # None = not recovered by the last valid NAV
    recovered: bool


@dataclass(frozen=True)
class TearSheet:
    version: str
    periods_per_year: int
    sessions: int                       # sessions in the series
    returns: int                        # daily returns that support the volatility figures
    missing_nav_sessions: int
    first_session: str | None
    last_session: str | None
    trades_closed: int | None           # None = no trade list
    trades_open: int | None
    trades_breakeven: int | None
    longest_drawdown: DrawdownSpan | None
    metrics: dict[str, Metric] = field(default_factory=dict)


def _missing(reason: str) -> Metric:
    return Metric(None, reason)


def compute(nav: Sequence[tuple[str, Decimal | None]], trades: Sequence[TradeIn] | None = None, *, initial: Decimal | None = None,
            periods_per_year: int = PERIODS_PER_YEAR) -> TearSheet:
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be positive")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        return _compute(list(nav), None if trades is None else list(trades), initial, periods_per_year)


def _compute(nav: list[tuple[str, Decimal | None]], trades: list[TradeIn] | None, initial: Decimal | None, ppy: int) -> TearSheet:
    # position -1 is the base capital (only when ``initial`` is given); positions 0.. are the series' sessions
    pts: list[tuple[int, Decimal]] = [] if initial is None else [(-1, initial)]
    pts += [(i, n) for i, (_, n) in enumerate(nav) if n is not None]
    sessions = [d for d, _ in nav]

    def label(p: int) -> str | None:
        return None if p < 0 else sessions[p]

    missing_nav = sum(1 for _, n in nav if n is None)
    rets = [pts[k][1] / pts[k - 1][1] - 1 for k in range(1, len(pts)) if pts[k][0] - pts[k - 1][0] == 1 and pts[k - 1][1] > 0]
    m: dict[str, Metric] = {}
    span_dd: DrawdownSpan | None = None

    # ---- return and risk ------------------------------------------------------------------------------------------------------
    if not pts:
        for k in ("total_return", "annualised_return", "max_drawdown", "calmar", "longest_drawdown_sessions"):
            m[k] = _missing("no_nav")
    elif len(pts) == 1:
        for k in ("total_return", "annualised_return"):
            m[k] = _missing("single_nav_point")
        m["max_drawdown"] = _missing("single_nav_point")
        m["longest_drawdown_sessions"] = _missing("single_nav_point")
    else:
        base_pos, base = pts[0]
        last_pos, last = pts[-1]
        if base <= 0:
            m["total_return"] = m["annualised_return"] = _missing("base_nav_not_positive")
        else:
            tr = last / base - 1
            m["total_return"] = Metric(tr)
            if last <= 0:
                m["annualised_return"] = _missing("final_nav_not_positive")
            else:
                m["annualised_return"] = Metric((D(1) + tr) ** (D(ppy) / D(last_pos - base_pos)) - 1)
        m["max_drawdown"], span_dd = _drawdown(pts, label)
        m["longest_drawdown_sessions"] = Metric(D(span_dd.sessions)) if span_dd is not None else m["max_drawdown"]
    ann = m.get("annualised_return", _missing("no_nav"))
    dd = m["max_drawdown"]
    if ann.value is None:
        m["calmar"] = _missing(ann.reason or "no_nav")
    elif dd.value is None:
        m["calmar"] = _missing(dd.reason or "no_nav")
    elif dd.value == 0:
        m["calmar"] = _missing("no_drawdown")
    else:
        m["calmar"] = Metric(ann.value / abs(dd.value))

    root = D(ppy).sqrt()
    n = len(rets)
    if n < 2:
        m["annualised_volatility"] = m["sharpe"] = _missing("fewer_than_2_returns")
    else:
        mean = sum(rets, D(0)) / n
        std = (sum(((r - mean) ** 2 for r in rets), D(0)) / (n - 1)).sqrt()
        m["annualised_volatility"] = Metric(std * root)
        m["sharpe"] = _missing("zero_volatility") if std == 0 else Metric(mean / std * root)
    if n < 1:
        m["sortino"] = _missing("no_returns")
    else:
        mean = sum(rets, D(0)) / n
        down = (sum((min(r, D(0)) ** 2 for r in rets), D(0)) / n).sqrt()
        m["sortino"] = _missing("no_downside") if down == 0 else Metric(mean / down * root)

    # ---- trades ---------------------------------------------------------------------------------------------------------------
    closed = [t for t in trades if t.pnl is not None] if trades is not None else None
    n_open = n_be = None
    if trades is None or closed is None:
        for k in ("profit_factor", "win_rate", "avg_win", "avg_loss", "payoff_ratio", "avg_win_return", "avg_loss_return", "exposure"):
            m[k] = _missing("trade_list_unavailable")
    else:
        wins = [t for t in closed if t.pnl is not None and t.pnl > 0]
        losses = [t for t in closed if t.pnl is not None and t.pnl < 0]
        n_be = len(closed) - len(wins) - len(losses)
        n_open = len(trades) - len(closed)
        gw = sum((t.pnl for t in wins if t.pnl is not None), D(0))
        gl = sum((t.pnl for t in losses if t.pnl is not None), D(0))
        m["win_rate"] = Metric(D(len(wins)) / len(closed)) if closed else _missing("no_closed_trades")
        m["profit_factor"] = (_missing("no_closed_trades") if not closed else _missing("no_losses") if not losses else Metric(gw / abs(gl)))
        aw = gw / len(wins) if wins else None
        al = gl / len(losses) if losses else None
        m["avg_win"] = Metric(aw) if aw is not None else _missing("no_wins")
        m["avg_loss"] = Metric(al) if al is not None else _missing("no_losses")
        m["payoff_ratio"] = Metric(aw / abs(al)) if aw is not None and al is not None else _missing("no_wins" if aw is None else "no_losses")
        for key, side, why in (("avg_win_return", wins, "no_wins"), ("avg_loss_return", losses, "no_losses")):
            rr = [t.ret for t in side if t.ret is not None]
            m[key] = Metric(sum(rr, D(0)) / len(rr)) if rr else _missing(why)
        if not sessions:
            m["exposure"] = _missing("no_nav")
        else:
            held = sum(1 for s in sessions if any(t.entry_day <= s and (t.exit_day is None or s < t.exit_day) for t in trades))
            m["exposure"] = Metric(D(held) / len(sessions))
    return TearSheet(TEARSHEET_VERSION, ppy, len(sessions), n, missing_nav, sessions[0] if sessions else None, sessions[-1] if sessions else None,
                     None if closed is None else len(closed), n_open, n_be, span_dd, {k: m[k] for k in METRIC_NAMES})


def _drawdown(pts: list[tuple[int, Decimal]], label: Callable[[int], str | None]) -> tuple[Metric, DrawdownSpan]:
    """Max drawdown and the longest peak-to-recovery stretch over the valid points (position -1 = base capital)."""
    peak_pos, peak = pts[0]
    worst = D(0)
    best: DrawdownSpan | None = None
    in_dd = False
    for pos, v in pts[1:]:
        if v >= peak:
            if in_dd:
                best = _longer(best, DrawdownSpan(pos - peak_pos, label(peak_pos), label(pos), True))
            peak_pos, peak, in_dd = pos, v, False
            continue
        in_dd = True
        if peak > 0:
            worst = min(worst, v / peak - 1)
    if in_dd:
        best = _longer(best, DrawdownSpan(pts[-1][0] - peak_pos, label(peak_pos), None, False))
    if best is None:                                                  # never below a peak: the longest stretch is 0 sessions
        best = DrawdownSpan(0, label(peak_pos), label(peak_pos), True)
    return (Metric(worst) if pts[0][1] > 0 else _missing("base_nav_not_positive")), best


def _longer(a: DrawdownSpan | None, b: DrawdownSpan) -> DrawdownSpan:
    return b if a is None or b.sessions > a.sessions else a


# ---- monthly returns (LEGACY_TRIAGE #35: the monthly heatmap of the old strategy backtest) ---------------------------------------------------
@dataclass(frozen=True)
class MonthReturn:
    year: int
    month: int
    value: Decimal | None               # None = no valid NAV in the month (nothing to compare)
    sessions: int                       # sessions of the series that fall in the month (a short first / last month is visible here)
    reason: str | None = None


def monthly_returns(nav: Sequence[tuple[str, Decimal | None]], initial: Decimal | None = None) -> list[MonthReturn]:
    """Calendar-month returns of a daily NAV series (sessions are ISO ``YYYY-MM-DD`` strings in session order).

    month return = last valid NAV of the month / last valid NAV before the month - 1. The base of the FIRST month is ``initial`` (the capital before
    the first session) or, without it, the first valid NAV of the series (then that month starts one session late: its ``sessions`` says how many).
    A month without any valid NAV has ``value`` None with a reason (never 0); a later month is then measured against the last valid NAV before it.
    A base that is not positive gives None. Pure Decimal; does not change ``compute`` or any tear-sheet figure."""
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
        months: dict[tuple[int, int], list[Decimal | None]] = {}
        for day, v in nav:
            months.setdefault((int(day[:4]), int(day[5:7])), []).append(v)
        out: list[MonthReturn] = []
        base: Decimal | None = initial
        for (y, m), vals in months.items():
            valid = [v for v in vals if v is not None]
            if not valid:
                out.append(MonthReturn(y, m, None, len(vals), "no_nav_in_month"))
                continue
            last = valid[-1]
            start = base if base is not None else valid[0]
            if start <= 0:
                out.append(MonthReturn(y, m, None, len(vals), "base_nav_not_positive"))
            elif base is None and len(valid) < 2:
                out.append(MonthReturn(y, m, None, len(vals), "single_nav_point"))
            else:
                out.append(MonthReturn(y, m, last / start - 1, len(vals)))
            base = last
        return out
