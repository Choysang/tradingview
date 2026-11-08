"""Market event tape: what changed between two consecutive snapshots.

The feed has no tick stream, so "events" are derived by diffing frames. That is
only meaningful between two frames of the same data source, which is why the
engine never calls `diff` on its first frame or across a SIM/LIVE switch.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

import polars as pl

from contracts.schema import MarketEvent

UNKNOWN_SECTOR = "A股全市场"
SURGE_PCT = 5.0
PLUNGE_PCT = -5.0
REARM_SURGE_PCT = 3.0
REARM_PLUNGE_PCT = -3.0
COOLDOWN_SEQUENCES = 15
TAPE_SIZE = 300
MAX_THEMES = 3

_PREV = "p_"
_NEEDED = (
    "code", "name", "price", "pct_chg", "is_limit_up", "ever_limit_up_today",
    "limit_streak", "open_board_count", "seal_order_amount", "theme_tags", "is_suspended",
)


class SpeedGate:
    """Hysteresis for surge/plunge so a stock hovering at ±5% is reported once.

    A code that fired stays disarmed until its move falls back inside ±3%
    (re-arm), and in any case cannot fire again within the cooldown window.
    """

    def __init__(self, cooldown: int = COOLDOWN_SEQUENCES) -> None:
        self._cooldown = cooldown
        self._disarmed: dict[str, str] = {}  # code -> "surge" | "plunge"
        self._last_fired: dict[str, int] = {}

    def allow(self, code: str, kind: str, sequence: int) -> bool:
        if code in self._disarmed:
            return False
        last = self._last_fired.get(code)
        return last is None or sequence - last >= self._cooldown

    def fire(self, code: str, kind: str, sequence: int) -> None:
        self._disarmed[code] = kind
        self._last_fired[code] = sequence

    def rearm(self, curr: pl.DataFrame, sequence: int) -> None:
        """Release codes whose move has retreated; drop cooldowns that expired."""
        if self._disarmed:
            held = curr.filter(pl.col("code").is_in(list(self._disarmed))).select("code", "pct_chg")
            seen = set()
            for code, pct in held.iter_rows():
                seen.add(code)
                kind = self._disarmed[code]
                retreated = pct is not None and (
                    pct < REARM_SURGE_PCT if kind == "surge" else pct > REARM_PLUNGE_PCT
                )
                if retreated:
                    del self._disarmed[code]
            # A code that left the frame cannot retreat visibly; release it.
            for code in set(self._disarmed) - seen:
                del self._disarmed[code]
        self._last_fired = {
            code: at for code, at in self._last_fired.items() if sequence - at < self._cooldown
        }

    def reset(self) -> None:
        self._disarmed.clear()
        self._last_fired.clear()


def diff(
    prev: pl.DataFrame,
    curr: pl.DataFrame,
    sequence: int,
    clock: str,
    gate: SpeedGate | None = None,
) -> list[MarketEvent]:
    """Events that happened going from `prev` to `curr`, in frame order.

    Suspended names are skipped on both sides: they do not trade, so a flicker
    in a frozen row is not news. `gate` (optional) suppresses repeated
    surge/plunge; without it every threshold crossing is reported.
    """
    if prev.is_empty() or curr.is_empty():
        return []
    if gate is not None:
        gate.rearm(curr, sequence)
    # A new height means "above everything on the previous board", so the
    # reference is the previous snapshot's maximum, not the stock's own history.
    prev_height = int(prev["limit_streak"].max() or 0)
    before = prev.select(
        "code",
        pl.col("is_limit_up").alias(_PREV + "up"),
        pl.col("ever_limit_up_today").alias(_PREV + "ever"),
        pl.col("pct_chg").alias(_PREV + "pct"),
        pl.col("limit_streak").alias(_PREV + "streak"),
        pl.col("is_suspended").alias(_PREV + "suspended"),
    )
    joined = curr.select(*_NEEDED).join(before, on="code", how="inner")
    up, was_up = pl.col("is_limit_up"), pl.col(_PREV + "up")
    pct, was_pct = pl.col("pct_chg"), pl.col(_PREV + "pct")
    streak = pl.col("limit_streak")
    candidates = joined.filter(
        ~pl.col("is_suspended")
        & ~pl.col(_PREV + "suspended")
        & pl.any_horizontal(
            up != was_up,
            ~up & (was_pct < SURGE_PCT) & (pct >= SURGE_PCT),
            (was_pct > PLUNGE_PCT) & (pct <= PLUNGE_PCT),
            pl.lit(prev_height >= 1)
            & (streak > prev_height)
            & (streak > pl.col(_PREV + "streak").fill_null(0)),
        ).fill_null(False)
    )
    events: list[MarketEvent] = []
    for row in candidates.to_dicts():
        events.extend(_row_events(row, prev_height, sequence, clock, gate))
    return events


def _row_events(
    row: dict, prev_height: int, sequence: int, clock: str, gate: SpeedGate | None
) -> Iterable[MarketEvent]:
    price, pct = row["price"], row["pct_chg"]
    if price is None or pct is None:
        return
    streak = row["limit_streak"]
    was_pct = row[_PREV + "pct"]
    now_up, was_up = bool(row["is_limit_up"]), bool(row[_PREV + "up"])

    def make(kind: str, detail: str) -> MarketEvent:
        return MarketEvent(
            id=f"{sequence}-{row['code']}-{kind}",
            sequence=sequence,
            time=clock,
            kind=kind,
            code=row["code"],
            name=row["name"],
            price=float(price),
            pct_chg=round(float(pct), 2),
            limit_streak=int(streak) if streak is not None else None,
            detail=detail,
            themes=_themes(row["theme_tags"]),
        )

    if now_up and not was_up:
        if row[_PREV + "ever"]:
            yield make("reseal", "回封涨停")
        else:
            yield make("seal", _seal_detail(streak, row["seal_order_amount"]))
    elif was_up and not now_up:
        opened = row["open_board_count"]
        yield make("break", f"炸板 · 第 {int(opened)} 次开板" if opened else "炸板")
    code = row["code"]
    if not now_up and was_pct is not None and was_pct < SURGE_PCT <= pct:
        if gate is None or gate.allow(code, "surge", sequence):
            if gate:
                gate.fire(code, "surge", sequence)
            yield make("surge", f"涨幅突破 {SURGE_PCT:.0f}%")
    if was_pct is not None and was_pct > PLUNGE_PCT >= pct:
        if gate is None or gate.allow(code, "plunge", sequence):
            if gate:
                gate.fire(code, "plunge", sequence)
            yield make("plunge", f"跌幅扩大至 {pct:.1f}%")
    previous_streak = row[_PREV + "streak"] or 0
    if prev_height >= 1 and streak is not None and streak > prev_height and streak > previous_streak:
        yield make("new_high", f"晋级 {int(streak)} 板 · 市场新高度")


def _seal_detail(streak: int | None, amount: float | None) -> str:
    parts = ["封涨停"]
    if streak:
        parts.append(f"{streak} 连板" if streak > 1 else "首板")
    if amount is not None and amount > 0:
        parts.append(f"封单 {amount:.2f} 亿")
    return " · ".join(parts)


def _themes(tags: list[str] | None) -> list[str]:
    return [tag for tag in (tags or []) if tag != UNKNOWN_SECTOR][:MAX_THEMES]


class EventTape:
    """Ring buffer of recent events; the caller owns locking."""

    def __init__(self, maxlen: int = TAPE_SIZE) -> None:
        self._items: deque[MarketEvent] = deque(maxlen=maxlen)
        self.gate = SpeedGate()

    def extend(self, events: Iterable[MarketEvent]) -> None:
        self._items.extend(events)

    def clear(self) -> None:
        self._items.clear()
        self.gate.reset()

    def latest(self, since: int | None, limit: int) -> list[MarketEvent]:
        """Newest first. `since` is exclusive: pass the last sequence already seen."""
        out: list[MarketEvent] = []
        for event in reversed(self._items):
            if since is not None and event.sequence <= since:
                break
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def __len__(self) -> int:
        return len(self._items)
