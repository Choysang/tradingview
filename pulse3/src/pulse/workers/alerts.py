"""Alerts, calendar and queued-job worker CLI (extension group C).

Usage (from pulse3/)::

    uv run python -m pulse.workers.alerts init-rules --principal <uuid>   # P0/P1/P2 rules on the in-app inbox
    uv run python -m pulse.workers.alerts scan                            # claims -> alert intents (idempotent)
    uv run python -m pulse.workers.alerts deliver                         # webhook attempts; sends nothing unless a URL is configured
    uv run python -m pulse.workers.alerts calendar-sync --days 60         # Eastmoney 财经日历, <= 1 request/s, stops on a block
    uv run python -m pulse.workers.alerts calendar-derive                 # lock-up/suspension/meeting events from announcement titles
    uv run python -m pulse.workers.alerts calendar-expiries --year 2026 --month 12
    uv run python -m pulse.workers.alerts seal-workspace --principal <uuid>
    uv run python -m pulse.workers.alerts drain [--max 20]                # execute queued proposal/export/manual jobs

Webhook push: set ``PULSE_PUSH_WEBHOOK`` (+ ``PULSE_PUSH_KIND`` wecom|feishu|dingtalk|generic) or a secrets file
(``PULSE_PUSH_WEBHOOK_FILE`` / ``<PULSE_SECRETS_DIR>/push_webhook.url``). Exit codes: 0 ok, 1 failure, 2 source blocked us.
"""
from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.sources import calendar_em
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.application import alert_service, calendar_service, ops_service, workspace_service


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.alerts")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init-rules")
    p.add_argument("--principal", required=True)
    sub.add_parser("scan")
    sub.add_parser("deliver")
    p = sub.add_parser("calendar-sync")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--min-interval", type=float, default=1.0)
    sub.add_parser("calendar-derive")
    p = sub.add_parser("calendar-expiries")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p = sub.add_parser("seal-workspace")
    p.add_argument("--principal", required=True)
    p = sub.add_parser("drain")
    p.add_argument("--max", type=int, default=20)
    return ap


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        if a.cmd == "init-rules":
            out = alert_service.init_default_rules(conn, UUID(a.principal))
        elif a.cmd == "scan":
            out = alert_service.scan(conn)
        elif a.cmd == "deliver":
            out = alert_service.deliver(conn)
        elif a.cmd == "calendar-sync":
            client = RateLimitedClient(min_interval_seconds=a.min_interval)
            try:
                out = calendar_service.sync(conn, calendar_em.CalendarAdapter(client), LakeStore(a.lake or cfg.lake_root), days=a.days)
                out["http_requests"] = client.requests_made
            finally:
                client.close()
        elif a.cmd == "calendar-derive":
            out = calendar_service.derive_company_events(conn)
        elif a.cmd == "calendar-expiries":
            out = calendar_service.add_rule_expiries(conn, a.year, a.month)
        elif a.cmd == "seal-workspace":
            out = {"sealed_objects": workspace_service.seal_all(conn, UUID(a.principal))}
        else:
            out = []
            for _ in range(a.max):
                r = ops_service.run_next(conn)
                if r is None:
                    break
                out.append({**r, "job_run_id": str(r["job_run_id"])})
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    except SourceBlocked as exc:
        print(f"BLOCKED: {exc}; stopped immediately (circuit OPEN)", file=sys.stderr)
        return 2
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
