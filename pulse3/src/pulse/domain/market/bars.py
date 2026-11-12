"""Canonical bars, session coverage and multi-period aggregation (spec FINAL §4.1, M1-E05).

Why: execution uses raw prices; research uses versioned adjusted series. Bars
carry *interval* volume, never cumulative snapshots. Suspension, source gap and
a genuine zero-volume bar are three different facts. Weekly..yearly bars are
aggregated over trading sessions and stay ``is_final=False`` until the calendar
proves the period ended and every session is accounted for.

Invariants: no minute is forward-filled; a missing component makes the aggregate
field NULL instead of a partial sum; derived ``available_at`` = max(inputs,
computed_at); aggregation never mixes adjustment bases.
Not covered: persistence, source fetching, tick-level reconstruction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Sequence

from .calendar import TradingCalendar, session_close_utc, session_open_utc, CST

SESSION_SCOPE_REGULAR = "regular_continuous_v1"
PRESENT_OBSERVED, PRESENT_TRUE_ZERO, PRESENT_SUSPENDED, PRESENT_MISSING = "observed", "true_zero", "suspended", "missing"


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    timeframe: str
    session_scope: str
    trade_date: date
    bar_open_time: datetime
    bar_close_time: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume_shares: int | None
    turnover_cny: Decimal | None
    available_at: datetime
    is_final: bool = False
    quality_status: str = "valid"
    exchange_reference_price: Decimal | None = None
    adjustment_mode: str = "raw"
    adjustment_version: str | None = None
    issues: tuple[str, ...] = ()
    estimated: bool = False

    @property
    def is_true_zero(self) -> bool:
        return self.volume_shares == 0 and self.turnover_cny is not None and self.turnover_cny == 0


def minute_grid(d: date) -> list[datetime]:
    """Expected 1m bar *close* instants of one regular session (09:31..11:30, 13:01..15:00), UTC."""
    out: list[datetime] = []
    for start, end in (((9, 30), (11, 30)), ((13, 0), (15, 0))):
        t = datetime(d.year, d.month, d.day, *start, tzinfo=CST)
        stop = datetime(d.year, d.month, d.day, *end, tzinfo=CST)
        while t < stop:
            t += timedelta(minutes=1)
            out.append(t.astimezone(timezone.utc))
    return out


def classify_sessions(expected: Iterable[date], bars: Iterable[Bar],
                      suspended: Iterable[date] = ()) -> dict[date, str]:
    """Per expected session: observed | true_zero | suspended | missing.

    A suspended session with no bar is ``suspended``; with a zero-volume bar
    ``true_zero``; a session without bar and without suspension evidence is a
    source gap ``missing``. Missing is never silently turned into zero.
    """
    by_date = {b.trade_date: b for b in bars}
    susp = set(suspended)
    out: dict[date, str] = {}
    for d in expected:
        b = by_date.get(d)
        if b is not None:
            out[d] = PRESENT_TRUE_ZERO if b.is_true_zero else PRESENT_OBSERVED
        elif d in susp:
            out[d] = PRESENT_SUSPENDED
        else:
            out[d] = PRESENT_MISSING
    return out


def window_report(expected: Sequence[date], bars: Sequence[Bar], suspended: Iterable[date] = ()) -> dict:
    """Completeness of a requested window; empty and incomplete are different outcomes."""
    status = classify_sessions(expected, bars, suspended)
    missing = [d.isoformat() for d, s in status.items() if s == PRESENT_MISSING]
    return {
        "expected_count": len(expected), "observed_count": sum(1 for s in status.values() if s in (PRESENT_OBSERVED, PRESENT_TRUE_ZERO)),
        "suspended_count": sum(1 for s in status.values() if s == PRESENT_SUSPENDED),
        "missing_dates": missing, "window_complete": not missing and len(expected) > 0,
        "empty": not any(s in (PRESENT_OBSERVED, PRESENT_TRUE_ZERO) for s in status.values()),
    }


@dataclass(frozen=True)
class CumulativePoint:
    """One cumulative snapshot of a session (volume in shares, amount in CNY)."""
    at: datetime
    cum_volume_shares: int | None
    cum_amount_cny: Decimal | None
    price: Decimal | None = None


def cumulative_to_interval(points: Sequence[CumulativePoint], grid: Sequence[datetime]) -> list[dict]:
    """Difference cumulative snapshots into per-minute interval volume/amount.

    A grid minute without a snapshot is reported ``missing`` (not filled). The
    first snapshot after a gap spans several minutes, so its interval cannot be
    attributed to one minute: it is ``gap_span`` with NULL interval values.
    A decreasing cumulative value (reset/revision) is ``cumulative_decreased``.
    """
    by_time = {p.at: p for p in points}
    out: list[dict] = []
    prev: CumulativePoint | None = None
    prev_idx = -1
    for i, t in enumerate(grid):
        p = by_time.get(t)
        if p is None:
            out.append({"at": t, "status": "missing", "volume_shares": None, "turnover_cny": None, "price": None})
            continue
        status, vol, amt = "ok", None, None
        if prev is None:
            if i == 0:
                vol, amt = p.cum_volume_shares, p.cum_amount_cny
            else:
                status = "gap_span"
        elif prev_idx != i - 1:
            status = "gap_span"
        elif p.cum_volume_shares is None or prev.cum_volume_shares is None:
            status = "missing_cumulative"
        else:
            dv = p.cum_volume_shares - prev.cum_volume_shares
            if dv < 0 or (p.cum_amount_cny is not None and prev.cum_amount_cny is not None and p.cum_amount_cny < prev.cum_amount_cny):
                status = "cumulative_decreased"
            else:
                vol = dv
                if p.cum_amount_cny is not None and prev.cum_amount_cny is not None:
                    amt = p.cum_amount_cny - prev.cum_amount_cny
        out.append({"at": t, "status": status, "volume_shares": vol, "turnover_cny": amt, "price": p.price})
        prev, prev_idx = p, i
    return out


def sampled_approx_bars(instrument_id: str, points: Sequence[CumulativePoint], interval: dict[str, int],
                        available_at: datetime) -> list[Bar]:
    """Build sampled-approximation bars from price snapshots; always ``estimated=True``.

    Only prices observed at sample instants are known; the true high/low of the
    interval is unobservable, so the approximation uses the observed extremes and
    carries ``sampled_approximation`` so no consumer mistakes it for real OHLC.
    """
    step = timedelta(seconds=interval["seconds"])
    buckets: dict[datetime, list[CumulativePoint]] = {}
    for p in points:
        if p.price is None:
            continue
        ts = p.at.timestamp()
        k = datetime.fromtimestamp(ts - ts % step.total_seconds() + step.total_seconds(), tz=timezone.utc)
        buckets.setdefault(k, []).append(p)
    bars = []
    for close_at in sorted(buckets):
        ps = sorted(buckets[close_at], key=lambda x: x.at)
        prices = [x.price for x in ps]
        bars.append(Bar(
            instrument_id=instrument_id, timeframe=f"{interval['seconds']}s", session_scope=SESSION_SCOPE_REGULAR,
            trade_date=close_at.astimezone(CST).date(), bar_open_time=close_at - step, bar_close_time=close_at,
            open=prices[0], high=max(prices), low=min(prices), close=prices[-1], volume_shares=None, turnover_cny=None,
            available_at=available_at, is_final=False, quality_status="partial",
            issues=("sampled_approximation", f"samples={len(ps)}"), estimated=True))
    return bars


def _sum_or_none(values: list) -> object | None:
    return None if any(v is None for v in values) else sum(values)


def aggregate(daily: Sequence[Bar], calendar: TradingCalendar, timeframe: str, *,
              suspended: Iterable[date] = (), computed_at: datetime) -> list[Bar]:
    """Aggregate session bars into 1w/1mo/1q/1y bars over the trading calendar.

    ``is_final`` requires: calendar proves the period ended, every session is
    observed/true-zero/suspended, and every input bar is final. Otherwise the
    bar is provisional (``is_final=False``). Missing sessions make the bar
    ``partial``. Volume/amount are NULL if any component is NULL.
    """
    if timeframe == "1d":
        return list(daily)
    if not daily:
        return []
    modes = {b.adjustment_mode for b in daily}
    if len(modes) != 1:
        raise ValueError("cannot aggregate bars with mixed adjustment bases")
    ids = {b.instrument_id for b in daily}
    if len(ids) != 1:
        raise ValueError("aggregate one instrument at a time")
    susp = set(suspended)
    groups: dict[tuple[date, date], list[Bar]] = {}
    for b in sorted(daily, key=lambda x: x.trade_date):
        groups.setdefault(calendar.period_key(b.trade_date, timeframe), []).append(b)
    out: list[Bar] = []
    for (first, last), bs in sorted(groups.items()):
        sessions = calendar.between(first, last)
        status = classify_sessions(sessions, bs, susp)
        missing = [d for d, s in status.items() if s == PRESENT_MISSING]
        observed = [b for b in bs if b.trade_date in status]
        if not observed:
            continue
        prices_ok = all(b.open is not None and b.high is not None and b.low is not None and b.close is not None for b in observed)
        vol = _sum_or_none([b.volume_shares for b in observed])
        amt = _sum_or_none([b.turnover_cny for b in observed])
        complete = calendar.period_is_complete(first, timeframe) and not missing and all(b.is_final for b in observed)
        issues = []
        if missing:
            issues.append("missing_sessions:" + ",".join(d.isoformat() for d in missing))
        if not calendar.period_is_complete(first, timeframe):
            issues.append("period_not_complete")
        issues.extend(sorted({i for b in observed for i in b.issues}))
        quality = "valid" if prices_ok and vol is not None and amt is not None and not missing else "partial"
        out.append(Bar(
            instrument_id=bs[0].instrument_id, timeframe=timeframe, session_scope=bs[0].session_scope,
            trade_date=observed[-1].trade_date, bar_open_time=session_open_utc(observed[0].trade_date),
            bar_close_time=session_close_utc(observed[-1].trade_date),
            open=observed[0].open, high=max((b.high for b in observed if b.high is not None), default=None),
            low=min((b.low for b in observed if b.low is not None), default=None), close=observed[-1].close,
            volume_shares=vol, turnover_cny=amt,
            available_at=max([b.available_at for b in observed] + [computed_at]),
            is_final=complete, quality_status=quality, adjustment_mode=bs[0].adjustment_mode, adjustment_version=bs[0].adjustment_version,
            issues=tuple(issues), estimated=any(b.estimated for b in observed),
            exchange_reference_price=None))
    return out


def with_finality(bar: Bar, *, evidence_ok: bool) -> Bar:
    """Final only with evidence and only once the session close has passed at availability."""
    ok = evidence_ok and bar.available_at >= bar.bar_close_time
    return replace(bar, is_final=ok)
