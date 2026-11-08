"""Deterministic synthetic history for the SIM dataset.

The generator is built so that research code has something real to find:
markets alternate between bull, bear and range segments; stock returns load on
a market factor and a size factor; idiosyncratic returns trend in bull
segments and mean-revert in ranges; limit-up chains start more often and last
longer when sentiment is hot. A momentum playbook should therefore win in bull
segments and a reversal playbook in ranges — if the backtester finds that, the
pipeline works. None of it is market data, and every consumer labels it SIM.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import numpy as np
import polars as pl

from ..market.universe import LARGE_CAP, universe
from .store import Warehouse

SEED = 20261030
DAYS = 780
SEGMENTS = {
    # name: (daily drift, daily vol, idio autocorrelation, limit-up start prob, chain continuation, liquidity)
    "bull": (0.0016, 0.012, 0.10, 0.012, 0.55, 1.35),
    "bear": (-0.0013, 0.016, 0.0, 0.003, 0.25, 0.75),
    "range": (0.0, 0.009, -0.12, 0.006, 0.38, 0.95),
}
INDEXES = {"000985": "中证全指（合成）", "000300": "沪深300（合成）", "000852": "中证1000（合成）"}


def _weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days[::-1]


def _segments(rng: np.random.Generator, count: int) -> list[str]:
    labels: list[str] = []
    order = ["range", "bull", "range", "bear", "bull", "range", "bear", "range", "bull"]
    index = 0
    while len(labels) < count:
        length = int(rng.integers(45, 130))
        labels += [order[index % len(order)]] * length
        index += 1
    return labels[:count]


def _ratio(code: str) -> float:
    if code.startswith(("30", "68")):
        return 0.20
    if code[0] in "48" or code.startswith("92"):
        return 0.30
    return 0.10


def generate(end: date | None = None, days: int = DAYS) -> dict[str, pl.DataFrame]:
    """Return bars, index_bars and securities frames for the SIM dataset."""
    end = end or date.today()
    calendar = _weekdays(end, days)
    rng = np.random.default_rng(SEED)
    regime = _segments(rng, days)
    drift = np.array([SEGMENTS[label][0] for label in regime])
    vol = np.array([SEGMENTS[label][1] for label in regime])
    market = drift + vol * rng.standard_normal(days)
    # Small caps lead in bull runs and lag in drawdowns: a style factor to detect.
    size = np.array([0.0006 if label == "bull" else -0.0004 if label == "bear" else 0.0001 for label in regime])
    size = size + 0.004 * rng.standard_normal(days)

    identities = universe()
    count = len(identities)
    codes = [identity["code"] for identity in identities]
    large = np.array([code in LARGE_CAP for code in codes])
    ratio = np.array([_ratio(code) for code in codes])
    # Static per-stock traits come from per-code generators, so adding a stock
    # never reshuffles the others.
    beta = np.empty(count)
    exposure = np.empty(count)
    price = np.empty(count)
    base_volume = np.empty(count)
    statics = []
    for index, code in enumerate(codes):
        stock_rng = np.random.default_rng(SEED + int(code))
        beta[index] = stock_rng.uniform(0.6, 1.4)
        exposure[index] = -0.6 if large[index] else stock_rng.uniform(0.4, 1.4)
        price[index] = stock_rng.uniform(40, 260) if large[index] else stock_rng.uniform(4, 60)
        base_volume[index] = stock_rng.uniform(2e7, 9e7) if large[index] else stock_rng.uniform(3e6, 3e7)
        statics.append(stock_rng)
    idio_vol = np.where(large, 0.012, 0.02) * np.where(ratio >= 0.2, 1.3, 1.0)

    # Daily draws for the whole market at once: the loop runs over days, not
    # stock-days, so building the set never holds the interpreter for seconds.
    draws = np.random.default_rng(SEED + 1)
    shape = (days, count)
    eps = draws.standard_normal(shape)
    u_carry, u_start, u_seal, u_word, u_volume = (draws.random(shape) for _ in range(5))
    reversal = draws.normal(-0.015, 0.035, shape)
    gap_noise, high_noise, low_noise = (draws.standard_normal(shape) for _ in range(3))
    u_volume = 0.7 + 0.6 * u_volume

    out = {key: np.empty(shape) for key in ("open", "high", "low", "close", "volume")}
    returns = np.empty(shape)
    idio_prev = np.zeros(count)
    chain = np.zeros(count, dtype=np.int64)
    small = ~large
    for t in range(days):
        _, _, phi, start_prob, carry, liquidity = SEGMENTS[regime[t]]
        idio = phi * idio_prev + idio_vol * eps[t]
        idio_prev = idio
        raw = beta * market[t] + exposure * size[t] + idio
        in_chain = small & (chain > 0)
        continues = in_chain & (u_carry[t] < carry)
        snapped = in_chain & ~continues
        raw = np.where(snapped, reversal[t], raw)
        starts = small & (chain == 0) & (u_start[t] < start_prob)
        sealed = continues | (starts & (u_seal[t] > 0.25))
        broken = starts & ~sealed
        chain = np.where(sealed, chain + 1, 0)
        raw = np.clip(np.where(sealed, ratio, raw), -ratio, ratio)
        previous = price
        limit_up = np.round(previous * (1 + ratio), 2)
        floor = previous * (1 - ratio)
        close = np.where(sealed, limit_up, np.round(np.maximum(0.5, previous * (1 + raw)), 2))
        gap = 0.35 * raw + 0.004 * gap_noise[t]
        opening = np.round(np.minimum(limit_up, np.maximum(floor, previous * (1 + gap))), 2)
        opening = np.where(sealed & (u_word[t] < 0.2), limit_up, opening)  # one-word board
        high = np.round(np.minimum(limit_up, np.maximum(opening, close) * (1 + np.abs(0.006 * high_noise[t]))), 2)
        low = np.round(np.maximum(floor, np.minimum(opening, close) * (1 - np.abs(0.006 * low_noise[t]))), 2)
        high = np.where(broken, limit_up, high)
        close = np.where(broken, np.round(np.minimum(close, limit_up * 0.97), 2), close)
        low = np.where(broken, np.minimum(low, close), low)
        word = sealed & (opening == limit_up)
        low = np.where(word, limit_up, low)
        high = np.where(word, limit_up, high)
        volume = base_volume * liquidity * (1 + 2.5 * np.abs(close / previous - 1) / ratio) * u_volume[t]
        volume = np.where(word, volume * 0.25, volume)
        out["open"][t] = opening
        out["high"][t] = np.maximum.reduce([high, opening, close])
        out["low"][t] = np.minimum.reduce([low, opening, close])
        out["close"][t] = close
        out["volume"][t] = np.round(volume)
        returns[t] = close / previous - 1
        price = close

    # Code-major rows, the order every reader expects.
    rows = {
        "code": np.repeat(codes, days),
        "date": np.tile(np.array(calendar, dtype="datetime64[D]"), count),
        **{key: values.T.reshape(-1) for key, values in out.items()},
    }
    securities = []
    for index, identity in enumerate(identities):
        stock_rng = statics[index]
        float_cap = price[index] * base_volume[index] * stock_rng.uniform(40, 120) / 1e8
        securities.append({
            "code": identity["code"],
            "name": identity["name"],
            "board": identity["board"],
            "total_cap": round(float_cap * stock_rng.uniform(1.0, 1.6), 2),
            "float_cap": round(float_cap, 2),
            "pe_ttm": round(float(stock_rng.uniform(6, 80)) if stock_rng.random() > 0.08 else -float(stock_rng.uniform(5, 60)), 2),
            "pb": round(float(stock_rng.uniform(0.5, 9)), 2),
            "theme": identity["theme_tags"][0] if identity["theme_tags"] else None,
        })

    index_rows: dict[str, list] = {key: [] for key in ("symbol", "date", "open", "high", "low", "close", "volume")}
    weight = np.where(large, 3.0, 1.0)
    members = {"000985": np.ones(count, dtype=bool), "000300": large, "000852": small}
    for symbol, member in members.items():
        daily = (returns[:, member] * weight[member]).sum(axis=1) / weight[member].sum()
        turnover = np.abs(returns[:, member]).sum(axis=1) * 1e10
        level = 3000.0
        for t, value in enumerate(daily):
            previous = level
            level = previous * (1 + value)
            index_rows["symbol"].append(symbol)
            index_rows["date"].append(calendar[t])
            index_rows["open"].append(round(previous * (1 + 0.3 * value), 2))
            index_rows["high"].append(round(max(previous, level) * (1 + abs(value) * 0.3), 2))
            index_rows["low"].append(round(min(previous, level) * (1 - abs(value) * 0.3), 2))
            index_rows["close"].append(round(level, 2))
            index_rows["volume"].append(float(round(turnover[t])))

    return {
        "bars": pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date), pl.col("volume").cast(pl.Float64)),
        "index_bars": pl.DataFrame(index_rows).with_columns(pl.col("date").cast(pl.Date)),
        "securities": pl.DataFrame(securities).with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("list_date"), pl.lit(end.isoformat()).alias("as_of")
        ),
        "regimes": pl.DataFrame({"date": calendar, "segment": regime}).with_columns(pl.col("date").cast(pl.Date)),
    }


def ensure_synthetic(sim: Warehouse, today: date | None = None) -> None:
    """(Re)build the SIM dataset when missing or when its last day is stale."""
    today = today or date.today()
    end = _weekdays(today, 1)[0]
    marker = sim.read_doc("meta", "synthetic")
    if marker and marker.get("end") == end.isoformat() and sim.has_bars():
        return
    frames = generate(end)
    for table in ("bars", "index_bars", "securities"):
        sim.replace(table, frames[table])
    sim.write_doc("meta", "synthetic", {
        "end": end.isoformat(),
        "seed": SEED,
        "days": DAYS,
        "segments": frames["regimes"].with_columns(pl.col("date").cast(pl.Utf8)).to_dicts()[:: max(1, DAYS // 60)],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
