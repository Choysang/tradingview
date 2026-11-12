"""Review fix M6: plan price levels come from Decimal raw prices; float kernel outputs are converted with a declared step."""
import uuid
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_HALF_EVEN

from pulse.application import release_service as R

# last 20 closes in cents: the exact MA20 is 10.565 (a half-cent tie); float summation gives 10.565000000000001
LAST20 = [1035, 1032, 1062, 1095, 1015, 1029, 1006, 1099, 1040, 1084, 1000, 1075, 1076, 1090, 1091, 1056, 1088, 1075, 1071, 1011]


def _series():
    cents = [1000 + (i % 7) for i in range(41)] + LAST20
    closes = [D(c) / 100 for c in cents]
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(len(closes))]
    f = [float(c) for c in closes]
    return {"d": days, "o": f, "h": [x + 0.5 for x in f], "l": [x - 0.5 for x in f], "c": f, "v": [1e6] * len(f), "dc": closes}


def test_ma_tie_is_rounded_half_even_like_the_exact_decimal_mean():
    s = _series()
    exact = (sum(s["dc"][-20:]) / 20)
    assert exact == D("10.565")
    ind = R.raw_indicators(s)
    assert ind.ma[20] == exact.quantize(R.TICK, ROUND_HALF_EVEN) == D("10.56")        # the float path used to give 10.57


def test_reference_close_is_the_decimal_raw_close_not_a_float_round_trip():
    s = _series()
    s["dc"][-1] = D("10.114999999999999999")          # not representable as a float: float path would read 10.115 -> 10.12
    s["c"][-1] = float(s["dc"][-1])
    assert R.raw_indicators(s).close == D("10.11")


def test_levels_equal_a_decimal_recomputation():
    s = _series()
    ind = R.raw_indicators(s)
    h = R.HORIZONS[1]
    lv = R.plan_conditions(uuid.UUID(int=1), h, ind)["levels"]
    atr, close = ind.atr, ind.close
    assert lv["entry_upper"] == close and lv["reference_close"] == close
    assert lv["entry_lower"] == (close - atr / 2).quantize(R.TICK, ROUND_HALF_EVEN)
    assert lv["stop"] == (close - atr * D("1.5")).quantize(R.TICK, ROUND_HALF_EVEN)
    assert lv["target"] == (close + atr * 2).quantize(R.TICK, ROUND_HALF_EVEN)
    assert all(isinstance(v, D) for k, v in lv.items() if k in ("entry_lower", "entry_upper", "stop", "target"))
