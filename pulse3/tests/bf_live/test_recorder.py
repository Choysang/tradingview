"""The native recorder (written, tested, NOT scheduled): mock transports only, rate limit, same dataset shape, stop on a block."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import httpx
import pytest

from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.adapters.sources.legacy_recordings import LegacyRecordings
from pulse.application import bf_live_service as B

from . import support as T

pytestmark = pytest.mark.pg
UTC = timezone.utc


class World:
    """A fake clock shared by the service, the HTTP client's rate limiter and the recorder's sleep."""

    def __init__(self, start_utc: datetime):
        self.t = start_utc
        self.mono = 0.0
        self.request_times: list[float] = []
        self.block_after: int | None = None
        self.requests = 0

    def now(self) -> datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += timedelta(seconds=secs)
        self.mono += secs

    def monotonic(self) -> float:
        return self.mono

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.request_times.append(self.mono)
        if self.block_after is not None and self.requests > self.block_after:
            return httpx.Response(403, content=b"forbidden")
        symbols = str(req.url).split("list=")[1].split(",")
        cst = self.t.astimezone(timezone(timedelta(hours=8)))
        lines = []
        for sym in symbols:
            f = [""] * 33
            sealed = sym.endswith("000001")
            f[0], f[1], f[2], f[3], f[4], f[5] = "平安银行" if sealed else "浦发银行", "10.00", "10.00", "11.00" if sealed else "10.50", "11.00" if sealed else "10.60", "10.00"
            f[6], f[7], f[8], f[9] = "11.00" if sealed else "10.49", "0.000" if sealed else "10.51", "123400", "1234000.00"
            f[10], f[11] = "500000" if sealed else "20000", "11.00" if sealed else "10.49"
            f[20], f[21] = "0" if sealed else "30000", "0.000" if sealed else "10.51"
            f[30], f[31] = cst.strftime("%Y-%m-%d"), cst.strftime("%H:%M:%S")
            lines.append(f'var hq_str_{sym}="{",".join(f)}";')
        return httpx.Response(200, content="\n".join(lines).encode("gbk"), headers={"content-type": "application/javascript"})


@pytest.fixture
def world():
    return World(datetime(2026, 11, 12, 1, 58, tzinfo=UTC))          # 09:58 CST


def _recorder(conn, lake, legacy_root, world, chunk=1):
    svc = B.LiveService(conn, lake, LegacyRecordings(legacy_root), clock=world.now)
    svc.bootstrap()
    client = RateLimitedClient(min_interval_seconds=1.0, transport=httpx.MockTransport(world.handler), clock=world.monotonic, sleep=world.sleep, now=world.now)
    return svc, client, B.IntradayRecorder(svc, client, chunk=chunk, sleep=world.sleep)


def test_records_same_dataset_shape_at_most_one_request_per_second(conn, lake, legacy_root, world):
    svc, client, rec = _recorder(conn, lake, legacy_root, world)
    s = rec.run(["000001", "600000"], T.DAY, time(10, 0), cycle_seconds=60.0, flush_every=10)
    assert s.counts["cycles"] == 2 and s.counts["rows_polled"] == 4 and s.counts["rows_published"] == 4 and client.requests_made == 4
    gaps = [b - a for a, b in zip(world.request_times, world.request_times[1:])]
    assert min(gaps) >= 1.0 - 1e-9                                       # never faster than one request per second
    man = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert man["row_count"] == 4 and man["coverage_document"]["recorder"] == "pulse_native_sina" and man["coverage_document"]["not_tick_data"]
    df = lake.read_snapshot_frames(man["content_hash"])
    # the same columns as the imported legacy snapshots (the schema both producers share)
    assert set(df.columns) == set(B.LR_SNAPSHOT_SCHEMA)
    a = df.filter(df["code"] == "000001").sort("quote_at").row(0, named=True)
    assert str(a["limit_up"]) == "11.0000" and str(a["price"]) == "11.0000" and a["ask1_lots"] == 0.0 and a["bid1_lots"] == 5000.0   # shares / 100
    assert a["bid2_px"] is None and a["quote_at"] == datetime(2026, 11, 12, 1, 58, tzinfo=UTC) and a["source_stream"] == "native"
    # and the seal derivation reads it exactly like an imported day
    f = svc.publish_seal_facts(T.DAY)
    assert f.counts["sealed_ever_count"] == 1 and f.counts["instruments_observed"] == 2
    raws = conn.execute("SELECT count(*) n FROM ingest.raw_object WHERE source_id='sina'").fetchone()["n"]
    assert raws == 4                                                     # every response kept as raw evidence


def test_a_block_stops_immediately_and_keeps_what_was_captured(conn, lake, legacy_root, world):
    world.block_after = 2                                                # the first cycle (2 requests) works, the next request is refused
    svc, client, rec = _recorder(conn, lake, legacy_root, world)
    with pytest.raises(SourceBlocked):
        rec.run(["000001", "600000"], T.DAY, time(10, 0), cycle_seconds=60.0)
    assert world.requests == 3 and client.circuit_open                    # no request after the refusal
    man = svc.latest_ready(B.DATASET_QUOTES, T.DAY)
    assert man is not None and man["row_count"] == 2                      # the cycle before the block is published
    h = conn.execute("SELECT runtime_state, last_error_code FROM ingest.source_health WHERE source_id='sina'").fetchone()
    assert h["runtime_state"] == "unavailable" and h["last_error_code"].startswith("blocked")


def test_idle_outside_trading_windows_polls_nothing(conn, lake, legacy_root):
    w = World(datetime(2026, 11, 12, 4, 0, tzinfo=UTC))                  # 12:00 CST, lunch break
    svc, client, rec = _recorder(conn, lake, legacy_root, w)
    s = rec.run(["000001"], T.DAY, time(12, 5), cycle_seconds=60.0)
    assert client.requests_made == 0 and s.counts["idle_waits"] >= 1 and svc.latest_ready(B.DATASET_QUOTES, T.DAY) is None


def test_native_limit_up_is_null_when_unverified_never_guessed():
    from decimal import Decimal

    assert B._limit_up("000001", "平安银行", Decimal("10.00"), T.DAY) == Decimal("11.00")
    assert B._limit_up("000001", "*ST平安", Decimal("10.00"), T.DAY) is None          # SZSE risk-warning band unverified in the package
    assert B._limit_up("688001", "科创股", Decimal("10.00"), T.DAY) is None           # not a main-board code: not modelled here
    assert B._limit_up("000001", "平安银行", None, T.DAY) is None
