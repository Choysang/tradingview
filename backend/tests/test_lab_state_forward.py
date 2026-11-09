"""E089 market state log (scripts/lab/state_forward.py) on small hand-built recordings."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sys

import polars as pl
import pytest

LAB = Path(__file__).resolve().parents[2] / "scripts" / "lab"
sys.path.insert(0, str(LAB))

import state_forward as S  # noqa: E402

from backend.app.warehouse.intraday_ext import SCHEMA  # noqa: E402

S_SCHEMA = SCHEMA

DAY = date(2026, 11, 10)


def row(symbol: str, at: time, price: float, prev: float = 1.0, *, kind: str = "grid", amount: float | None = None, volume: float | None = None,
        oi: float | None = None, quote: datetime | None = None, day: date = DAY) -> dict:
    stamp = datetime.combine(day, at)
    return {**{c: None for c in SCHEMA}, "symbol": symbol, "kind": kind, "poll_ts": stamp, "quote_ts": quote or stamp, "price": price,
            "prev_close": prev, "amount": amount, "volume": volume, "open_interest": oi, "asset": "x", "host": "x", "name": symbol, "prev_kind": "close"}


def etf_rows(day_total: float, late_total: float, *, skip: tuple[str, ...] = (), day: date = DAY) -> list[dict]:
    """Each ETF's cumulative amount: late_total / 7 at 14:30, day_total / 7 at the close."""
    out = []
    for s in S.ETFS:
        if s in skip:
            continue
        out.append(row(s, time(14, 30, 5), 4.0, 4.0, amount=late_total / 7, day=day))
        out.append(row(s, time(15, 0, 5), 4.04, 4.0, amount=day_total / 7, volume=day_total / 7 / 400, day=day))
    return out


def write_ext(root: Path, rows: list[dict], summary: dict | None = None, day: date = DAY) -> Path:
    d = root / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=SCHEMA).write_parquet(d / "quotes_ext.parquet")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def write_quotes(root: Path, pcts: list[float], day: date = DAY) -> Path:
    d = root / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"code": f"600{i:03d}", "name": f"n{i}", "kind": "close", "poll_ts": datetime.combine(day, time(15, 1, 30)), "price": 10 * (1 + p / 100),
             "prev_close": 10.0, "open": 10.0, "limit_up": 11.0, "turnover": 1.0, "float_cap": 50.0, "volume_ratio": 1.0} for i, p in enumerate(pcts)]
    pl.DataFrame(rows).write_parquet(d / "quotes.parquet")
    return d


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(S, "LIVE", tmp_path / "live")
    monkeypatch.setattr(S, "STATE_LOG", tmp_path / "live" / "state.jsonl")
    monkeypatch.setattr(S, "SEED", tmp_path / "live" / "seed.csv")
    (tmp_path / "live").mkdir()
    return tmp_path / "live"


def seed_days(live: Path, amount: float = 100.0, n: int = 25) -> None:
    days = [DAY - timedelta(days=i + 1) for i in range(n)]
    (live / "seed.csv").write_text("date,amount\n" + "".join(f"{d},{amount}\n" for d in sorted(days)), encoding="utf-8")


def quotes(root: Path, pcts: list[float]) -> pl.DataFrame:
    return S.snapshots(write_quotes(root, pcts))


def test_an_ordinary_day(live: Path, tmp_path: Path):
    seed_days(live)
    rows = [*etf_rows(120.0, 90.0),
            row("sh000001", time(15, 0, 5), 101.0, 100.0), row("sh000300", time(15, 0, 5), 4000.0),
            row("IF2611", time(15, 0, 5), 3960.0, oi=500.0, quote=datetime.combine(DAY, time(14, 58))),
            row("IF2612", time(15, 0, 5), 3980.0, oi=900.0, quote=datetime.combine(DAY, time(14, 58))),
            row("hf_HSI", time(14, 59, 5), 102.0, 100.0), row("em_CN00Y", time(15, 0), 99.0, 100.0, kind="minute")]
    d = write_ext(tmp_path / "ext", rows, {"end": "done", "missed": 0, "never_returned": ["x"], "blocked": {"h": "t"}})
    r = S.state_row(DAY, d, quotes(tmp_path / "q", [1.0, 3.0, -1.0]))
    assert r["etf_amount"] == 120.0 and r["n_etf"] == 7 and r["ratio"] == 1.2 and r["surge"] is False and r["panic_support"] is False
    assert r["late_share"] == 0.25 and r["late_heavy"] is False
    assert r["vwap_gap_pct"] == 1.0  # the close 4.04 against an average price of 4.00
    assert r["ew_pct"] == 1.0 and r["up_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["sh_pct"] == 1.0 and r["hsi_pct"] == 2.0 and r["a50_pct"] == -1.0 and r["cnh_pct"] is None
    assert r["futures"]["IF"]["contract"] == "IF2612" and r["futures"]["IF"]["basis_pct"] == -0.5 and r["futures"]["IM"] is None
    assert r["health"]["end"] == "done" and r["health"]["a50_bars"] == 1 and r["health"]["cnh_bars"] == 0 and r["health"]["blocked"] == ["h"]


@pytest.mark.parametrize("ew,surge,panic", [(-3.0, True, True), (-1.0, True, False)])
def test_panic_support_needs_both_the_surge_and_the_fall(live: Path, tmp_path: Path, ew: float, surge: bool, panic: bool):
    seed_days(live)
    d = write_ext(tmp_path / "ext", etf_rows(200.0, 150.0))
    r = S.state_row(DAY, d, quotes(tmp_path / "q", [ew]))
    assert r["surge"] is surge and r["panic_support"] is panic


def test_a_late_heavy_day(live: Path, tmp_path: Path):
    r = S.state_row(DAY, write_ext(tmp_path / "ext", etf_rows(100.0, 70.0)), None)
    assert r["late_share"] == 0.3 and r["late_heavy"] is True


def test_unknown_stays_null_not_false(live: Path, tmp_path: Path):
    """Under 20 sessions of baseline there is no ratio, so no flag; a missing ETF gives no sum at all."""
    seed_days(live, n=19)
    r = S.state_row(DAY, write_ext(tmp_path / "a", etf_rows(300.0, 90.0)), quotes(tmp_path / "q", [-5.0]))
    assert r["ratio"] is None and r["surge"] is None and r["panic_support"] is None
    seed_days(live)
    r = S.state_row(DAY, write_ext(tmp_path / "b", etf_rows(300.0, 90.0, skip=("sh510050",))), None)
    assert r["etf_amount"] is None and r["n_etf"] == 6 and r["ratio"] is None and r["late_share"] is None
    r = S.state_row(DAY, write_ext(tmp_path / "c", etf_rows(300.0, 90.0)), None)
    assert r["ratio"] == 3.0 and r["surge"] is True and r["ew_pct"] is None and r["panic_support"] is None


def test_the_baseline_prefers_recorded_sums_and_skips_incomplete_days(live: Path, tmp_path: Path):
    seed_days(live, amount=100.0)
    d1, d2 = date(2026, 11, 9).isoformat(), date(2026, 11, 8).isoformat()
    (live / "state.jsonl").write_text(json.dumps({"date": d1, "etf_amount": 500.0}) + "\n" + json.dumps({"date": d2, "etf_amount": None}) + "\n", encoding="utf-8")
    base = S.baseline(DAY)
    assert len(base) == 20 and base[-1] == 500.0 and base.count(100.0) == 19


def test_cffex_cadence_comes_from_quote_ts_and_ignores_the_poll_clock(tmp_path: Path, live: Path):
    stamps = [time(9, 30), time(9, 35), time(9, 40), time(9, 40), time(9, 50), time(15, 2)]
    rows = [row("sh000300", time(15, 0, 5), 4000.0)]
    for i, q in enumerate(stamps):
        rows.append(row("IF2612", time(10, i), 3980.0, oi=900.0, quote=datetime.combine(DAY, q)))
    rows.append(row("IF2612", time(15, 0, 5), 3980.0, oi=900.0, quote=datetime.combine(DAY, time(15, 2))))
    x = pl.DataFrame(rows, schema=SCHEMA)
    assert S.cffex_gap(x, DAY) == 5.0  # 5, 5, 10 minutes; 15:02 is after the session
    assert S.cffex_gap(pl.DataFrame([row("sh000300", time(15, 0, 5), 4000.0)], schema=SCHEMA), DAY) is None


NOON, EVENING = datetime.combine(DAY, time(12, 0)), datetime.combine(DAY, time(18, 0))


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: Path) -> tuple[Path, Path]:
    q, x = tmp_path / "q", tmp_path / "ext"
    monkeypatch.setenv("PULSE_INTRADAY_DIR", str(q))
    monkeypatch.setenv("PULSE_INTRADAY_EXT_DIR", str(x))
    monkeypatch.setattr(S, "load_calendar", lambda: S_CALENDAR)
    seed_days(live)
    return q, x


class _Calendar:
    @staticmethod
    def is_trading_day(day: date) -> bool:
        return day.weekday() < 5


S_CALENDAR = _Calendar()


def test_run_appends_once_and_waits_for_the_recorders(dirs: tuple[Path, Path]):
    q, _ = dirs
    write_quotes(q, [1.0], day=date(2026, 11, 9))  # before the start: not logged
    write_quotes(q, [1.0])
    assert S.run(NOON) == {"added": [], "errors": []}  # today is not over
    assert S.run(EVENING)["added"] == [DAY.isoformat()]
    assert S.run(EVENING)["added"] == []
    r = S.rows_logged()[0]
    assert r["ext_missing"] is True and r["ew_pct"] == 1.0 and r["etf_amount"] is None and r["health"]["end"] is None and r["surge"] is None


def test_a_session_with_no_recording_at_all_gets_a_row_and_a_weekend_none(dirs: tuple[Path, Path]):
    monday = datetime.combine(date(2026, 11, 16), time(18, 0))  # 11-10 ~ 11-13 are sessions, 11-14 and 11-15 a weekend
    assert S.run(monday)["added"] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]
    assert all(r["ext_missing"] and r["ew_pct"] is None for r in S.rows_logged())


@pytest.mark.parametrize("which", ["q", "ext"])
def test_an_unlisted_holiday_found_by_either_recorder_gets_no_row(dirs: tuple[Path, Path], which: str):
    d = dirs[0 if which == "q" else 1] / DAY.isoformat()
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({"end": "quotes not stamped today: not a trading day"}), encoding="utf-8")
    assert S.run(EVENING)["added"] == []


def test_a_broken_file_costs_that_day_only(dirs: tuple[Path, Path]):
    _, x = dirs
    next_day = DAY + timedelta(days=1)
    (x / DAY.isoformat()).mkdir(parents=True)
    (x / DAY.isoformat() / "quotes_ext.parquet").write_bytes(b"not a parquet file")
    out = S.run(datetime.combine(next_day, time(18, 0)))
    assert out["added"] == [DAY.isoformat(), next_day.isoformat()] and out["errors"] == [DAY.isoformat()]
    rows = S.rows_logged()
    assert rows[0]["error"].startswith("ComputeError") or rows[0]["error"]
    assert rows[1]["ext_missing"] is True
    assert DAY.isoformat() in S.summary()["todo"][0]


def test_a_broken_summary_costs_its_fields_only(live: Path, tmp_path: Path):
    seed_days(live)
    d = write_ext(tmp_path / "ext", etf_rows(120.0, 90.0))
    (d / "summary.json").write_text("{not json", encoding="utf-8")
    r = S.state_row(DAY, d, None)
    assert r["health"]["end"] is None and r["etf_amount"] == 120.0


def test_a_half_written_log_line_stops_nothing(dirs: tuple[Path, Path], live: Path):
    (live / "state.jsonl").write_text(json.dumps({"date": "2026-11-09", "etf_amount": 500.0}) + "\n" + '{"date": "2026-11-1', encoding="utf-8")
    assert [r["date"] for r in S.rows_logged()] == ["2026-11-09"] and S.baseline(DAY)[-1] == 500.0
    assert S.run(EVENING)["added"] == [DAY.isoformat()]
    assert [r["date"] for r in S.rows_logged()] == ["2026-11-09", DAY.isoformat()]  # the new row starts on its own line


def test_a_recorder_that_died_at_ten_reports_nothing_not_a_quiet_day(live: Path, tmp_path: Path):
    seed_days(live)
    rows = [row(e, time(10, 0, 5), 4.0, 4.0, amount=5.0, volume=1.0) for e in S.ETFS]
    rows += [row("hf_HSI", time(10, 0, 5), 102.0, 100.0), row("sh000001", time(10, 0, 5), 101.0, 100.0)]
    r = S.state_row(DAY, write_ext(tmp_path / "ext", rows), None)
    assert r["etf_amount"] is None and r["n_etf"] == 0 and r["ratio"] is None and r["surge"] is None and r["late_share"] is None
    assert r["vwap_gap_pct"] is None and r["hsi_pct"] is None and r["sh_pct"] is None
    assert S.baseline(DAY + timedelta(days=1)) == [100.0] * 20  # a partial day never enters the baseline


def test_stale_and_smoke_rows_do_not_count(live: Path, tmp_path: Path):
    """A holiday's feed repeats the last session (its quote_ts); a smoke test's rows are kind once."""
    seed_days(live)
    yesterday = datetime.combine(DAY - timedelta(days=1), time(15, 0))
    stale = [row(e, time(15, 0, 5), 4.0, 4.0, amount=500.0, volume=1.0, quote=yesterday) for e in S.ETFS]
    assert S.state_row(DAY, write_ext(tmp_path / "a", stale), None)["etf_amount"] is None
    smoke = [row(e, time(15, 0, 5), 4.0, 4.0, amount=500.0, volume=1.0, kind="once") for e in S.ETFS]
    assert S.state_row(DAY, write_ext(tmp_path / "b", smoke), None)["etf_amount"] is None


def test_the_parts_of_a_crashed_recorder_are_read(live: Path, tmp_path: Path):
    seed_days(live)
    d = tmp_path / "ext" / DAY.isoformat()
    (d / "parts").mkdir(parents=True)
    pl.DataFrame(etf_rows(120.0, 90.0), schema=S_SCHEMA).write_parquet(d / "parts" / "part-1.parquet")
    r = S.state_row(DAY, d, None)
    assert r["etf_amount"] == 120.0 and r["health"]["merged"] is False and "ext_missing" not in r


def h(a50: int = 300, cnh: int = 300, gap: float | None = 1.0) -> dict:
    return {"a50_bars": a50, "cnh_bars": cnh, "cffex_gap_min": gap}


def test_actions_need_an_unbroken_streak():
    missing = [{"health": h(a50=0)} for _ in range(4)]
    assert S.actions(missing)["todo"] == [] and S.actions(missing)["a50_cnh_missing_days"] == 4
    assert "PC pipeline" in S.actions([{"health": h(cnh=0)}, *missing])["todo"][0]
    assert S.actions([{"health": h(a50=0)}, {"health": h()}, *missing])["a50_cnh_missing_days"] == 4
    slow = [{"health": h(gap=5.25)} for _ in range(3)]
    assert "5 minutes" in S.actions(slow)["todo"][0]
    assert S.actions([*slow, {"health": h(gap=None)}])["cffex_slow_days"] == 0


def test_a_lost_day_is_its_own_problem_and_not_a_failed_source():
    ran = [{"date": f"d{i}", "health": h(a50=0)} for i in range(4)]
    lost = {"date": "lost", "ext_missing": True, "health": h(a50=0, cnh=0)}
    a = S.actions([*ran, lost])
    assert a["a50_cnh_missing_days"] == 4 and a["lost_days"] == ["lost"] and len(a["todo"]) == 1 and "lost" in a["todo"][0]
    assert S.actions([lost, {"date": "x", "error": "boom"}, *[{"date": "y", "health": h()}] * 5])["lost_days"] == []
    errored = S.actions([*[{"date": "y", "health": h()}] * 2, {"date": "x", "error": "boom"}])
    assert errored["lost_days"] == ["x"] and errored["a50_cnh_missing_days"] == 0


def test_summary_counts_episodes_and_compounds_only_full_windows(live: Path):
    days = [(DAY + timedelta(days=i)).isoformat() for i in range(30)]
    rows = [{"date": d, "ew_pct": 1.0, "health": h(), "panic_support": False} for d in days]
    for i in (2, 5, 20):  # 2 and 5 are one episode, 20 another
        rows[i]["panic_support"] = True
    rows[8]["ew_pct"] = None
    (live / "state.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    s = S.summary()
    assert s["days"] == 30 and s["panic_episodes"] == 2 and s["h1_check_at"] == 10
    first, second = s["panic_next"]
    assert first["date"] == days[2] and first["ew_1"] == 1.0 and first["ew_5"] == pytest.approx(5.101, abs=1e-3)
    assert first["ew_20"] is None  # the null on day 8 breaks the window
    assert second["date"] == days[20] and second["ew_5"] is not None and second["ew_20"] is None  # past the end


def test_seed_keeps_complete_days_only(live: Path, tmp_path: Path):
    wanted = [s[2:] for s in S.ETFS]
    a, b = date(2026, 11, 5), date(2026, 11, 6)
    rows = [{"symbol": s, "date": a, "amount": 10.0} for s in wanted] + [{"symbol": s, "date": b, "amount": 10.0} for s in wanted[:6]]
    rows += [{"symbol": "999999", "date": a, "amount": 1e9}, *[{"symbol": s, "date": DAY, "amount": 10.0} for s in wanted]]
    f = tmp_path / "etf.parquet"
    pl.DataFrame(rows).write_parquet(f)
    assert S.seed(f)["seed_days"] == 1
    assert (live / "seed.csv").read_text(encoding="utf-8") == "date,amount\n2026-11-05,70.0\n"
