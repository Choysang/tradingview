"""Batch 6 worker CLI (lane batch6): E093 / E094 historical run, E095 / E096 forward day step, field check, summary.

Usage (from pulse3/)::

    uv run python -m pulse.workers.batch6 history [--as-of ISO] [--no-publish] [--since YYYY-MM-DD] [--bars adjusted|raw]   # one-off run of E093 (H1, H2) and E094 (H3, H4); prints a JSON report
    uv run python -m pulse.workers.batch6 fieldcheck                                                  # auction field check on the recorded sessions before 2026-11-19 (stored once)
    uv run python -m pulse.workers.batch6 forward --date 2026-11-19 [--as-of ISO]                     # decide one session of E095 / E096 and append its rows
    uv run python -m pulse.workers.batch6 summary [--json]                                            # progress toward 40 signal days; descriptive at 20, judged at 40

``forward`` reads the imported recordings (run ``pulse.workers.ingest legacy-intraday --date D`` first) and the daily bars up to the previous session; a decided session is never
rewritten, so a repeated run is a no-op. It makes no network request and never writes to the legacy tree. Sessions before 2026-11-19 are never evaluated.
Exit codes: 0 ok; 2 a step failed; 3 nothing to decide (no complete recording yet / before the first evaluated session).
Environment: ``PULSE_DSN``, ``PULSE_LAKE``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.contracts import to_wire


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.batch6")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("history", help="E093 / E094 historical run")
    p.add_argument("--as-of", default=None)
    p.add_argument("--no-publish", action="store_true")
    p.add_argument("--since", default=None)
    p.add_argument("--bars", choices=("adjusted", "raw"), default="adjusted", help="adjusted = the fusion lane's forward-adjusted research bars (default, about 3 years); raw = raw daily bars")
    sub.add_parser("fieldcheck", help="auction field check (stored once)")
    p = sub.add_parser("forward", help="decide one session of E095 / E096")
    p.add_argument("--date", required=True)
    p.add_argument("--as-of", default=None)
    p = sub.add_parser("summary", help="forward progress")
    p.add_argument("--json", action="store_true")
    return ap


def _as_of(v: str | None) -> datetime:
    if not v:
        return datetime.now(timezone.utc)
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise SystemExit("--as-of needs a timezone offset")
    return t


def _history_report(out: dict) -> dict:
    rows = []
    for h, r in out["results"].items():
        for seg, res in r["segments"].items():
            rows.append({"hypothesis": h, "segment": seg, "signals": res.n_signals, "attempted": res.n_attempted, "filled": res.n_filled, "fill_rate": res.fill_rate,
                         "n_matched": res.n_matched, "signal_days": res.signal_days, "mean_ret_net": res.mean_ret_net, "mean_baseline": res.mean_baseline, "excess": res.excess,
                         "p": res.p_value, "holm_p_adj": out["holm"][h] if seg == "PRE" else None, "blocks": res.blocks, "reliable": res.reliable, "unfilled": dict(res.n_unfilled)})
    return {"rows": rows, "verdicts": {h: {"passed": v.passed, "reason": v.reason} for h, v in out["verdicts"].items()}, "event_counts": out["event_counts"],
            "decision_days": out["decision_days"], "notes": out["notes"], "cap": out["cap"], "snapshot_id": out.get("snapshot_id")}


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    from pulse.application import bf_batch6_reads as RD
    from pulse.application.bf_batch6_service import Batch6Service

    cfg = config.load()
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        svc = Batch6Service(conn, LakeStore(a.lake or cfg.lake_root))
        if a.cmd == "history":
            out = svc.history(as_of=_as_of(a.as_of), publish=not a.no_publish, since=date.fromisoformat(a.since) if a.since else None, source=a.bars)
            print(json.dumps(to_wire(_history_report(out)), ensure_ascii=False))
            return 0
        if a.cmd == "fieldcheck":
            out, new = svc.fieldcheck()
            print(json.dumps({"newly_written": new, **to_wire(out)}, ensure_ascii=False, default=str))
            return 0
        if a.cmd == "forward":
            out = svc.forward(date.fromisoformat(a.date), as_of=_as_of(a.as_of))
            print(json.dumps(to_wire(out), ensure_ascii=False, default=str))
            return 0 if out["decided"] or out.get("resolutions") or str(out.get("reason")).startswith("already") else 3
        ov = RD.overview(conn, svc.lake)
        print(json.dumps(to_wire(ov), ensure_ascii=False, default=str) if a.json else RD.describe_summary(ov))
        return 0
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
