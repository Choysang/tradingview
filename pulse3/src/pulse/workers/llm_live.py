"""Live web-search LLM judgement worker CLI: E100 (``gpt-6-luna``, open) and E092 (``gpt-6.1-terra``, closed by the user 2026-11-23, read and judge only).

Usage (from pulse3/, after ``fusion daily`` in ``run_daily.ps1``; ``--experiment`` defaults to E100)::

    uv run python -m pulse.workers.llm_live decide  --experiment E100 [--date ISO] [--check] [--resume-after-stop]   # after the close: 30 top + 10 control names, one paid web-search call each
    uv run python -m pulse.workers.llm_live probe   --experiment E100 --date 2026-11-20 --n 3 --allow-probe         # up to 3 uncounted cost-measurement calls before go-live
    uv run python -m pulse.workers.llm_live track   --experiment E100 [--today ISO]                                  # score the answers whose 5-day window has closed (append-only)
    uv run python -m pulse.workers.llm_live judge   --experiment E100                                                # the pre-registered verdict, written once
    uv run python -m pulse.workers.llm_live status  --experiment E100                                                # caps, spend estimate, progress, probe; no network
    uv run python -m pulse.workers.llm_live register --experiment E100                                               # the research-registry rows (decide does it on its first paid run too)

Each experiment has its own frozen document (shipped in ``pulse/research_specs`` and checked against its pinned sha256 before ANY command; a missing or different file stops the run), its own
model and its own log (E092 ``llm_live_log``, E100 ``llm_live_log_e100``), so request caps, decision days, the 20-day stop, the judgement and the status are separate; one run at a time (the lane lock
is shared). ``decide`` and ``probe`` on E092 are refused ("closed").
Paid calls need ALL of: the model in ``PULSE_LLM_ALLOWED_MODELS`` (default ``gpt-6-luna`` only; checked before any ledger row or request), the switch file (``PULSE_LLM_LIVE_SWITCH``, default
``<lake parent>/llm_live.enabled``; without it ``decide`` only reports what it would do), a usable key file (``PULSE_OPENAI_KEY_FILE``, plain UTF-8 without BOM; never printed or stored), the day inside
its deadline (next session 09:25 CST) and under the caps: 60 requests per decision day, ``PULSE_LLM_DAILY_USD_CAP`` (default 5.0, an estimate from token usage at the model's prices:
``PULSE_LLM_PRICE_IN_PER_M_GPT_6_LUNA`` / ``..._OUT_PER_M_...`` / ``..._SEARCH_PER_CALL_...`` / ``..._CACHED_IN_PER_M_...`` win over the general ``PULSE_LLM_PRICE_IN_PER_M`` etc.; the unknown-cost
reserve is ``PULSE_LLM_CALL_RESERVE_USD``), ``PULSE_LLM_MAX_OUTPUT_TOKENS`` (default 12000) and 20 decision days in all. 401, 403, 429, a refusal or 5 failures in a row stop the day.
``probe`` is not under the switch file (it runs BEFORE go-live): it needs the explicit ``--allow-probe``; it sends at most 3 calls in all (``probe_done`` after that), writes ``probe`` rows (ledger first),
and prints the usage per call and the estimated cost of a 40-name day at the configured prices; its rows never count toward the caps, the decision days or the score.
Exit codes: 0 done / stopped / complete / check / probed / written; 2 a failure (every call failed, or an exception); 3 refused or nothing to do (closed, model_not_allowed, late, limit, budget, cost_cap,
no_key, bad_key, busy, not_ready, switch_off, probe_not_allowed, probe_done, stopped_today, nothing_due ...). One JSON line on stdout.
Environment: ``PULSE_DSN``, ``PULSE_LAKE``. Not covered: scheduling (``deploy/windows/run_daily.ps1``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from pulse import config
from pulse.adapters.lake import LakeStore
from pulse.adapters.models import responses_client as RC
from pulse.adapters.postgres import db
from pulse.contracts import to_wire
from pulse.domain.llm_live import experiments as X
from pulse.domain.llm_live import rules as R

ENV_SWITCH = "PULSE_LLM_LIVE_SWITCH"
OK_STATUS = ("done", "partial", "stopped", "complete", "check", "tracked", "judged", "progress", "ok", "probed")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pulse.workers.llm_live")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--lake", default=None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--experiment", choices=sorted(X.EXPERIMENTS), default=X.DEFAULT_EXPERIMENT, help=f"default {X.DEFAULT_EXPERIMENT}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decide", parents=[common], help="ask the model about the day's 40 names (paid)")
    p.add_argument("--date", default=None, help="decision day (default today, Asia/Shanghai)")
    p.add_argument("--check", action="store_true", help="validate everything, no request")
    p.add_argument("--resume-after-stop", action="store_true", help="ask again on a day that a 401/403/429, refusal or failure streak stopped (caps still apply)")
    p = sub.add_parser("probe", parents=[common], help="up to 3 uncounted cost-measurement calls on a past day's candidate list (paid)")
    p.add_argument("--date", required=True, help="the day whose candidate list is used (a past decision day)")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--allow-probe", action="store_true", help="really send the calls (without it: report what would be asked)")
    p = sub.add_parser("track", parents=[common], help="score answers whose 5-day window has closed")
    p.add_argument("--today", default=None)
    sub.add_parser("judge", parents=[common], help="the pre-registered verdict, once")
    p = sub.add_parser("status", parents=[common], help="caps, spend estimate and progress")
    p.add_argument("--json", action="store_true")
    sub.add_parser("register", parents=[common], help="register the experiment in the research registry")
    return ap


def switch_path(lake_root: Path) -> Path:
    given = os.environ.get(ENV_SWITCH)
    return Path(given) if given else lake_root.parent / "llm_live.enabled"


def _exit_code(cmd: str, out: dict) -> int:
    status = out.get("status")
    if status in ("failed", "probe_partial"):
        return 2
    return 0 if status in OK_STATUS or (cmd == "register") else 3


def build_service(conn, lake: LakeStore, switch: Path, exp: X.Experiment):
    """The real wiring: the experiment's lake log, fusion-based inputs, the Responses client, the key file."""
    from pulse.application import bf_llm_live_inputs as IN
    from pulse.application import bf_llm_live_registry as REG
    from pulse.application.bf_llm_live_service import LlmLive
    from pulse.application.bf_llm_live_store import open_lake_store

    return LlmLive(open_lake_store(conn, lake, exp.dataset), context=lambda d: IN.load_day_plan(conn, lake, d), labels=lambda ds: IN.load_labels(conn, lake, ds),
                   key_loader=RC.read_key, client_factory=lambda key: RC.ResponsesClient(key), prices=R.prices_from_env(model=exp.model), cap_usd=R.daily_cap_from_env(),
                   max_output_tokens=R.max_output_tokens_from_env(), switch_on=switch.is_file, register=lambda now: REG.register(conn, now, exp), experiment=exp)


def run(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    from pulse.application.bf_llm_live_service import check_document

    exp = X.get(a.experiment)
    cfg = config.load()
    try:
        check_document(exp)
    except Exception as exc:                                  # fail closed: a missing or changed packaged pre-registration stops everything
        print(json.dumps({"status": "preregistration_invalid", "experiment": exp.key, "detail": str(exc)}, ensure_ascii=False))
        return 2
    conn = db.connect(a.dsn or cfg.dsn, autocommit=True)
    try:
        lake = LakeStore(a.lake or cfg.lake_root)
        try:
            svc = build_service(conn, lake, switch_path(cfg.lake_root), exp)
        except ValueError as exc:                             # a malformed price or cap variable: name it, never guess a value
            print(json.dumps({"status": "bad_config", "experiment": exp.key, "detail": str(exc)}, ensure_ascii=False))
            return 3
        if a.cmd == "decide":
            out = svc.decide(date.fromisoformat(a.date) if a.date else None, check_only=a.check, resume_after_stop=a.resume_after_stop)
        elif a.cmd == "probe":
            out = svc.probe(date.fromisoformat(a.date), a.n, allow=a.allow_probe)
        elif a.cmd == "track":
            out = svc.track(date.fromisoformat(a.today) if a.today else None)
        elif a.cmd == "judge":
            out = svc.judge()
        elif a.cmd == "status":
            out = svc.status()
        else:
            from pulse.application import bf_llm_live_registry as REG

            if exp.closed:
                out = {"status": "closed", "experiment": exp.key, "detail": exp.closed}
            else:
                run_row = REG.register(conn, datetime.now(timezone.utc), exp)
                out = {"status": "ok", "experiment": exp.key, "experiment_id": str(run_row["experiment_id"]), "run_state": run_row["run_state"]}
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
