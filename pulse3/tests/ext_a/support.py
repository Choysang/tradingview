"""Test helpers for extension group A: later raw bars published AFTER the rankings under test were decided."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db, ingest_repo, market_repo
from pulse.adapters.sources import registry
from pulse.application.data_service import DataService, _bars_frame, _split_months, ingest_repo_create_batch
from integration import world as W


def next_sessions(n: int, after: date = W.LAST) -> list[date]:
    out, d = [], after
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def publish_forward_bars(world: dict, n: int) -> list[date]:
    """n later sessions for every raw instrument; per-instrument drift (+-1.2% a day) gives the outcome statistics some dispersion."""
    conn = db.connect(world["dsn"], autocommit=True)
    try:
        svc = DataService(conn, LakeStore(world["lake_root"]), clock=lambda: datetime.now(timezone.utc))
        last = {r["instrument_id"]: r["close_price_raw"] for r in conn.execute(
            "select distinct on (instrument_id) instrument_id, close_price_raw from market.bar_projection where timeframe='1d' and trade_date=%s"
            " order by instrument_id, available_at desc", (W.LAST,))}
        days = next_sessions(n)
        snapshot_id = uuid.uuid4()
        rows, frame_rows = [], []
        for k, (iid, close0) in enumerate(sorted(last.items(), key=lambda kv: str(kv[0]))):
            px = close0
            drift = Decimal(((k % 7) - 3)) * Decimal("0.004")
            for d in days:
                nxt = (px * (1 + drift)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                o, h, l = px, max(px, nxt), min(px, nxt)
                open_t = datetime(d.year, d.month, d.day, 1, 30, tzinfo=timezone.utc)
                close_t = datetime(d.year, d.month, d.day, 7, 0, tzinfo=timezone.utc)
                rows.append({"trade_date": d, "instrument_id": iid, "timeframe": "1d", "session_scope": "regular_continuous_v1",
                             "bar_open_time": open_t, "bar_close_time": close_t, "snapshot_id": snapshot_id, "open": o, "high": h, "low": l,
                             "close": nxt, "volume_shares": 2_000_000, "turnover_cny": None, "exchange_reference_price": None, "is_final": True,
                             "available_at": close_t + timedelta(hours=1), "quality_status": "partial", "observation_id": None})
                frame_rows.append({"instrument_id": str(iid), "trade_date": d, "timeframe": "1d", "bar_open_time": open_t, "bar_close_time": close_t,
                                   "open_price_raw": o, "high_price_raw": h, "low_price_raw": l, "close_price_raw": nxt, "volume_shares": 2_000_000,
                                   "turnover_amount_cny": None, "is_final": True, "available_at": close_t + timedelta(hours=1), "quality_status": "partial"})
                px = nxt
        spec = registry.spec("sina", "kline_daily")
        batch_id = ingest_repo_create_batch(svc, spec, "forward", svc.clock())
        with conn.transaction():
            ingest_repo.commit_raw(conn, batch_id, svc.clock())
            ingest_repo.set_batch_state(conn, batch_id, "NORMALIZED")

        def projector():
            with conn.transaction():
                market_repo.insert_bars(conn, rows)

        svc.publish_dataset(dataset_id="bars_raw_1d", snapshot_id=snapshot_id, batch_id=batch_id, frames=_split_months(_bars_frame(frame_rows)),
                            contract_id="CanonicalBar", coverage={"price_basis": "raw", "adjustment_mode": "raw", "synthetic": True},
                            owner=f"job:{batch_id}", available_watermark=max(r["available_at"] for r in rows), projector=projector)
        return days
    finally:
        conn.close()
