"""The native intraday recorder end to end on mock transports (no network): a native day in the dataset, auction and break, Sina -> Tencent
fallback, both vendors blocked, a holiday, and the seal facts over a native day."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

import httpx
import polars as pl
import pytest

from pulse.adapters.sources.http import RateLimitedClient
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_live_service as B
from pulse.application.intraday_recorder import AllSourcesDown, IntradayRecorder, static_pool

from . import support as T

pytestmark = pytest.mark.pg
CODES = ["000001", "600000"]


def start(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 12, h, m, s, tzinfo=T.CST).astimezone(T.UTC)


def make(conn, lake, legacy_root, world, *, fallback=True, **kw):
    """Production defaults: Tencent primary, Sina fallback (pass source="sina" for the other way round)."""
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root), clock=world.now)
    svc.bootstrap()

    def client():
        return RateLimitedClient(min_interval_seconds=1.0, transport=httpx.MockTransport(world.handler), clock=world.monotonic, sleep=world.sleep,
                                 now=world.now)

    primary, second = client(), (client() if fallback else None)
    return svc, primary, second, IntradayRecorder(svc, primary, fallback_client=second, sleep=world.sleep, **kw)


def sina_primary(conn, lake, legacy_root, world, **kw):
    return make(conn, lake, legacy_root, world, source="sina", **kw)


def day_frame(svc, lake, day=T.DAY):
    man = svc.latest_ready(B.DATASET_QUOTES, day)
    return man, (None if man is None else lake.read_snapshot_frames(man["content_hash"]))


def cst(frame: pl.DataFrame) -> list[time]:
    return [t.astimezone(T.CST).time() for t in frame["poll_at"].unique().sort().to_list()]


def test_native_day_same_dataset_columns_lineage_and_seal_facts(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    svc, primary, _, rec = make(conn, lake, legacy_root, world)
    s = rec.run(CODES, T.DAY, time(10, 0, 30))
    assert s.counts["polls_full"] == 3 and s.counts.get("step_errors", 0) == 0 and s.counts["flushes"] == 2     # 09:58 / 09:59 / 10:00 and two parts
    man, df = day_frame(svc, lake)
    cov = man["coverage_document"]
    assert df.schema == pl.Schema(B.LR_SNAPSHOT_SCHEMA) and df.columns == list(B.LR_SNAPSHOT_SCHEMA)       # what the legacy import produces
    assert man["row_count"] == df.height == s.counts["rows_published"] and len(cov["parts"]) >= 2
    assert cov["recorder"] == "pulse_intraday_recorder" and cov["source_complete"] and cov["vendors"] == ["tencent"] and cov["primary"] == "tencent" and cov["not_tick_data"]
    assert {p["vendor"] for p in cov["parts"].values()} == {"tencent"} and {p["stream"] for p in cov["parts"].values()} == {"quotes", "hot"}
    assert set(df["kind"].unique()) == {"full", "hot"} and set(df["source_stream"].unique()) == {"quotes", "hot"}
    a = df.filter((df["code"] == "000001") & (df["kind"] == "full")).sort("quote_at").row(0, named=True)
    assert a["price"] == a["limit_up"] == a["bid1_px"] and a["bid1_lots"] == 5000.0 and a["ask1_px"] is None and a["ask1_lots"] == 0.0
    assert a["bid2_lots"] == 100.0 and a["quote_at"] == start(9, 58, 2) and a["poll_at"] == start(9, 58, 2) and a["source_part"].startswith("quotes/native-tencent-")
    assert a["outer_lots"] == 600.0 and a["inner_lots"] == 400.0 and a["turnover"] == Decimal("1.5000") and a["float_cap"] == Decimal("100.0000")
    # the hot subset: the sealed name is polled every 10 s, the ordinary one only with the full boards
    assert df.filter((df["kind"] == "hot") & (df["code"] == "600000")).height == 0 and df.filter(df["kind"] == "hot")["code"].n_unique() == 1
    assert df.filter(df["kind"] == "hot").height >= 8
    # lineage names the native source; the vendor bodies stay raw evidence under their own id
    sources = {r["source_id"] for r in conn.execute("SELECT source_id FROM ingest.batch").fetchall()}
    assert sources == {"pulse_intraday_recorder"}
    assert conn.execute("SELECT count(*) n FROM ingest.raw_object WHERE source_id='tencent'").fetchone()["n"] == primary.requests_made
    assert world.count("hq.sinajs.cn") == 0 and min(world.gaps("qt.gtimg.cn")) >= 1.0 - 1e-9        # never faster than 1 request/s, fallback unused
    # the seal facts read the native day exactly like an imported one
    f = svc.publish_seal_facts(T.DAY)
    assert f.counts["sealed_ever_count"] == 1 and f.counts["instruments_observed"] == 2 and f.counts["touched_limit_count"] == 1
    sf = svc.seal_clock(T.DAY)
    assert sf is not None and sf["estimation"]
    assert static_pool(svc, T.DAY + timedelta(days=1)) == {"000001"}                                  # tomorrow's static pool: names that touched a limit


def test_sina_as_primary_leaves_the_tencent_only_columns_null(conn, lake, legacy_root):
    """--source sina is still possible: the columns Sina does not carry are NULL (never 0 or invented) and the limits come from the rules."""
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    svc, primary, _, rec = sina_primary(conn, lake, legacy_root, world)
    rec.run(CODES, T.DAY, time(10, 0, 30))
    man, df = day_frame(svc, lake)
    assert man["coverage_document"]["vendors"] == ["sina"] and man["coverage_document"]["primary"] == "sina" and world.count("qt.gtimg.cn") == 0
    a = df.filter((df["code"] == "000001") & (df["kind"] == "full")).sort("quote_at").row(0, named=True)
    assert a["price"] == a["limit_up"] == a["bid1_px"] and a["bid1_lots"] == 5000.0 and a["source_part"].startswith("quotes/native-sina-")
    assert a["outer_lots"] is None and a["inner_lots"] is None and a["turnover"] is None and a["float_cap"] is None and a["vwap"] is None
    assert svc.publish_seal_facts(T.DAY).counts["sealed_ever_count"] == 1


def test_auction_phases_and_the_pause_before_the_open(conn, lake, legacy_root):
    world = T.World(start(9, 24), [T.sealed(), T.plain()])
    svc, _, _, rec = make(conn, lake, legacy_root, world, static={"000001"})
    s = rec.run(CODES, T.DAY, time(9, 31))
    man, df = day_frame(svc, lake)
    assert {"auction", "auction_hot", "auction_final", "full", "hot"} <= set(df["kind"].unique())
    assert s.counts["polls_auction"] == 1 and s.counts["polls_auction_final"] == 1
    final = df.filter(df["kind"] == "auction_final")
    assert final.height == 2 and final["poll_at"].min() >= start(9, 25, 10)
    dense = df.filter(df["kind"] == "auction_hot")
    assert set(dense["code"].unique()) == {"000001"} and dense["poll_at"].n_unique() >= 10         # the static pool every 5 s from 09:24
    assert all(t < time(9, 25, 11) for t in cst(dense))
    polled = cst(df)
    assert not any(time(9, 25, 11) < t < time(9, 30) for t in polled)                              # nothing between the auction result and the open
    assert polled[0] >= time(9, 24) and polled[-1] <= time(9, 31)


def test_nothing_is_polled_in_the_midday_break(conn, lake, legacy_root):
    world = T.World(start(11, 29), [T.plain()])
    svc, _, _, rec = make(conn, lake, legacy_root, world, tick=5.0)
    rec.run(["600000"], T.DAY, time(13, 1))
    _, df = day_frame(svc, lake)
    polled = cst(df)
    assert any(t < time(11, 31) for t in polled) and any(t >= time(13, 0) for t in polled)
    assert not any(time(11, 30, 31) < t < time(13, 0) for t in polled)


def test_primary_block_switches_to_sina_for_the_rest_of_the_session(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    world.block["qt.gtimg.cn"] = 4                          # full 09:58:02 = requests 1-2 (chunk 1), hot 09:58:10 = 3, hot 09:58:20 = 4 refused
    svc, primary, second, rec = make(conn, lake, legacy_root, world, chunk=1)
    s = rec.run(CODES, T.DAY, time(10, 0, 30))
    assert world.count("qt.gtimg.cn") == 4 and primary.circuit_open         # the refused host is never asked again
    assert world.count("hq.sinajs.cn") > 0 and not second.circuit_open and min(world.gaps("hq.sinajs.cn")) >= 1.0 - 1e-9
    assert s.counts["source_switches"] == 1 and any("switched to sina" in n for n in s.notes)
    man, df = day_frame(svc, lake)
    parts = man["coverage_document"]["parts"]
    assert {p["vendor"] for p in parts.values()} == {"sina", "tencent"} and man["coverage_document"]["vendors"] == ["sina", "tencent"]
    assert df.schema == pl.Schema(B.LR_SNAPSHOT_SCHEMA)
    tencent_rows = df.filter(df["source_part"].str.contains("native-tencent"))
    sina_rows = df.filter(df["source_part"].str.contains("native-sina"))
    assert tencent_rows.height > 0 and sina_rows.height > 0 and tencent_rows.height + sina_rows.height == df.height
    assert tencent_rows["turnover"].null_count() == 0 and tencent_rows["outer_lots"].null_count() == 0 and sina_rows["turnover"].null_count() == sina_rows.height
    # the batch that was refused was asked again from Sina at once: the first Sina row is the hot poll of the 09:58:20 slot
    first = sina_rows.sort("poll_at").row(0, named=True)
    assert start(9, 58, 20) <= first["poll_at"] < start(9, 58, 30) and first["kind"] == "hot"
    h = {r["source_id"]: r["runtime_state"] for r in conn.execute("SELECT source_id, runtime_state FROM ingest.source_health").fetchall()}
    assert h["tencent"] == "unavailable" and h.get("sina") != "unavailable"
    f = svc.publish_seal_facts(T.DAY)                       # facts over a day of two vendors
    assert f.counts["sealed_ever_count"] == 1 and f.counts["instruments_observed"] == 2


def test_repeated_transport_failures_switch_without_hammering(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    world.fail["qt.gtimg.cn"] = 3                            # from the third request on Tencent answers 500
    svc, primary, _, rec = make(conn, lake, legacy_root, world, chunk=1, max_fails=3)
    s = rec.run(CODES, T.DAY, time(10, 0, 30))
    assert world.count("qt.gtimg.cn") == 5 and s.counts["transport_errors"] == 3 and s.counts["source_switches"] == 1
    assert not primary.circuit_open                           # a 5xx is not a block; the host is simply out for the session
    man, df = day_frame(svc, lake)
    assert {p["vendor"] for p in man["coverage_document"]["parts"].values()} == {"sina", "tencent"}
    assert df.filter(df["source_part"].str.contains("native-sina")).height > 0


def test_both_vendors_blocked_stops_reports_and_keeps_what_was_captured(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    world.block["qt.gtimg.cn"], world.block["hq.sinajs.cn"] = 3, 1
    svc, primary, second, rec = make(conn, lake, legacy_root, world, chunk=1)
    with pytest.raises(AllSourcesDown) as info:
        rec.run(CODES, T.DAY, time(10, 0, 30))
    assert (world.count("qt.gtimg.cn"), world.count("hq.sinajs.cn")) == (3, 1)          # one refusal per host, then nothing
    assert primary.circuit_open and second.circuit_open
    msg = str(info.value)
    assert "sina: blocked" in msg and "tencent: blocked" in msg
    summary = info.value.summary
    assert summary is not None and summary.counts["source_switches"] == 1 and any("stopped:" in n for n in summary.notes)
    assert world.t < start(9, 59)                             # it stopped at once, it did not wait for until
    man, df = day_frame(svc, lake)
    assert man is not None and df.height == 2                 # the full board before the block is kept
    h = {r["source_id"]: r["runtime_state"] for r in conn.execute("SELECT source_id, runtime_state FROM ingest.source_health").fetchall()}
    assert h["sina"] == "unavailable" and h["tencent"] == "unavailable"
    assert man["coverage_document"]["source_complete"] is False


def test_single_vendor_block_without_a_fallback_stops(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    world.block["qt.gtimg.cn"] = 2
    svc, primary, _, rec = make(conn, lake, legacy_root, world, fallback=False, chunk=1)
    with pytest.raises(AllSourcesDown):
        rec.run(CODES, T.DAY, time(10, 0, 30))
    assert world.count("qt.gtimg.cn") == 2 and world.count("hq.sinajs.cn") == 0 and primary.circuit_open
    assert day_frame(svc, lake)[1].height == 1


def test_a_holiday_publishes_nothing(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.sealed(), T.plain()])
    world.stale = True                                        # the vendor repeats the last session: every quote is stamped 11-11
    svc, _, _, rec = make(conn, lake, legacy_root, world, chunk=1)
    s = rec.run(CODES, T.DAY, time(10, 0, 30))
    assert world.count("qt.gtimg.cn") == 2                     # the first board was enough to know; the run stopped at once
    assert any("not a trading day" in n for n in s.notes) and svc.latest_ready(B.DATASET_QUOTES, T.DAY) is None
    assert conn.execute("SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s", (B.DATASET_QUOTES,)).fetchone()["n"] == 0


def test_the_run_ends_at_until(conn, lake, legacy_root):
    world = T.World(start(9, 58), [T.plain()])
    svc, _, _, rec = make(conn, lake, legacy_root, world)
    rec.run(["600000"], T.DAY, time(9, 59, 5))
    _, df = day_frame(svc, lake)
    assert max(cst(df)) <= time(9, 59, 5) and world.t.astimezone(T.CST).time() <= time(9, 59, 7)


def test_after_hours_fields_reach_the_dataset(conn, lake, legacy_root):
    st = T.plain()
    st.after_lots, st.after_wan = 80, 9.368                  # Tencent fields 59 / 58 (the Sina trailer is covered in test_rows)
    world = T.World(start(15, 5), [st])
    svc, _, _, rec = make(conn, lake, legacy_root, world, static={"600000"})
    rec.run(["600000"], T.DAY, time(15, 6, 20))
    # a poll at 15:05 is stamped today: the first board after the auction is the 15:05:05 one-shot, which also verifies the day
    _, df = day_frame(svc, lake)
    assert {"after", "after_hot"} <= set(df["kind"].unique())
    row = df.filter(df["kind"] == "after").row(0, named=True)
    assert row["after_lots"] == 80.0 and str(row["after_amount_wan"]) == "9.3680"


def test_a_failed_flush_keeps_the_rows_for_the_next_one(conn, lake, legacy_root, monkeypatch):
    world = T.World(start(9, 58), [T.plain()])
    svc, _, _, rec = make(conn, lake, legacy_root, world)
    real = type(svc.lake).put_parquet
    calls = {"n": 0}

    def flaky(self, df, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real(self, df, **kw)

    monkeypatch.setattr(type(svc.lake), "put_parquet", flaky)
    s = rec.run(["600000"], T.DAY, time(10, 0, 30), flush_every=1)
    assert s.counts["flush_errors"] == 1 and s.counts["flushes"] >= 1
    _, df = day_frame(svc, lake)
    assert df.filter(df["kind"] == "full")["poll_at"].n_unique() == 3          # the board lost with the failed flush was republished later


# --- review fixes ----------------------------------------------------------------------------------------------------------------------------

def test_poll_at_is_the_response_time_of_each_batch(conn, lake, legacy_root):
    """At 1 request/s the batches of one board are known seconds apart: every row carries ITS batch's response time, not the board's start."""
    stocks = [T.plain(f"60000{i}", f"股票{i}") for i in range(6)]
    world = T.World(start(9, 58), stocks)
    svc, _, _, rec = make(conn, lake, legacy_root, world, chunk=1)
    rec.run([s.code for s in stocks], T.DAY, time(9, 58, 30))
    _, df = day_frame(svc, lake)
    board = df.filter((df["kind"] == "full") & (df["poll_at"] < start(9, 59))).sort("poll_at")
    assert board.height == 6
    asked = {sym[2:]: t for t, host, syms in world.log if host == "qt.gtimg.cn" and t < start(9, 59) for sym in syms}
    for row in board.iter_rows(named=True):
        assert row["poll_at"] == asked[row["code"]]                        # the instant its request was answered (mock: no latency)
        assert row["poll_at"] >= row["quote_at"]
    stamps = board["poll_at"].to_list()
    assert all((b - a).total_seconds() >= 1.0 - 1e-9 for a, b in zip(stamps, stamps[1:]))          # the pacing shows in the stamps
    assert (stamps[-1] - stamps[0]).total_seconds() >= 5.0                  # the last batch is known 5 s after the first, not at the same time


def test_auction_rows_are_kept_when_every_source_is_blocked_after_them(conn, lake, legacy_root):
    world = T.World(start(9, 20), [T.sealed(), T.plain()])
    world.block["qt.gtimg.cn"] = 3                          # the static pool poll and the board of 09:20 are answered (stamped today), the third request is refused
    svc, _, _, rec = make(conn, lake, legacy_root, world, chunk=1, fallback=False, static={"000001"})
    with pytest.raises(AllSourcesDown):
        rec.run(CODES, T.DAY, time(9, 24))
    man, df = day_frame(svc, lake)
    kinds = set(df["kind"].unique())
    assert man is not None and "auction" in kinds and kinds <= {"auction", "auction_hot"} and df.height >= 2     # what came before the block
    assert man["coverage_document"]["source_complete"] is False


def test_pre_open_quotes_of_the_previous_day_neither_stop_nor_publish(conn, lake, legacy_root):
    world = T.World(start(9, 20), [T.plain()])
    world.stale = True                                      # before the open the vendors still show yesterday: not yet a holiday
    svc, _, _, rec = make(conn, lake, legacy_root, world)
    s = rec.run(["600000"], T.DAY, time(9, 22))
    assert s.counts["polls_auction"] >= 1 and not any("not a trading day" in n for n in s.notes)
    assert svc.latest_ready(B.DATASET_QUOTES, T.DAY) is None


def test_a_switched_vendor_is_never_asked_for_more_than_its_cap(conn, lake, legacy_root):
    stocks = [T.plain(f"600{i:03d}", f"股票{i}") for i in range(250)]
    world = T.World(start(9, 58), stocks)
    world.block["hq.sinajs.cn"] = 1                         # Sina (batch 200) is refused on its first request; Tencent (batch 80) takes over
    svc, _, _, rec = sina_primary(conn, lake, legacy_root, world)
    s = rec.run([s.code for s in stocks], T.DAY, time(9, 58, 30))
    asked = [len(syms) for _, host, syms in world.log if host == "qt.gtimg.cn"]
    assert [len(syms) for _, host, syms in world.log if host == "hq.sinajs.cn"] == [200]
    assert asked[:4] == [80, 80, 80, 10] and max(asked) <= 80           # the 250 symbols were re-sliced for Tencent, nothing above its cap
    _, df = day_frame(svc, lake)
    assert df.filter((df["kind"] == "full") & (df["poll_at"] < start(9, 59)))["code"].n_unique() == 250 and s.counts["source_switches"] == 1
