"""Pure logic: finality, point-in-time revisions, refresh planning, and the reconcile report on a small synthetic legacy/native pair."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl

from pulse.application import refdata_core as C

UTC = timezone.utc
D = Decimal
T0 = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
IID = "00000000-0000-0000-0000-000000000001"
IID2 = "00000000-0000-0000-0000-000000000002"


def bar(day: date, close: str, *, iid: str = IID, vol: int = 1000, final: bool = True, at: datetime = T0, revision: int = 1) -> dict:
    c = D(close)
    return {"instrument_id": iid, "trade_date": day, "timeframe": "1d", "bar_open_time": at, "bar_close_time": at, "open_price": c, "high_price": c,
            "low_price": c, "close_price": c, "volume_shares": vol, "turnover_amount_cny": None, "adjustment_mode": "forward_adjusted",
            "adjustment_version": "sina-ratio-qfq-v1", "is_final": final, "available_at": at, "quality_status": "partial", "revision": revision}


def frame(*rows: dict) -> pl.DataFrame:
    return pl.DataFrame(list(rows), schema=C.ADJ_SCHEMA)


# ---- session finality ------------------------------------------------------------------------------------------------
def test_session_cut_is_today_on_a_weekday_before_1505_cst_only():
    cst = C.CST
    assert C.session_cut(datetime(2026, 11, 19, 10, 30, tzinfo=cst)) == date(2026, 11, 19)
    assert C.session_cut(datetime(2026, 11, 19, 15, 5, tzinfo=cst)) is None
    assert C.session_cut(datetime(2026, 11, 21, 10, 30, tzinfo=cst)) is None            # Saturday
    rows = [{"date": date(2026, 11, d)} for d in (17, 18, 19)]
    assert [r["date"].day for r in C.complete_rows(rows, date(2026, 11, 19))] == [17, 18]


# ---- point-in-time revisions ---------------------------------------------------------------------------------------------
def test_new_keys_are_revision_one_and_identical_refetch_publishes_nothing():
    fresh = frame(bar(date(2026, 11, 17), "10.00"), bar(date(2026, 11, 18), "10.10"))
    first = C.next_revisions(None, fresh)
    assert first["revision"].to_list() == [1, 1] and first.height == 2
    again = C.next_revisions(first, fresh.with_columns(pl.lit(T1).alias("available_at")))
    assert again.is_empty()                                                              # same values, only a later fetch time


def test_changed_past_values_become_a_new_revision_never_an_overwrite():
    stored = frame(bar(date(2026, 11, 17), "10.00"), bar(date(2026, 11, 18), "10.10"))
    after_ex_right = frame(bar(date(2026, 11, 17), "9.50", at=T1), bar(date(2026, 11, 18), "10.10", at=T1))   # only the first date moved
    new = C.next_revisions(stored, after_ex_right)
    assert new.height == 1 and new["trade_date"].to_list() == [date(2026, 11, 17)] and new["revision"].to_list() == [2]
    assert new["available_at"].to_list() == [T1]                                         # available_at is the fetch time of the new fact
    cumulative = pl.concat([stored, new])
    assert C.latest_bars(cumulative).filter(pl.col("trade_date") == date(2026, 11, 17))["close_price"].item() == D("9.50")
    # a reader at the earlier cutoff still sees revision 1: the old fact is intact
    seen = cumulative.filter(pl.col("available_at") <= T0).sort("revision").unique(C.BAR_KEY, keep="last")
    assert seen.filter(pl.col("trade_date") == date(2026, 11, 17))["close_price"].item() == D("10.00")


def test_a_bar_becoming_final_is_a_revision_and_volume_changes_count():
    stored = frame(bar(date(2026, 11, 18), "10.00", final=False))
    promoted = C.next_revisions(stored, frame(bar(date(2026, 11, 18), "10.00", final=True, at=T1)))
    assert promoted["revision"].to_list() == [2] and promoted["is_final"].to_list() == [True]
    assert C.next_revisions(frame(bar(date(2026, 11, 18), "10.00", final=True)), frame(bar(date(2026, 11, 18), "10.00", final=False, at=T1))).is_empty()
    changed = C.next_revisions(stored, frame(bar(date(2026, 11, 18), "10.00", vol=1001, final=False, at=T1)))
    assert changed["revision"].to_list() == [2]


def test_index_versions_append_only_on_change():
    def idx(day, close, at):
        return {"instrument_id": IID, "symbol": "000001", "date": day, "timeframe": "1d", "open": 1.0, "high": 2.0, "low": 0.5, "close": close,
                "volume": 5.0, "price_basis": "index_points_unadjusted", "ingested_at": at}

    old = pl.DataFrame([idx(date(2026, 11, 18), 3.0, T0)], schema=C.INDEX_SCHEMA)
    same = pl.DataFrame([idx(date(2026, 11, 18), 3.0, T1), idx(date(2026, 11, 19), 3.1, T1)], schema=C.INDEX_SCHEMA)
    assert C.next_index_versions(old, same)["date"].to_list() == [date(2026, 11, 19)]
    moved = pl.DataFrame([idx(date(2026, 11, 18), 3.5, T1)], schema=C.INDEX_SCHEMA)
    assert C.next_index_versions(old, moved)["close"].to_list() == [3.5]


# ---- refresh planning (ex-right detection from the published previous close) ----------------------------------------------
def q(code, day, prev, vol=1000.0, last=10.5):
    return {"code": code, "date": day, "volume": vol, "prev_close": prev, "open": last, "high": last, "low": last, "close": last}


def test_refresh_plan_splits_append_readjusted_gap_new_and_suspended():
    d17, d18, d19 = date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)
    tail = {"000001": {d17: 10.0, d18: 10.2}, "000002": {d17: 20.0, d18: 20.0}, "000003": {d17: 5.0, d18: 5.0},
            "000004": {d17: 7.0}, "000005": {d17: 8.0, d18: 8.0}}
    quotes = [q("000001", d19, 10.2), q("000002", d19, 18.0), q("000003", d19, 5.0, vol=0.0), q("000004", d19, 7.0), q("000005", d19, 8.0)]
    plan = C.plan_refresh(quotes, tail, ["000001", "000002", "000003", "000004", "000005", "000009"], None)
    assert list(plan.append) == ["000001", "000005"]                                    # published previous close == stored close -> append today's bar
    assert plan.refetch == {"000002": "readjusted", "000004": "gap", "000009": "new"}   # 18.0 vs 20.0: an ex-right day; 000004 misses 11-18
    assert plan.skipped_suspended == 1


def test_refresh_plan_waits_for_an_unfinished_session_and_skips_stored_days():
    d18, d19 = date(2026, 11, 18), date(2026, 11, 19)
    tail = {"000001": {d18: 10.0}, "000002": {date(2026, 11, 17): 3.0, d18: 3.0, d19: 3.1}}
    plan = C.plan_refresh([q("000001", d19, 10.0), q("000002", d19, 3.0)], tail, ["000001", "000002"], cut=d19)
    assert plan.append == {} and plan.up_to_date == 2                                   # today is unfinished / already stored
    ex = C.plan_refresh([q("000001", d19, 9.0)], tail, ["000001"], cut=d19)
    assert ex.refetch == {"000001": "readjusted"}                                       # an ex-right day is visible before the close


# ---- reconcile report on a synthetic legacy / native pair -----------------------------------------------------------------
def test_compare_bars_counts_rows_and_differing_cells_beyond_tolerance():
    d = [date(2026, 11, n) for n in (16, 17, 18)]
    legacy = frame(*[bar(x, c) for x, c in zip(d, ("10.00", "10.10", "10.20"))], *[bar(x, "5.00", iid=IID2) for x in d[:2]])
    native = frame(bar(d[0], "10.00", revision=1), bar(d[1], "10.1000001"), bar(d[2], "10.50"),          # 1e-8 relative: within tolerance; 10.50: not
                   bar(d[0], "5.00", iid=IID2, vol=999))
    rep = C.compare_bars(native, legacy)
    assert rep["window"] == "2026-11-16..2026-11-18" and rep["overlap_rows"] == 3 + 1
    assert rep["native_only_rows"] == 0 and rep["legacy_only_rows"] == 1                # legacy has IID2 on 11-17, native does not
    assert rep["columns"]["close_price"]["differ"] == 1 and rep["columns"]["close_price"]["compared"] == 4
    assert rep["columns"]["volume_shares"]["differ"] == 1                                 # 999 vs 1000 is 1e-3 relative
    assert rep["instruments_with_close_diff"] == 1 and rep["examples"][0]["trade_date"] == "2026-11-18"
    text = C.render_report({"adjusted bars": rep})
    assert "== reconcile adjusted bars ==" in text and "differ: 1" in text


def test_compare_bars_uses_the_latest_revision_of_each_side_and_reports_an_empty_side():
    stored = frame(bar(date(2026, 11, 17), "10.00"))
    revised = pl.concat([stored, frame(bar(date(2026, 11, 17), "9.50", revision=2, at=T1))])
    rep = C.compare_bars(revised, frame(bar(date(2026, 11, 17), "9.50")))
    assert rep["columns"]["close_price"]["differ"] == 0                                  # revision 2 is what is compared
    assert C.compare_bars(frame(), stored)["note"] == "one side is empty"


def test_compare_membership_reports_pair_node_and_primary_node_differences():
    def m(*pairs):
        return pl.DataFrame({"instrument_id": [p[0] for p in pairs], "node_id": [p[1] for p in pairs]})

    legacy = m(("a", "n1"), ("b", "n1"), ("c", "n2"), ("d", "n3"))
    native = m(("a", "n1"), ("b", "n9"), ("c", "n2"), ("c", "n1"), ("e", "n4"))
    rep = C.compare_membership(native, legacy)
    assert rep["pairs_only_native"] == 3 and rep["pairs_only_legacy"] == 2             # (b,n9)(c,n1)(e,n4) vs (b,n1)(d,n3)
    assert rep["nodes_only_native"] == ["n4", "n9"] and rep["nodes_only_legacy"] == ["n3"]
    assert rep["instruments_in_both"] == 3 and rep["instruments_primary_node_differs"] == 2   # b: n9 vs n1; c: lowest node n1 vs n2


def test_compare_listings_by_code():
    native = pl.DataFrame({"code": ["000001", "000002", "600000"], "listed_on": [date(1991, 4, 3), date(1991, 1, 30), None]})
    rep = C.compare_listings(native, {"000001": date(1991, 4, 3), "000002": date(1991, 1, 29), "600000": date(1999, 11, 10), "600519": date(2001, 8, 27)})
    assert rep["overlap"] == 3 and rep["date_differs"] == 1 and rep["native_null_legacy_set"] == 1
    assert rep["legacy_only"] == 1 and rep["examples"] == [{"code": "000002", "native": "1991-01-30", "legacy": "1991-01-29"}]
