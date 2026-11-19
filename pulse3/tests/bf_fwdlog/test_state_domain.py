"""E089 market-state rules with hand-computed cases (no database)."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal as D

from pulse.domain.fwdlog import state as ST
from pulse.domain.fwdlog.common import CST, UTC

DAY = date(2026, 11, 12)


def at(hh, mm, ss=0, day=DAY):
    return datetime.combine(day, time(hh, mm, ss), CST).astimezone(UTC)


def etf_rows(amounts, *, late=None, kind_close="close", quote_day=DAY, skip=()):
    """For each ETF a 14:30 grid row (cumulative ``late`` amount) and a 15:01 close row (cumulative ``amounts``)."""
    rows = []
    for s in ST.ETFS:
        if s in skip:
            continue
        qd = datetime.combine(quote_day, time(15, 0), CST).astimezone(UTC)
        rows.append(ST.ExtRow(s, "grid", at(14, 30, 2), qd, D("4"), D("4"), D(late or "0"), D("1000")))
        rows.append(ST.ExtRow(s, kind_close, at(15, 1, 30), qd, D("4"), D("4"), D(amounts), D("1000")))
    return rows


def test_etf_state_sums_all_seven_and_late_share():
    st = ST.etf_state(etf_rows("30", late="24"), DAY)
    # total 7 x 30 = 210; at 14:30 the cumulative sum was 7 x 24 = 168; late share = (210 - 168) / 210 = 0.2
    assert st["etf_amount"] == D("210.0000") and st["n_etf"] == 7 and st["late_share"] == D("0.2000")


def test_etf_state_needs_every_etf_and_todays_quote_stamp():
    one_missing = ST.etf_state(etf_rows("30", late="24", skip=("sz159845",)), DAY)
    assert one_missing["etf_amount"] is None and one_missing["n_etf"] == 6 and one_missing["late_share"] is None      # a died recorder is not a quiet day
    holiday = ST.etf_state(etf_rows("30", late="24", quote_day=date(2026, 11, 11)), DAY)
    assert holiday == {"etf_amount": None, "n_etf": 0, "late_share": None, "vwap_gap_pct": None}                     # the feed repeats the last session
    only_once = [r for r in etf_rows("30", late="24")]
    only_once = [ST.ExtRow(r.symbol, "once", r.poll_at, r.quote_at, r.price, r.prev_close, r.amount, r.volume) for r in only_once]
    assert ST.etf_state(only_once, DAY)["n_etf"] == 0                                                                  # a smoke-test poll never counts


def test_vwap_gap_of_510300():
    rows = etf_rows("30", late="24")
    # 510300: amount 400 000 000, volume 1 000 000 lots -> average price 400 000 000 / (1 000 000 x 100) = 4.00; close 4.04 -> +1.0 percent
    rows = [ST.ExtRow(r.symbol, r.kind, r.poll_at, r.quote_at, D("4.04"), r.prev_close, D("400000000"), D("1000000")) if (r.symbol == "sh510300" and r.kind == "close") else r for r in rows]
    assert ST.etf_state(rows, DAY)["vwap_gap_pct"] == D("1.0000")


def test_state_row_ratio_surge_and_panic_boundary():
    sums = {f"2026-10-{d:02d}": D("100") for d in range(1, 21)}                      # 20 earlier complete days: median 100
    rows = etf_rows("30", late="24")                                                  # today 210 -> ratio 2.1 -> surge
    common = dict(ext=rows, summary={"end": "done"}, merged=True, sums=sums)
    on_edge = ST.state_row(DAY, closing={"a": (D("9"), D("10")), "b": (D("10"), D("10")), "c": (D("10"), D("10"))}, **common)
    # ew = (-10 + 0 + 0) / 3 = -3.3333 percent <= -3 -> panic_support; up_share 0
    assert on_edge["ratio"] == D("2.1000") and on_edge["surge"] is True and on_edge["ew_pct"] == D("-3.3333") and on_edge["panic_support"] is True
    assert on_edge["up_share"] == D("0.0000") and on_edge["n_ew"] == 3 and on_edge["late_heavy"] is False
    exactly = ST.state_row(DAY, closing={"a": (D("9.7"), D("10"))}, **common)         # exactly -3.0 percent counts (<=)
    assert exactly["ew_pct"] == D("-3.0000") and exactly["panic_support"] is True
    mild = ST.state_row(DAY, closing={"a": (D("9.71"), D("10"))}, **common)
    assert mild["ew_pct"] == D("-2.9000") and mild["panic_support"] is False
    short = ST.state_row(DAY, closing={"a": (D("9"), D("10"))}, ext=rows, summary={}, merged=True, sums={"2026-10-01": D("100")})
    assert short["ratio"] is None and short["surge"] is None and short["panic_support"] is None                       # fewer than 20 days: unknown, not False
    no_quotes = ST.state_row(DAY, closing=None, **common)
    assert no_quotes["ew_pct"] is None and no_quotes["panic_support"] is None and no_quotes["surge"] is True


def test_late_heavy_threshold_is_inclusive():
    heavy = ST.state_row(DAY, closing=None, ext=etf_rows("100", late="70"), summary={}, merged=True, sums={})      # (700 - 490) / 700 = 0.30
    assert heavy["late_share"] == D("0.3000") and heavy["late_heavy"] is True
    light = ST.state_row(DAY, closing=None, ext=etf_rows("100", late="71"), summary={}, merged=True, sums={})
    assert light["late_heavy"] is False


def test_missing_extended_recording_still_gets_a_row():
    r = ST.state_row(DAY, closing={"a": (D("11"), D("10"))}, ext=None, summary={}, merged=False, sums={})
    assert r["ext_missing"] is True and r["etf_amount"] is None and r["surge"] is None and r["ew_pct"] == D("10.0000") and r["health"]["merged"] is False


def test_futures_main_contract_and_basis():
    def row(sym, price, oi, ts=at(15, 1)):
        return ST.ExtRow(sym, "grid", ts, ts, D(price), D("100"), None, None, None if oi is None else D(oi))
    rows = [row("sh000300", "4000", None), row("IF2611", "3990", "100"), row("IF2612", "3960", "500"), row("IF2701", "3950", "500"), row("IFxxxx", "1", "9")]
    f = ST.futures_state(rows, DAY)
    # largest open interest wins, a tie goes to the larger symbol: IF2701; basis = 3950 / 4000 - 1 = -1.25 percent; the other products have no index row
    assert f["IF"]["contract"] == "IF2701" and f["IF"]["basis_pct"] == D("-1.2500") and f["IH"] is None
    stale = [row("sh000300", "4000", None, at(10, 0)), row("IF2612", "3960", "500", at(10, 0))]
    assert ST.futures_state(stale, DAY)["IF"] is None                                                                  # last row before 14:30: the source died


def test_health_counts_and_cffex_gap():
    ts = lambda m: at(10, m)                                                                                           # noqa: E731
    rows = [ST.ExtRow("IF2612", "grid", ts(m), ts(m), D("1"), D("1"), None, None, D("5")) for m in (0, 5, 10)] + \
           [ST.ExtRow("sh000300", "grid", at(14, 31), at(14, 31), D("1"), D("1")), ST.ExtRow("IF2612", "grid", at(14, 31), at(14, 31), D("1"), D("1"), None, None, D("5"))] + \
           [ST.ExtRow("em_CN00Y", "minute", ts(1), None, D("1")), ST.ExtRow("em_USDCNH", "minute", ts(1), None, D("1")), ST.ExtRow("em_USDCNH", "minute", ts(2), None, D("1"))]
    h = ST.health_state({"end": "done", "missed": 0, "never_returned": ["a", "b"], "blocked": {"sina": "403"}}, rows, DAY, merged=True)
    # quote times 10:00, 10:05, 10:10, 14:31: gaps 5, 5, 261 minutes -> median 5
    assert h["cffex_gap_min"] == D("5.0000") and h["a50_bars"] == 1 and h["cnh_bars"] == 2 and h["never_returned"] == 2 and h["blocked"] == ["sina"]
    assert ST.health_state({"unreadable": True}, None, DAY, merged=False)["end"] is None


def test_episodes_compound_and_summary():
    assert ST.episodes([False, True, None, True, False, False, True]) == [1]                    # flagged days within 10 rows are one episode
    assert ST.episodes([True] + [False] * 11 + [True]) == [0, 12]
    pcts = [D("0"), D("1"), D("1"), None, D("2")]
    assert ST.compound(pcts, 0, 2) == D("2.0100") and ST.compound(pcts, 1, 3) is None and ST.compound(pcts, 3, 2) is None
    rows = [{"date": f"d{i}", "ew_pct": D("0"), "panic_support": i == 0, "etf_amount": D("1"), "ratio": D("1"), "surge": False, "late_heavy": False, "late_share": D("0.2"),
             "health": {"a50_bars": 1, "cnh_bars": 1, "cffex_gap_min": D("1")}} for i in range(3)]
    s = ST.summary(rows)
    assert s["days"] == 3 and s["panic_episodes"] == 1 and s["h1_check_at"] == 10 and s["panic_next"][0]["ew_1"] == D("0.0000") and s["late_share_median"] == D("0.2000")
    assert s["cffex_slow_days"] == 0 and s["todo"] == []
    lost = rows + [{"date": "d3", "ext_missing": True, "health": {}}]
    assert "extended recording or state row lost on d3" in ST.summary(lost)["todo"][0]
    slow = [{**r, "health": {"a50_bars": 0, "cnh_bars": 0, "cffex_gap_min": D("4")}} for r in rows]
    todo = ST.summary(slow)["todo"]
    assert any("A50 / CNH" in t for t in todo) is False and any("CFFEX" in t for t in todo)                         # 3 slow days trigger the CFFEX note; A50 needs 5 days
