"""Daily picks and track record: weights, ensemble maths, exclusions, point-in-time replay.

Day frames and trending markets are built by hand; the HTTP smoke test runs on the SIM
dataset once the playbook library and regime detector exist.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from backend.app.playbooks import evaluate as ev
from backend.app.playbooks import picks
from backend.app.playbooks.base import Playbook
from backend.app.warehouse.store import Warehouse
from contracts.schema import (
    ExitRules,
    PerformanceStats,
    PlaybookSpec,
    PlaybookSummary,
    RegimeState,
    SizingRule,
)

D0 = date(2026, 1, 5)


def spec(pid="p", style="trend", horizon="swing", holding=5, stop=8, take=None) -> PlaybookSpec:
    return PlaybookSpec(
        id=pid, name=f"战法{pid}", style=style, horizon=horizon, summary="s", rule="r", source="t",
        exits=ExitRules(holding_days=holding, stop_loss_pct=stop, take_profit_pct=take),
        sizing=SizingRule(method="equal", max_positions=5, max_new_per_day=2),
    )


def summary(pid, verdict, fitness, horizon="swing", style="trend") -> PlaybookSummary:
    oos = PerformanceStats(trades=25, win_rate=55, avg_return=1.2)
    return PlaybookSummary(
        spec=spec(pid, style, horizon), full=PerformanceStats(trades=80, win_rate=52, avg_return=0.9), out_of_sample=oos,
        by_regime=[], fitness=fitness, verdict=verdict, verdict_reason=f"{pid} 的判定理由", signals_today=3,
    )


# --- weights -------------------------------------------------------------------------

def test_weights_follow_fitness_and_verdict():
    rows = [
        summary("eff", "effective", 70),
        summary("dep", "regime_dependent", 60, horizon="short"),
        summary("lowfit", "effective", 39.9),
        summary("weak", "weak", 90),
        summary("over", "overfit", 90),
        summary("value", "unavailable", None, horizon="long", style="value"),
        summary("other", "unavailable", None, horizon="long", style="trend"),
    ]
    weights = {w.playbook_id: w for w in picks.weights_from_board(rows)}
    assert set(weights) == {"eff", "dep", "value"}
    assert weights["eff"].weight == pytest.approx(0.70)
    assert weights["dep"].weight == pytest.approx(0.42) and weights["dep"].horizon == "short"
    assert weights["value"].weight == 0.25 and weights["value"].untested and weights["value"].horizon == "long"


# --- ensemble maths ------------------------------------------------------------------------

def day_frame() -> pl.DataFrame:
    base = dict(close=10.0, pct_chg=1.0, amount=5.0, days_listed=300, limit_streak=0, volume=100.0, vol_ma20=100.0,
                ma5=10.0, ma10=9.5, ma20=9.0, ma60=8.0, ma120=7.0, macd_dif=1.0, macd_dea=0.5, macd_hist=1.0,
                high_60_prev=10.0, ret_20=5.0, ret_60=10.0, ret_120=20.0, volatility_20=2.0, atr_14=0.3, one_word=False)
    rows = [
        {"code": "A", "name": "甲", **base},
        {"code": "B", "name": "乙", **base, "ret_20": 1.0},
        {"code": "C", "name": "丙", **base, "ret_20": 9.0, "limit_streak": 2},
        {"code": "D", "name": "ST丁", **base},
        {"code": "E", "name": "戊", **base, "days_listed": 30},
        {"code": "F", "name": "己", **base, "amount": 0.3},
    ]
    return pl.DataFrame(rows)


def test_ensemble_is_weighted_mean_of_percentile_ranks():
    signals = {
        "p1": pl.DataFrame({"code": ["A", "B", "D", "E", "F"], "score": [3.0, 2.0, 9.0, 9.0, 9.0]}),
        "p2": pl.DataFrame({"code": ["A", "C"], "score": [1.0, 5.0]}),
    }
    weights = [picks.Weight("p1", "swing", 0.8), picks.Weight("p2", "swing", 0.2)]
    ranked, contributions = picks.ensemble_day(day_frame(), signals, weights, "climax")
    swing = {r["code"]: r for r in ranked["swing"].to_dicts()}
    # percentile rank inside each playbook, over all of its signals (excluded names included)
    r1 = pl.Series([3.0, 2.0, 9.0, 9.0, 9.0]).rank("average").to_list()
    r1 = {c: v / 5 for c, v in zip(["A", "B", "D", "E", "F"], r1)}
    r2 = {"A": 0.5, "C": 1.0}
    assert set(swing) == {"A", "B", "C"}  # ST, new listing and illiquid names are excluded
    # normalised by all voters' weight: a playbook that skipped a name counts as zero for it
    assert swing["A"]["ensemble"] == pytest.approx((0.8 * r1["A"] + 0.2 * r2["A"]) / 1.0)
    assert swing["B"]["ensemble"] == pytest.approx(0.8 * r1["B"])
    assert swing["C"]["ensemble"] == pytest.approx(0.2 * r2["C"])  # top of the weak playbook only
    assert swing["A"]["agreement"] == 2 and swing["B"]["agreement"] == 1
    assert swing["A"]["lead"] == "p1"
    for row in swing.values():  # swing blend: 50% ensemble, 15% sentiment, 35% technical
        expected = 100 * (0.5 * row["ensemble"] + (0.15 * row["sentiment"] + 0.35 * row["technical"]) / 100)
        assert row["score"] == pytest.approx(expected)
        assert all(0 <= row[k] <= 100 for k in ("technical", "sentiment", "trend", "score"))
    assert ranked["short"].is_empty() and ranked["long"].is_empty()
    assert ranked["swing"]["score"].to_list() == sorted(ranked["swing"]["score"].to_list(), reverse=True)
    assert set(contributions["swing"]["playbook_id"]) == {"p1", "p2"}


def test_an_untested_playbook_alone_produces_no_list():
    signals = {"value": pl.DataFrame({"code": ["A", "B"], "score": [1.0, 2.0]})}
    weights = [picks.Weight("value", "long", 0.25, untested=True)]
    ranked, _ = picks.ensemble_day(day_frame(), signals, weights, "climax")
    assert ranked["long"].is_empty()


def test_an_untested_playbook_only_adjusts_names_behind_tested_ones():
    signals = {
        "mom": pl.DataFrame({"code": ["A"], "score": [1.0]}),
        "value": pl.DataFrame({"code": ["A", "B"], "score": [1.0, 2.0]}),
    }
    weights = [picks.Weight("mom", "long", 0.5), picks.Weight("value", "long", 0.25, untested=True)]
    ranked, _ = picks.ensemble_day(day_frame(), signals, weights, "climax")
    ensemble = dict(ranked["long"].select("code", "ensemble").iter_rows())
    assert ensemble["A"] == pytest.approx((0.5 * 1.0 + 0.25 * 0.5) / 0.75)
    assert "B" not in ensemble  # only the untested playbook picked B: it never enters the list


def test_idle_horizons_are_named_with_the_closest_playbook():
    rows = [
        summary("eff", "effective", 70, horizon="swing"),
        summary("near", "weak", 24.7, horizon="short"),
        summary("far", "overfit", 8.0, horizon="short"),
        summary("value", "unavailable", None, horizon="long", style="value"),
    ]
    notes = picks.idle_notes(rows, picks.weights_from_board(rows))
    assert len(notes) == 2  # short and long sit out; swing has a qualified voter
    assert notes[0].startswith("短线：") and "观望" in notes[0] and "适配度 25，结论偏弱" in notes[0]
    assert notes[1].startswith("长线：") and "最接近" not in notes[1]  # value alone is untested


def test_sub_scores_reward_what_they_describe():
    frame = day_frame().filter(pl.col("code").is_in(["A", "C"]))
    scored = picks._day_scores(frame, "climax").sort("code")
    a, c = scored.row(0, named=True), scored.row(1, named=True)
    assert c["sentiment"] > a["sentiment"]  # a 2-board streak adds to sentiment
    assert c["technical"] > a["technical"]  # higher ret_20 rank
    cold = picks._day_scores(frame, "freezing").sort("code").row(0, named=True)
    assert cold["sentiment"] < a["sentiment"]  # the market phase feeds sentiment


def test_overall_keeps_one_row_per_code_at_its_best_horizon():
    def frame(rows):
        return pl.DataFrame(rows, schema={"code": pl.Utf8, "score": pl.Float64}, orient="row")

    ranked = {
        "short": frame([("A", 80.0), ("B", 60.0)]),
        "swing": frame([("A", 90.0), ("C", 70.0)]),
        "long": frame([]),
    }
    best = picks.overall_from(ranked)
    assert best["code"].to_list() == ["A", "C", "B"]
    assert best.filter(pl.col("code") == "A")["horizon"].item() == "swing"


# --- point-in-time weights and replay ---------------------------------------------------------

N_DAYS = 160
SPLIT_DATE = D0 + timedelta(days=int(N_DAYS * 0.7) - 1)


def market(change_after: int | None = None, shock: float = 0.0, n_codes: int = 12) -> ev.Context:
    """Rising market with noise; from index `change_after` on every price path is shocked."""
    rows = []
    for c in range(n_codes):
        rng = np.random.default_rng(100 + c)
        noise = rng.normal(0, 0.004, N_DAYS)
        drift = np.full(N_DAYS, 0.006)
        if change_after is not None:
            drift[change_after:] = shock
            noise[change_after:] = np.random.default_rng(900 + c).normal(0, 0.02, N_DAYS - change_after)
        close = 10 * np.cumprod(1 + drift + noise)
        ret_20 = np.r_[np.full(20, np.nan), (close[20:] / close[:-20] - 1) * 100]
        for i in range(N_DAYS):
            rows.append({
                "code": f"{c:06d}", "date": D0 + timedelta(days=i), "open": close[i], "high": close[i] * 1.003,
                "low": close[i] * 0.997, "close": close[i], "atr_14": close[i] * 0.02, "is_limit_down": False,
                "open_at_limit": False, "open_at_floor": False, "name": f"股{c}", "days_listed": i + 100,
                "amount": 3.0, "ret_20": ret_20[i], "volume": 100.0, "vol_ma20": 100.0,
            })
    panel = pl.DataFrame(rows)
    dates = [D0 + timedelta(days=i) for i in range(N_DAYS)]
    regimes = {d.isoformat(): {"trend": "bull", "liquidity": "steady", "sentiment": "repair", "style": "balanced"} for d in dates}
    return ev.Context(wh=SimpleNamespace(dataset="sim", root="x"), version="v", panel=panel, dates=dates,
                      regimes=regimes, benchmark=None, names={})


def drift_playbook() -> Playbook:
    def signal(panel: pl.LazyFrame, params):
        return (
            panel.with_columns(pl.int_range(pl.len()).over("code").alias("_i"))
            .filter((pl.col("_i") % 3 == 0) & (pl.col("_i") >= 1))
            .select("code", "date", (pl.col("code").cast(pl.Int64) + 1).cast(pl.Float64).alias("score"))
        )

    return Playbook(spec=spec("drift", holding=4, stop=None), signal=signal)


def test_in_sample_weight_thresholds():
    assert picks.in_sample_weight(PerformanceStats(trades=29, avg_return=2, t_stat=5)) == 0
    assert picks.in_sample_weight(PerformanceStats(trades=60, avg_return=-1, t_stat=5)) == 0
    assert picks.in_sample_weight(PerformanceStats(trades=60, avg_return=1, t_stat=1.9)) == 0
    assert picks.in_sample_weight(PerformanceStats(trades=60, avg_return=1, t_stat=2.5)) == pytest.approx(0.5)
    assert picks.in_sample_weight(PerformanceStats(trades=60, avg_return=1, t_stat=9)) == 1.0


def test_weights_use_in_sample_data_only(monkeypatch):
    seen = []
    real = picks.simulate

    def spy(panel, signals, *args, **kwargs):
        seen.append((panel["date"].max(), signals["date"].max(), kwargs.get("end")))
        return real(panel, signals, *args, **kwargs)

    monkeypatch.setattr(picks, "simulate", spy)
    pb = drift_playbook()
    base = picks.in_sample_weights(market(), SPLIT_DATE, [pb])
    assert base and base[0].weight > 0, "the planted IS drift should earn a vote"
    assert all(p <= SPLIT_DATE and s <= SPLIT_DATE and e == SPLIT_DATE for p, s, e in seen)
    # wreck every price from the split on: in-sample weights cannot notice
    after_split = N_DAYS * 7 // 10
    for shock in (-0.02, 0.05):
        assert picks.in_sample_weights(market(after_split, shock), SPLIT_DATE, [pb]) == base


def test_replay_picks_ignore_the_future(monkeypatch):
    captured: dict[str, dict[str, list]] = {}

    def run(ctx, tag):
        store: dict = {}

        def fake_outcomes(prices, trade_date, groups):
            store[trade_date] = {h: list(v) for h, v in groups.items()}
            return []

        monkeypatch.setattr(picks, "_outcomes", fake_outcomes)
        picks.simulated_outcomes(ctx, None, [drift_playbook()])
        captured[tag] = store

    cutoff = 135
    run(market(), "calm")
    run(market(cutoff, -0.03), "crash")
    safe = [d for d in captured["calm"] if d < (D0 + timedelta(days=cutoff)).isoformat()]
    assert len(safe) >= 3
    for d in safe:
        assert captured["calm"][d] == captured["crash"][d], f"picks on {d} changed with future data"
    assert any(captured["calm"][d]["swing"] for d in safe)
    # replay dates are all out of sample and sampled every REPLAY_STEP-th bar
    assert all(d > SPLIT_DATE.isoformat() for d in captured["calm"])


def test_simulated_track_record_scores_forward_returns():
    ctx = market()
    prices = picks.Prices(ctx)
    outcomes, weights, split = picks.simulated_outcomes(ctx, prices, [drift_playbook()])
    assert weights and split == SPLIT_DATE.isoformat()
    swing = [o for o in outcomes if o.horizon == "swing" and o.returns]
    assert swing and all(o.picks >= len(o.returns) for o in swing)
    points = picks._points(outcomes, "simulated")
    assert {p.kind for p in points} == {"simulated"}
    summary_rows = picks._summaries(outcomes, "simulated")
    swing_summary = next(s for s in summary_rows if s.horizon == "swing")
    assert swing_summary.matured_picks > 0 and swing_summary.avg_return is not None
    # forward return: next open to close 10 bars later, net of costs, on a rising market
    one = prices.forward("000003", prices.date_index(SPLIT_DATE + timedelta(days=1)), 10)
    assert one is not None and one[0] > 0 and one[1] is None


# --- report build, idempotency, persistence ------------------------------------------------------

REGIME = RegimeState(
    date="2026-06-13", trend="bull", liquidity="steady", sentiment="repair", style="balanced",
    label="牛市 · 存量 · 修复", explanation=["测试环境"],
)


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    ctx = market()
    ctx.wh = Warehouse(tmp_path / "wh", "sim")
    pb = drift_playbook()
    rows = [summary("drift", "effective", 80)]
    rows[0] = rows[0].model_copy(update={"spec": pb.spec})
    calls = {"build": 0}
    real_build = picks.build_report

    def counting_build(*args, **kwargs):
        calls["build"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(picks, "context", lambda: ctx)
    monkeypatch.setattr(picks, "board", lambda: SimpleNamespace(playbooks=rows))
    monkeypatch.setattr(picks, "current_regime_state", lambda wh: REGIME)
    monkeypatch.setattr(picks, "catalog", lambda: [pb])
    monkeypatch.setattr(picks, "build_report", counting_build)
    return ctx, calls


def test_report_contents_and_idempotency(report_env):
    ctx, calls = report_env
    first = picks.generate_report()
    assert first.trade_date == ctx.dates[-1].isoformat()
    assert calls["build"] == 1
    again = picks.generate_report()
    assert again.generated_at == first.generated_at and calls["build"] == 1  # idempotent per date
    picks.generate_report(force=True)
    assert calls["build"] == 2
    ctx.version = "v2"  # new bars for the same day (an after-close sync) rebuild the report
    picks.generate_report()
    assert calls["build"] == 3
    picks.generate_report()
    assert calls["build"] == 3
    assert picks.report_index().reports[0].trade_date == first.trade_date
    assert picks.latest_report().trade_date == first.trade_date
    assert picks.report("2000-01-01") is None
    assert any("研究用途" in n for n in first.notes) and any("SIM" in n for n in first.notes)
    assert first.weights[0].weight == pytest.approx(0.8) and first.weights[0].verdict == "effective"
    assert len(first.short) == 0 and len(first.long) == 0


def test_rotation_weights_scale_by_the_best_elected_playbook():
    from contracts.schema import RotationScore

    current = [
        RotationScore(playbook_id="a", name="甲", horizon="short", trades=20, avg_return=2.0, score=1.6, selected=True),
        RotationScore(playbook_id="b", name="乙", horizon="swing", trades=12, avg_return=1.0, score=0.8, selected=True),
        RotationScore(playbook_id="c", name="丙", horizon="long", trades=30, avg_return=3.0, score=2.5, selected=False),
    ]
    rows = [summary("value", "unavailable", None, horizon="long", style="value")]
    weights = {w.playbook_id: w for w in picks.weights_from_rotation(current, rows)}
    assert set(weights) == {"a", "b", "value"}  # not elected: no vote, whatever its score
    assert weights["a"].weight == pytest.approx(1.0) and weights["b"].weight == pytest.approx(0.5)
    assert weights["value"].untested


def test_a_validated_rotation_picks_the_voters_and_each_pick_has_a_sell_plan(report_env, monkeypatch):
    from contracts.schema import RotationReport, RotationScore

    ctx, _ = report_env
    real = picks.rotation.report(ctx=ctx, playbooks=picks.catalog())
    elected = real.model_copy(update={
        "validated": True,
        "current": [RotationScore(playbook_id="drift", name="漂移", horizon="swing", trades=15, avg_return=1.2,
                                  score=0.9, selected=True, window=20)],
    })
    monkeypatch.setattr(picks.rotation, "report", lambda **kwargs: elected)
    report = picks.generate_report(force=True)
    assert report.selection == "rotation"
    assert report.weights[0].weight == pytest.approx(1.0)
    top = report.swing[0]
    assert "轮换推选" in top.reasons[0].note and "15 笔" in top.reasons[0].note
    assert top.exit_plan and top.exit_plan[-1].startswith("以上都没触发")
    assert any("轮换推选" in note for note in report.notes)
    idle = [n for n in report.notes if n.startswith(("短线：", "长线："))]
    assert len(idle) == 2 and all("近 20 日轮换推选里没有" in n for n in idle)
    assert isinstance(real, RotationReport)


def test_picks_carry_exits_flags_and_reasons(report_env):
    ctx, _ = report_env
    # the final bar (index 159) is a signal bar: 159 % 3 == 0
    assert (N_DAYS - 1) % 3 == 0
    report = picks.generate_report()
    assert 0 < len(report.swing) <= 10 and report.overall and len(report.overall) <= 20
    top = report.swing[0]
    assert top.rank == 1 and top.horizon == "swing" and top.entry_note.startswith("次日")
    assert top.holding_days == 4
    assert top.reasons[0].playbook_id == "drift" and "判定理由" in top.reasons[0].note
    assert top.stop_loss is not None and top.stop_loss <= top.close  # max(close*(1-stop), close-2*atr)
    ranks = [p.rank for p in report.swing]
    assert ranks == list(range(1, len(ranks) + 1))
    assert len({p.code for p in report.overall}) == len(report.overall)


def test_long_playbooks_carry_their_last_rebalance_until_the_window_ends():
    dates = [D0 + timedelta(days=i) for i in range(60)]
    frame = pl.DataFrame({"code": ["A", "B", "C"], "date": [dates[2], dates[2], dates[40]], "score": [1.0, 2.0, 3.0]})
    long_pb = Playbook(spec("m", horizon="long", holding=20), lambda panel, params: panel)
    short_pb = Playbook(spec("s", horizon="short", holding=5), lambda panel, params: panel)
    window = picks._window(long_pb)
    assert window == picks.REBALANCE_CYCLE  # a month of holdings, even if each position is held 20 bars
    # Within one rebalance cycle the day-2 list is still in force for the long playbook, gone for the short one.
    assert picks._as_of(frame, dates, 2 + window - 1, window)["code"].to_list() == ["A", "B"]
    assert picks._as_of(frame, dates, 5, picks._window(short_pb)).is_empty()
    # Past the cycle it expires; the day-40 list is in the future until day 40 and never leaks back.
    assert picks._as_of(frame, dates, 2 + window, window).is_empty()
    assert picks._as_of(frame, dates, 39, window).is_empty()
    assert picks._as_of(frame, dates, 41, window)["code"].to_list() == ["C"]


def test_benchmark_window_matches_the_stock_window():
    ctx = market()
    ctx.benchmark = pl.DataFrame({
        "date": ctx.dates,
        "open": [100.0 + i for i in range(len(ctx.dates))],
        "close": [100.5 + i for i in range(len(ctx.dates))],
    })
    prices = picks.Prices(ctx)
    code = ctx.panel["code"][0]
    _, bench = prices.forward(code, 10, 3)
    # entry-day open (index 11) to exit-day close (index 14), like the stock itself
    assert bench == pytest.approx((114.5 / 111.0 - 1) * 100)


def test_a_failing_signal_is_named_in_the_report(report_env, monkeypatch):
    ctx, _ = report_env
    monkeypatch.setattr(picks, "run_signals", lambda pb, ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    report = picks.generate_report(force=True)
    assert any("信号计算失败" in note and "RuntimeError" in note for note in report.notes)


def test_risk_flags_are_chinese_and_specific():
    row = {"code": "X", "one_word": True, "close": 12.0, "ma20": 10.0, "volatility_20": 5.0, "amount": 0.8}
    flags = picks._flags(row, 4.0, {"X"})
    assert any("一字" in f and "买不到" in f for f in flags)
    assert any("偏离 20 日线" in f for f in flags)
    assert any("波动率" in f for f in flags)
    assert any("跌停" in f for f in flags)
    assert any("流动性" in f for f in flags)
    assert picks._flags({"code": "Y", "close": 10, "ma20": 10, "volatility_20": 1, "amount": 5}, 4.0, set()) == []


def test_stop_loss_takes_the_tighter_of_pct_and_atr():
    assert picks._stop_loss(100.0, 1.0, 8) == 98.0  # 2 ATR (98) is tighter than 8% (92)
    assert picks._stop_loss(100.0, 10.0, 8) == 92.0  # 8% (92) is tighter than 2 ATR (80)
    assert picks._stop_loss(100.0, None, None) is None


def test_recorded_track_record_matures_only_after_the_window(report_env, monkeypatch):
    ctx, _ = report_env
    prices = picks.Prices(ctx)
    report = picks.generate_report()
    assert picks.recorded_outcomes(ctx, prices)[0].returns == []  # report is on the last bar: nothing matured
    # an older stored report whose swing window has passed
    old = report.model_copy(update={"trade_date": ctx.dates[100].isoformat()})
    ctx.wh.write_doc("reports", old.trade_date, old.model_dump(mode="json"))
    outcomes = {(o.trade_date, o.horizon): o for o in picks.recorded_outcomes(ctx, prices)}
    swing = outcomes[(old.trade_date, "swing")]
    assert swing.picks == len(old.swing) and len(swing.returns) == len(old.swing) and swing.returns
    pts = picks._points([swing], "recorded")
    assert pts[0].kind == "recorded" and pts[0].matured == len(swing.returns)


# --- HTTP smoke on the SIM dataset ------------------------------------------------------------------

def test_picks_api_smoke():
    pytest.importorskip("backend.app.playbooks.library")
    pytest.importorskip("backend.app.playbooks.regime")
    from fastapi.testclient import TestClient

    from backend.app.main import app

    client = TestClient(app)
    today = client.get("/api/v1/picks/today")
    assert today.status_code == 200
    body = today.json()
    assert body["provenance"]["mode"] in ("SIM", "LIVE") and body["notes"]
    assert any("研究用途" in n for n in body["notes"])
    again = client.get("/api/v1/picks/today").json()
    assert again["generated_at"] == body["generated_at"]
    assert client.post("/api/v1/picks/generate").status_code == 200
    history = client.get("/api/v1/picks/history")
    assert history.status_code == 200 and history.json()["reports"][0]["trade_date"] == body["trade_date"]
    assert client.get(f"/api/v1/picks/{body['trade_date']}").status_code == 200
    assert client.get("/api/v1/picks/1999-01-01").status_code == 404
    record = client.get("/api/v1/picks/track-record")
    assert record.status_code == 200
    kinds = {s["kind"] for s in record.json()["summary"]}
    assert kinds == {"recorded", "simulated"}
