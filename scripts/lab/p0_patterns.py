"""P0 daily patterns (step 3): chip distribution, 缠论三买 and Wyckoff Spring, as event studies.

Every signal on day D uses bars up to D only and is bought at the D+1 open; outcomes are the D+5 and D+10
closes as excess over the same-day mean of tradable main-board stocks, D+1 sealed limit-up opens dropped.
Means carry t-stats clustered by date, split PRE / SELECT / VERIFY.

Chip distribution (成本分布, turnover decay): each day the existing chips shrink by the day's turnover t and
t new chips spread evenly over [low, high]. 120 price bins per stock between its lowest low and highest high.
- profit: share of chips at or below the close; conc90: (p95 - p5) / (p95 + p5) of the chips.
- single_peak_break: conc90 < 0.12 on D-1 and the close breaks above D-1's p95 with a gain over 3%.
- tombstone: 50%+ of chips sit 15%+ above the close (a heavy trapped peak overhead), a risk tag.
- profit and conc90 also get a daily rank IC against the outcomes.

缠论三买, daily and simplified: no K-line inclusion handling. Fractal tops / bottoms use two bars each side,
are known two bars later, alternate (a repeated type keeps the more extreme), and stand four bars apart.
The last three pens before a breakout form the center [ZD, ZG] (ZG = lowest pen high, ZD = highest pen low,
valid when ZG > ZD). Third buy: a pen leaves above ZG, the next pen pulls back with its low above ZG, and the
signal fires on the day the pullback's bottom fractal is confirmed.

Wyckoff Spring: a 40-bar trading range (bars D-45..D-6, high/low within 30%), then on D the low undercuts the
range low and the close is back above it. Variant spring_quiet adds volume below 1.5x its 20-day mean.

Writes p0_patterns.json in the lab folder (.work/lab/main/ under LAB_UNIVERSE=main).
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

import harness as H
from harness import CODE, LAB, MAIN_PREFIX, c, ctx, float_cap_pit, tradable
from lhasa import clustered

OUT = LAB / "p0_patterns.json"  # LAB is .work/lab/main under LAB_UNIVERSE=main
BINS = 120


def chips(o: np.ndarray, h: np.ndarray, lo: np.ndarray, cl: np.ndarray, turn: np.ndarray) -> np.ndarray:
    """Per day: profit, conc90, p5, p95, overhead (share 15%+ above the close). Rows are one stock, oldest first."""
    grid = np.linspace(np.nanmin(lo), np.nanmax(h), BINS + 1)
    mid = (grid[:-1] + grid[1:]) / 2
    dist = np.zeros(BINS)
    out = np.full((len(cl), 5), np.nan)
    for i in range(len(cl)):
        if not (np.isfinite(h[i]) and np.isfinite(lo[i])):
            continue
        band = (mid >= lo[i]) & (mid <= h[i])
        if not band.any():
            band = np.abs(mid - cl[i]) == np.abs(mid - cl[i]).min()
        t = 1.0 if dist.sum() == 0 else float(np.clip(turn[i], 0, 1)) if np.isfinite(turn[i]) else 0.0
        dist = dist * (1 - t)
        dist[band] += t / band.sum()
        tot = dist.sum()
        cdf = np.cumsum(dist) / tot
        p5, p95 = mid[np.searchsorted(cdf, 0.05)], mid[min(np.searchsorted(cdf, 0.95), BINS - 1)]
        out[i] = (dist[mid <= cl[i]].sum() / tot, (p95 - p5) / (p95 + p5), p5, p95, dist[mid > cl[i] * 1.15].sum() / tot)
    return out


def pivots(h: np.ndarray, lo: np.ndarray) -> list[tuple[int, int, float]]:
    """Alternating fractals (index, +1 top / -1 bottom, price), each confirmed at index + 2."""
    piv: list[tuple[int, int, float]] = []
    for i in range(2, len(h) - 2):
        kind = 1 if h[i] > max(h[i - 2], h[i - 1], h[i + 1], h[i + 2]) else -1 if lo[i] < min(lo[i - 2], lo[i - 1], lo[i + 1], lo[i + 2]) else 0
        if not kind:
            continue
        price = h[i] if kind == 1 else lo[i]
        if piv and piv[-1][1] == kind:
            if (kind == 1 and price > piv[-1][2]) or (kind == -1 and price < piv[-1][2]):
                piv[-1] = (i, kind, price)
            continue
        if piv and i - piv[-1][0] < 4:
            continue
        piv.append((i, kind, price))
    return piv


def third_buys(h: np.ndarray, lo: np.ndarray) -> np.ndarray:
    sig = np.zeros(len(h), dtype=bool)
    piv = pivots(h, lo)
    for j in range(5, len(piv)):
        # piv[j] is the pullback bottom; piv[j-1] the breakout top; piv[j-5..j-2] bound the three center pens
        if piv[j][1] != -1:
            continue
        center = piv[j - 5: j - 1]
        highs = [max(a[2], b[2]) for a, b in zip(center, center[1:])]
        lows = [min(a[2], b[2]) for a, b in zip(center, center[1:])]
        zg, zd = min(highs), max(lows)
        if zg <= zd:
            continue
        if piv[j - 1][2] > zg and piv[j][2] > zg and piv[j - 2][2] <= zg:
            k = piv[j][0] + 2  # the bottom fractal is known two bars later
            if k < len(h):
                sig[k] = True
    return sig


def third_buys_pit(h: np.ndarray, lo: np.ndarray) -> np.ndarray:
    """Point-in-time third buys (E060, 2026-11-07): on day t only the fractals known by t exist. `third_buys`
    runs `pivots` over the whole series, where a bottom later replaced by a lower one (no top in between)
    vanishes, so the signals it fired in real time are missing; on 60 sampled codes the full-series version
    kept 242 of 363 real-time signals. Equals third_buys(h[:t+1], lo[:t+1])[t] for every t."""
    n = len(h)
    sig = np.zeros(n, dtype=bool)
    piv: list[tuple[int, int, float]] = []
    for t in range(4, n):
        i = t - 2
        kind = 1 if h[i] > max(h[i - 2], h[i - 1], h[i + 1], h[i + 2]) else -1 if lo[i] < min(lo[i - 2], lo[i - 1], lo[i + 1], lo[i + 2]) else 0
        if not kind:
            continue
        price = h[i] if kind == 1 else lo[i]
        if piv and piv[-1][1] == kind:
            if (kind == 1 and price > piv[-1][2]) or (kind == -1 and price < piv[-1][2]):
                piv[-1] = (i, kind, price)
            else:
                continue
        elif piv and i - piv[-1][0] < 4:
            continue
        else:
            piv.append((i, kind, price))
        j = len(piv) - 1
        if kind != -1 or j < 5 or piv[j][0] != i:
            continue
        center = piv[j - 5: j - 1]
        highs = [max(a[2], b[2]) for a, b in zip(center, center[1:])]
        lows = [min(a[2], b[2]) for a, b in zip(center, center[1:])]
        zg, zd = min(highs), max(lows)
        if zg > zd and piv[j - 1][2] > zg and piv[j][2] > zg and piv[j - 2][2] <= zg:
            sig[t] = True
    return sig


def build() -> pl.DataFrame:
    k = ctx()
    p = k.panel.lazy().filter(c(CODE).str.slice(0, 3).is_in(MAIN_PREFIX)).sort(CODE, "date").with_columns(
        tradable().fill_null(False).alias("ok"), (c("amount") / float_cap_pit()).alias("turn")).collect()
    parts = []
    for (code,), g in p.group_by(CODE, maintain_order=True):
        if g.height < 60:
            continue
        h, lo, cl = g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy()
        ch = chips(g["open"].to_numpy(), h, lo, cl, g["turn"].to_numpy())
        parts.append(pl.DataFrame({CODE: [code] * g.height, "date": g["date"], "profit": ch[:, 0], "conc90": ch[:, 1],
                                   "p95": ch[:, 3], "overhead": ch[:, 4], "third_buy": third_buys(h, lo)}))
    feat = pl.concat(parts)
    nxt = c("open").shift(-1).over(CODE)
    q = p.lazy().join(feat.lazy(), on=[CODE, "date"], how="left").sort(CODE, "date").with_columns(
        ((c("close").shift(-5).over(CODE) / nxt - 1) * 100).alias("r5"),
        ((c("close").shift(-10).over(CODE) / nxt - 1) * 100).alias("r10"),
        c("open_at_limit").shift(-1).over(CODE).fill_null(False).alias("blocked"),
        c("conc90").shift(1).over(CODE).alias("conc90_prev"), c("p95").shift(1).over(CODE).alias("p95_prev"),
        c("high").shift(6).rolling_max(40).over(CODE).alias("rng_hi"), c("low").shift(6).rolling_min(40).over(CODE).alias("rng_lo"),
        c("volume").rolling_mean(20).over(CODE).alias("vol20"),
    ).filter(c("ok"))
    q = q.with_columns(*(pl.when(c(x).is_finite()).then(c(x)).alias(x) for x in ("r5", "r10")))
    q = q.with_columns(*((c(x) - c(x).mean().over("date")).alias(f"{x}_ex") for x in ("r5", "r10")))
    spring = (c("rng_hi") / c("rng_lo") < 1.3) & (c("low") < c("rng_lo")) & (c("close") > c("rng_lo"))
    return q.with_columns(
        ((c("conc90_prev") < 0.12) & (c("close") > c("p95_prev")) & (c("pct_chg") > 3)).fill_null(False).alias("single_peak_break"),
        (c("overhead") >= 0.5).fill_null(False).alias("tombstone"),
        c("third_buy").fill_null(False),
        spring.fill_null(False).alias("spring"),
        (spring & (c("volume") < 1.5 * c("vol20"))).fill_null(False).alias("spring_quiet"),
    ).select(CODE, "date", "blocked", "r5_ex", "r10_ex", "profit", "conc90", "single_peak_break", "tombstone",
             "third_buy", "spring", "spring_quiet").collect()


def daily_ic(df: pl.DataFrame, col: str, y: str) -> dict:
    ics = []
    for _, g in df.drop_nulls([col, y]).group_by("date"):
        if g.height >= 30:
            ics.append(np.corrcoef(g[col].rank().to_numpy(), g[y].rank().to_numpy())[0, 1])
    a = np.array([x for x in ics if np.isfinite(x)])
    return {"ic": round(float(a.mean()), 4), "t_naive": round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2), "days": len(a)} if len(a) > 2 else {}


def main() -> None:
    df = build()
    res: dict = {}
    for period in ("PRE", "SELECT", "VERIFY"):
        lo, hi = H.period_bounds(period)
        part = df.filter(c("date").is_between(lo, hi) & ~c("blocked"))
        r = {name: {y: clustered(part.filter(c(name)), y) for y in ("r5_ex", "r10_ex")}
             for name in ("single_peak_break", "tombstone", "third_buy", "spring", "spring_quiet")}
        r["ic"] = {f"{col}:{y}": daily_ic(part, col, y) for col in ("profit", "conc90") for y in ("r5_ex", "r10_ex")}
        res[period] = r
        print(f"--- {period}")
        for name, ys in r.items():
            print(f"  {name:18s}", "  ".join(f"{y} {v}" for y, v in ys.items()))
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
