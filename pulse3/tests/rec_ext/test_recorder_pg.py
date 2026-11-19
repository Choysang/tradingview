"""Native ext recorder on real PostgreSQL + a mock network: cadence, per-host pacing, block handling, restarts, publish and the read back through
the readers of the downstream lanes (fwdlog inputs). Fake clock throughout; no request leaves the machine."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx
import polars as pl
import pytest

from pulse.adapters.sources import ext_quotes as X
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_ext_record as R
from pulse.application import bf_fwdlog_inputs as IN
from pulse.application import bf_live_service as B

from .conftest import FIXTURES, World

pytestmark = pytest.mark.pg
UTC = timezone.utc
DAY = date(2026, 11, 6)


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Net:
    """Mock of the three hosts. ``log`` holds (mono time, host, url) of every request; ``override`` maps host -> callable(request, n) | None."""

    def __init__(self, world: World):
        self.world, self.log, self.override = world, [], {}
        self.em_calls: dict[str, int] = {}

    def count(self, host: str) -> int:
        return sum(1 for e in self.log if e[1] == host)

    def handler(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        self.log.append((self.world.mono, host, str(req.url)))
        hook = self.override.get(host)
        if hook is not None:
            answer = hook(req, self.count(host))
            if answer is not None:
                return answer
        if host == X.QUOTE_HOST:
            return httpx.Response(200, content=fx("tencent_etf_index.txt") + b"\n" + fx("tencent_overseas.txt"))
        if host == X.CFFEX_HOST:
            head, body = fx(f"cffex_{str(req.url)[-6:-4]}.txt").split(b"\n", 1)
            return httpx.Response(200, content=body, headers={"last-modified": head.decode().split(": ", 1)[1]})
        secid = str(req.url).rsplit("secid=", 1)[1]
        n = self.em_calls[secid] = self.em_calls.get(secid, 0) + 1
        doc = json.loads(fx("eastmoney_trends_CN00Y.txt" if secid.endswith("CN00Y") else "eastmoney_trends_USDCNH.txt"))
        if n > 1 and secid.endswith("CN00Y"):                                 # the later fetch carries the corrected last value of the first bar
            first = doc["data"]["trends"][0].split(",")
            first[2] = "12760.0"
            doc["data"]["trends"][0] = ",".join(first)
        return httpx.Response(200, content=json.dumps(doc).encode("utf-8"))


def setup(conn, lake, legacy_root, start_cst: datetime, cadence: R.Cadence | None = None, **kw):
    world = World(start_cst)
    net = Net(world)
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root), clock=world.now)
    svc.bootstrap()
    fetcher = X.ExtFetcher(transport=httpx.MockTransport(net.handler), clock=world.monotonic, sleep=world.sleep, now=world.now)
    rec = R.ExtRecorder(svc, fetcher, cadence=cadence, sleep=world.sleep, **kw)
    return world, net, svc, fetcher, rec


def frame_of(svc, day=DAY):
    man = svc.latest_ready(B.DATASET_EXT, day)
    return man, svc.lake.read_snapshot_frames(man["content_hash"])


def at(h, m, s=0):
    return datetime(2026, 11, 6, h, m, s)


def test_morning_cycles_publish_one_dataset_with_native_lineage_and_polite_pacing(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 31, 0))
    s = rec.run(DAY, time(9, 33, 30))
    assert s.counts["polls_grid"] == 3 and s.counts["http_requests"] == 15 and s.counts["rows_polled"] == 3 * (20 + 16)
    assert net.count(X.QUOTE_HOST) == 3 and net.count(X.CFFEX_HOST) == 12 and net.count(X.EM_HOST) == 0    # the minute bars are not part of the grid
    man, df = frame_of(svc)
    assert man["manifest_state"] == "READY" and man["row_count"] == df.height == 108 and man["available_watermark"] == world.t
    cov = man["coverage_document"]
    assert cov["recorder"] == "pulse_native_ext" and cov["session_date"] == "2026-11-06" and list(cov["parts"]) == [p for p in cov["parts"]] and len(cov["parts"]) == 1
    assert cov["cadence_seconds"]["cffex"] == 60 and cov["end_reason"] == "stop time" and cov["source_complete"] is False
    assert cov["capability"] == "sampled_snapshots_estimated" and "units" in cov
    assert set(df["kind"]) == {"grid"} and df["session_date"].unique().to_list() == [DAY]
    # at most one request per second per host, polls five seconds into the minute
    for host in (X.QUOTE_HOST, X.CFFEX_HOST):
        t = [e[0] for e in net.log if e[1] == host]
        assert all(b - a >= 1.0 - 1e-9 for a, b in zip(t, t[1:]))
    firsts = df.group_by("kind").agg(pl.col("poll_at").min().alias("first"))["first"][0]
    assert firsts == datetime(2026, 11, 6, 1, 31, 5, tzinfo=UTC)                                           # 09:31:05 CST
    # lineage: every raw answer is stored under the native source ids, nothing under the legacy recorder
    raws = conn.execute("SELECT source_id, count(*) n FROM ingest.raw_object GROUP BY source_id ORDER BY 1").fetchall()
    assert {r["source_id"]: r["n"] for r in raws} == {"native_ext_cffex": 12, "native_ext_tencent": 3}
    assert conn.execute("SELECT count(*) n FROM ingest.batch WHERE source_id LIKE 'native_ext_%%'").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) n FROM ingest.batch WHERE source_id='legacy_recordings'").fetchone()["n"] == 0
    health = {(h["source_id"], h["runtime_state"]) for h in conn.execute("SELECT source_id, runtime_state FROM ingest.source_health").fetchall()}
    assert health == {("native_ext_tencent", "healthy"), ("native_ext_cffex", "healthy")}


def test_the_cffex_cadence_is_a_parameter_default_is_the_old_one(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 31, 0), R.Cadence(cffex_seconds=300))
    s = rec.run(DAY, time(9, 37, 30))
    assert s.counts["polls_grid"] == 7 and net.count(X.QUOTE_HOST) == 7
    assert net.count(X.CFFEX_HOST) == 2 * 4                                                              # 09:31 and 09:36 only
    _, df = frame_of(svc)
    cffex = df.filter(pl.col("host") == X.CFFEX_HOST)
    assert cffex["poll_at"].n_unique() == 2 and df.filter(pl.col("host") == X.QUOTE_HOST)["poll_at"].n_unique() == 7


def test_the_whole_afternoon_close_end_and_one_merged_part_where_the_later_bar_wins(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(15, 0, 30))
    s = rec.run(DAY)
    assert s.counts["polls_grid"] == 61 and s.counts["polls_close"] == 1 and s.counts["polls_end"] == 1
    assert net.count(X.EM_HOST) == 4                                                                      # close: 2 series, end: 2 series
    assert s.counts["flushes"] >= 4                                                                       # a part every 15 minutes, then the merge
    versions = conn.execute("SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s AND coverage_document->>'session_date'='2026-11-06'",
                            (B.DATASET_EXT,)).fetchone()["n"]
    assert versions == s.counts["flushes"]
    man, df = frame_of(svc)
    cov = man["coverage_document"]
    assert list(cov["parts"]) == ["native/quotes_ext.parquet"] and cov["source_complete"] is True and cov["end_reason"] == "done"
    assert cov["steps_done"] == ["close", "end"]
    assert df.select(pl.struct(X.SORT_KEYS).n_unique()).item() == df.height                               # the old compact: unique on symbol, poll, kind
    assert df["source_part"].unique().to_list() == ["native/quotes_ext.parquet"]
    bars = df.filter(pl.col("kind") == "minute")
    n_a50 = len(X.parse_minutes(fx("eastmoney_trends_CN00Y.txt").decode(), "close", at(15, 1)))
    n_cnh = len(X.parse_minutes(fx("eastmoney_trends_USDCNH.txt").decode(), "close", at(15, 1)))
    assert bars.height == n_a50 + n_cnh                                                                    # two fetches, one series
    first = bars.filter(pl.col("symbol") == "em_CN00Y").sort("poll_at").row(0, named=True)
    assert str(first["price"]) == "12760.000000"                                                           # the later fetch won
    assert df.height == 61 * 36 + 36 + bars.height and set(df["kind"]) == {"grid", "close", "minute"}
    assert df.sort(X.SORT_KEYS).equals(df)                                                                 # sorted as the old merged file


def test_downstream_readers_read_the_native_dataset_unchanged(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 59, 50))
    rec.run(DAY, time(10, 2))
    got = IN.day_frames(svc, B.DATASET_EXT, DAY, None, code_col="symbol", main_only=False)
    assert got is not None and got.manifest["coverage_document"]["recorder"] == "pulse_native_ext"
    rows = IN.ext_rows(got.frame)
    assert len(rows) == got.frame.height and {r.symbol for r in rows} >= {"sh000300", "sh510300", "IF2612", "hf_GC"}
    ten, _, _ = IN.index_quotes(rows, "sh000300", DAY)
    assert "sh000300" in ten and ten["sh000300"].price is not None                                        # the E078 reader finds its 10:00 quote
    etf = next(r for r in rows if r.symbol == "sh510300")
    assert float(etf.amount) == 5211672667.0 and etf.volume is not None


def test_published_frame_equals_what_the_legacy_import_publishes_for_the_same_rows(conn, lake, legacy_root):
    rows = (X.parse_tencent((fx("tencent_etf_index.txt") + fx("tencent_overseas.txt")).decode("gbk"), "grid", at(10, 0, 5))
            + X.parse_cffex(fx("cffex_IF.txt").decode(), "grid", at(10, 0, 5)))
    d = legacy_root / "intraday_ext" / DAY.isoformat()
    d.mkdir(parents=True)
    pl.DataFrame(rows, schema=X.ROW_SCHEMA).write_parquet(d / "quotes_ext.parquet")
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(10, 0))
    svc.import_ext(DAY)
    legacy_man, legacy_df = frame_of(svc)
    assert legacy_man["coverage_document"]["recorder"] == "legacy_recordings"
    # a native session of another day with the same rows through the real recorder path
    other = date(2026, 11, 9)
    rec.day, rec.confirmed = other, True
    rec.buffer = [{**r, "poll_ts": datetime(2026, 11, 9, 10, 0, 5)} for r in rows]
    rec.flush(other, B.Summary("t"))
    native_man, native_df = frame_of(svc, other)
    assert native_man["content_hash"] != legacy_man["content_hash"]
    assert native_df.schema == legacy_df.schema and native_df.columns == legacy_df.columns
    assert native_man["contract_id"] == legacy_man["contract_id"] == "pulse.bf_live.ext_quote_snapshot"
    assert native_df.drop("source_part", "session_date", "poll_at").equals(legacy_df.drop("source_part", "session_date", "poll_at"))


def test_a_refusal_closes_that_host_only_and_health_goes_open(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 31, 0))
    net.override[X.CFFEX_HOST] = lambda req, n: httpx.Response(403, content=b"denied") if n == 2 else None      # the second file of the first poll
    s = rec.run(DAY, time(9, 33, 30))
    assert net.count(X.CFFEX_HOST) == 2 and net.count(X.QUOTE_HOST) == 3                   # IC, IM and later polls were never sent to that host
    assert s.counts["hosts_blocked"] == 1 and any(n.startswith("blocked www.cffex.com.cn") for n in s.notes)
    h = {(r["source_id"], r["endpoint_id"]): r for r in conn.execute("SELECT * FROM ingest.source_health").fetchall()}
    assert h[("native_ext_cffex", "quote_file")]["runtime_state"] == "unavailable" and h[("native_ext_cffex", "quote_file")]["last_error_code"].startswith("blocked")
    assert h[("native_ext_tencent", "quote")]["runtime_state"] == "healthy"
    _, df = frame_of(svc)
    assert df.filter(pl.col("host") == X.CFFEX_HOST).height == 4 and df.filter(pl.col("host") == X.QUOTE_HOST).height == 60   # what came before the refusal is kept


@pytest.mark.parametrize("answer", [
    httpx.Response(302, headers={"location": "https://waf.tencent.com/x"}, content=b""),
    httpx.Response(200, content=b"<html>please verify</html>"),
])
def test_a_waf_page_or_redirect_is_kept_as_evidence_and_closes_the_host(conn, lake, legacy_root, answer):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 31, 0))
    net.override[X.QUOTE_HOST] = lambda req, n: answer if n == 2 else None
    rec.run(DAY, time(9, 33, 30))
    assert net.count(X.QUOTE_HOST) == 2 and net.count(X.CFFEX_HOST) == 12
    assert conn.execute("SELECT count(*) n FROM ingest.raw_object WHERE source_id='native_ext_tencent'").fetchone()["n"] == 2   # 1 good + the refusal page


def test_twenty_failed_polls_in_a_row_close_a_host_and_a_success_resets_the_count(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 31, 0))
    net.override[X.QUOTE_HOST] = lambda req, n: httpx.Response(502, content=b"bad gateway") if n != 10 else None
    s = rec.run(DAY, time(10, 11))
    assert net.count(X.QUOTE_HOST) == 30                                  # 9 failures, one success (resets the count), then 20 failures close the host
    assert s.counts["hosts_blocked"] == 1 and "failures in a row" in " ".join(s.notes)
    assert net.count(X.CFFEX_HOST) == 4 * 40                              # the other host kept its cadence to the stop time


def test_a_restart_continues_the_day_keeps_a_blocked_host_closed_and_merges_into_one_part(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(15, 0, 50))
    net.override[X.CFFEX_HOST] = lambda req, n: httpx.Response(403, content=b"") if n == 3 else None
    rec.run(DAY, time(15, 2))
    man1, df1 = frame_of(svc)
    assert man1["coverage_document"]["steps_done"] == ["close"] and net.count(X.EM_HOST) == 2
    # second process, 10 minutes later, same day
    world2, net2, svc2, fetcher2, rec2 = setup(conn, lake, legacy_root, at(15, 2, 10))
    s2 = rec2.run(DAY, time(15, 3, 30))
    assert any("restarted" in n for n in s2.notes) and net2.count(X.EM_HOST) == 0                          # the close step is not repeated
    assert net2.count(X.CFFEX_HOST) == 0 and s2.counts["hosts_blocked"] == 1                              # the block of the earlier process stands
    man2, df2 = frame_of(svc2)
    assert list(man2["coverage_document"]["parts"]) == ["native/quotes_ext.parquet"] and df2.height > df1.height
    assert df2.select(pl.struct(X.SORT_KEYS).n_unique()).item() == df2.height


def test_not_a_trading_day_weekend_and_stale_stamps(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, datetime(2026, 11, 8, 9, 0))
    s = rec.run(date(2026, 11, 8), time(9, 10))
    assert net.log == [] and "not a trading day" in s.notes[0] and svc.latest_ready(B.DATASET_EXT, date(2026, 11, 8)) is None
    # an unlisted holiday: the index quotes carry yesterday's stamp
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 30, 50))
    net.override[X.QUOTE_HOST] = lambda req, n: httpx.Response(200, content=(fx("tencent_etf_index.txt") + fx("tencent_overseas.txt")).replace(b"20261106", b"20261105"))
    s = rec.run(DAY, time(9, 50))
    assert any("not a trading day" in n for n in s.notes) and s.counts["polls_grid"] == 6 and net.count(X.QUOTE_HOST) == 6      # 09:31 .. 09:35 verdicts, 09:30 first
    assert svc.latest_ready(B.DATASET_EXT, DAY) is None and s.counts["rows_dropped_unconfirmed"] > 0       # the stale rows are dropped, never published


def test_a_failed_publish_keeps_the_rows_and_the_next_flush_retries(conn, lake, legacy_root, monkeypatch):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(10, 0))
    rec.day, rec.confirmed = DAY, True
    rec.poll("grid", at(10, 0, 5), rec.plan(DAY))
    n = len(rec.buffer)
    assert n == 36
    real = svc.data._stage_and_publish
    monkeypatch.setattr(svc.data, "_stage_and_publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        rec.flush(DAY, B.Summary("t"))
    assert len(rec.buffer) == n and svc.latest_ready(B.DATASET_EXT, DAY) is None
    assert conn.execute("SELECT count(*) n FROM ingest.batch WHERE batch_state='FAILED'").fetchone()["n"] == 1
    monkeypatch.setattr(svc.data, "_stage_and_publish", real)
    s = B.Summary("t")
    rec.flush(DAY, s)
    assert s.counts["rows_published"] == n and rec.buffer == [] and svc.latest_ready(B.DATASET_EXT, DAY)["row_count"] == n


def test_a_full_disk_stops_before_any_request(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(8, 59, 50), min_free_gb=1e12)
    s = rec.run(DAY, time(9, 3))
    assert net.log == [] and any("end: disk low" in n for n in s.notes) and svc.latest_ready(B.DATASET_EXT, DAY) is None


def test_once_dry_prints_rows_and_publishes_nothing_once_publishes_kind_once(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(12, 0))
    s, rows = rec.once(DAY, publish=False)
    assert len(net.log) == 7 and {r["kind"] for r in rows} == {"once", "minute"} and svc.latest_ready(B.DATASET_EXT, DAY) is None
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(12, 0))
    s, rows = rec.once(DAY)
    man, df = frame_of(svc)
    assert man["coverage_document"]["end_reason"] == "once" and set(df["kind"]) == {"once", "minute"} and df.height == len(rows)


# --- a weekday holiday must never leak stale quotes into the dataset -----------------------------------------------------------------------
def _stale(net):
    net.override[X.QUOTE_HOST] = lambda req, n: httpx.Response(200, content=(fx("tencent_etf_index.txt") + fx("tencent_overseas.txt")).replace(b"20261106", b"20261105"))


def test_a_weekday_holiday_publishes_nothing_not_even_the_early_flush(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(8, 59, 50))
    _stale(net)                                                   # the feed repeats yesterday's stamps; the weekday check cannot know the holiday
    s = rec.run(DAY, time(10, 0))
    assert s.counts["polls_grid"] > 30 and any("not a trading day" in n for n in s.notes)       # 900 s flush interval passed long before the 5th stale verdict
    assert svc.latest_ready(B.DATASET_EXT, DAY) is None and rec.buffer == [] and s.counts["rows_dropped_unconfirmed"] > 0
    assert conn.execute("SELECT count(*) n FROM market.dataset_manifest WHERE dataset_id=%s", (B.DATASET_EXT,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) n FROM ingest.batch WHERE source_id LIKE 'native_ext_%%'").fetchone()["n"] == 0


def test_nothing_is_published_before_the_day_is_confirmed_and_the_early_rows_follow_once_it_is(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(8, 59, 50), R.Cadence(flush_seconds=300.0))
    s = rec.run(DAY, time(9, 29, 59))                              # ends before any index quote can confirm the day
    assert svc.latest_ready(B.DATASET_EXT, DAY) is None and s.counts["polls_grid"] > 25 and "rows_published" not in s.counts
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(9, 20, 0), R.Cadence(flush_seconds=300.0))
    s = rec.run(DAY, time(9, 33, 30))
    man, df = frame_of(svc)
    assert man["coverage_document"]["recorder"] == "pulse_native_ext" and df["poll_at"].min() == datetime(2026, 11, 6, 1, 20, 5, tzinfo=UTC)   # the 09:20 rows are kept


def test_a_calendar_closure_ends_the_run_before_any_request(conn, lake, legacy_root, monkeypatch):
    from pulse.domain.market_rules import holidays as H

    sched = {2026: H.YearSchedule(2026, H.COMPLETE, frozenset({DAY}), "synthetic test notice", "test", date(2025, 12, 1), "test://notice")}
    monkeypatch.setattr(H, "OFFICIAL_SCHEDULES", sched)
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(8, 59, 50))
    s = rec.run(DAY, time(9, 3))
    assert net.log == [] and "not a trading day" in s.notes[0]
    assert R.ExtRecorder(svc, fetcher).is_trading_day(date(2026, 11, 9)) is True


def test_once_on_a_stale_feed_publishes_nothing(conn, lake, legacy_root):
    world, net, svc, fetcher, rec = setup(conn, lake, legacy_root, at(12, 0))
    _stale(net)
    s, rows = rec.once(DAY)
    assert rows and svc.latest_ready(B.DATASET_EXT, DAY) is None and any("not published" in n for n in s.notes)
