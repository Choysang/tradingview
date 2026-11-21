"""Native reference-data worker: adjusted bars, index bars, boards, listing dates, reconcile (Step 2, lane D).

Why: until the M6 cutover the old system fetches these on the server and ``ingest legacy-import`` imports its warehouse. This CLI is
the native producer, written and tested now and switched on ONLY at the cutover (it is not scheduled anywhere; see
``deploy/windows/run_refdata.ps1``). It publishes NEW dataset ids beside the legacy ones and never writes a legacy dataset.

Usage (from pulse3/)::

    uv run python -m pulse.workers.refdata listings                          # 2 requests
    uv run python -m pulse.workers.refdata index-bars --years 3              # 6 requests
    uv run python -m pulse.workers.refdata boards                            # ~1000 requests, resumable (same day), publishes at the end
    uv run python -m pulse.workers.refdata adjusted-bars --universe-listings --max-codes 300   # 2 requests per code, resumable
    uv run python -m pulse.workers.refdata adjusted-bars --mode refresh --universe-listings    # daily: ~len/50 quote requests + ex-right refetches
    uv run python -m pulse.workers.refdata adjusted-bars --codes 000001 600000                 # ad hoc, no cursor
    uv run python -m pulse.workers.refdata adjusted-bars --universe-listings --raw-only --max-codes 600   # raw kline only, 1 request per code, cursor raw:listings
    # every full (non raw-only) fetch also publishes bars_native_raw_1d and adj_factor_native from the SAME two responses
    uv run python -m pulse.workers.refdata register-native                                     # native switch: new listings, name / ST versions, cap snapshot (no HTTP)
    uv run python -m pulse.workers.refdata fill-listed-on [--dry]                              # cutover helper: NULL listed_on from the native listing dataset
    uv run python -m pulse.workers.refdata reconcile                                           # read only report vs the legacy datasets

Rate: at most 1 request per second (``--min-interval`` below 1.0 is refused). The first block (HTTP 403/429/456/501, a redirect or an HTML
page for a data call) stops the run at once, publishes only what was already fetched and exits 2; a run refuses to start while the
circuit of an endpoint is open and its cooldown has not elapsed. Never run the full universe from the development PC.
Exit codes: 0 ok, 1 failure, 2 source blocked us, 3 source unreachable.
Not covered: scheduling, switching consumers to the native datasets.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources import ref_listing as L
from pulse.adapters.sources import ref_market as M
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.tencent import TencentAdapter
from pulse.application import refdata_core as C
from pulse.application.data_service import DataService
from pulse.application.refdata_service import MAIN_PREFIXES, RefDataError, RefDataService

MAX_YEARS = 4          # one Sina kline request returns at most 1023 bars (about 4.1 years)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.refdata")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    ap.add_argument("--min-interval", type=float, default=1.0, help="seconds between HTTP requests; not below 1.0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("adjusted-bars", help="ratio forward-adjusted daily bars (Sina kline / qfq factor), dataset bars_native_fwd_adjusted_1d")
    p.add_argument("--mode", choices=("full", "refresh"), default="full")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--codes", nargs="+", help="ad hoc codes, no cursor")
    g.add_argument("--universe", action="store_true", help="codes of the research snapshot (legacy import), resumable cursor")
    g.add_argument("--universe-listings", action="store_true", help="main-board codes of the native listing dataset, resumable cursor")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--chunk", type=int, default=300, help="codes per published snapshot (2 requests per code)")
    p.add_argument("--max-codes", type=int, default=None, help="process at most this many codes in this run (resume later)")
    p.add_argument("--max-refetch", type=int, default=None, help="refresh mode: cap on whole-history refetches")
    p.add_argument("--restart", action="store_true", help="ignore a running cursor and start a new pass")
    p.add_argument("--raw-only", action="store_true", help="fetch the raw kline only (1 request per code, no factor file) and publish bars_native_raw_1d; own cursor")
    p = sub.add_parser("index-bars", help="six index series, dataset index_bars_native_1d")
    p.add_argument("--years", type=int, default=3)
    p = sub.add_parser("boards", help="industry + concept membership snapshot, datasets sector_membership_native_boards_*")
    p.add_argument("--chunk-boards", type=int, default=25)
    p.add_argument("--limit", type=int, default=None, help="smoke test: first N boards only, nothing is published")
    p = sub.add_parser("listings", help="listing dates, dataset instrument_listing_native")
    p.add_argument("--min-rows", type=int, default=L.MIN_ROWS)
    p = sub.add_parser("register-native", help="native instrument register: new listings, short-name / risk-warning versions and the cap snapshot from pulse3's own datasets")
    p.add_argument("--no-caps", action="store_true", help="register only; do not publish the cap snapshot")
    p.add_argument("--force", action="store_true", help="run although PULSE_REFDATA_SOURCE is not native (the legacy import registers the instruments then)")
    p = sub.add_parser("fill-listed-on", help="fill core.instrument.listed_on from instrument_listing_native where it is NULL; differences are reported, never overwritten")
    p.add_argument("--dry", action="store_true", help="report only, change nothing")
    p = sub.add_parser("reconcile", help="read only comparison of each native dataset with the legacy-imported one")
    p.add_argument("--only", nargs="+", choices=("adjusted", "index", "boards", "listings"), default=["adjusted", "index", "boards", "listings"])
    p.add_argument("--rel-tol", type=float, default=C.REL_TOL)
    return ap


def _start(years: int, today: date) -> date:
    if not 1 <= years <= MAX_YEARS:
        raise SystemExit(f"--years must be 1..{MAX_YEARS} (one kline request returns at most {M.MAX_BARS} bars)")
    return today - timedelta(days=365 * years)


def _codes(a, rs: RefDataService, svc: DataService) -> list[str]:
    if a.codes:
        return list(a.codes)
    if a.universe:
        from pulse.workers.ingest import universe_codes

        return universe_codes(svc.conn, svc.lake)
    frame = rs.read_frame(C.LISTING_DATASET)
    if frame is None:
        raise SystemExit("no native listing dataset: run the `listings` command first")
    return sorted(c for c in frame["code"].to_list() if c.startswith(MAIN_PREFIXES))


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.min_interval < 1.0:
        print("--min-interval below 1.0 is refused: the sources are limited to 1 request per second", file=sys.stderr)
        return 1
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    svc = DataService(conn, LakeStore(a.lake or cfg.lake_root))
    client = RateLimitedClient(min_interval_seconds=a.min_interval)
    try:
        svc.bootstrap()
        rs = RefDataService(svc)
        today = svc.clock().astimezone(C.CST).date()
        if a.cmd == "adjusted-bars":
            codes = _codes(a, rs, svc)
            if not codes:
                raise SystemExit("no codes")
            start = _start(a.years, today)
            sina = M.SinaRefAdapter(client)
            if a.raw_only and a.mode == "refresh":
                raise SystemExit("--raw-only is a fetch mode of the full pass; the daily refresh publishes raw bars from the same quote anyway")
            if a.mode == "refresh":
                out = rs.adjusted_refresh(sina, TencentAdapter(client), codes, start=start, today=today, chunk=a.chunk, max_refetch=a.max_refetch)
            else:
                out = rs.adjusted_bars(sina, codes, start=start, today=today, chunk=a.chunk, scope=None if a.codes else ("universe" if a.universe else "listings"),
                                       restart=a.restart, max_codes=a.max_codes, raw_only=a.raw_only)
            print(out.render())
        elif a.cmd == "index-bars":
            print(rs.index_bars(M.SinaRefAdapter(client), M.TencentRefAdapter(client), start=_start(a.years, today), today=today).render())
        elif a.cmd == "boards":
            print(rs.boards(M.SinaRefAdapter(client), chunk_boards=a.chunk_boards, limit=a.limit).render())
        elif a.cmd == "listings":
            print(rs.listings(L.ListingAdapter(client), min_rows=a.min_rows).render())
        elif a.cmd == "register-native":
            from pulse.application import bindings as B
            from pulse.application import native_register as NR

            if B.refdata_source() != "native" and not a.force:
                print("register-native runs only with PULSE_REFDATA_SOURCE=native (the legacy import owns the register otherwise); --force overrides", file=sys.stderr)
                return 1
            if a.no_caps:
                print(NR.register_native(svc, svc.lake).render())
            else:
                print(NR.refresh(svc, svc.lake)[0].render())
        elif a.cmd == "fill-listed-on":
            print(rs.fill_listed_on(dry=a.dry).render())
        else:
            print(rs.reconcile(a.only, rel_tol=a.rel_tol))
        print(f"http_requests: {client.requests_made}")
        return 0
    except SourceBlocked as exc:
        if getattr(exc, "summary", None) is not None:
            print(exc.summary.render())
        print(f"BLOCKED: {exc}; stopped immediately, fetched work was published, health marked unavailable (circuit OPEN)", file=sys.stderr)
        print(f"http_requests: {client.requests_made}", file=sys.stderr)
        return 2
    except TransportFailure as exc:
        if getattr(exc, "summary", None) is not None:
            print(exc.summary.render())
        print(f"SOURCE UNREACHABLE: {exc}; fetched work was published, cursor unchanged past it, rerun to resume", file=sys.stderr)
        return 3
    except RefDataError as exc:
        if getattr(exc, "summary", None) is not None:
            print(exc.summary.render())
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
