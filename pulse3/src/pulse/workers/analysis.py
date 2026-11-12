"""Analysis worker CLI: build / daily / open-account / actionability / show (I1).

Why: one command line over ``release_service.build_release`` and ``account_service`` so a release can be built,
published and re-run idempotently (FINAL_SYSTEM_SPEC §3, §12-13).

Usage (from pulse3/)::

    uv run python -m pulse.workers.analysis build --as-of latest
    uv run python -m pulse.workers.analysis daily            # ingest increments, then build
    uv run python -m pulse.workers.analysis open-account     # paper account, 1,000,000 CNY, user cost profile
    uv run python -m pulse.workers.analysis actionability    # Sina quotes -> quote snapshot -> ActionabilitySnapshot
    uv run python -m pulse.workers.analysis show

Exit codes: 0 ok, 1 failure (release not published; reasons printed), 2 a source blocked us (nothing built).
``daily`` fetches increments only for the focus set (plan instruments and holdings) at <= 1 request/s; a full-universe
raw refresh is the ingest worker's ``live-daily`` over the universe (about 55 minutes at 1 request/s).
Not covered: scheduling.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.adapters.sources.cninfo import CninfoAdapter
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.adapters.sources.sina import SinaAdapter
from pulse.application import account_service as acct
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.application.release_service import BuildConfig, BuildFailed, BuildInProgress, RawCoverageMissing, build_release


def parse_as_of(text: str) -> datetime | None:
    if text == "latest":
        return None
    if len(text) == 10:                                   # a date means the end of that day in Asia/Shanghai
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=B.CST)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("--as-of needs a timezone (e.g. 2026-11-10T15:30:00+08:00)")
    return dt.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.analysis")
    ap.add_argument("--dsn")
    ap.add_argument("--lake")
    ap.add_argument("--slot", default=B.SLOT_KEY)
    ap.add_argument("--min-interval", type=float, default=1.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "daily"):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default="latest")
        p.add_argument("--top-n", type=int, default=10)
        p.add_argument("--legacy-warehouse")
        p.add_argument("--availability", choices=("observed", "reconstructed"), default="observed",
                       help="reconstructed = history rebuild with policy_usable_at (needs an explicit --as-of)")
    sub.choices["daily"].add_argument("--focus-limit", type=int, default=60)
    p = sub.add_parser("open-account")
    p.add_argument("--cash", default=acct.INITIAL_CASH)
    p = sub.add_parser("actionability")
    p.add_argument("--horizon", default="20td")
    p.add_argument("--limit", type=int, default=30)
    sub.add_parser("show")
    _ext_a_parsers(sub)                                                       # EXT-A: tape / outcomes
    return ap


def _cfg(a, conn) -> BuildConfig:
    wh = getattr(a, "legacy_warehouse", None)
    if wh is None:
        from pulse.workers.ingest import DEFAULT_WAREHOUSE
        import os
        env = os.environ.get("PULSE_LEGACY_WAREHOUSE")
        wh = env or (str(DEFAULT_WAREHOUSE) if DEFAULT_WAREHOUSE.exists() else None)
    return BuildConfig(slot_key=a.slot, top_n_plans=getattr(a, "top_n", 10), legacy_warehouse=wh,
                       availability=B.RECONSTRUCTED_POLICY if getattr(a, "availability", "observed") == "reconstructed" else B.AVAILABILITY_POLICY)


def _account_if_open(conn) -> uuid.UUID | None:
    row = conn.execute("SELECT 1 FROM execution.account_head WHERE account_id=%s", (acct.PAPER_ACCOUNT_ID,)).fetchone()
    return acct.PAPER_ACCOUNT_ID if row else None


def _print_result(r) -> None:
    print(json.dumps({"release_id": str(r.release_id), "replayed": r.replayed, "decision_as_of": r.decision_as_of.isoformat(),
                      "session_date": None if r.session_date is None else r.session_date.isoformat(),
                      "pointer_generation": r.pointer_generation, "degraded_components": list(r.degraded),
                      "wall_seconds": round(r.wall_seconds, 1), "counts": r.counts}, ensure_ascii=False, indent=2, default=str))


def _focus_codes(conn, limit: int) -> list[str]:
    cur = PgAnalysisRelease(conn).current(B.SLOT_KEY)
    codes: list[str] = []
    if cur is not None:
        rows = conn.execute(
            "SELECT DISTINCT a.source_symbol FROM decision.trade_plan t JOIN core.instrument_alias a ON a.instrument_id=t.instrument_id"
            " AND a.source_id='legacy_warehouse' WHERE t.based_on_release_id=%s ORDER BY 1 LIMIT %s", (cur.release_id, limit)).fetchall()
        codes = [r["source_symbol"] for r in rows]
    held = conn.execute("SELECT DISTINCT a.source_symbol FROM execution.position_projection p JOIN core.instrument_alias a"
                        " ON a.instrument_id=p.instrument_id AND a.source_id='legacy_warehouse' WHERE p.quantity>0").fetchall()
    return sorted(set(codes) | {r["source_symbol"] for r in held})


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg0 = config.load()
    conn = db.connect(a.dsn or cfg0.dsn, autocommit=True)
    lake = LakeStore(a.lake or cfg0.lake_root)
    client = RateLimitedClient(min_interval_seconds=a.min_interval)
    try:
        if a.cmd == "open-account":
            cal_day = datetime.now(B.CST).date()
            made = acct.open_paper_account(conn, trading_day=cal_day, initial_cash=a.cash)
            print(f"account {acct.PAPER_ACCOUNT_ID} {'opened' if made else 'already open'} (cash {a.cash} CNY, cost profile user-account-2026)")
            return 0
        if a.cmd == "show":
            cur = PgAnalysisRelease(conn).current(a.slot)
            print("no release published" if cur is None else json.dumps(
                {"release_id": str(cur.release_id), "pointer_generation": cur.pointer_generation, "decision_as_of": cur.decision_as_of.isoformat(),
                 "published_at": cur.published_at.isoformat(), "degraded": list(cur.degraded_components)}, indent=2))
            return 0
        if a.cmd in EXT_A_COMMANDS:                                              # EXT-A
            return _ext_a_run(a, conn, lake)
        if a.cmd == "actionability":
            return _actionability(a, conn, lake, client)
        if a.cmd == "daily":
            svc = DataService(conn, lake)
            svc.bootstrap()
            print(svc.ingest_announcements(CninfoAdapter(client), days=1).render())
            codes = _focus_codes(conn, a.focus_limit)
            if codes:
                print(svc.ingest_daily(SinaAdapter(client), "sina", codes, count=10).render())
            print(f"http_requests: {client.requests_made}")
        r = build_release(conn, lake, as_of=parse_as_of(getattr(a, "as_of", "latest")), cfg=_cfg(a, conn), account_id=_account_if_open(conn))
        _print_result(r)
        return 0
    except SourceBlocked as exc:
        print(f"BLOCKED: {exc}; stopped immediately, nothing built", file=sys.stderr)
        return 2
    except (BuildFailed, RawCoverageMissing) as exc:
        print(f"NOT PUBLISHED: {exc}", file=sys.stderr)
        return 1
    except BuildInProgress as exc:
        print(f"BUSY: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
        conn.close()


def _actionability(a, conn, lake, client) -> int:
    cur = PgAnalysisRelease(conn).current(a.slot)
    if cur is None:
        print("no published release", file=sys.stderr)
        return 1
    account_id = _account_if_open(conn)
    if account_id is None:
        print("open the paper account first (open-account)", file=sys.stderr)
        return 1
    run_row = conn.execute("SELECT ranking_id FROM decision.ranking_run WHERE release_id=%s AND ranking_scope='market' AND horizon_id=%s",
                           (cur.release_id, a.horizon)).fetchone()
    ent = conn.execute(
        "SELECT e.instrument_id, al.source_symbol code FROM decision.ranking_entry e JOIN core.instrument_alias al ON al.instrument_id=e.instrument_id"
        " AND al.source_id='legacy_warehouse' WHERE e.ranking_id=%s AND e.plan_id IS NOT NULL ORDER BY e.alpha_rank LIMIT %s",
        (run_row["ranking_id"], a.limit)).fetchall()
    svc = DataService(conn, lake)
    quote = acct.publish_quote_snapshot(svc, SinaAdapter(client), {r["instrument_id"]: r["code"] for r in ent})
    series = B.load_raw_series(conn, [r["instrument_id"] for r in ent], datetime.now(B.CST).date(), cur.published_at)
    payload = acct.build_actionability(conn, release_id=cur.release_id, ranking_id=run_row["ranking_id"], account_id=account_id,
                                       quote=quote, raw_series=series, checked_at=datetime.now(timezone.utc))
    from collections import Counter
    print(json.dumps({"actionability_id": payload["actionability_id"], "release_id": payload["release_id"], "account_id": payload["account_id"],
                      "account_version": payload["account_version"], "quote_snapshot_id": payload["quote_snapshot_id"],
                      "entries": len(payload["entries"]), "status": dict(Counter(e["actionability_status"] for e in payload["entries"])),
                      "quote_missing": list(quote.missing), "http_requests": client.requests_made}, indent=2))
    return 0


# ==== EXT-A BEGIN: market tape and ranking outcomes (extension group A) ====
EXT_A_COMMANDS = ("tape", "outcomes")


def _ext_a_parsers(sub) -> None:
    sub.add_parser("tape", help="diff consecutive quote snapshots into the market tape log (idempotent)")
    p = sub.add_parser("outcomes", help="resolve forward outcomes of published rankings whose horizon has been observed")
    p.add_argument("--as-of", default="latest")


def _ext_a_run(a, conn, lake) -> int:
    from pulse.application import market_today_service as MT

    svc = DataService(conn, lake)
    if a.cmd == "tape":
        print(json.dumps(MT.derive_tape(svc, slot=a.slot), indent=2))
        return 0
    as_of = parse_as_of(a.as_of) or datetime.now(timezone.utc)
    print(json.dumps(MT.evaluate_outcomes(svc, outcome_as_of=as_of, slot=a.slot), indent=2))
    return 0
# ==== EXT-A END ====


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
