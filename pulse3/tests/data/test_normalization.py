"""M1-E02: raw -> canonical mapping, units per exact endpoint, drift/missing/revision detection."""
from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from pulse.adapters.sources import legacy, sina, tencent
from pulse.domain.market.units import MappingDrift, comparable, detect_unit_change, normalize

from .helpers import NOW, PACKAGE_MAPPINGS, context, make_resolver, recorded

IID = "00000000-0000-4000-8000-000000000001"


def resolver(identifier, trade_date, market):
    if identifier in ("000001.SZ", "000001"):
        return IID
    raise ValueError("identifier_not_in_fixture")


def pkg_ctx(p, **kw):
    return {**{k: p[k] for k in ("source_id", "endpoint_id", "source_schema_version", "mapping_version")},
            "market": "XSHE", "trade_date": "2026-11-10", "adjustment_mode": "raw",
            "first_seen_at": "2026-11-10T07:00:01Z", "ingested_at": "2026-11-10T07:00:02Z",
            "available_at": "2026-11-10T07:00:03Z", **kw}


PROFILES = json.loads(PACKAGE_MAPPINGS.read_text(encoding="utf-8"))["mapping_profiles"] if PACKAGE_MAPPINGS.exists() else []
pytestmark = pytest.mark.skipif(not PROFILES, reason="spec_package mapping examples not present")


def run(i=0, changes=None, ctx=None):
    p = PROFILES[i]
    return normalize(p, {**p["synthetic_raw"], **(changes or {})}, ctx or pkg_ctx(p), resolver)


def test_package_mapping_expectations_hold_for_all_three_profiles():
    for i, p in enumerate(PROFILES):
        r = run(i)
        assert r["normalization_valid"], r["issues"]
        for key, expected in p["canonical_expected"].items():
            got = r[key]
            if isinstance(got, Decimal):
                assert got == Decimal(expected), key
            else:
                assert got == expected, key


def test_same_field_name_different_endpoint_units():
    daily, rt = run(0), run(1)
    assert daily["volume_shares"] == rt["volume_shares"] == 10025
    with pytest.raises(MappingDrift, match="endpoint_id"):
        normalize(PROFILES[0], PROFILES[0]["synthetic_raw"], pkg_ctx(PROFILES[1]), resolver)


def test_fractional_shares_are_invalid_not_rounded():
    r = run(changes={"vol": "100.001"})
    assert r["volume_shares"] is None and r["field_quality"]["volume_shares"]["status"] == "invalid"
    assert not r["normalization_valid"]


def test_null_and_zero_are_distinct():
    missing, zero = run(changes={"vol": None}), run(changes={"vol": "0", "amount": "0"})
    assert missing["volume_shares"] is None and missing["quality_status"] == "partial"
    assert zero["volume_shares"] == 0 and zero["field_quality"]["volume_shares"]["status"] == "observed"


def test_zero_volume_with_nonzero_amount_is_invalid():
    assert run(changes={"vol": "0"})["quality_status"] == "invalid"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", 100.25, True])
def test_nonfinite_negative_float_inputs_are_not_precise_facts(value):
    assert run(changes={"amount": value})["field_quality"]["turnover_amount_cny"]["status"] == "invalid"


def test_decimal_context_cannot_round_unit_conversion():
    with localcontext() as c:
        c.prec = 6
        r = run(changes={"amount": "9007199254740993.1234567890123"})
    assert r["turnover_amount_cny"] == Decimal("9007199254740993123.4567890123")


def test_precision_limits_and_unsafe_integers_rejected():
    assert run(changes={"amount": "1.1234567890123456"})["field_quality"]["turnover_amount_cny"]["status"] == "invalid"
    assert not run(1, changes={"vol": 9007199254740992})["normalization_valid"]


def test_adjusted_prices_cannot_enter_a_raw_mapping():
    with pytest.raises(MappingDrift, match="adjusted"):
        run(2, ctx={**pkg_ctx(PROFILES[2]), "adjust": "qfq"})


def test_required_field_rename_pauses_mapping_and_new_field_is_retained():
    raw = deepcopy(PROFILES[0]["synthetic_raw"])
    raw["volume"] = raw.pop("vol")
    with pytest.raises(MappingDrift, match="missing_mapped_keys"):
        normalize(PROFILES[0], raw, pkg_ctx(PROFILES[0]), resolver)
    r = run(changes={"vendor_future_field": "x"})
    assert r["extra_fields"] == {"vendor_future_field": "x"} and r["schema_drift"] == "additive_fields" and r["normalization_valid"]


def test_ohlc_contradiction_and_time_ordering():
    assert run(changes={"high": "9.00"})["field_quality"]["high_price_raw"]["status"] == "invalid"
    with pytest.raises(ValueError, match="availability"):
        run(ctx={**pkg_ctx(PROFILES[0]), "available_at": "2026-11-10T06:00:00Z"})
    with pytest.raises(MappingDrift, match="trade_date_context_conflict"):
        run(ctx={**pkg_ctx(PROFILES[0]), "trade_date": "2026-11-09"})


def test_times_and_finality_not_invented_and_revisions_keep_lineage():
    r = run(1)
    assert r["event_time"] is None and r["published_at"] is None and r["is_final"] is False
    final = run(ctx={**pkg_ctx(PROFILES[0]), "finalization_confirmed": True, "finalization_evidence_id": "audit-1"})
    assert final["is_final"] is True
    prior, revised = run(), run(changes={"amount": "101.00"})
    assert prior["raw_payload_hash"] != revised["raw_payload_hash"]
    assert prior["field_quality"]["turnover_amount_cny"]["raw_value"] == "100.25"


def test_comparable_requires_same_economic_observation():
    base = {**run(), "timeframe": "1d", "session_scope": "continuous", "event_time": "2026-11-10T07:00:00Z", "adjustment_version": "v1"}
    assert comparable(base, deepcopy(base))
    assert not comparable(base, {**base, "session_scope": "continuous_plus_afterhours"})
    assert not comparable(base, {**base, "event_time": None})


# ---- real recorded endpoints ---------------------------------------------------------------------
def ctx_for(spec_profile, **kw):
    return context(spec_profile, adjustment_mode=spec_profile.get("adjustment_mode", "raw"), **kw)


def test_sina_daily_and_tencent_daily_agree_on_shares_for_common_dates():
    """Sina volume is shares, Tencent volume is lots: after profile conversion both give the same shares (within one lot)."""
    res_s, res_t = make_resolver("sina"), make_resolver("tencent")
    s_rows = {r["day"]: r for r in sina.parse_kline(recorded("sina_kline_240_sz000001.json"), "sz000001")}
    t_rows = {r["date"]: r for r in tencent.parse_kline_day(recorded("tencent_day_raw_sz000001.json"), "sz000001")}
    common = sorted(set(s_rows) & set(t_rows))
    assert len(common) >= 20
    for day in common:
        ns = normalize(sina.KLINE_DAILY_PROFILE, s_rows[day], ctx_for(sina.KLINE_DAILY_PROFILE), res_s)
        nt = normalize(tencent.DAY_PROFILE, t_rows[day], ctx_for(tencent.DAY_PROFILE), res_t)
        assert ns["close_price_raw"] == nt["close_price_raw"]
        assert abs(ns["volume_shares"] - nt["volume_shares"]) <= 100, day
        assert ns["turnover_amount_cny"] is None and ns["field_quality"]["turnover_amount_cny"]["status"] == "missing"
        assert ns["quality_status"] == "partial"  # missing amount is never filled with 0


def test_sina_quote_units_and_zero_quote_is_missing():
    recs, empty = sina.parse_hq(recorded("sina_hq.txt"))
    assert len(recs) == 3 and not empty
    rec = next(r for r in recs if r["symbol"] == "sz000001")
    n = normalize(sina.HQ_PROFILE, rec, ctx_for(sina.HQ_PROFILE), make_resolver("sina"))
    assert isinstance(n["cumulative_volume_shares"], int) and n["cumulative_turnover_cny"] > 0
    assert n["event_time"].endswith("Z") and n["trade_date"] == "2026-11-11"
    # implied VWAP sits near the traded price: units are consistent (volume shares, amount yuan)
    assert detect_unit_change(n["cumulative_volume_shares"], n["cumulative_turnover_cny"], n["last_price_raw"]) is None
    # after the close the book is empty: ask price 0 is "no quote" (NULL), not 0
    assert n["ask_price_raw"] is None and n["field_quality"]["ask_price_raw"]["reason"] == "zero_means_no_quote"


def test_unit_change_detection_flags_lots_read_as_shares():
    price, vol, amt = Decimal("11.7"), 105_703_839, Decimal("1245341421.54")
    assert detect_unit_change(vol, amt, price) is None
    assert detect_unit_change(vol // 100, amt, price) == "unit_change_suspected:lots_as_shares"
    assert detect_unit_change(vol * 100, amt, price) == "unit_change_suspected:shares_as_lots"
    assert detect_unit_change(vol, amt / 1000, price) == "unit_change_suspected:yuan_as_thousand_yuan"


def test_tencent_quote_and_sina_quote_agree_on_cumulative_shares_and_amount():
    s, _ = sina.parse_hq(recorded("sina_hq.txt"))
    t, _ = tencent.parse_qt(recorded("tencent_qt.txt"))
    ns = normalize(sina.HQ_PROFILE, next(r for r in s if r["symbol"] == "sh600000"), ctx_for(sina.HQ_PROFILE), make_resolver("sina"))
    nt = normalize(tencent.QT_PROFILE, next(r for r in t if r["symbol"] == "sh600000"), ctx_for(tencent.QT_PROFILE), make_resolver("tencent"))
    assert ns["instrument_id"] == nt["instrument_id"]
    assert abs(ns["cumulative_volume_shares"] - nt["cumulative_volume_shares"]) <= 2000  # captured seconds apart


def test_tencent_schema_drift_is_detected_not_guessed():
    from pulse.domain.evidence.announcements import SchemaChanged

    body = recorded("tencent_qt.txt").decode("gbk")
    short = "~".join(body.split("~")[:30]) + '";'
    with pytest.raises(SchemaChanged):
        tencent.parse_qt(short.encode("gbk"))
    rows = tencent.parse_kline_day(recorded("tencent_day_raw_sz000001.json"), "sz000001")
    broken = {k: v for k, v in rows[0].items() if k != "volume_lots"}
    with pytest.raises(MappingDrift, match="missing_mapped_keys"):
        normalize(tencent.DAY_PROFILE, broken, ctx_for(tencent.DAY_PROFILE), make_resolver("tencent"))


def test_legacy_float_prices_are_precision_limited_and_amount_is_missing():
    raw = {"code": "000001", "date": "2026-11-10", "open": 11.83, "high": 11.88, "low": 11.73, "close": 11.86, "volume": 74889000.0,
           "amount": None}
    n = normalize(legacy.BARS_PROFILE, raw, {**ctx_for(legacy.BARS_PROFILE)}, make_resolver("legacy_warehouse"))
    assert n["close_price"] == Decimal("11.86") and n["volume_shares"] == 74889000
    assert n["field_quality"]["close_price"]["precision_note"].startswith("precision_limited")
    assert n["turnover_amount_cny"] is None and n["quality_status"] == "partial"
    assert n["adjustment_mode"] == "forward_adjusted"
    noisy = normalize(legacy.BARS_PROFILE, {**raw, "close": 10.828324032652016}, ctx_for(legacy.BARS_PROFILE), make_resolver("legacy_warehouse"))
    assert noisy["close_price"] == Decimal("10.828324032652") and "rounded half-even to 12 decimals" in noisy["field_quality"]["close_price"]["precision_note"]
    frac = normalize(legacy.BARS_PROFILE, {**raw, "volume": 74889000.5}, ctx_for(legacy.BARS_PROFILE), make_resolver("legacy_warehouse"))
    assert frac["volume_shares"] is None and frac["quality_status"] == "invalid"
