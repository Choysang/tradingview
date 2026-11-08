"""Seeded market scenarios and reproducible synthetic OHLC; never real history.

The simulator is the only data source that works without a market connection.
When a precomputed feature snapshot is supplied, its static per-stock inputs
(previous streak, 60-day high, one-year limit-up statistics) replace simulated
values so the same factor code path is exercised on realistic distributions.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
import random

import polars as pl

from contracts.schema import OHLCBar
from .calendar import has_price_limit
from .universe import LARGE_CAP, universe

CST = timezone(timedelta(hours=8))
SPARK_POINTS = 32


def price_limit(previous_close: float, board: str, is_st: bool = False) -> float:
    ratio = Decimal("0.05") if is_st and board == "10cm" else Decimal(
        {"10cm": ".10", "20cm": ".20", "30cm": ".30"}[board]
    )
    return float(
        (Decimal(str(previous_close)) * (1 + ratio)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    )


LOSS_MAKER_SHARE = 0.08


def valuation(code: str, float_cap: float) -> tuple[float, float, float]:
    """Deterministic (total cap 亿, PE TTM, PB) for a code.

    Uses its own generator keyed by the code so adding fields never shifts the
    scenario random stream the pinned simulator tests depend on. Total cap is
    never below the float cap; about 8% of names are loss-makers (negative PE).
    """
    rng = random.Random(f"valuation-{code}")
    total_cap = round(float_cap * (1 + rng.uniform(0.05, 0.9)), 2)
    if rng.random() < LOSS_MAKER_SHARE:
        pe = -round(rng.uniform(8, 120), 2)
    else:
        pe = round(math.exp(rng.uniform(math.log(6), math.log(90))), 2)
    pb = round(math.exp(rng.uniform(math.log(0.7), math.log(9))), 2)
    return total_cap, pe, pb


class Simulator:
    def __init__(self, seed: int = 20261020, features: dict[str, dict] | None = None) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.rows: list[dict] = []
        self.sequence = 0
        known = features or {}
        # Ladder shape mirrors a real hot session: many first boards, one leader.
        pending = [1] * 6 + [2] * 4 + [3] * 3 + [4, 5, 6, 7]
        self.rng.shuffle(pending)
        for index, identity in enumerate(universe()):
            rng = self.rng
            feature = known.get(identity["code"], {})
            tradable = identity["code"] not in LARGE_CAP
            # Listing age drives the no-limit window; suspension freezes the tape.
            is_new = tradable and index % 37 == 3
            days_since_listing = (index % 5) if is_new else None
            suspended = tradable and index % 89 == 0
            limited = has_price_limit(days_since_listing)
            high_60d = float(feature.get("high_60d") or 0.0)
            previous = (
                round(high_60d * rng.uniform(0.78, 1.0), 2)
                if high_60d > 0
                else round(rng.uniform(6, 95) if index % 13 else rng.uniform(120, 380), 2)
            )
            previous = max(previous, 1.2)
            tier = (
                pending.pop()
                if tradable and pending and index % 11 == 4 and not is_new and not suspended
                else 0
            )
            failed = tradable and not tier and not is_new and not suspended and index % 53 == 5
            upper = price_limit(previous, identity["board"]) if limited else None
            if suspended:
                price = previous
            elif tier:
                price = upper
            elif failed:
                price = min(upper, round(previous * (1 + rng.uniform(2.0, 6.5) / 100), 2))
            elif upper is None:
                price = round(previous * (1 + rng.uniform(-4.0, 12.0) / 100), 2)
            else:
                price = min(upper, round(previous * (1 + rng.uniform(-5.8, 6.2) / 100), 2))
            amount = round(rng.uniform(0.4, 18), 3)
            free_cap = round(rng.uniform(12, 85), 2) if tier else round(rng.uniform(18, 350), 2)
            turnover_yesterday = rng.uniform(2, 19)
            turnover = turnover_yesterday * rng.uniform(0.4, 1.8)
            previous_growth = int(feature.get("prev_growth") or min(7, int(rng.expovariate(0.55))))
            # A ladder slot defines yesterday's streak; a historical feature
            # value must not silently reshape the scenario ladder.
            if tier:
                previous_streak = tier - 1
            elif failed:
                previous_streak = int(feature.get("prev_limit") or ((index % 4) + 1))
            else:
                previous_streak = int(feature.get("prev_limit") or 0)
            change = (price / previous - 1) * 100
            total_cap, pe_ttm, pb = valuation(identity["code"], round(free_cap * 1.7, 2))
            self.rows.append(
                {
                    **identity,
                    "is_st": False,
                    "is_new": is_new,
                    "is_suspended": suspended,
                    "listing_date": (
                        (date.today() - timedelta(days=(days_since_listing or 0) + 1)).isoformat()
                        if is_new
                        else None
                    ),
                    "days_since_listing": days_since_listing,
                    "previous_close": previous,
                    "price": price,
                    "pct_chg": change,
                    "base_pct": change,
                    "limit_price": upper,
                    "amount_today": amount,
                    "float_mkt_cap": round(free_cap * 1.7, 2),
                    "free_float_mkt_cap": free_cap,
                    "total_mkt_cap": total_cap,
                    "pe_ttm": pe_ttm,
                    "pb": pb,
                    "ever_limit_up_today": bool(tier or failed),
                    "prev_limit": previous_streak,
                    "n_days_m_boards_label": "5天3板" if index % 17 == 8 else None,
                    "prev_growth": previous_growth,
                    "prev_cum": float(
                        feature.get("prev_cum") or ((1 + 0.023) ** previous_growth - 1) * 100
                    ),
                    "turnover_today": turnover,
                    "turnover_yesterday": turnover_yesterday,
                    "volume_ratio": rng.uniform(0.6, 3.2),
                    "first_seal_time": f"09:{26 + index % 30:02}:00" if tier else None,
                    "last_seal_time": f"09:{26 + index % 30:02}:00" if tier else None,
                    "open_board_count": (index % 3) if tier else ((index % 3) + 1 if failed else 0),
                    "seal_order_amount": round(amount * rng.uniform(0.04, 0.36), 3) if tier else 0.0,
                    "zt_count_1y": int(feature.get("zt_count_1y") or rng.randint(0, 24)),
                    "seal_success_rate_1y": float(
                        feature.get("seal_success_rate_1y") or rng.uniform(45, 96)
                    ),
                    "max_streak_1y": int(feature.get("max_streak_1y") or max(tier, rng.randint(1, 8))),
                    "high_60d": high_60d if high_60d > 0 else round(price * rng.uniform(0.97, 1.25), 2),
                    "intraday_sparkline": [
                        round(change * (point / (SPARK_POINTS - 1)) + rng.uniform(-0.45, 0.45), 3)
                        for point in range(SPARK_POINTS)
                    ],
                }
            )

    def tick(self) -> pl.DataFrame:
        self.sequence += 1
        for row in self.rows:
            rng = self.rng
            limit_price = row["limit_price"]
            sealed = limit_price is not None and row["price"] >= limit_price
            if row["is_suspended"]:
                # A suspended name has no intraday movement at all.
                row["pct_chg"] = (row["price"] / row["previous_close"] - 1) * 100
                continue
            if self.sequence > 1:
                if sealed and limit_price is not None and rng.random() < 0.006:
                    row["price"] = round(limit_price * 0.987, 2)
                    row["open_board_count"] += 1
                elif not sealed:
                    previous = row["previous_close"]
                    current = (row["price"] / previous - 1) * 100
                    # Mean-reverting walk around the opening change keeps the
                    # session stationary: no drift, no collapse to zero boards.
                    # Names that opened sealed re-seal faster than names that
                    # only approached the ceiling intraday.
                    pull = 0.02
                    if limit_price is not None:
                        ceiling = (limit_price / previous - 1) * 100
                        pull = 0.10 if row["base_pct"] >= ceiling - 0.5 else 0.02
                    step = current + (row["base_pct"] - current) * pull + rng.gauss(0, 0.22)
                    if limit_price is None:
                        # First five sessions after listing: no ceiling, wider band.
                        row["price"] = round(
                            max(0.01, min(previous * 1.44, previous * (1 + step / 100))), 2
                        )
                    elif step >= ceiling - 0.05:
                        row["price"] = limit_price
                    else:
                        floor = max(-ceiling * 0.98, -9.8)
                        row["price"] = round(
                            max(0.01, previous * (1 + max(floor, step) / 100)), 2
                        )
                row["amount_today"] = round(row["amount_today"] + rng.uniform(0.002, 0.025), 3)
                row["turnover_today"] = round(row["turnover_today"] + rng.uniform(0.001, 0.018), 4)
            row["pct_chg"] = (row["price"] / row["previous_close"] - 1) * 100
            if limit_price is not None and row["price"] >= limit_price:
                row["ever_limit_up_today"] = True
                row["seal_order_amount"] = max(
                    0.01, round(row["seal_order_amount"] + rng.uniform(-0.03, 0.035), 3)
                )
                if not sealed:
                    stamp = _session_stamp(self.sequence)
                    row["last_seal_time"] = stamp
                    row["first_seal_time"] = row["first_seal_time"] or stamp
            else:
                row["seal_order_amount"] = 0.0
            row["intraday_sparkline"] = (
                row["intraday_sparkline"] + [round(row["pct_chg"], 3)]
            )[-SPARK_POINTS:]
        return pl.DataFrame(self.rows, infer_schema_length=None)


def _session_stamp(sequence: int) -> str:
    """Map a tick counter onto the trading clock so replay looks like a session."""
    minutes = 30 + sequence * 2
    hour = 9 + minutes // 60
    return f"{hour:02}:{minutes % 60:02}:00"


def _series(
    code: str, end: date, count: int
) -> list[tuple[str, float, float, float, float, float]]:
    """Deterministic weekday OHLC tuples: (date, open, high, low, close, volume)."""
    rng = random.Random(int(code) + 20261020)
    dates: list[date] = []
    day = end - timedelta(days=1)
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day)
        day -= timedelta(days=1)
    dates.reverse()
    price = 12 + int(code) % 8000 / 100
    rows: list[tuple[str, float, float, float, float, float]] = []
    for index, day in enumerate(dates):
        opening = round(price * (1 + rng.uniform(-0.012, 0.012)), 2)
        drift = 0.001 + 0.007 * math.sin(index / 13 + int(code) % 17)
        closing = round(max(0.5, opening * (1 + drift + rng.gauss(0, 0.018))), 2)
        rows.append(
            (
                day.isoformat(),
                opening,
                round(max(opening, closing) * (1 + rng.uniform(0.001, 0.018)), 2),
                round(min(opening, closing) * (1 - rng.uniform(0.001, 0.018)), 2),
                closing,
                float(round(rng.uniform(1_000_000, 20_000_000))),
            )
        )
        price = closing
    return rows


def _scale(rows: list[tuple[str, float, float, float, float, float]], anchor: float | None):
    if not anchor or anchor <= 0 or not rows or rows[-1][4] <= 0:
        return rows, 1.0
    factor = anchor / rows[-1][4]
    return [
        (day, round(o * factor, 2), round(h * factor, 2), round(l * factor, 2), round(c * factor, 2), v)
        for day, o, h, l, c, v in rows
    ], factor


def synthetic_history(
    code: str, end: date, count: int = 260, anchor: float | None = None
) -> list[OHLCBar]:
    """Weekday-only synthetic bars. Explicitly not an exchange calendar.

    When `anchor` is given the series is scaled so the last close equals the
    current snapshot price, keeping the detail chart internally consistent.
    """
    rows, _ = _scale(_series(code, end, count), anchor)
    return [
        OHLCBar(date=day, open=o, high=h, low=l, close=c, volume=v)
        for day, o, h, l, c, v in rows
    ]


def synthetic_highs(
    code: str, end: date, count: int = 260, anchor: float | None = None
) -> list[float]:
    """Highs only. Same series as `synthetic_history`, without model overhead."""
    rows, _ = _scale(_series(code, end, count), anchor)
    return [row[2] for row in rows]


def session_now() -> datetime:
    return datetime.now(CST)
