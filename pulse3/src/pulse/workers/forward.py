"""Forward books, account grants and broker fill import worker CLI (extension group C).

Usage (from pulse3/)::

    uv run python -m pulse.workers.forward register --principal <uuid> [--principal <uuid2>]   # frozen main book + 10-day shadow
    uv run python -m pulse.workers.forward daily                                                 # settle yesterday, decide the new session
    uv run python -m pulse.workers.forward grant --principal <uuid> --account <uuid> --permission read|plan|import [--revoke]
    uv run python -m pulse.workers.forward import-fills --principal <uuid> --account <uuid> --source-id <name> --file fills.json
    uv run python -m pulse.workers.forward readmodels --account <uuid> --version N --start ISO --end ISO --knowledge-as-of ISO [--policy twr-cost-x1-v1]
    uv run python -m pulse.workers.forward show

``import-fills`` stores the file as an immutable raw object FIRST, then calls the same import as the API (``fills.json`` = a JSON
list of BrokerFill objects). The forward books are labelled 检验中, carry no ranking weight and start at the first session they see.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from uuid import UUID

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application import ops_service, portfolio_service


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.forward")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register")
    p.add_argument("--principal", action="append", default=[])
    sub.add_parser("daily")
    p = sub.add_parser("grant")
    p.add_argument("--principal", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--permission", choices=portfolio_service.PERMISSIONS, required=True)
    p.add_argument("--revoke", action="store_true")
    p = sub.add_parser("import-fills")
    p.add_argument("--principal", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--source-id", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--mapping-version", default="broker-fill-v1")
    p = sub.add_parser("readmodels")
    p.add_argument("--account", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--knowledge-as-of", required=True)
    p.add_argument("--policy", default="twr-cost-x1-v1")
    sub.add_parser("show")
    return ap


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        if a.cmd == "register":
            out = [str(i) for i in portfolio_service.register_forward_books(conn, [UUID(p) for p in a.principal])]
        elif a.cmd == "daily":
            out = portfolio_service.forward_daily(conn)
        elif a.cmd == "grant":
            out = {"grant_version": portfolio_service.grant(conn, UUID(a.principal), UUID(a.account), a.permission, revoke=a.revoke)}
        elif a.cmd == "import-fills":
            raw = open(a.file, "rb").read()
            fills = json.loads(raw.decode("utf-8"))
            lake = LakeStore(cfg.lake_root)
            rid = portfolio_service.store_broker_raw(conn, source_id=a.source_id, content=raw, lake=lake)
            head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (a.account,)).fetchone()
            req = {"account_id": UUID(a.account), "expected_account_version": head["current_sequence"], "raw_object_id": rid,
                   "mapping_version": a.mapping_version, "fills": fills}
            out = portfolio_service.import_broker_fills(conn, UUID(a.principal), _wire_req(req))
        elif a.cmd == "readmodels":
            out = portfolio_service.build_readmodels(conn, UUID(a.account), a.version, {"start": _t(a.start), "end": _t(a.end)}, _t(a.knowledge_as_of), [], a.policy)
        else:
            out = [{k: v for k, v in r.items() if k in ("book_key", "label", "status_label", "latest")} for r in
                   conn.execute("SELECT t.book_key, t.label, t.status_label, d.trade_date AS latest FROM execution.tracked_account t LEFT JOIN LATERAL"
                                " (SELECT trade_date FROM execution.tracked_book_day WHERE account_id=t.account_id ORDER BY trade_date DESC LIMIT 1) d ON true")]
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


def _wire_req(req: dict) -> dict:
    from uuid import UUID as U
    out = dict(req)
    out["fills"] = [{**f, "instrument_id": U(str(f["instrument_id"]))} for f in req["fills"]]
    return out


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
