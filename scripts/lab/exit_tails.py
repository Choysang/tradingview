"""E060 part 1: controlled exit test. One fixed set of entries per source, only the exit differs.

Entries (signal at D's close, bought at the D+1 open, inside sameday_base's pool):
- third_buy: 缠论三买, point-in-time (p0_patterns.third_buys_pit); third_buy_e050: the E050 full-series version,
  which drops real-time signals whose bottom was later replaced (look-ahead), kept only for comparison.
- ma_pullback: MA20 rising (above its value 5 bars ago), close > MA20 and MA60, low <= 1.01 * MA10 and
  close >= MA10, volume < 0.7 * 20-day mean volume; first day of a run only.
- main_book: the top 4 fusion_static40_safe scores of the day (the frozen main book takes 4 new names a day).
Initial stop S: 0.99 x the lowest low of D-9..D (just outside the support); entries with D+1 open <= S skipped.
Exits (all pay a 0.45% round trip):
- fixed_5 / fixed_10 / fixed_20: close of D+h.
- trail_ma10 / trail_ma5: before the first close above the entry price, exit at S when the low reaches it (at the
  open if it gaps below) or at the close of the 20th session; from that first profitable close on there is no
  time cap: a close below MA_n sells at the next open; S stays where it was (never moved).
- david_ma10: TP1 = entry + 1.5R (R = entry - S): half sells at TP1 when the high reaches it (at the open if it
  gaps above). From the next session the rest has a stop at the entry price (moved only after profit) and
  sells at the next open after a close below MA10. Before TP1 the same S / 20-session rule as trail_ma10.
- david_fib: as david_ma10, but the rest sells at the 1.618 extension of the prior swing (H = highest high of the
  60 bars before D+1, L = lowest low of the 20 bars before D+1; target L + 1.618 (H - L), at least entry + 3R)
  or at the entry-price stop; no MA exit.
A position still open at the last bar is closed at the last close (counted, flagged).
Metrics: n, win rate, mean net %, realized payoff (mean win / |mean loss|), share of the net profit from the top
10% of trades, skew, mean sessions held, and the max drawdown of an additive equity curve (each trade adds
net% / M on its exit date, M = mean number of open positions).
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe exit_tails.py
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import polars as pl

import harness as H
import sameday_base as B
from harness import CODE, c

OUT = H.LAB / "exit_tails.json"
COST = 0.45
CAP = 20  # sessions before the first profitable close / before TP1
EXITS = ("fixed_5", "fixed_10", "fixed_20", "trail_ma10", "trail_ma5", "david_ma10", "david_fib")


class Series:
    """Per-code numpy arrays of the panel, rows in date order."""

    def __init__(self, panel: pl.DataFrame):
        p = panel.sort(CODE, "date")
        self.idx: dict[str, tuple[int, int]] = {}
        codes = p[CODE].to_numpy()
        starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
        ends = np.r_[starts[1:], len(codes)]
        for s, e in zip(starts, ends):
            self.idx[codes[s]] = (s, e)
        self.date = p["date"].to_list()
        for k in ("open", "high", "low", "close", "ma5", "ma10"):
            setattr(self, k, p[k].to_numpy().astype(float))
        self.pos = {(cd, d): i for cd, (s, e) in self.idx.items() for i, d in zip(range(s, e), self.date[s:e])}


def _stop_fill(o: float, s: float) -> float:
    return min(o, s)


def run_exit(S: Series, i: int, end: int, stop: float, kind: str, target: float | None = None) -> tuple[float, int, bool]:
    """Trade from entry row i (bought at open[i]) to at most row end-1. Returns (gross % return, sessions, closed_at_end)."""
    o, h, lo, cl = S.open, S.high, S.low, S.close
    entry = o[i]
    if kind.startswith("fixed_"):
        n = int(kind.split("_")[1])
        j = i + n - 1
        if j >= end:
            return (cl[end - 1] / entry - 1) * 100, end - i, True
        return (cl[j] / entry - 1) * 100, n, False
    ma = S.ma5 if kind == "trail_ma5" else S.ma10
    R = entry - stop
    tp1 = entry + 1.5 * R
    half: float | None = None  # price of the first half when TP1 filled
    be_from = None  # row from which the breakeven stop applies
    profitable = False
    for j in range(i, end):
        if half is None:
            if lo[j] <= stop:
                px = _stop_fill(o[j], stop) if j > i else stop
                return (px / entry - 1) * 100, j - i + 1, False
            if kind.startswith("david") and h[j] >= tp1:
                half = max(o[j], tp1) if j > i else tp1
                be_from = j + 1
                continue
            if kind.startswith("trail"):
                if profitable and cl[j] < ma[j]:
                    px = o[j + 1] if j + 1 < end else cl[j]
                    return (px / entry - 1) * 100, j - i + 2, j + 1 >= end
                profitable = profitable or cl[j] > entry
                if not profitable and j - i + 1 >= CAP:
                    return (cl[j] / entry - 1) * 100, j - i + 1, False
            elif j - i + 1 >= CAP:
                return (cl[j] / entry - 1) * 100, j - i + 1, False
            continue
        # second half after TP1
        rest = None
        if j >= be_from and lo[j] <= entry:
            rest = _stop_fill(o[j], entry)
        elif kind == "david_fib" and target is not None and h[j] >= target:
            rest = max(o[j], target)
        elif kind == "david_ma10" and cl[j] < ma[j]:
            rest = o[j + 1] if j + 1 < end else cl[j]
            if j + 1 >= end:
                return ((0.5 * half + 0.5 * rest) / entry - 1) * 100, j - i + 1, True
            return ((0.5 * half + 0.5 * rest) / entry - 1) * 100, j - i + 2, False
        if rest is not None:
            return ((0.5 * half + 0.5 * rest) / entry - 1) * 100, j - i + 1, False
    px = cl[end - 1]
    gross = ((0.5 * half + 0.5 * px) / entry - 1) * 100 if half is not None else (px / entry - 1) * 100
    return gross, end - i, True


def trades(S: Series, entries: pl.DataFrame, stops: pl.DataFrame | None = None) -> pl.DataFrame:
    """entries: (code, date) signal rows. stops (optional): (code, date, stop) overriding the 10-bar-low stop."""
    st = {} if stops is None else {(a, b): s for a, b, s in stops.select(CODE, "date", "stop").iter_rows()}
    rows = []
    for code, d in entries.select(CODE, "date").iter_rows():
        k = S.pos.get((code, d))
        if k is None:
            continue
        s, e = S.idx[code]
        i = k + 1
        if i >= e or k - 9 < s:
            continue
        entry = S.open[i]
        stop = st.get((code, d), 0.99 * S.low[k - 9:k + 1].min())
        if not np.isfinite(entry) or entry <= stop or stop <= 0:
            continue
        hh = S.high[max(s, i - 60):i].max()
        ll = S.low[max(s, i - 20):i].min()
        target = max(ll + 1.618 * (hh - ll), entry + 3 * (entry - stop))
        row = {CODE: code, "date": d, "R_pct": (entry - stop) / entry * 100}
        for kind in EXITS:
            g, n, open_end = run_exit(S, i, e, stop, kind, target)
            row[kind] = g - COST
            row[f"{kind}_days"] = n
            row[f"{kind}_open"] = open_end
        rows.append(row)
    return pl.DataFrame(rows)


def metrics(t: pl.DataFrame, kind: str, dates: np.ndarray) -> dict:
    r = t[kind].to_numpy()
    if r.size == 0:
        return {"n": 0}
    wins, losses = r[r > 0], r[r <= 0]
    srt = np.sort(r)[::-1]
    top = srt[: max(1, int(np.ceil(0.1 * r.size)))].sum()
    days = t[f"{kind}_days"].to_numpy()
    # additive equity on exit dates
    entry_pos = np.searchsorted(dates, t["date"].to_numpy())
    exit_pos = entry_pos + 1 + days
    M = max(1.0, days.sum() / max(1, int(exit_pos.max() - entry_pos.min() + 1)))
    order = np.argsort(exit_pos, kind="stable")
    eq = np.cumsum(r[order] / M)
    mdd = float((np.maximum.accumulate(np.r_[0, eq]) - np.r_[0, eq]).max())
    m, sd = r.mean(), r.std()
    return {"n": int(r.size), "win": round(float((r > 0).mean()), 3), "mean": round(float(m), 3),
            "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if wins.size and losses.size and losses.mean() else None,
            "top10_share": round(float(top / r.sum()), 2) if r.sum() > 0 else None,
            "skew": round(float(((r - m) ** 3).mean() / sd ** 3), 2) if sd else None,
            "days": round(float(days.mean()), 1), "open_at_end": int(t[f"{kind}_open"].sum()),
            "mdd": round(mdd, 2), "slots": round(M, 1)}


def ma_pullback(p: pl.DataFrame) -> pl.Expr:
    e = ((c("ma20") > c("ma20").shift(5).over(CODE)) & (c("close") > c("ma20")) & (c("close") > c("ma60"))
         & (c("low") <= 1.01 * c("ma10")) & (c("close") >= c("ma10")) & (c("volume") < 0.7 * c("vol_ma20"))).fill_null(False)
    return e & ~e.shift(1).over(CODE).fill_null(False)


def entry_sets(panel: pl.DataFrame) -> dict[str, pl.DataFrame]:
    import p0_patterns as P0
    parts, full = [], []
    for (code,), g in panel.select(CODE, "date", "high", "low").group_by(CODE, maintain_order=True):
        if g.height < 60:
            continue
        h, lo = g["high"].to_numpy(), g["low"].to_numpy()
        for out, sig in ((parts, P0.third_buys_pit(h, lo)), (full, P0.third_buys(h, lo))):
            if sig.any():
                out.append(g.filter(pl.Series(sig)).select(CODE, "date"))
    # LAB_SCORES: rerun on a fixed score file (E082 uses the pinned copy the 11-07 run read); default unchanged
    sc = pl.read_parquet(os.environ.get("LAB_SCORES") or H.LAB / "fusion_static40_safe.parquet")
    book = sc.sort("score", descending=True).group_by("date", maintain_order=True).head(4).select(CODE, "date")
    return {"third_buy": pl.concat(parts), "third_buy_e050": pl.concat(full), "ma_pullback": panel.filter(ma_pullback(panel)).select(CODE, "date"),
            "main_book": book}


def main() -> None:
    t0 = time.perf_counter()
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)).sort(CODE, "date")
    out = B.outcomes(panel)
    pool = out.filter(c("in_pool") & c("period").is_not_null()).select(CODE, "date", "period")
    S = Series(panel)
    dates = np.array(sorted(panel["date"].unique().to_list()), dtype="datetime64[D]")
    res, base = {}, {}
    for name, ent in entry_sets(panel).items():
        base[name] = B.evaluate(ent, out, name)
        ent = ent.join(pool, on=[CODE, "date"])
        t = trades(S, ent).join(pool, on=[CODE, "date"])
        t.write_parquet(H.LAB / f"exit_tails_{name}.parquet")
        res[name] = {per: {k: metrics(t.filter(c("period") == per), k, dates) for k in EXITS} for per in ("PRE", "DEV")}
        print(f"[E060a] {name} n={t.height} {time.perf_counter() - t0:.0f}s", flush=True)
    OUT.write_text(json.dumps({"exits": res, "sameday": base}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(B.table([r for v in base.values() for r in v]))
    keys = ("n", "win", "mean", "payoff", "top10_share", "skew", "days", "mdd", "open_at_end")
    print("| entry | period | exit | " + " | ".join(keys) + " |\n|" + "---|" * (3 + len(keys)))
    for name, v in res.items():
        for per, ex in v.items():
            for k, m in ex.items():
                print(f"| {name} | {per} | {k} | " + " | ".join(str(m.get(x, "")) for x in keys) + " |")


if __name__ == "__main__":
    main()
