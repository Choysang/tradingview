"""Shared harness for the four-track strategy search (quant ML, RL, LLM, traditional).

Periods
- TRAIN  : first bar .. 2025-12-02. Fit models and choose settings here only.
- VALID  : 2025-12-03 .. 2026-07-31. Compare tracks and choose the fusion here.
- LOCKBOX: 2026-08-03 .. last bar. Opened once at the end (env LOCKBOX=1); the existing
  playbooks were already seen on it, the new methods were not.
Round 5 (current-regime protocol): PRE first bar .. 2026-06-30, SELECT 2026-07-01 .. 2026-08-31,
VERIFY 2026-09-01 .. last bar (env VERIFY=1). Sep-Oct was looked at in round 4.4's regime study, so
VERIFY is only half blind; forward tracking from November is the real test.
Since 2026-11-02 (design 1.0 review, user decision): 2026-07-01 .. 2026-10-30 is DEV, the development set
(DEV_A = SELECT, DEV_B = VERIFY; the old names still work). It was looked at many times and is never called
a test set. The out-of-sample test is OOS: decisions from 2026-11-02 on, recorded forward by frozen versions.

Every score frame is turned into trades by one book: each day the best `offer` tradable names
by score are signalled, the engine fills free slots (equal weight, `positions` slots, at most
positions / hold new per day), buys at the next open, sells at the close `hold` days later,
no stops, costs on. Every evaluate() call appends a line to trials.jsonl, so the number of
tries behind a result is known (used for the deflated Sharpe ratio).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.playbooks.backtest import simulate  # noqa: E402
from backend.app.playbooks.evaluate import context, stats  # noqa: E402
from backend.app.playbooks.library import CODE, c, liquid  # noqa: E402
from backend.app.playbooks.library_factor import not_st  # noqa: E402
from backend.app.playbooks.library_sentiment import float_cap_pit  # noqa: E402
from contracts.schema import ExitRules, SizingRule  # noqa: E402

UNIVERSE = os.environ.get("LAB_UNIVERSE", "all")  # "main": 沪深主板 only, index = 上证指数 (see tengine.py)
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
BASE = ROOT / ".work" / "lab"
LAB = BASE / "main" if UNIVERSE == "main" else BASE  # outputs (scores, trial log); git-ignored
LAB.mkdir(parents=True, exist_ok=True)
TRIALS = LAB / "trials.jsonl"
TRAIN_END = date(2025, 12, 2)
VALID_END = date(2026, 7, 31)
PRE_END, SELECT_END = date(2026, 6, 30), date(2026, 8, 31)
FORWARD_START = date(2024, 8, 20)  # first day with fusion scores
DEV_START, DEV_END = date(2026, 7, 1), date(2026, 10, 30)
OOS_START = date(2026, 11, 2)  # first forward session after the main book was frozen (2026-11-01)
MIN_AMOUNT, MIN_PRICE, MIN_DAYS = 0.5, 2.0, 120
PCT = c("pct_chg")

_CTX = None
_FEAT = None


def ctx():
    global _CTX
    if _CTX is None:
        t0 = time.perf_counter()
        _CTX = context()
        print(f"[harness] context {len(_CTX.dates)} days {_CTX.dates[0]}..{_CTX.dates[-1]} {time.perf_counter() - t0:.0f}s", flush=True)
    return _CTX


def period_bounds(name: str) -> tuple[date, date]:
    dates = ctx().dates
    if name == "TRAIN":
        return dates[0], TRAIN_END
    if name == "VALID":
        return date(2025, 12, 3), VALID_END
    if name == "LOCKBOX":
        if os.environ.get("LOCKBOX") != "1":
            raise RuntimeError("LOCKBOX is closed; set LOCKBOX=1 only for the final test")
        return date(2026, 8, 1), dates[-1]
    if name == "ALL_OPEN":  # TRAIN + VALID
        return dates[0], VALID_END
    # Current-regime protocol (round 5): the market turned down in 2026-07, so methods are chosen
    # on PRE + SELECT and checked once on VERIFY (env VERIFY=1). November onward is forward tracking.
    if name == "PRE":
        return dates[0], PRE_END
    if name == "DEV":
        return DEV_START, min(DEV_END, dates[-1])
    if name == "DEV_A":
        return DEV_START, SELECT_END
    if name == "DEV_B":
        return date(2026, 9, 1), min(DEV_END, dates[-1])
    if name == "OOS":
        if dates[-1] < OOS_START:
            raise RuntimeError(f"no out-of-sample session yet (last bar {dates[-1]})")
        return OOS_START, dates[-1]
    if name == "SELECT":
        return date(2026, 7, 1), SELECT_END
    if name == "VERIFY":
        if os.environ.get("VERIFY") != "1":
            raise RuntimeError("VERIFY is closed; set VERIFY=1 only for the final check")
        return date(2026, 9, 1), dates[-1]
    if name == "FORWARD":  # the live book, run without a break from the first fusion score (forward_book.py)
        return FORWARD_START, dates[-1]
    raise ValueError(name)


def listed_days() -> pl.Expr:
    """Trading days since listing: the backend panel's days_listed, which since 2026-11-01 counts a stock
    already trading on the warehouse's first date as long listed (backend/app/playbooks/panel.py
    `_days_listed`). Kept as a function so the lab and the product read one definition."""
    return c("days_listed")


def tradable() -> pl.Expr:
    base = liquid(MIN_AMOUNT) & (listed_days() >= MIN_DAYS) & (c("close") >= MIN_PRICE) & not_st()
    return base & c(CODE).str.slice(0, 3).is_in(MAIN_PREFIX) if UNIVERSE == "main" else base


# Every factor is oriented so that a higher value is expected to predict a higher return
# (signs from the 2026-10 factor study, which used TRAIN-period IC to orient them).
def factor_exprs() -> dict[str, pl.Expr]:
    turn = c("amount") / float_cap_pit() * 100
    mkt = PCT.mean().over("date")
    return {
        "rev_1": -PCT,
        "rev_5": -c("ret_5"),
        "rev_20": -c("ret_20"),
        "mom_60": c("ret_60"),
        "mom_120_20": (c("close").shift(20) / c("close").shift(120) - 1).over(CODE),
        "lowvol_20": -c("volatility_20"),
        "lowvol_60": -PCT.rolling_std(60).over(CODE),
        "low_turn_20": -turn.rolling_mean(20).over(CODE),
        "low_turn_5": -turn.rolling_mean(5).over(CODE),
        "turn_shrink_5_60": -(c("amount").rolling_mean(5) / c("amount").rolling_mean(60)).over(CODE),
        "small": -float_cap_pit().log(),
        "max_20": -PCT.rolling_max(20).over(CODE),
        "amp_20": -((c("high") - c("low")) / c("prev_close")).rolling_mean(20).over(CODE),
        "intraday_20": (c("close") / c("open") - 1).rolling_mean(20).over(CODE),
        "overnight_20": -(c("open") / c("prev_close") - 1).rolling_mean(20).over(CODE),
        "upper_shadow_20": -((c("high") - pl.max_horizontal("open", "close")) / c("prev_close")).rolling_mean(20).over(CODE),
        "lower_shadow_20": ((pl.min_horizontal("open", "close") - c("low")) / c("prev_close")).rolling_mean(20).over(CODE),
        "bias_20": -(c("close") / c("ma20") - 1),
        "bias_60": -(c("close") / c("ma60") - 1),
        "near_high_250": c("close") / c("high_250_prev") - 1,
        "near_high_60": c("close") / c("high_60_prev") - 1,
        "amihud_20": (PCT.abs() / c("amount")).rolling_mean(20).over(CODE),
        "skew_20": -PCT.rolling_skew(20).over(CODE),
        "zt_cnt_20": c("is_limit_up").cast(pl.Int32).rolling_sum(20).over(CODE),
        "rsi_low": -c("rsi_14"),
        "low_price": -c("close").log(),
        "pv_corr_20": -pl.rolling_corr(c("close"), c("volume"), window_size=20).over(CODE),
        "vol_ratio_low": -(c("volume") / c("volume").shift(1).rolling_mean(20)).over(CODE),
        "beta_low_60": -(pl.rolling_cov(PCT, mkt, window_size=60) / mkt.rolling_var(60)).over(CODE),
        "close_pos_20": (c("close") - c("low_20_prev")) / (c("high_20_prev") - c("low_20_prev")),
        "ma_trend": (c("ma5") / c("ma20") - 1) + (c("ma20") / c("ma60") - 1),
        "gap_today": -(c("open") / c("prev_close") - 1),
        "body_today": c("close") / c("open") - 1,
    }


def features(horizon: int = 20) -> pl.DataFrame:
    """Tradable rows with every factor (Float32) and the forward excess return used as a label.

    fwd = open[t+1+h] / open[t+1] - 1 (percent), minus the same-day mean over tradable rows.
    The label looks forward; models must only train on rows whose label window ends before
    the date they predict (see `label_end`)."""
    global _FEAT
    if _FEAT is not None and _FEAT[0] == horizon:
        return _FEAT[1]
    t0 = time.perf_counter()
    fx = factor_exprs()
    nxt = c("open").shift(-1).over(CODE)
    fwd = (c("open").shift(-1 - horizon).over(CODE) / nxt - 1) * 100
    frame = (
        ctx().panel.lazy()
        .with_columns(tradable().fill_null(False).alias("_ok"), *(e.alias(k) for k, e in fx.items()), fwd.alias("fwd"))
        .filter(c("_ok"))
        .with_columns(pl.when(c("fwd").is_finite()).then(c("fwd")).alias("fwd"))  # open = 0 bars give inf/NaN
        .with_columns(
            *(pl.when(c(k).is_finite()).then(c(k)).cast(pl.Float32).alias(k) for k in fx),
            (c("fwd") - c("fwd").mean().over("date")).cast(pl.Float32).alias("fwd_ex"),
        )
        .select("code", "date", *fx, "fwd_ex")
        .collect()
    )
    print(f"[harness] features {frame.height} rows x {len(fx)} {time.perf_counter() - t0:.0f}s", flush=True)
    _FEAT = (horizon, frame)
    return frame


def label_end(d: date, horizon: int) -> date:
    """Last date whose `horizon`-day label is fully known by the open of trading day after `d`."""
    dates = ctx().dates
    i = dates.index(d)
    return dates[max(0, i - horizon - 1)]


# --- benchmarks ----------------------------------------------------------------------

_EW = None


def _equal_weight():
    """Daily equal-weight return of tradable names: open-to-close on the entry day, close-to-close after."""
    global _EW
    if _EW is None:
        u = ctx().panel.lazy().filter(tradable().fill_null(False)).select(
            "date", (c("close") / c("open") - 1).alias("od"), (PCT / 100).alias("cc"))
        ew = u.filter(c("od").is_finite() & c("cc").is_finite() & (c("cc").abs() < 0.35)).group_by("date").agg(
            c("od").mean(), c("cc").mean()).sort("date").collect()
        pos = {d.isoformat(): i for i, d in enumerate(ew["date"].to_list())}
        logc = np.concatenate([[0.0], np.cumsum(np.log1p(ew["cc"].to_numpy()))])
        _EW = (pos, logc, ew["od"].to_numpy(), ew)
    return _EW


def index_return(start: date, end: date) -> float | None:
    b = pl.read_parquet(BASE / "benchmark_sh.parquet") if UNIVERSE == "main" else ctx().benchmark
    if b is None:
        return None
    w = b.filter((c("date") >= start) & (c("date") <= end))
    prev = b.filter(c("date") < start).tail(1)
    base = prev["close"][0] if prev.height else w["open"][0]
    return float((w["close"][-1] / base - 1) * 100)


def ew_return(start: date, end: date) -> float:
    ew = _equal_weight()[3].filter((c("date") >= start) & (c("date") <= end))
    return float((np.prod(1 + ew["cc"].to_numpy()) - 1) * 100)


def nw_t(x: np.ndarray, lag: int) -> float | None:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return None
    d = x - x.mean()
    var = d @ d / n
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * (d[k:] @ d[:-k]) / n
    return float(x.mean() / math.sqrt(var / n)) if var > 0 else None


def deflated_sharpe(daily: np.ndarray, n_trials: int, trial_sharpes_sd: float) -> float | None:
    """Bailey & Lopez de Prado (2014): probability the true Sharpe beats the best of n_trials noise
    trials. `daily` are the strategy's daily returns; `trial_sharpes_sd` is the cross-trial standard
    deviation of per-day Sharpe ratios."""
    r = daily[np.isfinite(daily)]
    if len(r) < 30 or r.std(ddof=1) == 0:
        return None
    sr = r.mean() / r.std(ddof=1)
    skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
    kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4)
    nd = statistics.NormalDist()
    n = max(n_trials, 2)
    gamma = 0.5772156649
    sr0 = trial_sharpes_sd * ((1 - gamma) * nd.inv_cdf(1 - 1 / n) + gamma * nd.inv_cdf(1 - 1 / (n * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    return float(nd.cdf((sr - sr0) * math.sqrt(len(r) - 1) / math.sqrt(denom)))


# --- the book ------------------------------------------------------------------------


def to_signals(scores: pl.DataFrame, offer: int) -> pl.DataFrame:
    """Top `offer` scored names per day; score becomes a positive rank weight."""
    return (
        scores.filter(c("score").is_finite())
        .with_columns(c("score").rank("ordinal", descending=True).over("date").alias("_r"))
        .filter(c("_r") <= offer)
        .select("code", "date", (offer + 1 - c("_r")).cast(pl.Float64).alias("score"))
    )


def evaluate(scores: pl.DataFrame, name: str, hold: int = 20, positions: int = 20, offer: int = 40,
             periods: tuple[str, ...] = ("TRAIN", "VALID"), note: str = "", log: bool = True,
             exposure: pl.DataFrame | None = None, exit_kw: dict | None = None) -> dict:
    """Trades for a score frame (code, date, score) on the standard book, per period.

    `exposure` (date, w in [0, 1]) scales how many new names may be bought that day (see gate_signals)."""
    k = ctx()
    per_day = max(1, round(positions / hold))
    sig = gate_signals(to_signals(scores, offer), exposure, offer, per_day)
    exits = ExitRules(holding_days=hold, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None, **(exit_kw or {}))
    sizing = SizingRule(method="equal", max_positions=positions, max_new_per_day=min(per_day, 20))
    pos, logc, od, _ = _equal_weight()
    out = {"name": name, "hold": hold, "positions": positions, "offer": offer, "note": note, "exit_kw": exit_kw or {}, "periods": {}}
    for p in periods:
        start, end = period_bounds(p)
        trades, eq = simulate(k.panel, sig, exits, sizing, k.benchmark, start=start, end=end)
        s = stats(trades, eq)
        ex = []
        for t in trades:
            a, b = pos.get(t.entry_date), pos.get(t.exit_date)
            if t.exit_reason == "end" or a is None or b is None:
                continue
            ex.append(t.return_pct - ((1 + od[a]) * math.exp(logc[b + 1] - logc[a + 1]) - 1) * 100)
        ex = np.array(ex)
        nav = eq["strategy"].to_numpy()
        daily = np.diff(nav) / nav[:-1] if len(nav) > 1 else np.array([])
        out["periods"][p] = {
            "start": start.isoformat(), "end": end.isoformat(), "trades": s.trades, "win": s.win_rate,
            "avg": s.avg_return, "sharpe": s.sharpe, "ann": s.annual_return, "mdd": s.max_drawdown,
            "total": round(float(nav[-1] / 100 - 1) * 100, 2) if len(nav) else None,
            "exposure": s.exposure_pct,
            "excess_avg": round(float(ex.mean()), 3) if len(ex) > 2 else None,
            "excess_t": nw_t(ex, max(1, hold // max(per_day, 1))) if len(ex) > 2 else None,
            "index": round(index_return(start, end) or float("nan"), 2),
            "ew": round(ew_return(start, end), 2),
            "daily_sharpe": round(float(daily.mean() / daily.std(ddof=1)), 4) if len(daily) > 2 and daily.std() > 0 else None,
        }
    if log:
        with TRIALS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**out, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")
    return out


def gate_signals(sig: pl.DataFrame, exposure: pl.DataFrame | None, offer: int, per_day: int) -> pl.DataFrame:
    """On a day with exposure w < 1 keep only the top round(w * per_day) names (0 = buy nothing). Cutting the
    offer list instead never bites, because only per_day names are bought."""
    if exposure is None:
        return sig
    keep = (c("w").fill_null(1.0) * per_day).round(0)
    return (sig.join(exposure.select("date", "w"), on="date", how="left")
            .filter((c("w").fill_null(1.0) >= 1.0) | (c("score") > offer - keep)).drop("w"))


def equity(scores: pl.DataFrame, period: str, hold: int = 20, positions: int = 20, offer: int = 40,
           exit_kw: dict | None = None, exposure: pl.DataFrame | None = None) -> pl.DataFrame:
    """The standard book's daily equity curve (date, strategy NAV, benchmark, exposure) for one period."""
    start, end = period_bounds(period)
    per_day = max(1, round(positions / hold))
    exits = ExitRules(holding_days=hold, stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None, **(exit_kw or {}))
    sizing = SizingRule(method="equal", max_positions=positions, max_new_per_day=min(per_day, 20))
    sig = gate_signals(to_signals(scores, offer), exposure, offer, per_day)
    _, eq = simulate(ctx().panel, sig, exits, sizing, ctx().benchmark, start=start, end=end)
    return eq


def show(res: dict) -> str:
    rows = [f"{res['name']} (hold {res['hold']}, {res['positions']} slots) {res['note']}"]
    for p, m in res["periods"].items():
        rows.append(
            f"  {p:7s} {m['start']}..{m['end']} trades {m['trades']:4d} total {m['total']:+7.2f}% "
            f"ann {m['ann'] if m['ann'] is not None else float('nan'):+6.1f}% sharpe {m['sharpe'] if m['sharpe'] is not None else float('nan'):+5.2f} "
            f"mdd {m['mdd'] if m['mdd'] is not None else float('nan'):6.1f}% ex/trade {m['excess_avg'] if m['excess_avg'] is not None else float('nan'):+6.2f} "
            f"t {m['excess_t'] if m['excess_t'] is not None else float('nan'):+5.2f} | index {m['index']:+6.2f}% ew {m['ew']:+6.2f}%")
    return "\n".join(rows)


def trial_count(prefix: str = "") -> int:
    if not TRIALS.exists():
        return 0
    return sum(1 for line in TRIALS.open(encoding="utf-8") if json.loads(line)["name"].startswith(prefix))
