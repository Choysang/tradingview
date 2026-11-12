"""Synthetic but structurally real world for the I1 tests.

Writes a small legacy warehouse (parquet files in the legacy layout) and imports it with the real
``DataService.import_legacy``; raw daily bars are published through the normal manifest protocol. Everything is synthetic
(random walks); it proves wiring, point-in-time handling and invariants, not market behaviour.
"""
from __future__ import annotations

import itertools
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, ingest_repo, market_repo
from pulse.adapters.sources import registry
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.application.data_service import DataService, _bars_frame, _split_months, ingest_repo_create_batch

T0 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
LEGACY_INGEST = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)      # 16:00 CST: bars of 2026-11-10 are final
REVISION_INGEST = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)    # after every clock tick of the build
LAST = date(2026, 11, 10)
REAL = ["000001", "600000", "002415"]                                   # recorded Sina quote fixtures exist for these
CODES = REAL + [f"6{i:05d}" for i in range(1, 59)]                       # 61 main-board stocks
ST = CODES[-12:-9]
NEW_LISTED = CODES[-9:-7]
HIGH_PRICE = CODES[-7:-5]
BIG_CAP = CODES[-5:-3]
INDUSTRIES = {"bk_a": "测试行业甲", "bk_b": "测试行业乙", "bk_c": "测试行业丙", "bk_d": "测试行业丁"}


def clock(start: datetime = T0):
    c = itertools.count()
    return lambda: start + timedelta(seconds=next(c))


def sessions(n: int = 130) -> list[date]:
    out, d = [], LAST
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def walk(code: str, days: list[date], base: float) -> list[tuple[float, float, float, float, float]]:
    rnd = random.Random(code)
    px, rows = base, []
    for _ in days:
        o = px * (1 + rnd.uniform(-0.01, 0.01))
        c = o * (1 + rnd.uniform(-0.03, 0.03))
        h, l = max(o, c) * 1.005, min(o, c) * 0.995
        rows.append((round(o, 2), round(h, 2), round(l, 2), round(c, 2), float(rnd.randint(50, 500) * 100_000)))
        px = c
    return rows


def write_warehouse(root: Path) -> dict[str, list]:
    days = sessions()
    series = {c: walk(c, days, 150.0 if c in HIGH_PRICE else 10.0 + (i % 17)) for i, c in enumerate(CODES)}
    bars = []
    for c, rows in series.items():
        for d, (o, h, l, cl, v) in zip(days, rows):
            if c in NEW_LISTED and d < date(2026, 10, 20):
                continue
            bars.append({"code": c, "date": d, "open": o, "high": h, "low": l, "close": cl, "volume": v, "amount": None, "ingested_at": LEGACY_INGEST})
    for sub in ("bars", "securities", "listings", "index_bars", "boards"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    pl.DataFrame(bars).write_parquet(root / "bars" / "1-a.parquet")
    # a revision of the last bar of one stock, ingested later than every build clock tick
    c0 = CODES[3]
    last = series[c0][-1]
    pl.DataFrame([{"code": c0, "date": LAST, "open": last[0], "high": last[1] * 1.6, "low": last[2], "close": round(last[3] * 1.5, 2),
                   "volume": last[4], "amount": None, "ingested_at": REVISION_INGEST}]).write_parquet(root / "bars" / "2-rev.parquet")
    names = {"000001": "平安银行", "600000": "浦发银行", "002415": "海康威视"}
    sec = [{"code": c, "name": (f"*ST测试{c}" if c in ST else names.get(c, f"测试{c}")),
            "board": "10cm", "total_cap": 600.0 if c in BIG_CAP else 80.0, "float_cap": 500.0 if c in BIG_CAP else 50.0, "pe_ttm": 10.0, "pb": 1.0,
            "as_of": "2026-11-10", "list_date": None, "ingested_at": LEGACY_INGEST} for c in CODES]
    pl.DataFrame(sec).write_parquet(root / "securities" / "1-a.parquet")
    lst = [{"code": c, "list_date": date(2026, 10, 20) if c in NEW_LISTED else date(2005, 1, 4), "ingested_at": LEGACY_INGEST} for c in CODES]
    pl.DataFrame(lst).write_parquet(root / "listings" / "1-a.parquet")
    idx = []
    for sym in ("000300", "000001", "399006"):
        px = 4000.0
        rnd = random.Random(sym)
        for d in days:
            o = px
            px = px * (1 + rnd.uniform(-0.01, 0.01))
            idx.append({"symbol": sym, "date": d, "open": o, "high": max(o, px), "low": min(o, px), "close": px, "volume": 1e10, "ingested_at": LEGACY_INGEST})
    pl.DataFrame(idx).write_parquet(root / "index_bars" / "1-a.parquet")
    boards = []
    for n, c in enumerate(CODES):
        b = list(INDUSTRIES)[n % 4]
        boards.append({"code": c, "kind": "industry", "board_id": b, "board_name": INDUSTRIES[b], "as_of": date(2026, 11, 9), "ingested_at": LEGACY_INGEST})
        boards.append({"code": c, "kind": "concept", "board_id": "gn_x", "board_name": "概念", "as_of": date(2026, 11, 9), "ingested_at": LEGACY_INGEST})
    pl.DataFrame(boards).write_parquet(root / "boards" / "1-a.parquet")
    return series


def publish_raw(svc: DataService, series: dict[str, list]) -> dict:
    ids = {r["source_symbol"]: r["instrument_id"] for r in svc.conn.execute(
        "select a.source_symbol, a.instrument_id from core.instrument_alias a join core.instrument i using (instrument_id)"
        " where a.source_id='legacy_warehouse' and i.instrument_type='equity'")}
    days = sessions()
    rows, frame_rows = [], []
    avail = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
    snapshot_id = uuid.uuid4()
    for c, ser in series.items():
        for d, (o, h, l, cl, v) in zip(days, ser):
            if c in NEW_LISTED and d < date(2026, 10, 20):
                continue
            open_t = datetime(d.year, d.month, d.day, 1, 30, tzinfo=timezone.utc)
            close_t = datetime(d.year, d.month, d.day, 7, 0, tzinfo=timezone.utc)
            rows.append({"trade_date": d, "instrument_id": ids[c], "timeframe": "1d", "session_scope": "regular_continuous_v1",
                         "bar_open_time": open_t, "bar_close_time": close_t, "snapshot_id": snapshot_id, "open": Decimal(str(o)),
                         "high": Decimal(str(h)), "low": Decimal(str(l)), "close": Decimal(str(cl)), "volume_shares": int(v),
                         "turnover_cny": None, "exchange_reference_price": None, "is_final": True, "available_at": avail,
                         "quality_status": "partial", "observation_id": None})
            frame_rows.append({"instrument_id": str(ids[c]), "trade_date": d, "timeframe": "1d", "bar_open_time": open_t, "bar_close_time": close_t,
                               "open_price_raw": Decimal(str(o)), "high_price_raw": Decimal(str(h)), "low_price_raw": Decimal(str(l)),
                               "close_price_raw": Decimal(str(cl)), "volume_shares": int(v), "turnover_amount_cny": None, "is_final": True,
                               "available_at": avail, "quality_status": "partial"})
    spec = registry.spec("sina", "kline_daily")
    batch_id = ingest_repo_create_batch(svc, spec, "world", svc.clock())
    with svc.conn.transaction():
        ingest_repo.commit_raw(svc.conn, batch_id, svc.clock())
        ingest_repo.set_batch_state(svc.conn, batch_id, "NORMALIZED")

    def projector():
        with svc.conn.transaction():
            market_repo.insert_bars(svc.conn, rows)

    return svc.publish_dataset(
        dataset_id="bars_raw_1d", snapshot_id=snapshot_id, batch_id=batch_id, frames=_split_months(_bars_frame(frame_rows)),
        contract_id="CanonicalBar", coverage={"price_basis": "raw", "adjustment_mode": "raw", "synthetic": True}, owner=f"job:{batch_id}",
        available_watermark=avail, projector=projector)


def build_world(dsn: str, tmp: Path) -> dict:
    wh_root = tmp / "warehouse"
    series = write_warehouse(wh_root)
    conn = db.connect(dsn, autocommit=True)
    lake = LakeStore(tmp / "lake")
    svc = DataService(conn, lake, clock=clock())
    svc.bootstrap()
    svc.import_legacy(LegacyWarehouse(wh_root), date(2025, 11, 1))
    publish_raw(svc, series)
    conn.close()
    return {"dsn": dsn, "lake_root": tmp / "lake", "warehouse": wh_root, "series": series}
