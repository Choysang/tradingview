"""E092 worker CLI: the live web-search LLM judgement of the fusion top 30 (pre-registration docs/research/2026-11-09-e092-llm-live-preregistration.md).

Usage (from pulse3/, after ``fusion daily`` in ``run_daily.ps1``)::

    uv run python -m pulse.workers.llm_live decide [--date ISO] [--check] [--resume-after-stop]   # after the close: 30 top + 10 control names, one paid web-search call each
    uv run python -m pulse.workers.llm_live track [--today ISO]                                   # score the answers whose 5-day window has closed (append-only)
    uv run python -m pulse.workers.llm_live judge                                                 # the pre-registered verdict, written once (20 valid days, or the 20-day limit with all scored)
    uv run python -m pulse.workers.llm_live status [--json]                                       # caps, spend estimate, progress; no network
    uv run python -m pulse.workers.llm_live register                                              # the research-registry rows (done by decide on its first paid run too)

Paid calls need ALL of: the switch file (``PULSE_LLM_LIVE_SWITCH``, default ``<lake parent>/llm_live.enabled``; without it ``decide`` only reports what it would do, as the pre-registration
says), a usable key file (``PULSE_OPENAI_KEY_FILE``, plain UTF-8 without BOM; never printed or stored), the day inside its deadline (next session 09:25 CST) and under the caps: 60 requests
per decision day, ``PULSE_LLM_DAILY_USD_CAP`` (default 5.0, an estimate from token usage: prices ``PULSE_LLM_PRICE_IN_PER_M`` / ``_OUT_PER_M`` / ``_SEARCH_PER_CALL`` and the unknown-cost reserve
``PULSE_LLM_CALL_RESERVE_USD`` are placeholders to confirm; ``PULSE_LLM_MAX_OUTPUT_TOKENS`` bounds one call's output incl. reasoning, default 12000), and 20 decision days in all. 401, 403, 429, a refusal or 5 failures in a row stop the day.
Exit codes: 0 done / stopped / complete / check / written; 2 a failure (every call failed, or an exception); 3 refused or nothing to do (late, limit, budget, cost_cap, no_key, bad_key, busy,
not_ready, switch_off, stopped_today, nothing_due ...). One JSON line on stdout.
Environment: ``PULSE_DSN``, ``PULSE_LAKE``. Not covered: scheduling (``deploy/windows/run_daily.ps1``).
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.models import responses_client as RC
from pulse.adapters.postgres import db
from pulse.contracts import to_wire
from pulse.domain.llm_live import rules as R

ENV_SWITCH = "PULSE_LLM_LIVE_SWITCH"
OK_STATUS = ("done", "partial", "stopped", "complete", "check", "tracked", "judged", "progress", "ok")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.llm_live")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decide", help="ask the model about the day's 40 names (paid)")
    p.add_argument("--date", default=None, help="decision day (default today, Asia/Shanghai)")
    p.add_argument("--check", action="store_true", help="validate everything, no request")
    p.add_argument("--resume-after-stop", action="store_true", help="ask again on a day that a 401/403/429, refusal or failure streak stopped (caps still apply)")
    p = sub.add_parser("track", help="score answers whose 5-day window has closed")
    p.add_argument("--today", default=None)
    sub.add_parser("judge", help="the pre-registered verdict, once")
    p = sub.add_parser("status", help="caps, spend estimate and progress")
    p.add_argument("--json", action="store_true")
    sub.add_parser("register", help="register E092 in the research registry")
    return ap


def switch_path(lake_root: Path) -> Path:
    import os

    given = os.environ.get(ENV_SWITCH)
    return Path(given) if given else lake_root.parent / "llm_live.enabled"


def _exit_code(cmd: str, out: dict) -> int:
    status = out.get("status")
    if status == "failed":
        return 2
    return 0 if status in OK_STATUS or (cmd == "register") else 3


def build_service(conn, lake: LakeStore, switch: Path):
    """The real wiring: lake log, fusion-based inputs, the Responses client, the key file."""
    from pulse.application import bf_llm_live_inputs as IN
    from pulse.application import bf_llm_live_registry as REG
    from pulse.application.bf_llm_live_service import LlmLive
    from pulse.application.bf_llm_live_store import open_lake_store

    return LlmLive(open_lake_store(conn, lake), context=lambda d: IN.load_day_plan(conn, lake, d), labels=lambda ds: IN.load_labels(conn, lake, ds),
                   key_loader=RC.read_key, client_factory=lambda key: RC.ResponsesClient(key), prices=R.prices_from_env(), cap_usd=R.daily_cap_from_env(), max_output_tokens=R.max_output_tokens_from_env(),
                   switch_on=switch.is_file, register=lambda now: REG.register(conn, now))


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    from pulse.application.bf_llm_live_service import check_document

    cfg = config.load()
    try:
        check_document()
    except Exception as exc:                                  # fail closed: a missing or changed packaged pre-registration stops everything
        print(json.dumps({"status": "preregistration_invalid", "detail": str(exc)}, ensure_ascii=False))
        return 2
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        lake = LakeStore(a.lake or cfg.lake_root)
        try:
            svc = build_service(conn, lake, switch_path(cfg.lake_root))
        except ValueError as exc:                             # a malformed price or cap variable: name it, never guess a value
            print(json.dumps({"status": "bad_config", "detail": str(exc)}, ensure_ascii=False))
            return 3
        if a.cmd == "decide":
            out = svc.decide(date.fromisoformat(a.date) if a.date else None, check_only=a.check, resume_after_stop=a.resume_after_stop)
        elif a.cmd == "track":
            out = svc.track(date.fromisoformat(a.today) if a.today else None)
        elif a.cmd == "judge":
            out = svc.judge()
        elif a.cmd == "status":
            out = svc.status()
        else:
            from pulse.application import bf_llm_live_registry as REG

            run_row = REG.register(conn, datetime.now(timezone.utc))
            out = {"status": "ok", "experiment_id": str(run_row["experiment_id"]), "run_state": run_row["run_state"]}
        print(json.dumps(to_wire(out), ensure_ascii=False, default=str))
        return _exit_code(a.cmd, out)
    except Exception:                                         # a failed step is reported with its trace and exit 2 (the schedule continues)
        traceback.print_exc()
        return 2
    finally:
        conn.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
