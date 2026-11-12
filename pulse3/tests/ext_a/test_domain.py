"""Extension group A, pure domain: session statistics, sentiment gauge, regime timeline, tape and outcomes (no database)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from pulse.domain.market_rules import Board, Exchange, PriceLimitRegistry
from pulse.domain.market_state import daily, mood, outcomes, panels, regime, tape
from pulse.domain.market_state.daily import Bar, Stock

D = Decimal


def days(n: int, start: date = date(2026, 10, 12)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars(ds, closes, *, highs=None, opens=None, vol=1000):
    return [Bar(d, D(str(o)) if opens else D(str(c)), D(str(h)) if highs else D(str(c)), D(str(c)), D(str(c)), vol)
            for d, c, o, h in zip(ds, closes, opens or closes, highs or closes)]


def stock(iid, code, exchange=Exchange.SSE, rw=False):
    return Stock(iid, code, "名" + code, exchange, Board.MAIN, rw)


DS = days(6)


def world():
    st = {"A": stock("A", "600001"), "B": stock("B", "600002"), "C": stock("C", "600003"), "D": stock("D", "000004", Exchange.SZSE, True),
          "E": stock("E", "600005")}
    series = {
        # A: limit-up on day 3 and 4 (10.00 -> 11.00 -> 12.10), then down
        "A": bars(DS, [10, 10, 11, 12.1, 11.5, 11.9]),
        # B: day 3 touches the limit (high 11.00) but closes 10.6 (broken), day 4 limit-up from 10.6 -> 11.66
        "B": bars(DS, [10, 10, 10.6, 11.66, 11.2, 11.0], highs=[10, 10, 11.0, 11.66, 11.2, 11.0]),
        # C: an ex-rights style -40% day on day 3 (impossible price move)
        "C": bars(DS, [10, 10, 6, 6.1, 6.2, 6.3]),
        # D: SZSE risk-warning share: ratio has no verified rule -> excluded from limit statistics
        "D": bars(DS, [10, 10, 10.5, 10.4, 10.6, 10.7]),
        # E: first listed on day 2 (no previous bar on day 1)
        "E": bars(DS[1:], [20, 20.5, 20.7, 20.0, 20.1]),
    }
    return st, series


def hist():
    st, series = world()
    return st, daily.compute_history(st, series, PriceLimitRegistry(), DS)


def test_limit_price_rounds_half_up_to_the_tick():
    up, down = daily.limit_prices(D("10.55"), D("0.10"))
    assert (up, down) == (D("11.61"), D("9.50"))           # 11.605 -> 11.61 (half up); 9.495 -> 9.50


def test_limit_up_broken_streak_and_ladder():
    st, h = hist()
    d3, d4 = h.sessions[2], h.sessions[3]
    assert (d3.limit_up, d3.broken) == (1, 1)                        # A sealed; B touched 11.00 and closed 10.60
    assert d3.ladder == {1: 1}
    assert d4.limit_up == 2 and d4.ladder == {1: 1, 2: 1}            # A second board, B first board (from broken)
    assert d4.max_height == 2
    assert h.last_rows["A"].streak == 0 and h.last_rows["A"].limit_up is False


def test_unverifiable_moves_and_rules_are_counted_not_zeroed():
    st, h = hist()
    d3 = h.sessions[2]
    assert d3.pct_unverifiable >= 1                                   # C's -40% day
    assert d3.limit_unverifiable == 1                                 # D: SZSE risk-warning has no verified ratio
    assert d3.limit_verified + d3.limit_unverifiable == d3.pct_valid
    first = h.sessions[0]
    assert first.pct_valid == 0 and first.limit_verified == 0         # no previous bar: nothing to measure on day 1
    assert h.sessions[1].observed == 5 and h.sessions[0].observed == 4  # E lists on day 2


def test_promotion_uses_yesterdays_frozen_set_and_premium():
    st, h = hist()
    d4 = h.sessions[3]
    # yesterday's (day 3) rung 1 = {A}; today A is a 2nd board -> promoted 1/1
    assert d4.promo[1] == [1, 1, 0]
    assert daily.promotion_rate(d4.promo[1]) == D("100.0000")
    # premium: A's close return today vs yesterday's close = 10%
    assert d4.prem_n == 1 and daily.mean_pct(d4.prem_close_sum, d4.prem_n) == D("10.0000")
    d5 = h.sessions[4]
    assert d5.promo[2] == [1, 0, 0] and daily.promotion_rate(d5.promo[2]) == D("0.0000")


def test_missing_member_leaves_the_evaluable_denominator():
    st, series = world()
    series["A"] = [b for b in series["A"] if b.d != DS[3]] + []   # A has no bar on day 4 ... remove day 4 and yes day 3 sealed
    h = daily.compute_history(st, series, PriceLimitRegistry(), DS)
    d4 = h.sessions[3]
    assert d4.promo[1][0] == 1 and d4.promo[1][2] == 1             # frozen 1, missing 1
    assert daily.promotion_rate(d4.promo[1]) is None               # nothing evaluable: unknown, not 0 and not 100


def test_histogram_has_eleven_bins_and_flat_bin():
    assert daily.hist_bin(D("0")) == 5 and daily.hist_bin(D("-7")) == 1 and daily.hist_bin(D("-6.99")) == 1
    assert daily.hist_bin(D("-7.01")) == 0 and daily.hist_bin(D("9.9")) == 10 and daily.hist_bin(D("0.0001")) == 6
    assert len(daily.HIST_LABELS) == 11
    st, h = hist()
    assert sum(h.sessions[3].hist) == h.sessions[3].pct_valid


def test_mood_weights_are_declared_and_none_when_a_component_is_unknown():
    assert sum(mood.WEIGHTS.values()) == D("1.00") and mood.WEIGHTS["limit_ratio"] == D("0.40")
    st, h = hist()
    sc = mood.score(h.sessions[3])
    assert sc["calibrated"] is False and sc["version"].endswith("uncalibrated") and sc["score"] is not None
    empty = daily.SessionStats(DS[0])                                 # nothing verified: no score, never 0
    e = mood.score(empty)
    assert e["score"] is None and e["band"] is None and set(e["missing_components"]) == {"limit_ratio", "breadth", "height", "seal_quality"}
    # all limit-ups and nothing broken, saturated ratio and height, all up -> 100.0
    full = daily.SessionStats(DS[0])
    full.up, full.down, full.limit_verified, full.limit_up, full.ladder = 100, 0, 100, 100, {7: 1}
    assert mood.score(full)["score"] == D("100.0") and mood.score(full)["band"]["word"] == "高潮"
    assert [b["word"] for b in mood.score(full)["bands"]] == ["冰点", "低迷", "中性", "升温", "亢奋", "高潮"]


def test_band_edges():
    assert mood.band_of(D("24.9"))["word"] == "冰点" and mood.band_of(D("25"))["word"] == "低迷"
    assert mood.band_of(D("85"))["word"] == "高潮" and mood.band_of(None) is None


def _stats(n: int, seed: int = 3):
    import random

    rnd = random.Random(seed)
    out = []
    for i, d in enumerate(days(n, date(2025, 1, 6))):
        s = daily.SessionStats(d)
        s.observed = s.pct_valid = s.limit_verified = 3000
        s.limit_up, s.broken = rnd.randint(5, 120), rnd.randint(0, 30)
        s.ladder = {rnd.randint(1, 6): 1}
        s.up, s.down = rnd.randint(800, 2200), rnd.randint(800, 2200)
        s.ma_above, s.ma_n = rnd.randint(900, 2100), 3000
        s.volume_n, s.amount_estimate = 3000, D(str(1e11 * (1 + 0.002 * i)))
        s.prem_n, s.prem_close_sum = 40, D(str(rnd.uniform(-80, 120)))
        out.append(s)
    return out


def _indices(stats, symbols=("000985", "000300", "000852")):
    import math

    return {sym: {s.d: 4000 * math.exp(0.001 * i + 0.01 * math.sin(i / 5 + k)) for i, s in enumerate(stats)} for k, sym in enumerate(symbols)}


def test_regime_has_four_dimensions_basis_and_default_labels_are_flagged():
    stats = _stats(300)
    r = regime.build_regime(stats, _indices(stats))
    assert r["available"] and r["index"]["symbol"] == "000985" and len(r["points"]) == 300
    first, last = r["points"][0], r["points"][-1]
    assert first["basis"]["trend"] == "insufficient_history" and first["trend"] == "range" and first["liquidity"] == "steady"
    assert last["basis"] == {"trend": "ok", "liquidity": "ok", "sentiment": "ok", "style": "ok"}
    assert {last["trend"], last["liquidity"], last["sentiment"], last["style"]} <= set(sum((list(v) for v in regime.LABELS.values()), []))
    assert "·" in r["current"]["label"] and len(r["current"]["explanation"]) == 4


def test_regime_label_never_changes_when_later_days_arrive():
    stats = _stats(300)
    idx = _indices(stats)
    full = regime.build_regime(stats, idx)["points"]
    for cut in (150, 220):
        part = regime.build_regime(stats[:cut], {k: {d: v for d, v in s.items() if d <= stats[cut - 1].d} for k, s in idx.items()})["points"]
        for a, b in zip(part, full[:cut]):
            assert {k: a[k] for k in ("trend", "liquidity", "sentiment", "style")} == {k: b[k] for k in ("trend", "liquidity", "sentiment", "style")}


def test_regime_missing_indices_are_reported_not_defaulted():
    stats = _stats(120)
    r = regime.build_regime(stats, {"000300": _indices(stats)["000300"]})
    assert r["available"] and r["index"]["fallback"] is True and r["points"][-1]["style"] == "unknown"
    assert r["points"][-1]["basis"]["style"] == "missing_index"
    none = regime.build_regime(stats, {})
    assert none["available"] is False and none["reason"] == "NO_INDEX_BARS" and none["points"] == []


def test_mode_smooth_is_trailing_and_ties_go_to_the_latest():
    assert regime.mode_smooth(["a", "a", "b", "b", "c"], 5) == ["a", "a", "a", "b", "b"]
    out = regime.mode_smooth(["a", "b", "a", "b"], 2)
    assert out == ["a", "b", "a", "b"]                                 # ties: most recent label wins


# ---------------------------------------------------------------------------------------------------- tape
def q(iid, last, ask, t):
    return tape.Quote(iid, None if last is None else D(str(last)), None if ask is None else D(str(ask)), t)


T = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
REF = {"A": tape.TapeRef("A", "600001", "甲", D("10.00"), D("0.10"), 1, ("行业甲",)),
       "B": tape.TapeRef("B", "600002", "乙", D("10.00"), D("0.10"), 0)}


def evs(prev, curr, **kw):
    kw = {"yesterday_max_height": 2, "ever_sealed": frozenset(), "last_fired": {}, **kw}
    return [(e.kind, e.iid) for e in tape.diff_snapshots(prev, curr, REF, **kw)]


def test_tape_seal_reseal_break_and_new_high():
    a = {"A": q("A", 10.9, 11.0, T), "B": q("B", 10.2, 10.3, T)}
    b = {"A": q("A", 11.0, None, T + timedelta(minutes=1)), "B": q("B", 10.2, 10.3, T + timedelta(minutes=1))}
    assert evs(a, b) == [("seal", "A")]                                # 2nd board, not above yesterday's height 2
    assert ("new_high", "A") not in evs(a, b)
    assert evs(a, b, yesterday_max_height=1) == [("seal", "A"), ("new_high", "A")]
    assert evs(a, b, ever_sealed=frozenset({"A"})) == [("reseal", "A")]
    assert evs(b, a) == [("break", "A")]
    assert evs(a, {"A": b["A"]}) == [("seal", "A")]                     # B missing in one snapshot: nothing for B


def test_tape_surge_plunge_cooldown_and_unknown_inputs():
    a = {"B": q("B", 10.4, 10.5, T)}
    b = {"B": q("B", 10.6, 10.7, T + timedelta(minutes=2))}
    assert evs(a, b) == [("surge", "B")]
    assert evs(a, b, last_fired={("B", "surge"): T + timedelta(minutes=1)}) == []        # inside the 15 minute cooldown
    assert evs(a, b, last_fired={("B", "surge"): T - timedelta(hours=1)}) == [("surge", "B")]
    assert evs({"B": q("B", 9.6, 9.7, T)}, {"B": q("B", 9.4, 9.5, T)}) == [("plunge", "B")]
    assert evs({"B": q("B", None, None, T)}, {"B": q("B", 10.6, 10.7, T)}) == []          # unknown previous price: no event
    no_ref = {"Z": q("Z", 10, 10, T)}
    assert evs(no_ref, no_ref) == []


# ---------------------------------------------------------------------------------------------------- outcomes
def test_outcome_pending_until_the_horizon_is_observed_then_resolved_with_denominators():
    cal = days(10)
    ranked = [(f"S{i}", i + 1) for i in range(6)]
    series = {f"S{i}": [Bar(d, D("10"), D("10"), D("10"), D("10") * (1 + D("0.01") * (i + 1) * (k > 0)), 100) for k, d in enumerate(cal)]
              for i in range(5)}                                      # S5 has no bars at all -> unevaluable
    kw = dict(ranking_id="r", release_id="rel", horizon_id="5td", horizon_sessions=5, ranked=ranked, series=series, cost_ratio=D("0.002"),
              cost_version="c1", top_n=3)
    pending = outcomes.evaluate(decision_session=cal[6], sessions=cal, **kw)
    assert pending["status"] == "pending" and pending["reason"] == "HORIZON_NOT_REACHED"
    assert all(m["value"] is None and m["missing_reason"] == "HORIZON_NOT_REACHED" for m in pending["metrics"])
    done = outcomes.evaluate(decision_session=cal[0], sessions=cal, **kw)
    assert done["status"] == "resolved" and done["entry_session"] == cal[1] and done["exit_session"] == cal[5]
    m = {x["name"]: x for x in done["metrics"]}
    assert m["top_opportunity_count"]["value"] == D("3") and m["top_evaluable_count"]["value"] == D("3")
    assert m["avoid_descriptive_long_only_opportunity_count"]["value"] == D("3")
    assert m["avoid_descriptive_long_only_evaluable_count"]["value"] == D("2")             # S5 unevaluable stays in the denominator
    assert m["avoid_descriptive_long_only_hit_rate_over_opportunities"]["sample_count"] == 3
    assert done["unevaluable"] == {"NO_ENTRY_BAR": 1}
    assert m["top_mean_gross_return"]["value"] is not None and m["universe_median_gross_return"]["sample_count"] == 5


def test_outcome_flags_impossible_price_paths():
    cal = days(8)
    s = [Bar(d, D("10"), D("10"), D("10"), D("10") if k < 3 else D("5"), 100) for k, d in enumerate(cal)]
    r = outcomes.path_return("X", s, cal[1], cal[5])
    assert r.gross is None and r.reason == "UNVERIFIABLE_PRICE_PATH"


def test_panels_ladder_and_theme_strength_and_gaps():
    st, series = world()
    h = daily.compute_history(st, series, PriceLimitRegistry(), DS[:4])             # final session = day 4: A on 2 boards, B on 1
    lad = panels.ladder(h, st)
    assert lad["max_height"] == 2 and [r["streak"] for r in lad["rungs"]] == [2, 1]
    assert [r["sealed_count"] for r in lad["rungs"]] == [1, 1]
    assert lad["rungs"][0]["promotion"]["rate_pct"] == D("100.0000") and lad["rungs"][1]["promotion"] is None
    assert [m["code"] for m in lad["rungs"][1]["sealed"]] == ["600002"]
    # day 3 had B broken at rung 1: shown on the day-3 ladder
    h3 = daily.compute_history(st, series, PriceLimitRegistry(), DS[:3])
    assert [m["code"] for m in panels.ladder(h3, st)["rungs"][0]["broken"]] == ["600002"]
    sc = panels.seal_clock(h)
    assert sc["available"] is False and sc["reason_code"] == "NO_INTRADAY_SEAL_TIMES"
    th = panels.themes(h, st, {"n1": "行业一"}, {"n1": ["A", "B", "C", "E"]})
    t = th["themes"][0]
    assert t["node_id"] == "n1" and t["limit_up"] == 2
    assert t["strength"] == (D(2 * t["limit_up"] + t["growth_streak_ge3"]) + t["avg_pct"] * D("0.5")).quantize(D("0.01"))
    assert th["calibrated"] is False and "2 x limit_up" in th["formula"]
