"""Panel bodies of the market overview (PEX01 ``pulse.market_today``, PEX09 ``pulse.market_sentiment``).

Why: each old-site overview block becomes one *panel*: a self-contained, versioned JSON body that the API
hands out behind a ``TypedFeature`` artifact reference and the page only draws (the client never recomputes).
Panels: thermometer, vitals, ladder, seal_clock, breadth, themes, growth_strip, regime.
Invariants: counts are exact integers; rates are percent strings produced by ``daily``; an unknown value is
``None`` with a reason, never 0; ``seal_clock`` is an explicit capability gap because first-seal times need
intraday data the platform has not ingested; theme strength is the old site's
``2 x limit-ups + growth-streak>=3 members + 0.5 x average change`` (versioned, uncalibrated).
Not covered: the intraday mood curve, per-theme sparklines.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import mood as M
from .daily import (HIST_LABELS, MARKET_STATE_DEFINITIONS, InstrumentRow, MarketHistory, SessionStats, Stock, mean_pct,
                    promotion_rate, seal_rate)

D = Decimal
PANELS = ("thermometer", "vitals", "ladder", "seal_clock", "breadth", "themes", "growth_strip", "regime")
THEME_STRENGTH_VERSION = "theme-strength-v1-old-site-uncalibrated"
GROWTH_BUCKETS = 7
MEMBERS_PER_RUNG = 10
TOP_THEMES = 8


def _who(stocks: Mapping[str, Stock], iid: str) -> dict[str, Any]:
    s = stocks[iid]
    return {"instrument_id": iid, "code": s.code, "name": s.name}


def _q4(x: Decimal | None) -> Decimal | None:
    return None if x is None else x.quantize(D("0.0001"))


def subset_stats(d: date, rows: Sequence[InstrumentRow]) -> SessionStats:
    """SessionStats of the final session restricted to ``rows`` (a sector scope). Promotion missing is not countable here."""
    st = SessionStats(d)
    st.observed = len(rows)
    for r in rows:
        if r.pct is None:
            st.pct_unverifiable += 1
            continue
        st.pct_valid += 1
        st.pct_sum += r.pct
        st.up += r.pct > 0
        st.down += r.pct < 0
        st.flat += r.pct == 0
        if r.limit_up is None:
            st.limit_unverifiable += 1
            continue
        st.limit_verified += 1
        st.limit_up += bool(r.limit_up)
        st.broken += bool(r.broken)
        st.limit_down += bool(r.limit_down)
        if r.limit_up:
            st.ladder[r.streak] = st.ladder.get(r.streak, 0) + 1
        if r.consecutive and r.prev_lu_streak > 0:
            cell = st.promo.setdefault(r.prev_lu_streak, [0, 0, 0])
            cell[0] += 1
            cell[1] += bool(r.limit_up and r.streak == r.prev_lu_streak + 1)
            if r.pct is not None:
                st.prem_n += 1
                st.prem_close_sum += r.pct
    return st


def coverage_of(st: SessionStats, expected: int | None) -> dict[str, Any]:
    return {"expected_instruments": expected, "observed": st.observed, "pct_valid": st.pct_valid, "pct_unverifiable": st.pct_unverifiable,
            "limit_verified": st.limit_verified, "limit_unverifiable": st.limit_unverifiable,
            "missing": None if expected is None else max(expected - st.observed, 0)}


def thermometer(h: MarketHistory, curve_days: int = 60) -> dict[str, Any]:
    cur = h.sessions[-1]
    body = M.score(cur)
    curve = []
    for st in h.sessions[-curve_days:]:
        sc = M.score(st)["score"]
        curve.append({"date": st.d, "score": sc})
    tail = [c["score"] for c in curve[-6:] if c["score"] is not None]
    delta = (tail[-1] - tail[0]) if len(tail) >= 3 else None
    return {**body, "session": cur.d, "history": curve, "window_note": f"history truncated at {h.window_sessions} loaded sessions",
            "trend_delta_5d": delta, "trend_word": None if delta is None else "走强" if delta > D("1.5") else "走弱" if delta < D("-1.5") else "持平",
            "definitions": MARKET_STATE_DEFINITIONS}


def vitals(h: MarketHistory, stocks: Mapping[str, Stock], expected: int) -> dict[str, Any]:
    cur = h.sessions[-1]
    prior = [s.volume_shares for s in h.sessions[-21:-1] if s.volume_n]
    ratio = None
    if cur.volume_n and len(prior) == 20:
        ratio = (D(cur.volume_shares) / (D(sum(prior)) / 20)).quantize(D("0.0001"))
    top = sorted((r for r in h.last_rows.values() if r.limit_up), key=lambda r: (-r.streak, r.iid))
    prem = mean_pct(cur.prem_close_sum, cur.prem_n)
    return {
        "session": cur.d, "coverage": coverage_of(cur, expected),
        "limit_up": cur.limit_up if cur.limit_verified else None, "broken": cur.broken if cur.limit_verified else None,
        "limit_down": cur.limit_down if cur.limit_verified else None, "seal_rate_pct": seal_rate(cur.limit_up, cur.broken),
        "max_height": cur.max_height if cur.limit_verified else None,
        "max_height_stocks": [_who(stocks, r.iid) for r in top if r.streak == cur.max_height][:5] if cur.max_height else [],
        "premium": {"close_pct": mean_pct(cur.prem_close_sum, cur.prem_n), "open_pct": mean_pct(cur.prem_open_sum, cur.prem_open_n),
                    "sample_count": cur.prem_n, "definition": "yesterday limit-up set (previous bar on the previous session), traded today"},
        "up": cur.up, "down": cur.down, "flat": cur.flat,
        "volume": {"shares": cur.volume_shares if cur.volume_n else None, "instruments": cur.volume_n,
                   "ratio_vs_20d_mean": ratio,
                   "amount_estimate_cny": cur.amount_estimate.quantize(D("1")) if cur.volume_n else None,
                   "amount_is_estimate": True, "amount_method": "sum(volume shares x close); daily bars carry no turnover amount"},
        "limit_definition": MARKET_STATE_DEFINITIONS,
        "mean_pct": mean_pct(cur.pct_sum, cur.pct_valid), "premium_value_note": None if prem is not None else "no limit-up set yesterday or none traded today",
    }


def ladder(h: MarketHistory, stocks: Mapping[str, Stock]) -> dict[str, Any]:
    cur = h.sessions[-1]
    rungs = []
    top = max(cur.max_height, 1)
    for k in range(top, 0, -1):
        sealed = sorted((r for r in h.last_rows.values() if r.limit_up and r.streak == k), key=lambda r: r.iid)
        broken = sorted((r for r in h.last_rows.values() if r.broken and r.prev_lu_streak == k - 1), key=lambda r: r.iid)
        cell = cur.promo.get(k - 1) if k > 1 else None
        rungs.append({
            "streak": k, "label": "首板" if k == 1 else f"{k}连板", "sealed_count": len(sealed), "broken_count": len(broken),
            "promotion": None if cell is None else {"from_streak": k - 1, "frozen_count": cell[0], "promoted": cell[1], "missing": cell[2],
                                                    "rate_pct": promotion_rate(cell)},
            "sealed": [{**_who(stocks, r.iid), "pct": _q4(r.pct)} for r in sealed[:MEMBERS_PER_RUNG]],
            "broken": [{**_who(stocks, r.iid), "pct": _q4(r.pct)} for r in broken[:MEMBERS_PER_RUNG]],
        })
    return {"session": cur.d, "max_height": cur.max_height, "rungs": rungs,
            "note": "streak = consecutive limit-up closes since the loaded window start; promotion denominators are yesterday's frozen set"}


def seal_clock(h: MarketHistory) -> dict[str, Any]:
    cur = h.sessions[-1]
    return {"session": cur.d, "available": False, "reason_code": "NO_INTRADAY_SEAL_TIMES",
            "reason": "first-seal times need intraday quotes or minute bars; the platform has only daily bars and sparse quote snapshots",
            "sealed_today": cur.limit_up if cur.limit_verified else None, "broken_today": cur.broken if cur.limit_verified else None}


def breadth(h: MarketHistory) -> dict[str, Any]:
    cur = h.sessions[-1]
    return {"session": cur.d, "bins": [{"label": lab, "count": n} for lab, n in zip(HIST_LABELS, cur.hist)],
            "up": cur.up, "down": cur.down, "flat": cur.flat, "pct_valid": cur.pct_valid, "pct_unverifiable": cur.pct_unverifiable,
            "above_ma20": {"count": cur.ma_above, "of": cur.ma_n, "pct": None if cur.ma_n == 0 else (D(cur.ma_above) * 100 / D(cur.ma_n)).quantize(D("0.01"))}}


def themes(h: MarketHistory, stocks: Mapping[str, Stock], names: Mapping[str, str], members: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    cur = h.sessions[-1]
    out = []
    for node, ids in members.items():
        rows = [h.last_rows[i] for i in ids if i in h.last_rows]
        valid = [r for r in rows if r.pct is not None]
        if len(valid) < 3:
            continue
        lu = sum(1 for r in rows if r.limit_up)
        g3 = sum(1 for r in rows if r.growth_streak >= 3)
        avg = sum((r.pct for r in valid), D(0)) / len(valid)       # type: ignore[misc]
        leader = max(rows, key=lambda r: (r.streak, r.pct if r.pct is not None else D(-1000), r.iid))
        out.append({"node_id": node, "name": names.get(node, node), "members": len(ids), "observed": len(rows), "limit_up": lu,
                    "growth_streak_ge3": g3, "avg_pct": avg.quantize(D("0.01")), "strength": (D(2 * lu + g3) + avg * D("0.5")).quantize(D("0.01")),
                    "leader": _who(stocks, leader.iid)})
    out.sort(key=lambda t: (-t["strength"], t["node_id"]))
    return {"session": cur.d, "version": THEME_STRENGTH_VERSION, "calibrated": False,
            "formula": "2 x limit_up + count(growth_streak>=3) + 0.5 x avg_pct", "themes": out[:TOP_THEMES], "ranked": len(out)}


def growth_strip(h: MarketHistory, stocks: Mapping[str, Stock]) -> dict[str, Any]:
    cur = h.sessions[-1]
    buckets = []
    for k in range(1, GROWTH_BUCKETS + 1):
        sel = sorted((r for r in h.last_rows.values() if (r.growth_streak == k if k < GROWTH_BUCKETS else r.growth_streak >= k)),
                     key=lambda r: (-(r.pct or D(-1000)), r.iid))
        buckets.append({"streak": k, "label": f"{k}连阳" if k < GROWTH_BUCKETS else f"{k}连阳+", "count": len(sel),
                        "top": [{**_who(stocks, r.iid), "growth_streak": r.growth_streak, "pct": _q4(r.pct)} for r in sel[:6]] if k >= 3 else []})
    return {"session": cur.d, "buckets": buckets, "ge3_total": sum(b["count"] for b in buckets if b["streak"] >= 3),
            "definition": "consecutive sessions with close above the previous close; an unverifiable day leaves the streak unchanged"}
