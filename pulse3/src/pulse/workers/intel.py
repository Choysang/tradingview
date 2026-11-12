"""Intelligence worker CLI: news, claims, community, seats, fundflow, counts (I3).

Why: a thin command line over :class:`pulse.application.intelligence_service.IntelligenceService`; each command prints
a summary. Network commands honour a configurable rate limit (default 1 request/second) and stop at the first
block (exit code 2, health circuit OPEN).

Usage (from pulse3/)::

    uv run python -m pulse.workers.intel news --source both --pages 1
    uv run python -m pulse.workers.intel claims
    uv run python -m pulse.workers.intel community --codes 000001 600000 --pages 2
    uv run python -m pulse.workers.intel seats --days 2          # live Eastmoney LHB, weekdays only
    uv run python -m pulse.workers.intel seats --legacy          # normalize the legacy lhb raw objects
    uv run python -m pulse.workers.intel fundflow --codes 000001 600000 --limit 10

The optional OpenAI-compatible claim provider is used by ``claims --provider openai`` only when
``PULSE_OPENAI_KEY_FILE`` names a readable key file; the key is never printed.
Exit codes: 0 ok, 1 failure, 2 source blocked us (nothing half-published).
Not covered: scheduling; there is deliberately no full-market crawl (explicit codes only).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.models.openai_compat import OpenAICompatProvider
from pulse.adapters.models.port import claim_extraction_policy
from pulse.adapters.models.rules import RulesProvider
from pulse.adapters.models.runner import ModelRunner
from pulse.adapters.postgres import db
from pulse.adapters.sources.cls_news import ClsNewsAdapter
from pulse.adapters.sources.eastmoney_news import EastmoneyNewsAdapter
from pulse.adapters.sources.fundflow import FundFlowAdapter
from pulse.adapters.sources.guba import GubaAdapter
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.adapters.sources.lhb import LhbAdapter
from pulse.application.intelligence_service import IntelligenceService


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.intel")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    ap.add_argument("--min-interval", type=float, default=1.0, help="seconds between HTTP requests (default 1.0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("news")
    p.add_argument("--source", choices=("eastmoney", "cls", "both"), default="eastmoney")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--page-size", type=int, default=30)
    p = sub.add_parser("claims")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--provider", choices=("rules", "openai"), default="rules")
    p = sub.add_parser("community")
    p.add_argument("--codes", nargs="+", required=True)
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--window-hours", type=int, default=24)
    p = sub.add_parser("seats")
    p.add_argument("--days", type=int, default=0, help="live: last N weekdays including today (0 = none)")
    p.add_argument("--legacy", action="store_true", help="normalize legacy lhb raw objects")
    p = sub.add_parser("fundflow")
    p.add_argument("--codes", nargs="+", required=True)
    p.add_argument("--limit", type=int, default=10)
    sub.add_parser("counts")
    return ap


def _weekdays(n: int, today: date) -> list[date]:
    out, d = [], today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    lake = LakeStore(a.lake or cfg.lake_root)
    client = RateLimitedClient(min_interval_seconds=a.min_interval)
    providers = {"rules": RulesProvider()}
    policy = claim_extraction_policy("rules", {"provider": "rules"})
    if a.cmd == "claims" and a.provider == "openai":
        oa = OpenAICompatProvider.from_env()
        if oa is None:
            print("openai provider not enabled: PULSE_OPENAI_KEY_FILE is unset or unreadable; using nothing", file=sys.stderr)
            return 1
        providers["openai_compat"] = oa
        policy = claim_extraction_policy("openai_compat", oa.locator, rollout_state="candidate")
    from pulse.adapters.models.rules import LOCATOR

    if a.cmd == "claims" and a.provider == "rules":
        policy = claim_extraction_policy("rules", LOCATOR)
    svc = IntelligenceService(conn, lake, runner=ModelRunner(conn, lake, providers), claim_policy=policy)
    try:
        svc.bootstrap()
        out = []
        if a.cmd == "news":
            if a.source in ("eastmoney", "both"):
                out.append(svc.ingest_news(EastmoneyNewsAdapter(client), "eastmoney", pages=a.pages, page_size=a.page_size))
            if a.source in ("cls", "both"):
                out.append(svc.ingest_news(ClsNewsAdapter(client), "cls", pages=a.pages, page_size=a.page_size))
        elif a.cmd == "claims":
            out.append(svc.process_claims(limit=a.limit))
        elif a.cmd == "community":
            out.append(svc.ingest_community(GubaAdapter(client), a.codes, pages=a.pages, window_hours=a.window_hours))
        elif a.cmd == "seats":
            if a.legacy:
                out.append(svc.import_legacy_lhb())
            if a.days:
                out.append(svc.ingest_lhb(LhbAdapter(client), _weekdays(a.days, datetime.now(timezone.utc).astimezone().date())))
        elif a.cmd == "fundflow":
            out.append(svc.ingest_fundflow(FundFlowAdapter(client), a.codes, limit=a.limit))
        for s in out:
            print(s.render())
        if a.cmd == "counts" or out:
            print("table_counts:", svc.counts())
        print(f"http_requests: {client.requests_made}")
        return 0
    except SourceBlocked as exc:
        print(f"BLOCKED: {exc}; stopped immediately, health marked unavailable (circuit OPEN)", file=sys.stderr)
        return 2
    finally:
        client.close()
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
