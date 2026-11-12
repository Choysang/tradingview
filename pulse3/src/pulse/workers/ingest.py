"""Ingest worker CLI: legacy-import, live-daily, minute, quote, announcements, reconcile, gc (W03/W04, M1).

Why: a thin command line over :class:`pulse.application.data_service.DataService`; each command runs
raw -> observation -> bars -> manifest and prints a summary. Network commands honour a
configurable rate limit (default 1 request/second) and stop at the first block (exit code 2, health OPEN).

Usage (from pulse3/)::

    uv run python -m pulse.workers.ingest legacy-import --since 2025-11-01
    uv run python -m pulse.workers.ingest live-daily --codes 000001 600000 --source both
    uv run python -m pulse.workers.ingest minute --codes 000001 --source tencent --period 1
    uv run python -m pulse.workers.ingest announcements --days 3
    uv run python -m pulse.workers.ingest reconcile --codes 000001 600000

Exit codes: 0 ok, 1 failure, 2 source blocked us (circuit OPEN, nothing half-published).
Not covered: scheduling; there is deliberately no full-market live crawl command (pass explicit codes).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources.cninfo import CninfoAdapter
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.adapters.sources.sina import SinaAdapter
from pulse.adapters.sources.tencent import TencentAdapter
from pulse.application.data_service import DataService

DEFAULT_WAREHOUSE = Path(__file__).resolve().parents[4] / ".work" / "snapshots" / "server" / "data" / "warehouse" / "live"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.ingest")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    ap.add_argument("--min-interval", type=float, default=1.0, help="seconds between HTTP requests (default 1.0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("legacy-import")
    p.add_argument("--since", required=True, type=date.fromisoformat)
    p.add_argument("--codes", nargs="*")
    p.add_argument("--boards", choices=("main", "all"), default="main")
    p.add_argument("--warehouse", default=os.environ.get("PULSE_LEGACY_WAREHOUSE") or str(DEFAULT_WAREHOUSE))
    p = sub.add_parser("live-daily")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", nargs="+")
    g.add_argument("--universe", action="store_true", help="every code imported from the legacy warehouse (main board)")
    p.add_argument("--source", choices=("sina", "tencent", "both"), default="sina")
    p.add_argument("--count", type=int, default=320)
    p.add_argument("--chunk", type=int, default=200, help="codes per READY snapshot (one batch each)")
    p = sub.add_parser("minute")
    p.add_argument("--codes", nargs="+", required=True)
    p.add_argument("--source", choices=("tencent", "sina"), default="tencent")
    p.add_argument("--period", type=int, default=1)
    p.add_argument("--count", type=int, default=240)
    p = sub.add_parser("quote")
    p.add_argument("--codes", nargs="+", required=True)
    p.add_argument("--source", choices=("sina", "tencent"), default="sina")
    p = sub.add_parser("announcements")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--page-size", type=int, default=30)
    p = sub.add_parser("reconcile")
    p.add_argument("--codes", nargs="+", required=True)
    p.add_argument("--last", type=int, default=5)
    p = sub.add_parser("gc")
    p.add_argument("--min-age-seconds", type=float, default=3600.0)
    return ap


def universe_codes(conn, lake: LakeStore, recent_sessions: int = 5) -> list[str]:
    """Codes of the research universe: instruments with a bar in the latest READY research snapshot's last sessions.

    The legacy alias table also holds delisted codes (Sina returns their 2002 bars, outside every partition), so it is
    not the universe."""
    from pulse.application import bindings as B

    man = B._ready_manifest(conn, B.RESEARCH_DATASET, None)
    df = None if man is None else lake.read_snapshot_frames(man["content_hash"])
    if df is None or df.height == 0:
        return []
    days = sorted(df["trade_date"].unique().to_list())[-recent_sessions:]
    ids = df.filter(df["trade_date"].is_in(days))["instrument_id"].unique().to_list()
    rows = conn.execute("SELECT DISTINCT source_symbol FROM core.instrument_alias WHERE source_id='legacy_warehouse'"
                        " AND instrument_id = ANY(%s::uuid[]) ORDER BY 1", ([str(i) for i in ids],)).fetchall()
    return [r["source_symbol"] for r in rows]


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    svc = DataService(conn, LakeStore(a.lake or cfg.lake_root))
    client = RateLimitedClient(min_interval_seconds=a.min_interval)
    try:
        svc.bootstrap()
        if a.cmd == "legacy-import":
            out = [svc.import_legacy(LegacyWarehouse(Path(a.warehouse)), a.since, codes=a.codes, boards=a.boards)]
        elif a.cmd == "live-daily":
            codes = universe_codes(conn, svc.lake) if a.universe else list(a.codes)
            out = []
            for i in range(0, len(codes), max(1, a.chunk)):
                part = codes[i:i + max(1, a.chunk)]
                if a.source in ("sina", "both"):
                    out.append(svc.ingest_daily(SinaAdapter(client), "sina", part, count=a.count))
                if a.source in ("tencent", "both"):
                    out.append(svc.ingest_daily(TencentAdapter(client), "tencent", part, count=a.count))
        elif a.cmd == "minute":
            adapter = TencentAdapter(client) if a.source == "tencent" else SinaAdapter(client)
            out = [svc.ingest_minute(adapter, a.source, a.codes, period_minutes=a.period, count=a.count)]
        elif a.cmd == "quote":
            adapter = SinaAdapter(client) if a.source == "sina" else TencentAdapter(client)
            out = [svc.ingest_quotes(adapter, a.source, a.codes)]
        elif a.cmd == "announcements":
            out = [svc.ingest_announcements(CninfoAdapter(client), days=a.days, page_size=a.page_size)]
        elif a.cmd == "reconcile":
            out = [svc.reconcile_daily(a.codes, last_n_dates=a.last)]
        else:
            rep = svc.gc(min_age_seconds=a.min_age_seconds)
            print(f"== gc ==\n  {rep}")
            return 0
        for s in out:
            print(s.render())
        print(f"http_requests: {client.requests_made}")
        return 0
    except SourceBlocked as exc:
        print(f"BLOCKED: {exc}; stopped immediately, health marked unavailable (circuit OPEN)", file=sys.stderr)
        return 2
    except TransportFailure as exc:                 # network down / timeout: the batch is FAILED, the cursor did not move
        print(f"SOURCE UNREACHABLE: {exc}; batch marked failed, cursor unchanged, rerun to resume", file=sys.stderr)
        return 3
    finally:
        client.close()
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
