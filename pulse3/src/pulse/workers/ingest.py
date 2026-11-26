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
    uv run python -m pulse.workers.ingest legacy-intraday --date 2026-11-12   # old recorder's 60 s snapshots -> dataset (BF live lane)
    uv run python -m pulse.workers.ingest seal-facts --date 2026-11-12        # estimated seal facts from that dataset
    uv run python -m pulse.workers.ingest legacy-news --date 2026-11-12       # 7x24 messages -> documents -> claims (then alerts scan)
    uv run python -m pulse.workers.ingest legacy-ext|legacy-guba|legacy-alerts --date 2026-11-12
    uv run python -m pulse.workers.ingest intraday-record --until 15:33 --universe   # native recorder, NOT scheduled before the M6 cutover
    ... intraday-record ... --limit-pools                                      # OPTIONAL vendor limit pools after the close (off by default)
    uv run python -m pulse.workers.pool_compare --date 2026-11-13               # vendor pools vs seal facts (read only)

Exit codes: 0 ok, 1 failure, 2 source blocked us (circuit OPEN, nothing half-published).
Not covered: scheduling; there is deliberately no full-market live crawl command (pass explicit codes).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.postgres.instrument_repo import CODE_SOURCES_SQL
from pulse.adapters.sources.cninfo import CninfoAdapter
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.adapters.sources.sina import SinaAdapter
from pulse.adapters.sources.tencent import TencentAdapter
from pulse.application.data_service import DataService
from pulse.adapters.sources.legacy_recordings import LegacyRecordings

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
    for name in ("legacy-intraday", "legacy-ext", "legacy-guba", "legacy-alerts", "seal-facts", "legacy-news"):
        p = sub.add_parser(name, help="BF live lane: import the old recorder's files (read-only) for one session")
        p.add_argument("--date", type=date.fromisoformat, default=None, help="session date (default: today, Asia/Shanghai)")
        p.add_argument("--legacy-data", default=None, help="root of the old recorder data (default PULSE_LEGACY_DATA; unset reads as no recordings)")
        if name == "legacy-news":
            p.add_argument("--no-claims", action="store_true", help="documents only; skip claim extraction")
    p = sub.add_parser("intraday-record", help="native main-board recorder (auction, 5-level book, hot subset; Tencent primary, optional Sina fallback); "
                                                 "NOT scheduled before the M6 cutover (the old system records meanwhile)")
    p.add_argument("--until", type=time.fromisoformat, default=time(15, 33), help="stop at this Asia/Shanghai time (default 15:33, after the after-hours session)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", nargs="+")
    g.add_argument("--universe", action="store_true")
    p.add_argument("--source", choices=("sina", "tencent"), default="tencent", help="primary vendor (default tencent, the old recorder's vendor)")
    p.add_argument("--fallback", choices=("sina", "tencent"), default=None,
                   help="OPT-IN second vendor, used for the rest of the session after the primary is blocked (default: none, a block stops the run; "
                        "hq.sinajs.cn already answered 403 to the server IP); must differ from --source")
    p.add_argument("--cycle-seconds", type=float, default=60.0, help="period of the full-board snapshots")
    p.add_argument("--flush-every", type=int, default=2,
                   help="full cycles between published parts (default 2 = every 2 minutes: the tape / actionability read the newest part and accept quotes <= 3 minutes old)")
    p.add_argument("--limit-pools", action="store_true",
                   help="OPTIONAL: also fetch the Eastmoney limit-up / broken-board / limit-down pools once after the close (15:25-15:50) into the "
                        "dataset limit_pools_eastmoney, to reconcile the seal facts (default off; compare with: python -m pulse.workers.pool_compare)")
    p.add_argument("--batch", type=int, default=None, choices=range(1, 201), metavar="N",
                   help="symbols per quote request (default: the vendor's own, Tencent 80 as the old recorder sent). At 1 request/s a full board of ~3200 codes "
                        "takes 40 s of every minute, so a hot grid of 10 s would lose about half of its slots (the 20 s default fits); a larger N shortens the board. Tencent's real "
                        "maximum is NOT verified: probe it once by hand first, and watch short_batches in the run summary (responses shorter than asked)")
    p.add_argument("--static-sessions", type=int, default=3, help="earlier sessions whose limit touches form the static pool (0 = none)")
    return ap


LIVE_CMDS = ("legacy-intraday", "legacy-ext", "legacy-guba", "legacy-alerts", "seal-facts", "legacy-news", "intraday-record")


def _today_cst() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()


def run_live(a, conn, lake: LakeStore, client: RateLimitedClient) -> list:
    """BF live lane commands (kept apart from the data-plane commands above)."""
    from pulse.application.bf_live_service import LegacyNewsImporter, LiveService

    if a.cmd == "intraday-record" and a.fallback == a.source:
        raise SystemExit("--fallback must be the other vendor than --source")
    day = getattr(a, "date", None) or _today_cst()
    rec = LegacyRecordings(getattr(a, "legacy_data", None))
    svc = LiveService(conn, lake, rec)
    svc.bootstrap()
    if a.cmd == "legacy-intraday":
        return [svc.import_intraday(day)]
    if a.cmd == "legacy-ext":
        return [svc.import_ext(day)]
    if a.cmd == "legacy-guba":
        return [svc.import_guba(day)]
    if a.cmd == "legacy-alerts":
        return [svc.import_alerts(day)]
    if a.cmd == "seal-facts":
        return [svc.publish_seal_facts(day)]
    if a.cmd == "legacy-news":
        imp = LegacyNewsImporter(conn, lake)
        imp.bootstrap()
        out = [imp.import_news(rec, day)]
        if not a.no_claims:
            out.append(imp.process_new_claims())
        return out
    codes = list(a.codes) if a.codes else universe_codes(conn, lake)
    if not codes:
        raise SystemExit("no codes: pass --codes or import the research snapshot first")
    from pulse.application.intraday_recorder import AllSourcesDown, IntradayRecorder, static_pool
    from pulse.application.intraday_pools import LimitPoolRecorder

    today = _today_cst()
    static = static_pool(svc, today, a.static_sessions) if a.static_sessions > 0 else set()
    fallback = RateLimitedClient(min_interval_seconds=client.min_interval) if a.fallback else None   # its own circuit and pacing
    pool_client = RateLimitedClient(min_interval_seconds=client.min_interval) if a.limit_pools else None      # its own circuit and pacing
    try:
        pools = LimitPoolRecorder(svc, pool_client) if pool_client is not None else None
        rec = IntradayRecorder(svc, client, source=a.source, fallback_client=fallback, chunk=a.batch, static=static, limit_pools=pools)
        try:
            return [rec.run(codes, today, a.until, cycle_seconds=a.cycle_seconds, flush_every=a.flush_every)]
        except AllSourcesDown as exc:
            if exc.summary is not None:
                print(exc.summary.render())
            raise
    finally:
        for extra in (fallback, pool_client):
            if extra is not None:
                extra.close()


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
    rows = conn.execute("SELECT DISTINCT source_symbol FROM core.instrument_alias WHERE source_id " + CODE_SOURCES_SQL +
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
        if a.cmd in LIVE_CMDS:
            out = run_live(a, conn, svc.lake, client)
        elif a.cmd == "legacy-import":
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
