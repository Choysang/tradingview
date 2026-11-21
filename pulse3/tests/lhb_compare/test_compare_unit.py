"""LHB comparison core without a database: tolerance, multiset pairing, verdicts, old-warehouse reading (synthetic parquet)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from pulse.application import lhb_compare as lc

from . import support as sup

D = date(2026, 11, 10)
R1 = "日涨幅偏离值达到7%的前5只证券"
R2 = "日换手率达到20%的前5只证券"


def _rows(*items):
    return [(s, n, Decimal(a)) for s, n, a in items]


def test_tolerance_is_one_yuan_or_half_a_percent():
    assert lc.amounts_agree(Decimal(100), Decimal(101))  # 1 yuan
    assert not lc.amounts_agree(Decimal(100), Decimal(102))  # 2 yuan and 2%
    assert lc.amounts_agree(Decimal(10_000_000), Decimal(10_049_000))  # 0.49%
    assert not lc.amounts_agree(Decimal(10_000_000), Decimal(10_060_000))  # 0.6%


def test_identical_sessions_are_same_and_counts_include_institution_and_lhasa_rows():
    rows = _rows(("buy", "机构专用", "5000000"), ("buy", "机构专用", "3000000"), ("sell", "东方财富证券股份有限公司拉萨东环路第二证券营业部", "2000000"))
    res = lc.compare_session(D, {"000530": {R1: rows}}, {"000530": {R1: list(rows)}})
    assert res.verdict == "SAME" and res.reasons == [] and res.diffs == []
    assert (res.old.institution_rows, res.old.lhasa_rows, res.old.seat_rows) == (2, 1, 3) and res.old == res.new


def test_duplicate_institution_names_pair_by_descending_amount():
    old = _rows(("buy", "机构专用", "3000000"), ("buy", "机构专用", "5000000"))
    new = _rows(("buy", "机构专用", "5000000"), ("buy", "机构专用", "3000000"))
    assert lc.compare_session(D, {"000001": {R1: old}}, {"000001": {R1: new}}).verdict == "SAME"
    off = _rows(("buy", "机构专用", "5000000"), ("buy", "机构专用", "2000000"))
    res = lc.compare_session(D, {"000001": {R1: old}}, {"000001": {R1: off}})
    assert res.verdict == "DIFF" and [(d.kind, d.old, d.new) for d in res.diffs] == [("amount", "3000000", "2000000")]


def test_one_extra_duplicate_seat_is_reported_on_the_side_that_has_it():
    old = _rows(("buy", "机构专用", "5000000"), ("buy", "机构专用", "3000000"))
    new = _rows(("buy", "机构专用", "5000000"))
    res = lc.compare_session(D, {"000001": {R1: old}}, {"000001": {R1: new}})
    assert [(d.kind, d.seat, d.old) for d in res.diffs] == [("seat_only_old", "机构专用", "3000000")]
    assert res.reasons == ["seat rows only in old: 1"]


def test_stock_and_reason_differences_and_registration_note():
    old = {"000001": {R1: []}, "000002": {R1: []}, "000003": {R1: [], R2: []}}
    new = {"000001": {R1: []}, "000004": {R1: []}, "000003": {R1: []}}
    res = lc.compare_session(D, old, new, new_registered={"000004"})
    assert res.verdict == "DIFF" and res.stocks_only_old == ["000002"] and res.stocks_only_new == ["000004"]
    kinds = {(d.code, d.kind): d for d in res.diffs}
    assert kinds[("000002", "only_old")].note and "not registered" in kinds[("000002", "only_old")].note
    assert ("000003", "reason_only_old") in kinds and kinds[("000003", "reason_only_old")].reason == R2
    assert lc.compare_session(D, old, new, new_registered={"000002"}).diffs[0].note is None


def test_session_with_data_on_one_side_only_is_diff():
    res = lc.compare(old={D: {"000001": {R1: []}}}, new={}, since=D, until=D)
    assert res.sessions[0].verdict == "DIFF" and "old side only" in res.sessions[0].reasons[0] and not res.all_same


def test_same_tail_counts_the_newest_consecutive_same_sessions():
    days = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17)]
    old = {d: {"000001": {R1: []}} for d in days}
    new = {d: {"000001": {R1: []}} for d in days}
    new[days[1]] = {}  # one early DIFF does not reduce the tail
    c = lc.compare(old, new, days[0], days[-1])
    assert c.same_tail == 10 and c.ready_to_retire
    new[days[-3]] = {"000009": {R1: []}}
    c = lc.compare(old, new, days[0], days[-1])
    assert c.same_tail == 2 and not c.ready_to_retire


def test_scope_keeps_codes_both_pipelines_can_store():
    assert lc.in_scope("600000") and lc.in_scope("300750") and lc.in_scope("688001") and lc.in_scope("830799")
    assert not lc.in_scope("123001") and not lc.in_scope("110001") and not lc.in_scope("20000") and not lc.in_scope("200002")


# ---- the old warehouse --------------------------------------------------------------------------------------------------------
def _stock(code="000530", day=D, reason=R1):
    return {"code": code, "date": day, "reason": reason}


def _seat(rank, side="buy", seat="某证券股份有限公司拉萨某路证券营业部", buy=0.1, sell=None, code="000530", day=D, reason=R1):
    return {"code": code, "date": day, "reason": reason, "side": side, "seat": seat, "rank": rank, "buy": buy, "sell": sell}


def test_old_amounts_convert_to_exact_yuan_and_rows_without_amount_or_name_are_dropped(tmp_path):
    sup.write_old(tmp_path, [_stock()], [_seat(0, buy=0.269411), _seat(1, buy=None), _seat(2, seat="  ", buy=0.5), _seat(3, side="sell", buy=None, sell=0.06119884179999999)])
    side, dropped = lc.load_old(tmp_path, D, D)
    rows = side[D]["000530"][R1]
    assert sorted(rows) == [("buy", "某证券股份有限公司拉萨某路证券营业部", Decimal("26941100")), ("sell", "某证券股份有限公司拉萨某路证券营业部", Decimal("6119884.18"))]
    assert dropped == {"old_rows_without_amount": 1, "old_rows_without_name": 1}


def test_old_duplicates_resolve_to_the_newest_ingest_and_retired_files_are_skipped(tmp_path):
    from datetime import datetime, timezone
    sup.write_old(tmp_path, [_stock()], [_seat(0, buy=0.1)], name="1.parquet", ingested_at=datetime(2026, 11, 10, 10, tzinfo=timezone.utc))
    sup.write_old(tmp_path, [_stock()], [_seat(0, buy=0.2)], name="2.parquet", ingested_at=datetime(2026, 11, 10, 12, tzinfo=timezone.utc))
    assert [r[2] for r in lc.load_old(tmp_path, D, D)[0][D]["000530"][R1]] == [Decimal("20000000")]
    (tmp_path / "lhb_seats" / ".retired.json").write_text(json.dumps({"2.parquet": 1.0}), encoding="utf-8")
    assert [r[2] for r in lc.load_old(tmp_path, D, D)[0][D]["000530"][R1]] == [Decimal("10000000")]


def test_old_load_filters_range_and_scope_and_requires_the_stock_table(tmp_path):
    sup.write_old(tmp_path, [_stock(), _stock(code="123001"), _stock(day=date(2026, 11, 9))], [_seat(0), _seat(0, code="123001")])
    side, _ = lc.load_old(tmp_path, D, D)
    assert list(side) == [D] and list(side[D]) == ["000530"]
    with pytest.raises(lc.LhbCompareError):
        lc.load_old(tmp_path / "nothing", D, D)
    with pytest.raises(lc.LhbCompareError):
        lc.run_compare(None, tmp_path, date(2026, 11, 11), D)


def test_render_lists_differences_and_the_retirement_verdict():
    old = {D: {"000001": {R1: _rows(("buy", "A证券营业部", "1000"))}}}
    new = {D: {"000001": {R1: _rows(("buy", "A证券营业部", "9000"))}}}
    text = lc.render(lc.compare(old, new, D, D, {"old_rows_without_amount": 2}))
    assert "DIFF" in text and "amount buy A证券营业部" in text and "old=1000 new=9000" in text
    assert "old_rows_without_amount=2" in text and "not ready" in text
