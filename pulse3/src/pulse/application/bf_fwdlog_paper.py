"""Inputs of the intraday paper replay (E053-E059): the recorded session, the previous session's bars, the industry context and the calendar catalysts.

Why: ``domain.fwdlog.paper`` is pure; this module loads what it needs point in time. The recorded snapshots come from the imported lake dataset of the
session; daily raw bars come from ``market.bar_projection`` (visible rows only: ``available_at`` <= the cutoff and ``is_final``) for the main board;
the 申万 map is frozen data; calendar events come from the old calendar store (read only) and are linked by the frozen ``event-links-v1`` rules.
Invariants: nothing newer than the cutoff is read; a missing previous session or an empty recording returns a REASON, not a guess; the sector medians are
computed in float64 exactly as the old replay did and rounded to 9 places before they meet a threshold (so float noise never decides a sector dump).
Not covered: the rules (``domain.fwdlog``), publishing (``bf_fwdlog_service``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

import polars as pl
import psycopg

from pulse.application import bindings as B
from pulse.application import bf_fwdlog_inputs as IN
from pulse.application import market_today_service as MT
from pulse.domain.fwdlog import board as BD
from pulse.domain.fwdlog import box5 as BX
from pulse.domain.fwdlog import heat as HT
from pulse.domain.fwdlog import paper as PP
from pulse.domain.fwdlog import triggers as TR
from pulse.domain.fwdlog.common import CST, D, UTC
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code
from pulse.domain.portfolio.forward_book import HybridCalendar

HISTORY_DAYS = 400                          # the 250-session market-state history
SHORT_DAYS = 25                             # the previous session and the close before it
EPS = 1e-6


@dataclass(frozen=True)
class Bars:
    by_code: Mapping[str, Sequence[tuple[date, Decimal | None, Decimal | None, Decimal | None]]]   # (date, high, close, volume shares)
    sessions: tuple[date, ...]                                                                        # observed full sessions
    calendar_days: tuple[date, ...]


def load_bars(conn: psycopg.Connection, as_of: datetime, until: date, since: date) -> Bars:
    """Visible raw daily bars of every main-board instrument in [since, until] (one row per instrument and day: the newest visible revision)."""
    if B.reconstructed(B.AVAILABILITY_POLICY):
        raise RuntimeError("fwdlog reads the observed availability policy only")
    stocks = MT.load_stocks(conn, as_of, B.AVAILABILITY_POLICY)
    code_of = {iid: s.code for iid, s in stocks.items()}
    import uuid as _uuid
    ids = [_uuid.UUID(i) for i in code_of]
    rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id, trade_date) instrument_id, trade_date, high_price_raw h, close_price_raw c, volume_shares v"
        " FROM market.bar_projection WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s AND is_final"
        " ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id", (ids, since, until, as_of), prepare=False).fetchall()
    out: dict[str, list] = {}
    count: dict[date, int] = {}
    for r in rows:
        out.setdefault(code_of[str(r["instrument_id"])], []).append((r["trade_date"], r["h"], r["c"], r["v"]))
        if r["c"] is not None:
            count[r["trade_date"]] = count.get(r["trade_date"], 0) + 1
    for v in out.values():
        v.sort(key=lambda x: x[0])
    sessions: tuple[date, ...] = ()
    if count:
        med = sorted(count.values())[len(count) // 2]
        sessions = tuple(sorted(dd for dd, n in count.items() if n >= 0.5 * med))
    days = [r["trade_date"] for r in conn.execute("SELECT DISTINCT trade_date FROM market.bar_projection WHERE timeframe='1d'")]
    return Bars(out, sessions, tuple(sorted(days)))


def load_hist_cents(conn: psycopg.Connection, as_of: datetime, until: date, since: date) -> dict[str, list[tuple[date, int | None, int | None]]]:
    """Visible raw (high, close) in whole cents of every main-board instrument in [since, until]: the market-state history as integers (about 800 k rows a year)."""
    if B.reconstructed(B.AVAILABILITY_POLICY):
        raise RuntimeError("fwdlog reads the observed availability policy only")
    stocks = MT.load_stocks(conn, as_of, B.AVAILABILITY_POLICY)
    code_of = {iid: s.code for iid, s in stocks.items()}
    import uuid as _uuid
    ids = [_uuid.UUID(i) for i in code_of]
    out: dict[str, list] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (instrument_id, trade_date) instrument_id, trade_date, round(high_price_raw*100)::bigint h, round(close_price_raw*100)::bigint c"
            " FROM market.bar_projection WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s AND is_final"
            " ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id", (ids, since, until, as_of), prepare=False)
        for r in cur:
            out.setdefault(code_of[str(r["instrument_id"])], []).append((r["trade_date"], r["h"], r["c"]))
    return out


def hist_from_bars(bars: "Bars") -> dict[str, list[tuple[date, int | None, int | None]]]:
    """The same history from already loaded bars (tests and local reconciliation)."""
    return {c: [(d, None if h is None else HT.cents(h), None if cl is None else HT.cents(cl)) for d, h, cl, _ in rows] for c, rows in bars.by_code.items()}


def sector_context(frame: pl.DataFrame, sw2: Mapping[str, str]) -> PP.SectorContext:
    """Per full-snapshot known time: sealed names per industry, each industry's median move since the previous full snapshot, the live failed-board rate."""
    full = frame.filter(pl.col("kind") == "full").select(
        "code", (pl.col("poll_at") + pl.duration(seconds=int(IN.KNOWN_LAG_S["full"]))).alias("known_at"),
        pl.col("price").cast(pl.Float64).alias("px"), pl.col("high").cast(pl.Float64).alias("hi"), pl.col("limit_up").cast(pl.Float64).alias("lim"),
        pl.col("ask1_px").cast(pl.Float64).alias("ask")).with_columns(pl.col("code").replace_strict(dict(sw2), default=None).alias("sw2"))
    times, counts, moves, fails = [], [], [], []
    prev = None
    for snap in full.sort("known_at").partition_by("known_at", maintain_order=True):
        sealed = snap.filter((pl.col("px") >= pl.col("lim") - EPS) & (pl.col("ask").fill_null(0) == 0) & pl.col("sw2").is_not_null())
        cnt = dict(sealed.group_by("sw2").len().iter_rows())
        mv: dict[str, Decimal] = {}
        if prev is not None:
            j = snap.join(prev.select("code", pl.col("px").alias("p0")), on="code").filter(pl.col("sw2").is_not_null())
            for k, v in j.group_by("sw2").agg(((pl.col("px") / pl.col("p0") - 1) * 100).median().alias("m")).iter_rows():
                if v is not None and v == v:
                    mv[k] = Decimal(repr(round(float(v), 9)))
        main = snap.filter(pl.col("lim").is_not_null())
        touched = main.filter(pl.col("hi") >= pl.col("lim") - EPS)
        now_sealed = touched.filter((pl.col("px") >= pl.col("lim") - EPS) & (pl.col("ask").fill_null(0) == 0))
        fail = D(touched.height - now_sealed.height) / D(touched.height) if touched.height else None
        times.append(snap["known_at"][0])
        counts.append(cnt)
        moves.append(mv)
        fails.append(fail)
        prev = snap
    return PP.SectorContext(tuple(times), tuple(counts), tuple(moves), tuple(fails))


def _snap(r: Mapping) -> TR.Snap:
    return TR.Snap(r["code"], r["kind"], r["known_at"], r["quote_at"], IN.d(r["price"]), IN.d(r["high"]), IN.d(r["prev_close"]), IN.d(r["open"]), IN.d(r["limit_up"]),
                   IN.d(r["bid1_px"]), IN.d(r["bid1_lots"]), IN.d(r["ask1_px"]), IN.d(r["vwap"]), IN.d(r["volume_lots"]), IN.d(r["amount_yuan"]))


def build(conn: psycopg.Connection, day_frames: IN.DayFrames, frozen: IN.Frozen, events: Sequence[BD.CalEvent], *, as_of: datetime, bars_loader=None, hist_loader=None, recorded: Sequence[date] = ()) -> tuple[PP.PaperInputs | None, str | None, dict]:
    """(inputs, None, notes) or (None, reason, notes). ``as_of`` is the point-in-time cutoff of the bars."""
    day = day_frames.day
    notes: dict = {}
    frame = day_frames.frame
    if frame.is_empty():
        return None, "the recording of the session is empty", notes
    t_open = datetime.combine(day, time(9, 30), CST).astimezone(UTC)
    t_close = datetime.combine(day, time(15, 0), CST).astimezone(UTC)
    bars = (bars_loader or load_bars)(conn, as_of, day - timedelta(days=1), day - timedelta(days=SHORT_DAYS))
    prior = [s for s in bars.sessions if s < day]
    if not prior:
        return None, "no previous session in the visible daily bars", notes
    last = prior[-1]
    gap = [r for r in recorded if last < r < day]
    if gap:
        return None, f"the daily bars of the previous session {gap[-1]} are not visible yet (last visible session {last})", notes
    names = {r["code"]: r["name"] for r in frame.select("code", "name").unique("code", keep="last", maintain_order=True).iter_rows(named=True)}
    # previous session: last close, close before it (within 10 calendar days), amount = volume x close
    prev_rows, prev_amount = {}, {}
    for code, rows in bars.by_code.items():
        if not rows or rows[-1][0] != last:
            continue
        pc = rows[-2][2] if len(rows) > 1 and rows[-2][0] >= last - timedelta(days=10) else None
        prev_rows[code] = (rows[-1][2], pc)
        if rows[-1][3] is not None and rows[-1][2] is not None:
            prev_amount[code] = rows[-1][3] * rows[-1][2]
    book = PP.previous_limit_ups(prev_rows, names)
    hist = (hist_loader or load_hist_cents)(conn, as_of, last, last - timedelta(days=HISTORY_DAYS))
    market = HT.state_at(HT.daily_counts_cents(hist), last)
    # session rows of the book and of every name that sealed at some point (B3 can only fire on a sealed name)
    lag = pl.col("kind").replace_strict({k: int(v * 1000) for k, v in IN.KNOWN_LAG_S.items()}, default=5000, return_dtype=pl.Int64)
    sess = (frame.filter(pl.col("kind").is_in(list(IN.SESSION_KINDS))).with_columns((pl.col("poll_at") + pl.duration(milliseconds=lag)).alias("known_at"))
            .filter((pl.col("known_at") >= t_open) & (pl.col("known_at") <= t_close + timedelta(seconds=10))))
    sealed_codes = sess.filter((pl.col("limit_up").is_not_null()) & (pl.col("price") >= pl.col("limit_up")) & (pl.col("bid1_px") >= pl.col("limit_up"))
                               & pl.col("ask1_px").is_null())["code"].unique().to_list()
    picks = sorted({c for c, _ in book} | set(sealed_codes))
    pick_rows = (sess.filter(pl.col("code").is_in(picks)).unique(subset=["code", "kind", "poll_at"], keep="first", maintain_order=True)
                 .sort(["code", "known_at"], maintain_order=True))
    by_code: dict[str, list[TR.Snap]] = {}
    for r in pick_rows.iter_rows(named=True):
        by_code.setdefault(r["code"], []).append(_snap(r))
    finals = {r["code"]: _snap(r) for r in frame.filter(pl.col("kind") == "auction_final").with_columns((pl.col("poll_at") + pl.duration(seconds=5)).alias("known_at"))
              .iter_rows(named=True)}
    t_final = max((s.known_at for s in finals.values()), default=datetime.combine(day, time(9, 25, 15), CST).astimezone(UTC))
    close_px = {r["code"]: IN.d(r["price"]) for r in frame.filter(pl.col("kind") == "close").iter_rows(named=True)}
    sw2 = frozen.sw2
    linked = BD.link_events(events, frozen.members(day), frozen.link_rules, frozen.skip_categories) if events else None
    minute = {}
    for code, _ in book:
        rows = by_code.get(code)
        if rows:
            minute[code] = BX.day_bars([(s.quote_at, s.price, s.volume_lots) for s in rows if s.quote_at is not None and s.kind in BX.KINDS])
    cal = HybridCalendar(bars.calendar_days or [last])
    notes.update({"previous_session": last.isoformat(), "bars_visible_sessions": len(bars.sessions), "book": len(book), "picked_codes": len(picks),
                  "events_linked": len(linked) if linked else 0})
    inp = PP.PaperInputs(day=day, names=names, book=book, session=by_code, finals=finals, close_px=close_px, prev_amount=prev_amount, market=market,
                         sector=sector_context(frame, sw2), sw2=sw2, events=linked, minute_bars=minute, t_open=t_open,
                         t_close=t_close, t_final=t_final, instrument_id=lambda c: str(instrument_uuid(venue_for_code(c), c)), next_session=cal.next_trading_day(day),
                         book_day=last)
    return inp, None, notes
