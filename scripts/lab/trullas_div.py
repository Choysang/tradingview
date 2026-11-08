"""E060 part 2: the divergence system as taught (big-frame support / Fibonacci + MACD / RSI divergence + size by
the number of confluences), on daily bars, default parameters only: MACD(12,26,9) (the panel's macd_dif /
macd_dea / macd_hist, hist = 2 x (DIF - DEA)) and RSI(14). No tuning.

Pivot lows: fractal lows with two bars each side, known two bars later (as p0_patterns); the previous pivot low
must be 5..60 bars earlier.
Base signal (one of, required):
- regular: low_j < low_i and (DIF_j > DIF_i or hist_j > hist_i);
- hidden: MA20 > MA60 at j, low_j > low_i and (DIF_j < DIF_i or hist_j < hist_i);
- failure swing: hist crosses above 0 at t (hist_t > 0 >= hist_t-1); in t-5..t-1 hist was negative, rose at least
  two bars in a row and then dipped once (a brief re-expansion), and the lowest low of t-5..t stays at or above
  the last pivot low. Its "low" is the lowest bar of t-10..t.
Confluences (N_conf = 1 + count):
- RSI(14) diverges the same way at the same two lows (regular: RSI_j > RSI_i; hidden: RSI_j < RSI_i; failure:
  RSI at the low > RSI at the previous pivot low);
- volume at the new low < volume at the previous low, and the entry bar's volume > its 20-day mean;
- the low sits at the 0.5 / 0.618 retracement of the prior up-wave (H = highest high of the 40 bars before the
  low, L0 = lowest low of the 60 bars before H; retracement (H - low) / (H - L0) in [0.45, 0.67]) or within 1.5%
  of MA60.
Entry: within 5 bars of the signal (the signal bar included), the first close above the 0.236 retracement of
the down-wave into the low (Hd = highest high of the 20 bars before the low): low + 0.236 (Hd - low). Bought at
the next open. Initial stop: 0.995 x the low (outside the 0.0 level).
Evaluation: sameday_base (fixed 5 / 10 / 20, same-day random), by N_conf and pattern; equal weight vs weight =
N_conf; and the exit_tails exits with this stop.
The intraday version (60m / 1m small frame, first hour only) needs intraday history that does not exist before
2026-11-02, so it is not run here (see the spec).
Run: cd scripts/lab && LAB_UNIVERSE=main PYTHONPATH=../.. ../../backend/.venv/Scripts/python.exe trullas_div.py
"""
from __future__ import annotations

import json
import time

import numpy as np
import polars as pl

import exit_tails as X
import harness as H
import sameday_base as B
from harness import CODE, c

OUT = H.LAB / "trullas_div.json"


def pivot_lows(lo: np.ndarray) -> np.ndarray:
    n = len(lo)
    piv = np.zeros(n, dtype=bool)
    if n < 5:
        return piv
    m = lo[2:-2]
    piv[2:-2] = (m < lo[1:-3]) & (m < lo[:-4]) & (m <= lo[3:-1]) & (m <= lo[4:])
    return piv


def signals_one(g: dict[str, np.ndarray]) -> list[tuple[int, int, str, int]]:
    """(signal row t, low row j, pattern, n_conf) for one code."""
    lo, hi, cl, vol = g["low"], g["high"], g["close"], g["volume"]
    dif, hist, rsi, ma20, ma60, vma = g["macd_dif"], g["macd_hist"], g["rsi_14"], g["ma20"], g["ma60"], g["vol_ma20"]
    n = len(lo)
    piv = np.flatnonzero(pivot_lows(lo))
    out = []

    def fib_ok(j: int) -> bool:
        a = max(0, j - 40)
        if j - a < 5:
            return False
        hpos = a + int(np.argmax(hi[a:j]))
        b = max(0, hpos - 60)
        if hpos - b < 5:
            return False
        H_, L0 = hi[hpos], lo[b:hpos].min()
        if H_ <= L0:
            return False
        ret = (H_ - lo[j]) / (H_ - L0)
        near_ma = np.isfinite(ma60[j]) and abs(lo[j] / ma60[j] - 1) < 0.015
        return 0.45 <= ret <= 0.67 or near_ma

    def confl(i: int, j: int, t: int, kind: str) -> int:
        k = 1
        if np.isfinite(rsi[i]) and np.isfinite(rsi[j]):
            k += int(rsi[j] < rsi[i]) if kind == "hidden" else int(rsi[j] > rsi[i])
        k += int(vol[j] < vol[i] and np.isfinite(vma[t]) and vol[t] > vma[t])
        k += int(fib_ok(j))
        return k

    for a, b in zip(piv[:-1], piv[1:]):
        i, j = a, b
        if not 5 <= j - i <= 60:
            continue
        t = j + 2
        if t >= n or not (np.isfinite(dif[i]) and np.isfinite(dif[j])):
            continue
        if lo[j] < lo[i] and (dif[j] > dif[i] or hist[j] > hist[i]):
            out.append((t, j, "regular", 0))
        elif ma20[j] > ma60[j] and lo[j] > lo[i] and (dif[j] < dif[i] or hist[j] < hist[i]):
            out.append((t, j, "hidden", 0))
    for t in range(12, n):
        if not (hist[t] > 0 >= hist[t - 1]):
            continue
        w = hist[t - 5:t]
        if not np.all(np.isfinite(w)) or w.max() >= 0:
            continue
        d = np.diff(w)
        rose = any(d[q] > 0 and d[q + 1] > 0 and q + 2 < len(d) and d[q + 2] < 0 for q in range(len(d) - 2))
        if not rose:
            continue
        prev = piv[piv < t - 5]
        if prev.size == 0 or lo[t - 5:t + 1].min() < lo[prev[-1]]:
            continue
        j = t - 10 + int(np.argmin(lo[t - 10:t + 1]))
        out.append((t, j, "failure", int(prev[-1])))
    res = []
    for t, j, kind, i0 in out:
        i = i0 if kind == "failure" else int(piv[piv < j][-1]) if (piv < j).any() else j
        res.append((t, j, kind, confl(i, j, t, kind) if i != j else 1))
    return res


def entries(panel: pl.DataFrame) -> pl.DataFrame:
    cols = ("low", "high", "close", "volume", "macd_dif", "macd_hist", "rsi_14", "ma20", "ma60", "vol_ma20")
    rows = []
    for (code,), g in panel.select(CODE, "date", *cols).group_by(CODE, maintain_order=True):
        if g.height < 80:
            continue
        arr = {k: g[k].to_numpy().astype(float) for k in cols}
        dates = g["date"].to_list()
        lo, hi, cl = arr["low"], arr["high"], arr["close"]
        for t, j, kind, k in signals_one(arr):
            hd = hi[max(0, j - 20):j].max() if j > 0 else hi[j]
            level = lo[j] + 0.236 * (hd - lo[j])
            for e in range(t, min(t + 5, len(cl))):
                if cl[e] > level:
                    rows.append((code, dates[e], kind, k, 0.995 * lo[j]))
                    break
    df = pl.DataFrame(rows, schema=[CODE, "date", "pattern", "n_conf", "stop"], orient="row")
    return df.sort("n_conf", descending=True).unique([CODE, "date"], keep="first")


DAYS_5M = ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")


def _ema(x: np.ndarray, span: int) -> np.ndarray:
    a, out = 2 / (span + 1), np.empty_like(x)
    out[0] = x[0]
    for k in range(1, len(x)):
        out[k] = a * x[k] + (1 - a) * out[k - 1]
    return out


def _rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(x, prepend=x[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad, out = np.zeros_like(x), np.zeros_like(x), np.full_like(x, np.nan)
    for k in range(1, len(x)):
        au[k] = au[k - 1] + (up[k] - au[k - 1]) / n
        ad[k] = ad[k - 1] + (dn[k] - ad[k - 1]) / n
        if k >= n:
            out[k] = 100 if ad[k] == 0 else 100 - 100 / (1 + au[k] / ad[k])
    return out


def _roll_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    cs = np.cumsum(np.r_[0, x])
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def intraday_5m() -> dict:
    """Small-frame version on the five recorded days: 5-minute bars from the 1-minute snapshot bars, MACD(12,26,9)
    and RSI(14) over the days joined, entries only on bars ending 09:35..10:30, bought at the next bar's open,
    sold at the next session's open and close (T+1). Big frame: the previous daily close above the daily MA60.
    Baseline: the mean of the same trade over every main-board name with bars that day (same entry bar)."""
    root = H.ROOT / ".work" / "intraday"
    m = pl.concat([pl.read_parquet(root / d / "minute_synth.parquet", columns=["code", "date", "minute", "open", "high", "low", "price", "lots"])
                   for d in DAYS_5M]).filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX))
    mins = c("minute").str.slice(0, 2).cast(pl.Int32) * 60 + c("minute").str.slice(2, 2).cast(pl.Int32)
    m = m.with_columns(((mins + 4) // 5 * 5).alias("b"))
    bars = (m.sort(CODE, "date", "minute").group_by(CODE, "date", "b", maintain_order=True)
            .agg(c("open").first(), c("high").max(), c("low").min(), c("price").last().alias("close"), c("lots").sum().alias("volume"))
            .sort(CODE, "date", "b"))
    daily = pl.concat([H.ctx().panel.select(CODE, "date", "close"), pl.read_parquet(H.LAB / "paper_server" / "bars_week.parquet").select(CODE, "date", "close")]
                      ).unique([CODE, "date"]).sort(CODE, "date").with_columns(c("close").rolling_mean(60).over(CODE).alias("ma60d"))
    big = daily.with_columns((c("close") > c("ma60d")).shift(1).over(CODE).alias("big_ok")).select(CODE, "date", "big_ok")
    day_list = sorted(bars["date"].unique().to_list())
    nxt = {d: day_list[i + 1] for i, d in enumerate(day_list[:-1])}
    first_open = bars.group_by(CODE, "date").agg(c("open").first().alias("o_first"), c("close").last().alias("c_last"))
    rows = []
    for (code,), g in bars.group_by(CODE, maintain_order=True):
        if g.height < 120:
            continue
        cl = g["close"].to_numpy().astype(float)
        dif = _ema(cl, 12) - _ema(cl, 26)
        dea = _ema(dif, 9)
        arr = {"low": g["low"].to_numpy().astype(float), "high": g["high"].to_numpy().astype(float), "close": cl,
               "volume": g["volume"].to_numpy().astype(float), "macd_dif": dif, "macd_hist": 2 * (dif - dea), "rsi_14": _rsi(cl),
               "ma20": _roll_mean(cl, 20), "ma60": _roll_mean(cl, 60), "vol_ma20": _roll_mean(g["volume"].to_numpy().astype(float), 20)}
        arr["macd_dif"][:34] = np.nan
        dates, bs, opens = g["date"].to_list(), g["b"].to_numpy(), g["open"].to_numpy()
        lo, hi = arr["low"], arr["high"]
        for t, j, kind, k in signals_one(arr):
            hd = hi[max(0, j - 20):j].max() if j > 0 else hi[j]
            level = lo[j] + 0.236 * (hd - lo[j])
            for e in range(t, min(t + 5, len(cl) - 1)):
                if cl[e] > level:
                    if 575 <= bs[e] <= 630 and dates[e + 1] == dates[e] and dates[e] in nxt:
                        rows.append((code, dates[e], int(bs[e]), kind, k, float(opens[e + 1])))
                    break
    if not rows:
        return {"n": 0}
    s = pl.DataFrame(rows, schema=[CODE, "date", "b", "pattern", "n_conf", "entry"], orient="row").unique([CODE, "date"], keep="first")
    s = s.with_columns(c("date").replace_strict(nxt, default=None).alias("d1"))
    s = s.join(first_open.rename({"date": "d1"}), on=[CODE, "d1"], how="left").join(big, on=[CODE, "date"], how="left")
    s = s.with_columns(((c("o_first") / c("entry") - 1) * 100).alias("r_open1"), ((c("c_last") / c("entry") - 1) * 100).alias("r_close1"))
    entry_px = bars.with_columns(c("open").shift(-1).over(CODE, "date").alias("e_px")).select(CODE, "date", "b", "e_px")
    base = (entry_px.with_columns(c("date").replace_strict(nxt, default=None).alias("d1")).join(first_open.rename({"date": "d1"}), on=[CODE, "d1"])
            .with_columns(((c("o_first") / c("e_px") - 1) * 100).alias("p_open1"), ((c("c_last") / c("e_px") - 1) * 100).alias("p_close1"))
            .group_by("date", "b").agg(c("p_open1").mean(), c("p_close1").mean()))
    s = s.join(base, on=["date", "b"], how="left").drop_nulls(["r_open1", "p_open1"])
    out = {}
    for name, f in (("all", pl.lit(True)), ("big_ok", c("big_ok").fill_null(False)), ("n1", c("n_conf") == 1), ("n2", c("n_conf") == 2),
                    ("n3+", c("n_conf") >= 3)):
        q = s.filter(f)
        if q.is_empty():
            out[name] = {"n": 0}
            continue
        out[name] = {"n": q.height, "days": q["date"].n_unique(), "next_open": round(q["r_open1"].mean(), 3),
                     "ex_next_open": round((q["r_open1"] - q["p_open1"]).mean(), 3), "next_close": round(q["r_close1"].mean(), 3),
                     "ex_next_close": round((q["r_close1"] - q["p_close1"]).mean(), 3), "win_open": round(float((q["r_open1"] > 0).mean()), 3)}
        if name == "all":
            out["weighted_ex_next_open"] = round(float(((q["r_open1"] - q["p_open1"]) * q["n_conf"]).sum() / q["n_conf"].sum()), 3)
    return out


def main() -> None:
    import sys
    if sys.argv[1:2] == ["--intraday"]:
        r = intraday_5m()
        (H.LAB / "trullas_div_5m.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return
    t0 = time.perf_counter()
    panel = H.ctx().panel.filter(c(CODE).str.slice(0, 3).is_in(H.MAIN_PREFIX)).sort(CODE, "date")
    out = B.outcomes(panel)
    ent = entries(panel)
    print(f"[E060b] entries {ent.height} {time.perf_counter() - t0:.0f}s", flush=True)
    res: dict = {"all": B.evaluate(ent, out, "div_all"),
                 "weighted": B.evaluate(ent, out, "div_w_nconf", weights="n_conf")}
    for k in (1, 2, 3, 4):
        res[f"n{k}"] = B.evaluate(ent.filter(c("n_conf") == k), out, f"div_n{k}")
    for pat in ("regular", "hidden", "failure"):
        res[pat] = B.evaluate(ent.filter(c("pattern") == pat), out, f"div_{pat}")
    pool = out.filter(c("in_pool") & c("period").is_not_null()).select(CODE, "date", "period")
    S = X.Series(panel)
    dates = np.array(sorted(panel["date"].unique().to_list()), dtype="datetime64[D]")
    e2 = ent.join(pool, on=[CODE, "date"])
    t = X.trades(S, e2, stops=e2).join(e2.select(CODE, "date", "period", "n_conf"), on=[CODE, "date"])
    t.write_parquet(H.LAB / "trullas_div_trades.parquet")
    res["exits"] = {per: {f"n{k}" if k else "all": {x: X.metrics(t.filter((c("period") == per) & ((c("n_conf") == k) if k else pl.lit(True))), x, dates)
                                                    for x in X.EXITS} for k in (0, 1, 2, 3, 4)} for per in ("PRE", "DEV")}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    flat = [r for k, v in res.items() if k != "exits" for r in v]
    print(B.table(flat, ("rule", "period", "hold", "n", "days", "mean", "random", "excess", "p_mc", "p_boot", "win",
                         "w_mean", "w_excess", "ex_intraday", "ex_overnight", "ex_rest")))
    keys = ("n", "win", "mean", "payoff", "top10_share", "days", "mdd")
    print("| period | conf | exit | " + " | ".join(keys) + " |\n|" + "---|" * (3 + len(keys)))
    for per, v in res["exits"].items():
        for g, ex in v.items():
            for x, m in ex.items():
                print(f"| {per} | {g} | {x} | " + " | ".join(str(m.get(q, "")) for q in keys) + " |")


if __name__ == "__main__":
    main()
