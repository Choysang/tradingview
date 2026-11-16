"""Seal facts on hand-made snapshots (pure domain): first seal, broken and re-sealed, never sealed, unknown values, seal clock."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal as D

from pulse.domain.market_state import seal as S

UTC = timezone.utc
DAY = date(2026, 11, 12)
LU = D("11.00")


def at(hhmm: str) -> datetime:
    """CST wall clock of DAY -> UTC instant."""
    h, m, *s = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 12, h, m, s[0] if s else 0, tzinfo=S.CST).astimezone(UTC)


def snap(t: str, price: str, bid_px="11.00", bid_lots="5000", asks=("0", "0", "0", "0", "0")):
    return S.SealSnapshot(at(t), D(price), LU, None if bid_px is None else D(bid_px), None if bid_lots is None else D(bid_lots),
                          tuple(None if a is None else D(a) for a in asks))


def sealed(t: str, lots="5000"):
    return snap(t, "11.00", bid_lots=lots)


def open_(t: str):
    return snap(t, "10.90", bid_px="10.89", bid_lots="400", asks=("500", "0", "0", "0", "0"))


def test_first_seal_broken_and_resealed():
    f = S.derive("000001", "平安", DAY, [snap("09:20", "11.00"), open_("09:31"), sealed("09:35"), sealed("10:00"), open_("10:10"), sealed("10:20", "7000"),
                                         sealed("14:50", "7500"), sealed("14:56", "8000")])
    assert f.first_seal_at == at("09:35") and f.last_seal_at == at("10:20")
    assert f.broken_count == 1 and f.touched_limit and f.sealed_at_close is True
    assert f.close_seal_order_amount_cny == D("8800000.00")            # 8000 lots x 100 x 11.00
    assert f.first_seal_uncertainty_seconds == 4 * 60 and f.estimation == "estimated_from_60s_snapshots"
    assert f.snapshots_in_window == 7                                  # the 09:20 auction match is outside the windows


def test_never_sealed_asks_standing_and_never_touched():
    touched = S.derive("600000", None, DAY, [snap("09:40", "11.00", bid_lots="100", asks=("300", "0", "0", "0", "0")), open_("14:55")])
    assert touched.touched_limit and touched.first_seal_at is None and touched.last_seal_at is None
    assert touched.broken_count == 0 and touched.sealed_at_close is False and touched.close_seal_order_amount_cny is None
    never = S.derive("000002", None, DAY, [open_("09:40"), open_("14:55")])
    assert not never.touched_limit and never.first_seal_at is None


def test_broken_for_good_is_not_sealed_at_close_and_close_unobserved_is_none():
    f = S.derive("600519", None, DAY, [sealed("09:40"), open_("10:30"), open_("14:55")])
    assert f.broken_count == 1 and f.sealed_at_close is False and f.last_seal_at == at("09:40")
    early = S.derive("600519", None, DAY, [sealed("09:40"), sealed("11:20")])              # recording stops at 11:20
    assert early.sealed_at_close is None and early.close_seal_order_amount_cny is None     # not observed, never False


def test_unknown_values_change_nothing():
    deep = snap("10:06", "11.00", asks=("0", "300", "0", "0", "0"))
    assert S.classify(deep) is False                                                         # a deeper ask contradicts the empty best ask
    assert S.classify(snap("10:06", "11.00", asks=("0", None, None, None, None))) is True     # level 1 only (native recorder)
    unknown = snap("10:05", "11.00", asks=(None, "0", "0", "0", "0"))                     # the best ask is missing
    f = S.derive("000001", None, DAY, [sealed("09:35"), unknown, sealed("10:10")])
    assert S.classify(unknown) is None and f.broken_count == 0 and f.first_seal_at == at("09:35")
    nobid = S.SealSnapshot(at("09:36"), LU, LU, None, None, (D(0),) * 5)
    assert S.classify(nobid) is None
    nolimit = S.SealSnapshot(at("09:36"), D("11.00"), None, D("11.00"), D(10), (D(0),) * 5)
    assert S.classify(nolimit) is None and not S.touched(nolimit)


def test_lunch_gap_is_not_a_sampling_gap_and_window_edges():
    f = S.derive("000001", None, DAY, [sealed("11:28"), sealed("11:29"), sealed("13:01"), sealed("13:03")])
    assert f.max_gap_seconds == 120 and f.broken_count == 0
    assert S.in_window(at("09:25:00")) and S.in_window(at("14:56:59")) and not S.in_window(at("14:57:00")) and not S.in_window(at("09:24:59")) and not S.in_window(at("12:00"))


def test_seal_clock_histogram_and_cumulative_curve():
    facts = [S.derive("a", None, DAY, [sealed("09:25")]), S.derive("b", None, DAY, [open_("09:31"), sealed("09:36")]),
             S.derive("c", None, DAY, [sealed("09:37")]), S.derive("d", None, DAY, [sealed("14:56")]), S.derive("e", None, DAY, [open_("10:00")])]
    clock = S.seal_clock(facts, 5)
    by = {b["clock"]: b for b in clock["buckets"]}
    assert by["09:25"]["first_seal_count"] == 1 and by["09:35"]["first_seal_count"] == 2 and by["14:55"]["first_seal_count"] == 1
    assert by["09:35"]["cumulative_count"] == 3 and by["14:55"]["cumulative_ratio"] == D("1.0000")
    assert clock["sealed_ever_count"] == 4 and clock["instruments_observed"] == 5 and clock["touched_limit_count"] == 4
    assert [b["clock"] for b in clock["buckets"]][:3] == ["09:25", "09:30", "09:35"] and "11:30" not in by and "12:00" not in by and "13:00" in by
    empty = S.seal_clock([S.derive("e", None, DAY, [open_("10:00")])])
    assert all(b["cumulative_ratio"] is None for b in empty["buckets"])               # nobody sealed: no ratio, not 0


def test_closing_call_auction_is_not_a_broken_seal():
    """14:57-15:00 is the closing call auction: the vendor shows bid 1 = ask 1 = the tentative match (ask lots > 0) and an empty
    book at 15:00 (server data 2026-11-13, 001358 and 002685). Neither is a break; the close state is the one before 14:57."""
    auction = [snap("14:57:54", "11.00", bid_lots="18", asks=("18", "0", "0", "0", "0")),
               snap("14:59:42", "11.00", bid_lots="35", asks=("35", "0", "0", "0", "0")),
               snap("15:00:01", "11.00", bid_px="0", bid_lots="0", asks=("0", "0", "0", "0", "0"))]
    f = S.derive("001358", None, DAY, [sealed("10:00"), sealed("14:56", "185888")] + auction)
    assert f.broken_count == 0 and f.sealed_at_close is True and f.snapshots_in_window == 2
    assert f.close_seal_order_amount_cny == D("204476800.00")          # 185888 lots x 100 x 11.00
    assert f.policy_version == "seal-facts-v2"
