"""Daily panel of main-board bars and forward returns with the batch 6 fill rules (E093 / E094).

Why: both historical experiments need, for every (code, session), the limit prices, the limit-up streak, a turnover estimate, 20-day volatility and the forward return
of "buy at D+1 open, sell at D+h close" with the section 1 fill rules.
Input ``bars``: columns ``code, date, o, h, l, c`` (integer cents), ``v`` (shares), one row per (code, session); ``sessions`` = ascending observed sessions.
Panel columns: code, date, si (session index), o, h, l, c, v, prev_c, ok_move (a limit verdict exists), up_c, down_c, lu (closed at the limit-up price), streak, amt
(estimate = close x shares, the repository's estimate: the daily bars carry no turnover), vol20 (std of the last up to 20 valid daily returns, at least 10).
Streak: consecutive limit-up closes in the code's own series; a day without a limit verdict leaves the streak unchanged (the repository's market-state rule); a
known non-limit close ends it.
``forward`` result columns: code, date (decision session D), si, status, entry_c, exit_c, exit_si, ret (gross). Status:
  filled | blocked_buy (D+1 open at the limit-up price) | no_bar (no bar on D+1: suspended) | pending_entry (D+1 not observed yet) | pending_exit (exit not observable yet).
Adjusted variant (``build_panel_adjusted``, the fusion lane's panel definitions): prices are forward-adjusted floats (ratio-preserving), the limit price is ``round(prev x 1.1, 2)`` with half a
cent of tolerance (``up_c`` / ``down_c`` hold the TOLERANT thresholds: a close at or above ``up_c`` is a limit-up close, a price at or below ``down_c`` is at the limit-down), ``amt`` = typical
price x shares, same columns as the raw panel. Difference on ex-dividend / ex-rights days: raw prices jump on the ex day (the raw limit price from the raw previous close is then wrong and
the move looks like a loss), adjusted returns are the true returns, so streaks and limits there are right; the price is NOT the traded price, so the cent rounding of the limit is only
approximate for early history (an adjusted price has no tick), and ``amt`` carries the adjustment factor of its date (a few percent for most stocks).
Not covered: costs (applied by the evaluator), scope (the caller filters).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

from pulse.domain.batch6 import rules as R


def build_panel(bars: pl.DataFrame, sessions: Sequence[date], *, ratio_bp: int = R.RATIO_BP) -> pl.DataFrame:
    si = pl.DataFrame({"date": list(sessions), "si": list(range(len(sessions)))}, schema={"date": pl.Date, "si": pl.Int64})
    b = bars.join(si, on="date", how="inner").filter((pl.col("c") > 0) & pl.col("o").is_not_null()).sort(["code", "date"])
    b = b.with_columns(pl.col("c").shift(1).over("code").alias("prev_c"))
    up = (pl.col("prev_c") * (10000 + ratio_bp) + 5000) // 10000
    down = (pl.col("prev_c") * (10000 - ratio_bp) + 5000) // 10000
    ok = pl.col("prev_c").is_not_null() & ((pl.col("c") - pl.col("prev_c")).abs() * 10000 <= pl.col("prev_c") * R.GUARD_BP)
    b = b.with_columns(ok.alias("ok_move"), up.alias("up_c"), down.alias("down_c"))
    b = b.with_columns((pl.col("ok_move") & (pl.col("c") >= pl.col("up_c"))).alias("lu"),
                       pl.when(pl.col("ok_move")).then(pl.col("c") / pl.col("prev_c") - 1.0).otherwise(None).alias("pct"),
                       (pl.col("c") / 100.0 * pl.col("v")).alias("amt"))
    b = b.with_columns(pl.col("pct").rolling_std(window_size=20, min_samples=10).over("code").alias("vol20"))
    streak = _streaks(b)
    return b.with_columns(pl.Series("streak", streak, dtype=pl.Int64)).drop("pct")

HALF_CENT = 0.005


def build_panel_adjusted(bars: pl.DataFrame, sessions: Sequence[date]) -> pl.DataFrame:
    """Panel from forward-adjusted float bars (columns code, date, o, h, l, c, v), same output columns as ``build_panel``."""
    si = pl.DataFrame({"date": list(sessions), "si": list(range(len(sessions)))}, schema={"date": pl.Date, "si": pl.Int64})
    c = pl.col
    b = bars.join(si, on="date", how="inner").filter((c("c") > 0) & c("o").is_not_null()).sort(["code", "date"])
    b = b.with_columns(c("c").shift(1).over("code").alias("prev_c"))
    r = R.RATIO_BP / 10000
    up = (c("prev_c") * (1 + r)).round(2)
    down = (c("prev_c") * (1 - r)).round(2)
    ok = c("prev_c").is_not_null() & ((c("c") / c("prev_c") - 1).abs() <= R.GUARD_BP / 10000)
    b = b.with_columns(ok.alias("ok_move"), (up - HALF_CENT).alias("up_c"), (down + HALF_CENT).alias("down_c"))
    b = b.with_columns((c("ok_move") & (c("c") >= c("up_c"))).alias("lu"),
                       pl.when(c("ok_move")).then(c("c") / c("prev_c") - 1.0).otherwise(None).alias("pct"),
                       ((c("h") + c("l") + c("c")) / 3.0 * c("v")).alias("amt"))
    b = b.with_columns(c("pct").rolling_std(window_size=20, min_samples=10).over("code").alias("vol20"))
    return b.with_columns(pl.Series("streak", _streaks(b), dtype=pl.Int64)).drop("pct")


def _streaks(b: pl.DataFrame) -> list[int]:
    streak: list[int] = []
    cur, last = 0, None
    for code, okm, lu in zip(b["code"].to_list(), b["ok_move"].to_list(), b["lu"].to_list()):
        if code != last:
            cur, last = 0, code
        if okm:
            cur = cur + 1 if lu else 0
        streak.append(cur if (okm and lu) else (0 if okm else cur))
    return streak


def _shift_cols(p: pl.DataFrame, k: int) -> pl.DataFrame:
    return p.select("code", (pl.col("si") - k).alias("si"), pl.col("o").alias(f"o{k}"), pl.col("c").alias(f"c{k}"),
                    pl.col("up_c").alias(f"up{k}"), pl.col("down_c").alias(f"dn{k}"))


def forward(panel: pl.DataFrame, hold: int, n_sessions: int, *, max_defer: int = R.MAX_DEFER_SESSIONS) -> pl.DataFrame:
    """Forward legs of every (code, D): buy D+1 open, sell D+hold close, with fill rules and sell deferral (section 1). Columnar: three years of bars stay within a few hundred MB."""
    last = n_sessions - 1
    base = panel.select("code", "date", "si")
    has_ok = "flag_ok" in panel.columns                           # batch 7 exact panel: a row whose limit flags are unknown / uncertain cannot be classified as buyable or not
    first = _shift_cols(panel, 1)
    if has_ok:
        first = first.join(panel.select("code", (pl.col("si") - 1).alias("si"), pl.col("flag_ok").alias("fok1")), on=["code", "si"], how="left")
    e = base.join(first, on=["code", "si"], how="left").join(_shift_cols(panel, hold).select("code", "si", f"c{hold}", f"dn{hold}"), on=["code", "si"], how="left")
    code, date_, si = e["code"].to_list(), e["date"].to_list(), e["si"].to_list()
    fok1 = e["fok1"].to_list() if has_ok else None
    o1, up1, ch, dh = e["o1"].to_list(), e["up1"].to_list(), e[f"c{hold}"].to_list(), e[f"dn{hold}"].to_list()
    status = [""] * len(si)
    entry: list = [None] * len(si)
    exit_c: list = [None] * len(si)
    exit_si: list = [None] * len(si)
    ret: list = [None] * len(si)
    need: list[int] = []                                           # rows whose planned exit cannot be sold: scanned later with a lookup of those codes only
    for i in range(len(si)):
        if si[i] + 1 > last:
            status[i] = "pending_entry"
        elif o1[i] is None:
            status[i] = "no_bar"
        elif fok1 is not None and not fok1[i]:
            status[i] = "flag_unknown"
        elif not R.buy_filled(o1[i], up1[i]):
            status[i] = "blocked_buy"
        else:
            entry[i] = o1[i]
            if si[i] + hold > last:
                status[i] = "pending_exit"
            elif R.sell_possible(ch[i], dh[i]):
                status[i], exit_c[i], exit_si[i] = "filled", ch[i], si[i] + hold
            else:
                need.append(i)
    if need:
        codes = {code[i] for i in need}
        lookup = {(r[0], r[1]): (r[2], r[3], r[4]) for r in panel.filter(pl.col("code").is_in(list(codes))).select("code", "si", "o", "c", "down_c").iter_rows()}
        for i in need:
            status[i] = "pending_exit"
            for k in range(hold + 1, hold + 1 + max_defer):
                if si[i] + k > last:
                    break
                bar = lookup.get((code[i], si[i] + k))
                if bar is None:
                    continue
                o_, c_, dn_ = bar
                if R.sell_possible(o_, dn_):
                    exit_c[i], exit_si[i], status[i] = o_, si[i] + k, "filled"
                    break
                if R.sell_possible(c_, dn_):
                    exit_c[i], exit_si[i], status[i] = c_, si[i] + k, "filled"
                    break
    for i in range(len(si)):
        if exit_c[i] is not None and entry[i] is not None:
            ret[i] = exit_c[i] / entry[i] - 1.0
    px = panel.schema["c"]                                          # Int64 cents (raw panel) or Float64 prices (adjusted panel)
    return pl.DataFrame({"code": code, "date": date_, "si": si, "status": status, "entry_c": entry, "exit_c": exit_c, "exit_si": exit_si, "ret": ret},
                        schema={"code": pl.String, "date": pl.Date, "si": pl.Int64, "status": pl.String, "entry_c": px, "exit_c": px, "exit_si": pl.Int64, "ret": pl.Float64})
