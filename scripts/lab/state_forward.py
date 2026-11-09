"""E089: market state forward log and the recorder's daily health (docs/research/2026-11-09-batch5-preregistration.md).

Runs where the recordings are (the server, PulseForwardLab after the recorders finish), or on any copy of the day dirs.
Reads `$PULSE_INTRADAY_EXT_DIR/<day>/quotes_ext.parquet` (or its parts, after a crash) and `summary.json` (intraday_ext.py),
and `$PULSE_INTRADAY_DIR/<day>/quotes.parquet` for the main-board equal weight. Writes one row per session of the trading
calendar, append-only, to `$PULSE_LAB_LIVE/e089_state.jsonl`. A session with a missing recording or a broken file still
gets a row, so a gap shows; a day either recorder found to be an unlisted holiday gets none.
A descriptive log: it changes no buying or selling rule. Unknown values are null, never 0 or false.
The first WINDOW sessions' ETF baseline comes from `$PULSE_LAB_LIVE/etf_amount_seed.csv` (date, amount, from E074's Sina
daily bars; made on the PC by `seed`). Both are yuan: the recorded sum of 2026-11-06 equals Sina's to the yuan.
Usage: python state_forward.py run | summary | seed <etf_daily.parquet>
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
import re
import statistics
import sys

import numpy as np
import polars as pl

from tail_forward import LIVE, ROOT, at, closing, data_dir, last_by, snapshots

sys.path.insert(0, str(ROOT))

from backend.app.market.calendar import load_calendar  # noqa: E402

STATE_LOG, SEED = LIVE / "e089_state.jsonl", LIVE / "etf_amount_seed.csv"
START = date(2026, 11, 10)  # the first day recorded end to end by the fixed recorder (E087)
ETFS = ("sh510300", "sh510310", "sh510050", "sz159919", "sh510500", "sh512100", "sz159845")
VWAP_ETF, LOT = "sh510300", 100
INDEX_OF = {"IF": "sh000300", "IH": "sh000016", "IC": "sh000905", "IM": "sh000852"}
CONTEXT = {"hsi": "hf_HSI", "gold": "hf_GC", "oil": "hf_CL", "a50": "em_CN00Y", "cnh": "em_USDCNH"}
MINUTE_SYMBOLS = ("em_CN00Y", "em_USDCNH")
POLL_KINDS = ("grid", "close")  # not "once" (a smoke test) and not "minute" (bars)
CONTRACT = re.compile(r"^(IF|IH|IC|IM)\d{4}$")
WINDOW, RATIO, PANIC_PCT, LATE_SHARE = 20, 2.0, -3.0, 0.30
LATE_FROM, DAY_END, SESSION_OPEN, SESSION_CLOSE = time(14, 30), time(15, 5), time(9, 30), time(15, 0)
READY_AT = time(17, 0)  # both recorders have ended by then
EPISODE_GAP, H1_EPISODES, NEXT = 10, 10, (1, 5, 20)
SOURCE_FAIL_DAYS, CFFEX_SLOW_DAYS, CFFEX_SLOW_MIN, LOST_LOOKBACK = 5, 3, 4.0, 5
NOT_TRADING = "not a trading day"
ROUND = 4
NO_ETF_FLAGS = {"etf_amount": None, "n_etf": 0, "late_share": None, "vwap_gap_pct": None, "ratio": None, "surge": None,
                "panic_support": None, "late_heavy": None}


def _round(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), ROUND)


def _pct(price: float | None, base: float | None) -> float | None:
    return _round((price / base - 1) * 100) if price and base else None


def _json(path: Path) -> dict:
    """A broken summary costs its fields, not the day's row."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {"unreadable": True}


def read_ext(day_dir: Path) -> pl.DataFrame | None:
    """The day's merged file, else the parts a crashed recorder left."""
    f = day_dir / "quotes_ext.parquet"
    if f.exists():
        return pl.read_parquet(f)
    parts = sorted((day_dir / "parts").glob("*.parquet")) if (day_dir / "parts").exists() else []
    return pl.concat([pl.read_parquet(p) for p in parts], how="diagonal_relaxed") if parts else None


def last_of(x: pl.DataFrame, day: date, end: time) -> dict[str, dict]:
    """Per symbol, the last row up to `end` today, by poll_ts (a bar's minute for minute rows)."""
    rows = last_by(x.rename({"symbol": "code"}).filter(pl.col("price").is_not_null()), day, time(0, 0), end)
    return {r["code"]: r for r in rows.iter_rows(named=True)}


def fresh_last(x: pl.DataFrame, day: date, end: time) -> dict[str, dict]:
    """As last_of, without a symbol whose last row is from before 14:30 (the recorder died, or the source did)."""
    return {s: r for s, r in last_of(x, day, end).items() if r["poll_ts"].time() >= LATE_FROM}


def etf_state(x: pl.DataFrame, day: date) -> dict:
    """The seven ETFs' cumulative amount at the close and at 14:30, and 510300's close against its day's average price.
    Only polled rows stamped today count, and a sum needs every ETF polled after the close: a recorder that died at
    10:00 must not report a quiet day, and a holiday's feed repeats the last session."""
    cum = x.filter(pl.col("symbol").is_in(ETFS) & pl.col("amount").is_not_null() & pl.col("kind").is_in(POLL_KINDS)
                   & (pl.col("quote_ts").dt.date() == day))
    if cum.is_empty():
        return {"etf_amount": None, "n_etf": 0, "late_share": None, "vwap_gap_pct": None}
    end = {s: r for s, r in last_of(cum, day, DAY_END).items() if r["poll_ts"].time() >= SESSION_CLOSE}
    state = {"etf_amount": None, "n_etf": len(end), "late_share": None, "vwap_gap_pct": None}
    v = end.get(VWAP_ETF)
    if v and v.get("volume"):
        state["vwap_gap_pct"] = _pct(v["price"], v["amount"] / (v["volume"] * LOT))
    if len(end) < len(ETFS):
        return state
    total = sum(r["amount"] for r in end.values())
    state["etf_amount"] = _round(total)
    late = {r["code"]: r["amount"] for r in at(cum.rename({"symbol": "code"}), day, LATE_FROM, POLL_KINDS).iter_rows(named=True)}
    if set(late) == set(end) and total > 0:
        state["late_share"] = _round((total - sum(late.values())) / total)
    return state


def rows_logged() -> list[dict]:
    """The log, without a line a crash left half-written: it must not stop every later run."""
    if not STATE_LOG.exists():
        return []
    rows = []
    for line in STATE_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return sorted((r for r in rows if isinstance(r, dict) and r.get("date")), key=lambda r: r["date"])


def append_row(row: dict) -> None:
    broken_tail = STATE_LOG.exists() and STATE_LOG.stat().st_size and not STATE_LOG.read_bytes().endswith(b"\n")
    with STATE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if broken_tail else "") + json.dumps(row, ensure_ascii=False) + "\n")


def baseline(day: date) -> list[float]:
    """Up to WINDOW complete daily sums before `day`: the recorded ones, the seed for the days before them."""
    sums: dict[str, float] = {}
    if SEED.exists():
        for line in SEED.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip():
                d, amount = line.split(",")[:2]
                sums[d] = float(amount)
    for r in rows_logged():
        if r.get("etf_amount"):
            sums[r["date"]] = r["etf_amount"]
    return [sums[d] for d in sorted(k for k in sums if k < day.isoformat())][-WINDOW:]


def market_state(q: pl.DataFrame | None, day: date) -> dict:
    if q is None or q.is_empty():
        return {"ew_pct": None, "up_share": None, "n_ew": 0}
    c = closing(q, day).filter((pl.col("price") > 0) & (pl.col("prev_close") > 0))
    if c.is_empty():
        return {"ew_pct": None, "up_share": None, "n_ew": 0}
    r = c["price"] / c["prev_close"] - 1
    return {"ew_pct": _round(float(r.mean()) * 100), "up_share": _round(float((r > 0).mean())), "n_ew": c.height}


def context_state(x: pl.DataFrame, day: date) -> dict:
    last = fresh_last(x, day, SESSION_CLOSE)
    out = {}
    for name, symbol in CONTEXT.items():
        r = last.get(symbol)
        out[f"{name}_pct"] = _pct(r["price"], r["prev_close"]) if r else None
    idx = fresh_last(x, day, DAY_END).get("sh000001")
    out["sh_pct"] = _pct(idx["price"], idx["prev_close"]) if idx else None
    return out


def futures_state(x: pl.DataFrame, day: date) -> dict:
    """The main contract (highest open interest at the last poll) of each product, against its index at the close."""
    last = fresh_last(x, day, DAY_END)
    out: dict[str, dict | None] = {}
    for product, index in INDEX_OF.items():
        rows = [r for s, r in last.items() if (m := CONTRACT.match(s)) and m.group(1) == product and r.get("open_interest") is not None]
        if not rows or index not in last:
            out[product] = None
            continue
        main = max(rows, key=lambda r: (r["open_interest"], r["code"]))
        out[product] = {"contract": main["code"], "basis_pct": _pct(main["price"], last[index]["price"]),
                        "quote_ts": main["quote_ts"].isoformat() if main.get("quote_ts") else None}
    return out


def cffex_gap(x: pl.DataFrame, day: date) -> float | None:
    """Median minutes between changes of the IF main contract's quote_ts during the session."""
    main = futures_state(x, day).get("IF")
    if not main:
        return None
    a, b = datetime.combine(day, SESSION_OPEN), datetime.combine(day, SESSION_CLOSE)
    stamps = sorted(set(x.filter((pl.col("symbol") == main["contract"]) & (pl.col("quote_ts") >= a) & (pl.col("quote_ts") <= b))["quote_ts"].to_list()))
    gaps = [(u - v).total_seconds() / 60 for v, u in zip(stamps, stamps[1:])]
    return _round(statistics.median(gaps)) if gaps else None


def health_state(day_dir: Path, x: pl.DataFrame | None, day: date) -> dict:
    s = _json(day_dir / "summary.json")
    minutes = {sym: x.filter((pl.col("symbol") == sym) & (pl.col("kind") == "minute")).height if x is not None else 0 for sym in MINUTE_SYMBOLS}
    return {"end": s.get("end"), "missed": s.get("missed"), "failed_requests": s.get("failed_requests"), "never_returned": len(s.get("never_returned", [])),
            "blocked": sorted(s.get("blocked", {})), "merged": (day_dir / "quotes_ext.parquet").exists(), "a50_bars": minutes["em_CN00Y"],
            "cnh_bars": minutes["em_USDCNH"], "cffex_gap_min": cffex_gap(x, day) if x is not None else None}


def state_row(day: date, ext_dir: Path, q: pl.DataFrame | None) -> dict:
    x = read_ext(ext_dir)
    row: dict = {"date": day.isoformat(), **market_state(q, day), "health": health_state(ext_dir, x, day)}
    if x is None or x.is_empty():
        return {**row, "ext_missing": True, **NO_ETF_FLAGS}
    etf = etf_state(x, day)
    base = baseline(day)
    ratio = etf["etf_amount"] / statistics.median(base) if etf["etf_amount"] and len(base) >= WINDOW else None
    surge = None if ratio is None else ratio >= RATIO
    panic = None if surge is None or row["ew_pct"] is None else bool(surge and row["ew_pct"] <= PANIC_PCT)
    late = None if etf["late_share"] is None else etf["late_share"] >= LATE_SHARE
    return {**row, **etf, "ratio": _round(ratio), "surge": surge, "panic_support": panic, "late_heavy": late,
            **context_state(x, day), "futures": futures_state(x, day)}


def sessions(now: datetime) -> list[date]:
    """Sessions of the calendar since START whose recorders have ended."""
    cal = load_calendar()
    last = now.date() if now.time() >= READY_AT else now.date() - timedelta(days=1)
    days = (START + timedelta(days=i) for i in range((last - START).days + 1))
    return [d for d in days if cal.is_trading_day(d)]


def run(now: datetime | None = None) -> dict:
    root = Path(os.environ.get("PULSE_INTRADAY_DIR") or data_dir() / "intraday")
    ext = Path(os.environ.get("PULSE_INTRADAY_EXT_DIR") or data_dir() / "intraday_ext")
    done = {r["date"] for r in rows_logged()}
    LIVE.mkdir(parents=True, exist_ok=True)
    added, errors = [], []
    for day in sessions(now or datetime.now()):
        name = day.isoformat()
        if name in done or any(NOT_TRADING in str(_json(p / name / "summary.json").get("end")) for p in (root, ext)):
            continue
        try:
            row = state_row(day, ext / name, snapshots(root / name))
        except Exception as exc:  # a broken file costs that day's row, not the days after it
            row = {"date": name, "error": f"{type(exc).__name__}: {exc}"[:200], **NO_ETF_FLAGS}
            errors.append(name)
        append_row(row)
        added.append(name)
    return {"added": added, "errors": errors}


def compound(pcts: list[float | None], i: int, h: int) -> float | None:
    window = pcts[i + 1:i + 1 + h]
    if len(window) < h or any(p is None for p in window):
        return None
    return round((float(np.prod([1 + p / 100 for p in window])) - 1) * 100, ROUND)


def episodes(flags: list[bool | None]) -> list[int]:
    """Index of the first flagged day of each episode: flagged days within EPISODE_GAP rows are one episode."""
    starts, last = [], None
    for i, f in enumerate(flags):
        if f:
            if last is None or i - last > EPISODE_GAP:
                starts.append(i)
            last = i
    return starts


def streak(rows: list[dict], failed) -> int:
    n = 0
    for r in reversed(rows):
        if not failed(r):
            break
        n += 1
    return n


def actions(rows: list[dict]) -> dict:
    """E087's two pending decisions, from days the recorder ran; a lost day is another problem and gets its own line."""
    lost = [r["date"] for r in rows[-LOST_LOOKBACK:] if r.get("ext_missing") or r.get("error")]
    ran = [r for r in rows if not r.get("ext_missing") and not r.get("error")]
    miss = streak(ran, lambda r: not r["health"]["a50_bars"] or not r["health"]["cnh_bars"])
    slow = streak(ran, lambda r: (r["health"]["cffex_gap_min"] or 0) >= CFFEX_SLOW_MIN)
    todo = []
    if lost:
        todo.append(f"extended recording or state row lost on {', '.join(lost)}: read that day's run.log")
    if miss >= SOURCE_FAIL_DAYS:
        todo.append("A50 / CNH missing: fetch them from the PC pipeline and push to the server")
    if slow >= CFFEX_SLOW_DAYS:
        todo.append("CFFEX files update slowly in session: lower requests to one per 5 minutes")
    return {"a50_cnh_missing_days": miss, "cffex_slow_days": slow, "lost_days": lost, "todo": todo}


def summary() -> dict:
    rows = rows_logged()
    ew = [r.get("ew_pct") for r in rows]
    panic = episodes([r.get("panic_support") for r in rows])
    all5 = [v for i in range(len(rows)) if (v := compound(ew, i, 5)) is not None]
    return {"days": len(rows), "ext_missing": sum(1 for r in rows if r.get("ext_missing")), "errors": sum(1 for r in rows if r.get("error")),
            "with_ratio": sum(1 for r in rows if r.get("ratio") is not None),
            "surge_days": sum(1 for r in rows if r.get("surge")), "late_heavy_days": sum(1 for r in rows if r.get("late_heavy")),
            "late_share_median": _round(statistics.median(v)) if (v := [r["late_share"] for r in rows if r.get("late_share") is not None]) else None,
            "panic_episodes": len(panic), "h1_check_at": H1_EPISODES,
            "panic_next": [{"date": rows[i]["date"], **{f"ew_{h}": compound(ew, i, h) for h in NEXT}} for i in panic],
            "all_days_mean_next5": round(float(np.mean(all5)), ROUND) if all5 else None, **actions(rows)}


def seed(path: Path) -> dict:
    """One-off, on the PC: the seed from E074's daily bars. Only days with all seven ETFs count, as in E074."""
    e = pl.read_parquet(path).with_columns(pl.col("symbol").str.replace(r"^(?:sh|sz)", ""))
    wanted = [s[2:] for s in ETFS]
    daily = (e.filter(pl.col("symbol").is_in(wanted) & (pl.col("date") < START)).group_by("date")
             .agg(pl.col("amount").sum(), pl.len().alias("n")).filter(pl.col("n") == len(ETFS)).sort("date").tail(WINDOW + 5))
    LIVE.mkdir(parents=True, exist_ok=True)
    SEED.write_text("date,amount\n" + "".join(f"{d},{a}\n" for d, a in daily.select("date", "amount").iter_rows()), encoding="utf-8")
    return {"seed_days": daily.height, "last": str(daily["date"][-1]), "path": str(SEED)}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    out = seed(Path(sys.argv[2])) if arg == "seed" else run() if arg == "run" else summary()
    print(json.dumps(out, ensure_ascii=False))
