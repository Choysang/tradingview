"""Shared feature layer: fixed windows, warm-up, null policy, EMA init, FeatureKey identity, single-flight."""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pulse.contracts import check
from pulse.domain import features as F
from pulse.domain.features import indicators as ind
from pulse.domain.features.definitions import Series
from pulse.domain.features.key import FeatureKey, key_hash

UTC8 = timezone(timedelta(hours=8))
CLOSES = [10.0, 10.5, 10.2, 10.8, 11.0, 10.9, 11.4, 11.2, 11.8, 12.0, 11.7, 12.3, 12.1, 12.6, 12.4, 12.9, 13.1, 12.8, 13.3, 13.5]


def approx(a, b, tol=1e-9):
    return (a is None and b is None) or (a is not None and b is not None and math.isclose(a, b, abs_tol=tol))


def test_ma_window_warmup_and_null_policy():
    out = ind.ma([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None] and out[2:] == [2.0, 3.0, 4.0]
    holes = ind.ma([1, 2, None, 4, 5, 6], 3)
    assert holes == [None, None, None, None, None, 5.0]  # a None inside a window blanks it; nothing is skipped or zero-filled


def test_ema_sma_seed_and_first_value_init():
    x = [2.0, 4.0, 6.0, 8.0, 10.0]
    s = ind.ema(x, 3, "sma_seed")
    assert s[:2] == [None, None] and s[2] == 4.0 and approx(s[3], 4.0 + 0.5 * (8 - 4.0)) and approx(s[4], s[3] + 0.5 * (10 - s[3]))
    f = ind.ema(x, 3, "first_value")
    assert f[0] == 2.0 and approx(f[1], 3.0)
    gap = ind.ema([2.0, 4.0, 6.0, None, 8.0, 10.0, 12.0], 3, "sma_seed")
    assert gap[3] is None and gap[4] is None and gap[5] is None and gap[6] == 10.0  # state resets after a gap and re-warms


def naive_macd(close, fast, slow, signal, mult):
    def ema_naive(v, n):
        out, a = [], 2 / (n + 1)
        for i, y in enumerate(v):
            if y is None:
                out.append(None); continue
            vals = [z for z in out if z is not None]
            if i < n - 1 or any(q is None for q in v[:i + 1]):
                out.append(None)
            elif i == n - 1:
                out.append(sum(v[:n]) / n)
            else:
                out.append(out[-1] + a * (y - out[-1]))
        return out
    f, s = ema_naive(close, fast), ema_naive(close, slow)
    dif = [None if p is None or q is None else p - q for p, q in zip(f, s)]
    first = next(i for i, v in enumerate(dif) if v is not None)
    tail = ema_naive(dif[first:], signal)
    dea = [None] * first + tail
    return dif, dea, [None if a is None or b is None else (a - b) * mult for a, b in zip(dif, dea)]


@pytest.mark.parametrize("mult", [1, 2])
def test_macd_matches_independent_recursion_and_warmup_length(mult):
    out = ind.macd(CLOSES, 3, 6, 4, "sma_seed", mult)
    dif, dea, hist = naive_macd(CLOSES, 3, 6, 4, mult)
    assert all(approx(a, b) for a, b in zip(out["macd"], dif))
    assert all(approx(a, b) for a, b in zip(out["signal"], dea))
    assert all(approx(a, b) for a, b in zip(out["hist"], hist))
    assert sum(v is None for v in out["macd"]) == 5 and sum(v is None for v in out["signal"]) == 5 + 3
    assert F.definition("macd").warmup({"slow": 6, "signal": 4}) == 8  # first valid index of signal/hist


def test_rsi_atr_volume_ratio_relative_strength_known_values():
    up = [float(i) for i in range(1, 30)]
    assert ind.rsi(up, 14)[-1] == 100.0 and ind.rsi(up, 14)[13] is None and ind.rsi(up, 14)[14] == 100.0
    flat = [5.0] * 30
    assert ind.rsi(flat, 14)[-1] == 50.0
    hi, lo, cl = [11.0] * 20, [9.0] * 20, [10.0] * 20
    a = ind.atr(hi, lo, cl, 14)
    assert a[13] is None and approx(a[14], 2.0) and approx(a[-1], 2.0)
    vr = ind.volume_ratio([100, 100, 100, 100, 100, 300, None, 100, 100, 100, 100, 100, 100], 5)
    assert vr[5] == 3.0 and vr[6] is None and vr[7] is None  # the mean window containing a None is not defined
    rs = ind.relative_strength([10, 11, 12, 13], [100, 100, 110, 110], 2)
    assert rs[:2] == [None, None] and approx(rs[2], (12 / 10) / (110 / 100)) and approx(rs[3], (13 / 11) / (110 / 100))
    with pytest.raises(ValueError):
        ind.relative_strength([1, 2], [1], 1)


def test_compute_api_lengths_unknown_params_and_benchmark_requirement():
    s = Series(tuple(f"d{i}" for i in range(20)), tuple(CLOSES), tuple(CLOSES), tuple(CLOSES), tuple(CLOSES), tuple([1.0] * 20))
    for fid in ("ma", "ema", "macd", "rsi", "atr", "volume_ratio"):
        out = F.compute(fid, s)
        assert all(len(v) == 20 for v in out.values())
    with pytest.raises(ValueError, match="unknown parameters"):
        F.compute("ma", s, {"window": 5})
    with pytest.raises(KeyError):
        F.compute("nope", s)
    with pytest.raises(ValueError, match="benchmark"):
        F.compute("relative_strength", s)
    with pytest.raises(ValueError):
        Series(("a",), (1.0,), (1.0,), (1.0,), (1.0,), (1.0, 2.0))


def key(**over) -> FeatureKey:
    base = dict(feature_id="macd", params={"fast": 12}, instrument_ids=("260eb48a-a8aa-5638-a898-ea93c1e9d5b5",), timeframe="1d",
                adjustment_mode="raw", snapshot_id="6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d1111",
                decision_as_of=datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc), availability_policy_version="observed-v1",
                session_policy_version="regular_continuous_v1", input_revision="r1", input_schema_version="CanonicalBar-3.0.0",
                universe_policy_version="not-applicable", calendar_version="cal-1", adjustment_version="not-applicable",
                market_rules_version="rules-1")
    base.update(over)
    fid, params = base.pop("feature_id"), base.pop("params")
    return F.make_key(fid, params, **base)


def test_feature_key_has_19_dimensions_validates_against_the_contract_and_normalizes_time():
    k = key()
    wire = k.to_wire()
    assert len(wire) == 19
    check("FeatureKey", wire)
    same_instant = key(decision_as_of=datetime(2026, 11, 10, 15, 0, tzinfo=UTC8))
    assert key_hash(k) == key_hash(same_instant)
    assert key_hash(k) != key_hash(key(decision_as_of=datetime(2026, 11, 10, 7, 1, tzinfo=timezone.utc)))
    assert key_hash(k) != key_hash(key(snapshot_id="6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d2222"))
    assert key_hash(k) != key_hash(key(params={"fast": 13}))
    assert key_hash(k) != key_hash(key(adjustment_mode="forward_adjusted", adjustment_version="adj-1"))
    with pytest.raises(ValueError):
        key(decision_as_of=datetime(2026, 11, 10, 7, 0))
    with pytest.raises(ValueError):
        key(universe_policy_version=" ")
    with pytest.raises(ValueError):
        key(instrument_ids=("a", "a"))
    a, b = "260eb48a-a8aa-5638-a898-ea93c1e9d5b5", "6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d1111"
    assert key_hash(key(instrument_ids=(a, b))) != key_hash(key(instrument_ids=(b, a)))  # declared order is preserved
    assert F.definition("macd").implementation_digest == F.definition("macd").implementation_digest
    assert F.definition("macd").implementation_digest != F.definition("rsi").implementation_digest


def test_single_flight_runs_once_isolates_keys_and_retries_after_failure():
    async def scenario():
        store = F.SingleFlightFeatureStore(max_entries=4)
        calls = 0
        gate = asyncio.Event()

        async def compute():
            nonlocal calls
            calls += 1
            await gate.wait()
            return {"v": [1, 2, 3]}

        tasks = [asyncio.create_task(store.get(key(), compute)) for _ in range(50)]
        await asyncio.sleep(0.01)
        gate.set()
        results = await asyncio.gather(*tasks)
        assert calls == 1 and all(r == {"v": [1, 2, 3]} for r in results)
        results[0]["v"].append(99)
        assert (await store.get(key(), compute))["v"] == [1, 2, 3]  # consumers cannot poison the cache
        other = await store.get(key(decision_as_of=datetime(2026, 11, 11, tzinfo=timezone.utc)), compute)
        assert calls == 2 and other == {"v": [1, 2, 3]}

        async def boom():
            raise RuntimeError("input missing")

        with pytest.raises(RuntimeError):
            await store.get(key(snapshot_id="6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d3333"), boom)
        ok = await store.get(key(snapshot_id="6f0a7a8e-1c7b-4a53-9a43-2f9d9f8d3333"), compute)
        assert ok == {"v": [1, 2, 3]}  # a failure is not a cached empty success
        none_key = key(input_revision="r-none")

        async def returns_none():
            return None

        assert await store.get(none_key, returns_none) is None
        st = await store.stats()
        assert st.completed_entries <= 4
        await store.aclose()

    asyncio.run(scenario())
