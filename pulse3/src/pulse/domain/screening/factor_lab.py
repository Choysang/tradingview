"""Factor lab: six factors with rank IC, ICIR, IC t-statistic, quantile returns and a non-overlapping long-short.

Why: LEGACY inventory "Factor lab (research/factor_lab, 6 factors)". The old module (read for SEMANTICS only) rank-correlates a
signed factor with the forward return per date, buckets names into quantiles and compounds top-minus-bottom on every h-th
date so holding periods never overlap. Same method here on the shared InstrumentView features, with the pulse3 rules added:
the evaluation window is explicit (a context slice), forward returns that would end AFTER the window end are not used (no
peeking into the validation/blind slice from a train-slice run), and tiny cross-sections give a MISSING IC, not 0.
Invariants: factors are oriented so higher = expected higher return; IC = Spearman (average ranks); a date with fewer than
MIN_NAMES usable names has no IC; ties in quantile bucketing are broken by instrument id (deterministic); the t-statistic is
optimistic because adjacent forward windows overlap (documented in ``notes``). No costs, no tradability: a factor study is not
a strategy result.
Not covered: industry/size neutralisation, costs, limit-up/suspension fills.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..playbooks.view import InstrumentView, ok

MIN_NAMES = 30
QUANTILES = 5


@dataclass(frozen=True)
class FactorDef:
    id: str
    name: str
    description: str
    value: Callable[[InstrumentView, int], float | None]


def _momentum_20(v: InstrumentView, i: int):
    return v.ret(20)[i]


def _reversal_5(v: InstrumentView, i: int):
    r = v.ret(5)[i]
    return None if r is None else -r


def _lowvol_20(v: InstrumentView, i: int):
    s = v.ret_std(20)[i]
    return None if s is None else -s


def _volume_surge(v: InstrumentView, i: int):
    return v.volume_ratio20()[i]


def _dist_high_60(v: InstrumentView, i: int):
    h = v.hhv("high", 60)[i]
    return None if not ok(h, v.close[i]) or h <= 0 else v.close[i] / h - 1.0


def _ma_gap_20(v: InstrumentView, i: int):
    m = v.ma(20)[i]
    return None if not ok(m, v.close[i]) or m <= 0 else v.close[i] / m - 1.0


FACTORS: tuple[FactorDef, ...] = (
    FactorDef("momentum_20", "20日动量", "收盘 / 20 日前收盘 - 1，追强势", _momentum_20),
    FactorDef("reversal_5", "5日反转", "-(收盘 / 5 日前收盘 - 1)，近 5 日跌得多的预期反弹", _reversal_5),
    FactorDef("lowvol_20", "20日低波", "-(近 20 日日收益率标准差)，低波动异象", _lowvol_20),
    FactorDef("volume_surge", "放量倍数", "今日成交量 / 前 20 日平均成交量", _volume_surge),
    FactorDef("dist_high_60", "距60日高点", "收盘 / 近 60 日最高价 - 1，越接近新高越高", _dist_high_60),
    FactorDef("ma_gap_20", "MA20乖离", "收盘 / 20 日均价 - 1", _ma_gap_20),
)
BY_ID = {f.id: f for f in FACTORS}


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (1-based)."""
    order = sorted(range(len(values)), key=values.__getitem__)
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = r
        i = j + 1
    return out


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)


@dataclass
class FactorResult:
    factor_id: str
    horizon: int
    days: int                                  # dates with an IC
    names_mean: float | None
    ic_mean: float | None
    ic_std: float | None
    icir: float | None
    ic_t: float | None
    ic_positive_share: float | None
    quantile_mean_return: list[float | None]   # index 0 = lowest factor bucket (ratio per horizon)
    quantile_count: list[int]
    long_short_return: float | None            # compounded top-bottom over non-overlapping periods (ratio)
    long_short_periods: int
    top_turnover: float | None
    ic_series: list[tuple[str, float | None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_factor(views: Sequence[InstrumentView], sessions: Sequence[str], factor: FactorDef, *, horizon: int,
               start: int, end: int) -> FactorResult:
    """Evaluate ``factor`` on session indexes [start, end) of ``sessions``; forward windows must end before ``end``."""
    pos = [{d: i for i, d in enumerate(v.dates)} for v in views]
    ics: list[tuple[str, float | None]] = []
    bucket_sum = [0.0] * QUANTILES
    bucket_n = [0] * QUANTILES
    per_date_buckets: dict[int, tuple[list[float], set[str]]] = {}
    names: list[int] = []
    for s in range(start, end - horizon):
        d, d2 = sessions[s], sessions[s + horizon]
        f_vals: list[float] = []
        r_vals: list[float] = []
        ids: list[str] = []
        for v, p in zip(views, pos):
            i, j = p.get(d), p.get(d2)
            if i is None or j is None:
                continue
            fv = factor.value(v, i)
            c0, c1 = v.close[i], v.close[j]
            if fv is None or not math.isfinite(fv) or not ok(c0, c1) or c0 <= 0:
                continue
            f_vals.append(fv)
            r_vals.append(c1 / c0 - 1.0)
            ids.append(v.instrument_id)
        if len(f_vals) < MIN_NAMES:
            ics.append((d, None))
            continue
        names.append(len(f_vals))
        ics.append((d, spearman(f_vals, r_vals)))
        order = sorted(range(len(f_vals)), key=lambda k: (f_vals[k], ids[k]))
        n = len(order)
        sums = [0.0] * QUANTILES
        cnt = [0] * QUANTILES
        top_ids: set[str] = set()
        for rank, k in enumerate(order):
            q = rank * QUANTILES // n
            sums[q] += r_vals[k]
            cnt[q] += 1
            if q == QUANTILES - 1:
                top_ids.add(ids[k])
        means = [sums[q] / cnt[q] for q in range(QUANTILES)]
        for q in range(QUANTILES):
            bucket_sum[q] += means[q] * cnt[q]
            bucket_n[q] += cnt[q]
        per_date_buckets[s] = (means, top_ids)
    vals = [x for _, x in ics if x is not None]
    mean = std = icir = t = pos_share = None
    if len(vals) >= 2:
        mean = sum(vals) / len(vals)
        std = math.sqrt(sum((x - mean) ** 2 for x in vals) / (len(vals) - 1))
        pos_share = sum(1 for x in vals if x > 0) / len(vals)
        if std > 1e-12:
            icir = mean / std
            t = icir * math.sqrt(len(vals))
    growth, periods, changed = 1.0, 0, []
    prev: set[str] | None = None
    for s in sorted(per_date_buckets)[::horizon]:
        means, top = per_date_buckets[s]
        growth *= 1.0 + (means[-1] - means[0])
        periods += 1
        if prev is not None and top:
            changed.append(1.0 - len(prev & top) / len(top))
        prev = top
    notes = [f"逐日 IC 为 Spearman 秩相关（截面少于 {MIN_NAMES} 只记缺失）；未来 {horizon} 日收益相邻日重叠，t 值偏乐观，多空每隔 {horizon} 个交易日取样。",
             "未扣交易成本、未考虑涨跌停/停牌成交，未做行业与市值中性化；因子研究不是策略结果。"]
    return FactorResult(
        factor.id, horizon, len(vals), (sum(names) / len(names)) if names else None, mean, std, icir, t, pos_share,
        [(bucket_sum[q] / bucket_n[q]) if bucket_n[q] else None for q in range(QUANTILES)], bucket_n,
        (growth - 1.0) if periods else None, periods, (sum(changed) / len(changed)) if changed else None, ics, notes)
