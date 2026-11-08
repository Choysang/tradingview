"""Crowd features: industry contagion, name clusters and seat records, all point-in-time."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from backend.app.playbooks import crowd
from backend.app.warehouse.store import Warehouse

D0 = date(2026, 1, 5)
N = 14


def day(i: int) -> date:
    return D0 + timedelta(days=i)


def panel(names: dict[str, str], limit_ups: set[tuple[str, int]] | None = None, n: int = N) -> pl.DataFrame:
    """Rising prices (2% a day) so every forward return is positive."""
    rows = []
    for code, name in names.items():
        for i in range(n):
            price = 10 * 1.02 ** i
            rows.append({
                "code": code, "date": day(i), "name": name, "is_limit_up": (code, i) in (limit_ups or set()),
                "open": price, "close": price, "amount": 10.0,
            })
    return pl.DataFrame(rows).sort(["code", "date"])


def seat_rows(events: list[tuple[str, int, str, str, float, float]]) -> pl.DataFrame:
    """(code, day, side, seat, buy, sell)."""
    return pl.DataFrame(
        {
            "code": [e[0] for e in events], "date": [day(e[1]) for e in events], "side": [e[2] for e in events],
            "seat": [e[3] for e in events], "seat_code": ["0"] * len(events),
            "rank": list(range(1, len(events) + 1)), "reason": ["日涨幅偏离值达 7%"] * len(events),
            "buy": [e[4] for e in events], "sell": [e[5] for e in events],
            "net": [e[4] - e[5] for e in events],
        },
        schema_overrides={"rank": pl.Int32},
    )


def listed(pairs: set[tuple[str, int]]) -> pl.DataFrame:
    return pl.DataFrame({"code": [p[0] for p in pairs], "date": [day(p[1]) for p in pairs], "reason": ["r"] * len(pairs)})


def at(frame: pl.DataFrame, code: str, i: int) -> dict:
    return frame.filter((pl.col("code") == code) & (pl.col("date") == day(i))).row(0, named=True)


def test_industry_counts_limit_ups_of_the_same_industry(tmp_path):
    wh = Warehouse(tmp_path / "live", "live")
    wh.write("boards", pl.DataFrame({
        "code": ["A", "B", "C", "D"], "kind": ["industry"] * 4, "board_id": ["x", "x", "x", "y"],
        "board_name": ["传媒", "传媒", "传媒", "银行"], "as_of": [day(0)] * 4,
    }))
    frame = crowd.crowd_features(wh, panel({"A": "甲", "B": "乙", "C": "丙", "D": "丁", "E": "戊"}, {("A", 3), ("B", 3)}))
    assert at(frame, "C", 3)["ind_limit_ups"] == 2 and at(frame, "C", 3)["industry"] == "传媒"
    assert at(frame, "D", 3)["ind_limit_ups"] == 0
    assert at(frame, "E", 3)["ind_limit_ups"] is None  # no known industry: unknown, not zero
    assert at(frame, "C", 2)["ind_limit_ups"] == 0


def test_name_heat_counts_other_limit_ups_sharing_a_rare_character(tmp_path):
    wh = Warehouse(tmp_path / "live", "live")
    names = {"A": "金羊科技", "B": "羊城传媒", "C": "南羊股份", "D": "金牛电子", "E": "牛奶乳业"}
    frame = crowd.crowd_features(wh, panel(names, {("A", 4), ("B", 4)}))
    assert at(frame, "C", 4)["name_heat"] == 2 and at(frame, "C", 4)["name_char"] == "羊"
    assert at(frame, "A", 4)["name_heat"] == 1  # itself excluded
    assert at(frame, "D", 4)["name_heat"] == 1 and at(frame, "D", 4)["name_char"] == "金"  # shares 金 with A
    assert at(frame, "E", 4)["name_heat"] is None  # 牛 is shared only with D, which did not limit up
    assert at(frame, "C", 3)["name_heat"] is None
    assert frame["ind_limit_ups"].null_count() == frame.height  # no boards table: unknown everywhere


def test_common_characters_never_form_a_cluster():
    names = pl.DataFrame({"code": [str(i) for i in range(30)], "name": [f"中{i}科技" for i in range(30)]})
    chars = crowd.rare_chars(names)
    assert chars.is_empty() or not set(chars["char"]) & {"中", "科", "技"}


def history_seats() -> pl.DataFrame:
    # S1 and S2 buy H on days 0..4 (five events, each settling FORWARD_BARS days later);
    # both then buy T on day 6 and U on day 7.
    events = []
    for i in range(5):
        events += [("H", i, "buy", "S1", 1.0, 0.0), ("H", i, "buy", "S2", 1.0, 0.0)]
    events += [("T", 6, "buy", "S1", 1.0, 0.0), ("T", 6, "buy", "S2", 1.0, 0.0)]
    events += [("U", 7, "buy", "S1", 1.0, 0.0), ("U", 7, "buy", "S2", 1.0, 0.0)]
    return seat_rows(events)


def lake_with(tmp_path, seats: pl.DataFrame) -> Warehouse:
    wh = Warehouse(tmp_path / "live", "live")
    wh.write("lhb_seats", seats)
    wh.write("lhb_stocks", listed({(c, d.toordinal() - D0.toordinal()) for c, d in seats.select("code", "date").unique().iter_rows()}))
    return wh


def test_a_seat_is_smart_only_once_enough_of_its_buys_have_settled(tmp_path):
    wh = lake_with(tmp_path, history_seats())
    frame = crowd.crowd_features(wh, panel({"H": "甲", "T": "乙", "U": "丙"}))
    assert crowd.FORWARD_BARS == 3 and crowd.SMART_MIN_EVENTS == 5
    # Day 6: events 0..3 have settled (settle day = event day + 3 <= 6): four, not enough.
    assert at(frame, "T", 6)["smart_buyers"] == 0 and at(frame, "T", 6)["lhb_on"]
    # Day 7: event 4 settles too: five events, each about +4% shrunk to +2%: both seats are smart.
    row = at(frame, "U", 7)
    assert row["smart_buyers"] == 2 and row["smart_score"] > 2 * crowd.SMART_MIN_RETURN
    assert at(frame, "U", 6)["lhb_on"] is False and at(frame, "U", 6)["smart_buyers"] == 0  # list known, not on it
    assert at(frame, "U", 0)["lhb_on"] is False


def test_seat_features_ignore_data_after_the_day(tmp_path):
    full = crowd.crowd_features(lake_with(tmp_path / "a", history_seats()), panel({"H": "甲", "T": "乙", "U": "丙"}))
    seats = history_seats().filter(pl.col("date") <= day(7))
    cut = crowd.crowd_features(
        lake_with(tmp_path / "b", seats), panel({"H": "甲", "T": "乙", "U": "丙"}, n=8),
    )
    columns = ["code", "date", "lhb_on", "smart_buyers", "smart_score", "trip_buyers", "inst_net"]
    assert cut.select(columns).equals(full.filter(pl.col("date") <= day(7)).select(columns))


def test_one_day_trip_seats_and_institution_net(tmp_path):
    events = []
    for i in range(5):  # S3 buys a stock and sells it on the next day's list, five times
        events += [(f"X{i}", i, "buy", "S3", 1.0, 0.0), (f"X{i}", i + 1, "sell", "S3", 0.0, 1.0)]
    events += [("V", 9, "buy", "S3", 1.0, 0.0), ("V", 9, "buy", "机构专用", 3.0, 0.0), ("V", 9, "sell", "机构专用", 0.0, 1.0)]
    wh = lake_with(tmp_path, seat_rows(events))
    names = {f"X{i}": f"股{i}" for i in range(5)} | {"V": "乙"}
    frame = crowd.crowd_features(wh, panel(names))
    row = at(frame, "V", 9)
    assert row["trip_buyers"] == 1 and row["smart_buyers"] <= 1
    assert row["inst_net"] == pytest.approx(2.0)  # institutions bought 3, sold 1 (亿元)


def test_missing_sources_leave_every_crowd_column_null(tmp_path):
    wh = Warehouse(tmp_path / "live", "live")
    frame = crowd.crowd_features(wh, panel({"A": "甲", "B": "乙"}))
    for column in ("industry", "ind_limit_ups", "lhb_on", "smart_buyers", "inst_net"):
        assert frame[column].null_count() == frame.height, column
    assert list(frame.columns[-len(crowd.CROWD_COLUMNS):]) == list(crowd.CROWD_COLUMNS)


# --- the crowd playbooks ---------------------------------------------------------------

def crowd_panel(**today: object) -> pl.DataFrame:
    """25 quiet days for code A, then `today` overrides on the last row."""
    base = {
        "pct_chg": 0.5, "limit_ratio": 0.10, "touched_limit": False, "is_limit_up": False, "limit_streak": 0,
        "one_word": False, "volume": 100.0, "vol_ma5": 100.0, "amount": 2.0, "ind_limit_ups": 0, "name_heat": None,
        "lhb_on": False, "smart_buyers": 0, "trip_buyers": 0, "smart_score": 0.0, "inst_net": 0.0,
    }
    rows = [{"code": "A", "date": day(i), **base} for i in range(25)]
    rows[-1].update(today)
    return pl.DataFrame(rows)


@pytest.mark.parametrize(("playbook_id", "fires", "misses"), [
    ("industry_follower", dict(ind_limit_ups=3, pct_chg=6.0, volume=200.0), dict(ind_limit_ups=2, pct_chg=6.0, volume=200.0)),
    ("industry_first_board", dict(ind_limit_ups=4, is_limit_up=True, limit_streak=1, pct_chg=10.0, touched_limit=True),
     dict(ind_limit_ups=4, is_limit_up=True, limit_streak=1, one_word=True, pct_chg=10.0, touched_limit=True)),
    ("name_cluster_follower", dict(name_heat=2, pct_chg=4.0), dict(name_heat=2, pct_chg=4.0, touched_limit=True)),
    ("smart_seat_follow", dict(lhb_on=True, smart_buyers=2, smart_score=5.0), dict(lhb_on=True, smart_buyers=2, smart_score=5.0, trip_buyers=1)),
    ("institution_net_buy", dict(lhb_on=True, inst_net=0.1, pct_chg=5.0), dict(lhb_on=True, inst_net=0.05, pct_chg=5.0)),
])
def test_crowd_playbooks_fire_on_their_rule_and_not_on_a_near_miss(playbook_id, fires, misses):
    from backend.app.playbooks import base

    pb = base.resolve(playbook_id)
    hit = pb.signal(crowd_panel(**fires).lazy(), pb.params()).collect()
    assert hit["date"].to_list() == [day(24)] and hit["score"].min() > 0
    assert pb.signal(crowd_panel(**misses).lazy(), pb.params()).collect().is_empty()


# --- point-in-time and unknown-day regressions ------------------------------------------------

def test_seat_events_settle_on_the_stocks_own_bars_when_it_is_suspended(tmp_path):
    # H trades day 0, is suspended on days 1 and 2, resumes day 3. M keeps the market calendar full.
    # The forward return of a day-0 buy ends on H's third bar after day 0 (day 5), so it may only count from day 5.
    frame = panel({"H": "甲", "M": "乙"}).filter(~((pl.col("code") == "H") & pl.col("date").is_in([day(1), day(2)])))
    wh = lake_with(tmp_path, seat_rows([("H", 0, "buy", "S1", 1.0, 0.0)]))
    history = crowd.seat_history(wh, frame)
    assert history is not None
    settled = history[3]
    assert settled["settle_date"].to_list() == [day(5)]
    assert settled["n"].to_list() == [1]


def lhb_lake(tmp_path, dates: list[int]) -> Warehouse:
    wh = lake_with(tmp_path, history_seats())
    wh.write_doc("meta", "lhb_dates", {"dates": [day(i).isoformat() for i in dates]})
    return wh


def test_days_the_list_was_not_fetched_stay_unknown(tmp_path):
    # Fetched: days 0..9 except 5 (a failed day). Days 8 and 9 were fetched and had no listing of ours;
    # days 10..13 (e.g. today before 18:00) were never fetched.
    wh = lhb_lake(tmp_path, [i for i in range(10) if i != 5])
    frame = crowd.crowd_features(wh, panel({"H": "甲", "T": "乙", "U": "丙"}))
    known = at(frame, "U", 8)
    assert known["lhb_on"] is False and known["smart_buyers"] == 0 and known["trip_buyers"] == 0
    assert known["inst_net"] == 0.0 and known["smart_score"] == 0.0
    for i in (5, 10, 13):
        row = at(frame, "U", i)
        for column in ("lhb_on", "smart_buyers", "smart_score", "trip_buyers", "inst_net"):
            assert row[column] is None, (i, column)
    assert crowd.lhb_known_days(wh) >= {day(i) for i in (0, 4, 6, 7, 8, 9)}
    assert day(5) not in crowd.lhb_known_days(wh) and day(10) not in crowd.lhb_known_days(wh)


def test_lhb_known_days_is_none_without_the_tables(tmp_path):
    assert crowd.lhb_known_days(Warehouse(tmp_path / "live", "live")) is None


def test_institution_net_counts_both_sides_once(tmp_path):
    # One institution is top-5 on both lists (same entry twice); another only sells.
    events = [
        ("V", 9, "buy", "机构专用", 3.0, 1.0), ("V", 9, "sell", "机构专用", 3.0, 1.0),
        ("V", 9, "sell", "机构专用", 0.0, 0.5),
    ]
    frame = crowd.crowd_features(lake_with(tmp_path, seat_rows(events)), panel({"V": "乙"}))
    assert at(frame, "V", 9)["inst_net"] == pytest.approx((3.0 - 1.0) + (0.0 - 0.5))


def test_snapshot_notes_flag_an_unpublished_latest_day():
    from backend.app.playbooks import crowd_view

    assert crowd_view.lhb_notes(None, day(5)) and "同步" in crowd_view.lhb_notes(None, day(5))[0]
    assert crowd_view.lhb_notes({day(5)}, day(5)) == []
    note = crowd_view.lhb_notes({day(4)}, day(5))
    assert len(note) == 1 and "尚未" in note[0]


def test_crowd_snapshot_of_an_empty_warehouse_is_empty_not_an_error(tmp_path):
    from backend.app.playbooks import crowd_view

    snap = crowd_view.snapshot(Warehouse(tmp_path / "live", "live"))
    assert snap.trade_date == "" and snap.industries == [] and snap.lhb == [] and snap.seats == []
    assert snap.notes


# --- float cap must be point-in-time ----------------------------------------------------------

def cap_panel(rows: list[dict]) -> pl.LazyFrame:
    """Adds the panel's total_cap and cap_est (total_cap x close / latest close) like panel.py does."""
    frame = pl.DataFrame(rows).sort(["code", "date"]).with_columns(pl.col("float_cap").alias("total_cap"))
    return frame.with_columns(
        (pl.col("total_cap") * pl.col("close") / pl.col("close").last().over("code")).alias("cap_est"),
    ).lazy()


def test_high_low_switch_ranks_first_boards_by_float_cap_as_of_the_day():
    from backend.app.playbooks import base

    # Day 0: a 5-board dragon X and two first boards. Day 1: X breaks, first boards A, B, C.
    # A's snapshot float cap (10) is the smallest, but its price then fell 10 -> 2, so on day 1 it was
    # 10 * 10 / 2 = 50 (the largest). Ranking by the snapshot would let the future pick A.
    def rows(code, streaks, closes, cap):
        return [{"code": code, "date": day(i), "limit_streak": s, "one_word": False, "float_cap": cap, "close": px}
                for i, (s, px) in enumerate(zip(streaks, closes))]

    flat = [10.0, 10.0, 10.0]
    data = rows("X", [5, 0, 0], flat, 100.0) + rows("P", [1, 0, 0], flat, 100.0) + rows("Q", [1, 0, 0], flat, 100.0)
    data += rows("A", [0, 1, 0], [10.0, 10.0, 2.0], 10.0)
    data += rows("B", [0, 1, 0], flat, 30.0) + rows("C", [0, 1, 0], flat, 40.0)
    pb = base.resolve("high_low_switch")
    signals = pb.signal(cap_panel(data), pb.params()).collect()
    assert sorted(signals["code"].to_list()) == ["B", "C"]  # median of point-in-time {30, 40, 50} is 40
    scores = dict(zip(signals["code"], signals["score"]))
    assert scores["B"] == pytest.approx(1 / 30) and scores["C"] == pytest.approx(1 / 40)


def test_microcap_timed_ranks_by_float_cap_as_of_the_day():
    from backend.app.playbooks import base

    # 40 stocks, 35 days; the signal day is day 27 (2026-02-01, the first panel date of February).
    # C00's snapshot float cap (10) is the smallest, but its close fell 10 -> 2 after the signal day, so on
    # day 27 it was 10 * 10 / 2 = 50, the third smallest of {20, 40, 50, 60, ...}.
    data = []
    for i in range(40):
        for d in range(35):
            close = 2.0 if (i == 0 and d >= 28) else 10.0
            data.append({
                "code": f"C{i:02d}", "date": day(d), "close": close, "amount": 5.0, "ret_20": 0.0, "days_listed": 200,
                "float_cap": 10.0 if i == 0 else 20.0 * i,
            })
    pb = base.resolve("microcap_timed")
    signals = pb.signal(cap_panel(data), pb.params()).collect().filter(pl.col("date") == day(27))
    assert sorted(signals["code"].to_list()) == ["C01", "C02"]
    scores = dict(zip(signals["code"], signals["score"]))
    assert scores["C01"] == pytest.approx(1 / 20) and scores["C02"] == pytest.approx(1 / 40)
