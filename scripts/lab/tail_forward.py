"""E077 / E078 forward logs from the recorded 60 s snapshots (docs/research/2026-11-08-batch4-preregistration.md).

Runs where the snapshots are (the server; PulseForwardLab after the recorders finish), or on any copy of the day dirs.
Reads `$PULSE_INTRADAY_DIR/<day>/quotes.parquet` (kind full; intraday.py), `$PULSE_INTRADAY_EXT_DIR/<day>/
quotes_ext.parquet` (E076, index sh000001, when present) and the warehouse daily bars (PULSE_DATA_DIR) for the
20-day limit-up history. Writes one row per decision day, append-only, to `$PULSE_LAB_LIVE` (default
<repo>/.work/lab/live): e077_tail.jsonl (once the next session's 10:00 snapshot exists) and e078_halfhour.jsonl.
E077 (tail-session picks): the pool at the 14:50 snapshot = main board, no ST, price >= 2, below limit-up; signal =
change in [3%, 5%] at both 14:30 and 14:50, volume ratio >= 1, turnover in [5%, 10%], float cap <= 200 亿, a close
>= +9.7% (main-board limit-up, as intraday.py's static pool) among the 20 sessions before; top 5 by volume ratio.
Buy at the 14:50 price, sell at the next session's 10:00 price; excess over the same float-cap quintile of the pool.
E078: x = 10:00 / previous close - 1, x2 = 10:00 / 09:30 open - 1, y = close / 14:30 - 1, for the main-board
equal-weight mean (from 2026-11-02) and 上证指数 (from E076's recorder).
Usage: python tail_forward.py run [--days N]   |   python tail_forward.py summary
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
LIVE = Path(os.environ.get("PULSE_LAB_LIVE") or ROOT / ".work" / "lab" / "live")
E077_LOG, E078_LOG = LIVE / "e077_tail.jsonl", LIVE / "e078_halfhour.jsonl"
START = date(2026, 11, 2)
PCT_LO, PCT_HI, VR_MIN, TO_LO, TO_HI, CAP_MAX, LIMIT_MOVE, LOOK, TOP = 3.0, 5.0, 1.0, 5.0, 10.0, 200.0, 0.097, 20, 5
COST = 0.252  # percent, round trip (USER_COSTS)
INDEX = "sh000001"


def data_dir() -> Path:
    return Path(os.environ.get("PULSE_DATA_DIR") or ROOT / "backend" / "data")


def day_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10 and p.name[4] == "-") if root.exists() else []


def snapshots(day_dir: Path) -> pl.DataFrame | None:
    f = day_dir / "quotes.parquet"
    if not f.exists():
        return None
    q = pl.read_parquet(f, columns=["code", "name", "kind", "poll_ts", "price", "prev_close", "open", "limit_up", "turnover",
                                     "float_cap", "volume_ratio"])
    return q.filter(pl.col("kind").is_in(["full", "close"]) & pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX)).sort("code", "poll_ts")


def at(q: pl.DataFrame, day: date, hhmm: time, kinds: tuple[str, ...] = ("full",), slack_min: int = 3) -> pl.DataFrame:
    """Per code, the first snapshot at or after day hhmm (within slack_min minutes)."""
    t0 = datetime.combine(day, hhmm)
    t1 = datetime.combine(day, time(hhmm.hour, min(59, hhmm.minute + slack_min)))
    w = q.filter(pl.col("kind").is_in(list(kinds)) & (pl.col("poll_ts") >= t0) & (pl.col("poll_ts") <= t1))
    return w.sort("code", "poll_ts").group_by("code", maintain_order=True).first()


def last_by(q: pl.DataFrame, day: date, start: time, end: time) -> pl.DataFrame:
    """Per code, the last row between start and end: the first row after 15:00 may be 5 s in, before the final print."""
    w = q.filter((pl.col("poll_ts") >= datetime.combine(day, start)) & (pl.col("poll_ts") <= datetime.combine(day, end)))
    return w.sort("code", "poll_ts").group_by("code", maintain_order=True).last()


def closing(q: pl.DataFrame, day: date) -> pl.DataFrame:
    c = at(q, day, time(15, 1), ("close",), 5)
    return c if c.height else at(q, day, time(15, 0), ("full",), 2)


def limit_history(day: date) -> set[str]:
    """Codes with a close >= +9.7% (main-board limit-up) in the LOOK sessions before `day` (warehouse bars)."""
    from backend.app.warehouse.store import active
    os.environ.setdefault("PULSE_DATA_DIR", str(data_dir()))
    b = active().scan("bars")
    if b is None:
        return set()
    b = b.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX) & (pl.col("date") < day)).select("code", "date", "close").collect()
    days = sorted(b["date"].unique().to_list())[-(LOOK + 1):]
    b = b.filter(pl.col("date").is_in(days)).sort("code", "date").with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("code") - 1).alias("r"))
    return set(b.filter((pl.col("date") > days[0]) & (pl.col("r") >= LIMIT_MOVE))["code"].unique().to_list())


def e077_signals(q: pl.DataFrame, day: date, hist: set[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(pool at 14:50 with pct and cap quintile, the signal rows)."""
    a = at(q, day, time(14, 30)).select("code", (pl.col("price") / pl.col("prev_close") * 100 - 100).alias("pct30"))
    b = at(q, day, time(14, 50)).filter(~pl.col("name").str.contains("ST") & (pl.col("price") >= 2) & (pl.col("prev_close") > 0)
                                        & (pl.col("price") < pl.col("limit_up")))
    pool = b.join(a, on="code", how="left").with_columns((pl.col("price") / pl.col("prev_close") * 100 - 100).alias("pct50"))
    pool = pool.sort("code").with_columns(((pl.col("float_cap").rank() / pl.col("float_cap").count()) * 5).ceil().clip(1, 5).alias("cap5"))
    sig = pool.filter(pl.col("pct30").is_between(PCT_LO, PCT_HI) & pl.col("pct50").is_between(PCT_LO, PCT_HI)
                      & (pl.col("volume_ratio") >= VR_MIN) & pl.col("turnover").is_between(TO_LO, TO_HI)
                      & (pl.col("float_cap") <= CAP_MAX) & pl.col("code").is_in(list(hist)))
    return pool, sig.sort(["volume_ratio", "code"], descending=[True, False]).head(TOP)


def e077_row(day: date, q: pl.DataFrame, nxt_day: date, nq: pl.DataFrame) -> dict:
    pool, sig = e077_signals(q, day, limit_history(day))
    exit10 = at(nq, nxt_day, time(10, 0)).select("code", pl.col("price").alias("px_exit"))
    p = pool.join(exit10, on="code", how="inner").with_columns((pl.col("px_exit") / pl.col("price") * 100 - 100).alias("ret"))
    p = p.with_columns(pl.col("ret").mean().over("cap5").alias("cell"))
    s = p.filter(pl.col("code").is_in(sig["code"].to_list()))
    row = {"date": day.isoformat(), "exit_day": nxt_day.isoformat(), "pool": pool.height, "signals": sig["code"].to_list(),
           "n": s.height, "missing_exit": sig.height - s.height}
    if s.height:
        row.update({"ret": round(float(s["ret"].mean()), 3), "cell": round(float(s["cell"].mean()), 3),
                    "ex": round(float((s["ret"] - s["cell"]).mean()), 3), "net": round(float(s["ret"].mean()) - COST, 3)})
    return row


def halfhour(prices: dict[str, pl.DataFrame]) -> dict | None:
    """x, x2, y as means over the codes present at every time point."""
    j = None
    for k, f in prices.items():
        f = f.select("code", pl.col("price").alias(k), *([pl.col("prev_close"), pl.col("open")] if k == "p1000" else []))
        j = f if j is None else j.join(f, on="code", how="inner")
    if j is None or j.is_empty():
        return None
    j = j.filter((pl.col("prev_close") > 0) & (pl.col("open") > 0))
    return {"n": j.height, "x": round(float((j["p1000"] / j["prev_close"] - 1).mean()) * 100, 4),
            "x2": round(float((j["p1000"] / j["open"] - 1).mean()) * 100, 4),
            "y": round(float((j["pclose"] / j["p1430"] - 1).mean()) * 100, 4)}


def e078_row(day: date, q: pl.DataFrame, day_dir_ext: Path | None) -> dict:
    row: dict = {"date": day.isoformat()}
    ew = halfhour({"p1000": at(q, day, time(10, 0)), "p1430": at(q, day, time(14, 30)), "pclose": closing(q, day)})
    row["ew"] = ew
    f = day_dir_ext / "quotes_ext.parquet" if day_dir_ext else None
    if f is not None and f.exists():
        x = pl.read_parquet(f).filter(pl.col("symbol") == INDEX).rename({"symbol": "code"}).with_columns(pl.lit("full").alias("kind"))
        row["index"] = halfhour({"p1000": at(x, day, time(10, 0)), "p1430": at(x, day, time(14, 30)),
                                 "pclose": last_by(x, day, time(15, 0), time(15, 5))}) if x.height else None
    return row


def logged(path: Path) -> set[str]:
    return {json.loads(x)["date"] for x in path.read_text(encoding="utf-8").splitlines() if x.strip()} if path.exists() else set()


def run(max_days: int = 30) -> dict:
    root = Path(os.environ.get("PULSE_INTRADAY_DIR") or data_dir() / "intraday")
    ext = Path(os.environ.get("PULSE_INTRADAY_EXT_DIR") or data_dir() / "intraday_ext")
    dirs = [d for d in day_dirs(root) if date.fromisoformat(d.name) >= START and (d / "quotes.parquet").exists()][-max_days:]
    done77, done78 = logged(E077_LOG), logged(E078_LOG)
    LIVE.mkdir(parents=True, exist_ok=True)
    added = {"e077": 0, "e078": 0}
    cache: dict[str, pl.DataFrame | None] = {}
    snap = lambda d: cache.setdefault(d.name, snapshots(d))  # noqa: E731
    for i, d in enumerate(dirs):
        day = date.fromisoformat(d.name)
        if d.name not in done78 and snap(d) is not None:
            with E078_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e078_row(day, snap(d), ext / d.name), ensure_ascii=False) + "\n")
            added["e078"] += 1
        if d.name not in done77 and i + 1 < len(dirs) and snap(d) is not None and snap(dirs[i + 1]) is not None:
            with E077_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(e077_row(day, snap(d), date.fromisoformat(dirs[i + 1].name), snap(dirs[i + 1])), ensure_ascii=False) + "\n")
            added["e077"] += 1
    return {"days": [d.name for d in dirs], "added": added}


def summary() -> dict:
    rows77 = [json.loads(x) for x in E077_LOG.read_text(encoding="utf-8").splitlines() if x.strip()] if E077_LOG.exists() else []
    ex = np.array([r["ex"] for r in rows77 if r.get("ex") is not None])
    rng = np.random.default_rng(20261108)
    p = float((ex[rng.integers(0, ex.size, (10_000, ex.size))].mean(axis=1) <= 0).mean()) if ex.size > 1 else None
    rows78 = [json.loads(x) for x in E078_LOG.read_text(encoding="utf-8").splitlines() if x.strip()] if E078_LOG.exists() else []
    return {"e077": {"decision_days": len(rows77), "signal_days": int(ex.size), "mean_ex": round(float(ex.mean()), 3) if ex.size else None,
                     "p_boot": p, "mean_net": round(float(np.mean([r["net"] for r in rows77 if r.get("net") is not None])), 3) if ex.size else None,
                     "check_at": 40},
            "e078": {"days": len(rows78), "index_days": sum(1 for r in rows78 if r.get("index")), "check_at": 60}}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "run"
    print(json.dumps(run() if arg == "run" else summary(), ensure_ascii=False))
