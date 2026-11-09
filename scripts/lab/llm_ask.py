"""E092: the server side of the forward-only web-search LLM log (backend venv; no torch, no panel).

`llm_live.py prepare` on the PC writes a jobs file with the day's candidates and prompts. This script calls the model
with the server's own key and appends one row per verdict, so the key never leaves the server.

    python llm_ask.py check <jobs.json>   # validate everything; no network call
    python llm_ask.py run <jobs.json>     # paid: one web-search call per job not answered yet
    python llm_ask.py ping                # does the key work? one free GET of the model list

Each command prints one JSON line. A refusal (late, limit, budget, busy, no_key, bad_key) is an outcome and exits 0.
The key and API error bodies are never printed or logged (a 401 body quotes part of the key).

Guards on paid calls:
- every request is written to calls.jsonl before it is sent, so the limits below also count calls that failed or were
  lost, and they hold across reruns (rows without that ledger are refused: the limits would start again from zero);
- at most MAX_CALLS requests per decision day; a new decision day is refused once MAX_DECISION_DAYS have any request;
- nothing starts at or after `not_after` (the next weekday 09:25, before the entry open);
- HTTP 401, 403 or 429 stops the run at once; one run at a time (a lock file each request refreshes, stale after
  LOCK_STALE_S without one);
- only the pre-registered model and effort are accepted.
The log lives outside the application folder: $PULSE_LLM_DIR, set to C:/pulse/data/llm by daily_forward.py.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time as clock
from typing import Any

import httpx

import llm

ROOT = Path(__file__).resolve().parents[2]
CST = timezone(timedelta(hours=8))
DEADLINE = time(9, 25)
LIVE_MODEL, LIVE_EFFORT = "gpt-6.1-terra", "high"
ALERT_MODEL = "gpt-6-luna"
MODELS_URL = "https://api.openai.com/v1/models"
TOOLS = {"tools": [{"type": "web_search"}]}
ACTIONS = ("buy", "watch", "avoid")
MAX_JOBS, MAX_CALLS, MAX_DECISION_DAYS = 40, 60, 20
ATTEMPTS, WORKERS, CALL_TIMEOUT = 3, 4, 300
LOCK_STALE_S = 9000  # longer than the longest run: 60 calls x 300 s / 4 workers = 4500 s, plus the retry waits
STOP_HTTP = ("401", "403", "429")
JOB_KEYS = ("id", "code", "group", "score", "prompt")
PAYLOAD_KEYS = ("decision_date", "not_after", "model", "effort", "system", "jobs")


def _now() -> datetime:
    return datetime.now(CST)


def _server_key() -> str | None:
    """The one reader of the key file; imported late so the lab venv can use `deadline_after` without the backend."""
    sys.path.insert(0, str(ROOT))
    from backend.app.warehouse.news_alerts import read_api_key
    return read_api_key()


def deadline_after(day: date) -> datetime:
    """The next weekday 09:25. A holiday makes it earlier than it must be, which only skips a day; it never scores late."""
    next_day = day + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return datetime.combine(next_day, DEADLINE, CST)


def live_dir() -> Path:
    return Path(os.getenv("PULSE_LLM_DIR") or ROOT / ".work" / "lab" / "live" / "llm")


def decisions_path() -> Path:
    return live_dir() / "decisions.jsonl"


def calls_path() -> Path:
    return live_dir() / "calls.jsonl"


def lock_path() -> Path:
    return live_dir() / "run.lock"


def read_rows(path: Path) -> list[dict]:
    """Logged rows. A half-written line from a killed run is skipped, even one cut inside a character."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_bytes().split(b"\n"):
        try:
            rows.append(json.loads(line.decode("utf-8")))
        except ValueError:
            continue
    return rows


def spent_calls(path: Path) -> Counter[str]:
    return Counter(r.get("decision_date") for r in read_rows(path))


def load_jobs(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in PAYLOAD_KEYS if k not in payload]
    if missing:
        raise ValueError(f"jobs file lacks {missing}")
    jobs = payload["jobs"]
    if not jobs or len(jobs) > MAX_JOBS:
        raise ValueError(f"{len(jobs)} jobs; expected 1 to {MAX_JOBS}")
    if any(k not in job for job in jobs for k in JOB_KEYS) or len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("a job lacks a field or repeats an id")
    if datetime.fromisoformat(payload["not_after"]).tzinfo is None:
        raise ValueError("not_after has no time zone")
    if (payload["model"], payload["effort"]) != (LIVE_MODEL, LIVE_EFFORT):
        raise ValueError("model or effort differs from the pre-registration")
    return payload


def refusal(payload: dict, rows: list[dict], spent: Counter[str], now: datetime, key: str | None) -> str | None:
    if now >= datetime.fromisoformat(payload["not_after"]):
        return "late"
    if rows and not spent:
        return "ledger_lost"
    day = payload["decision_date"]
    days = set(spent) | {r.get("decision_date") for r in rows}
    if day not in days and len(days) >= MAX_DECISION_DAYS:
        return "limit"
    if spent[day] >= MAX_CALLS:
        return "budget"
    if not key:
        return "no_key"
    if not key.isascii():  # a byte order mark from a PowerShell write; sending it would only fail
        return "bad_key"
    return None


def parse_verdict(text: str) -> dict:
    obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
    p = obj.get("p_outperform")
    if obj.get("action") not in ACTIONS or isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 100:
        raise ValueError("verdict fields out of range")
    return obj


def error_label(exc: Exception) -> str:
    """The HTTP status from `llm.ask`, else the exception type. Never the message: it may quote the API's error body."""
    head = str(exc).split(":", 1)[0]
    return head if isinstance(exc, RuntimeError) and head.isdigit() else type(exc).__name__


class Gate:
    """What may still start: stops on a budget, a deadline or a hard API refusal. Shared by the worker threads.
    A request is on the ledger before it is sent; if the ledger cannot be written, the request is not made."""

    def __init__(self, payload: dict, ledger: Path, lock_file: Path, now: Callable[[], datetime]) -> None:
        self.day, self.deadline = payload["decision_date"], datetime.fromisoformat(payload["not_after"])
        self.ledger, self.lock_file, self.now = ledger, lock_file, now
        self.lock = threading.Lock()
        self.calls = 0
        self.stop: str | None = None

    def take(self, job_id: str) -> bool:
        with self.lock:
            if self.stop is None and self.now() >= self.deadline:
                self.stop = "late"
            if self.stop is None and spent_calls(self.ledger)[self.day] >= MAX_CALLS:  # read again: it is the only count
                self.stop = "budget"
            if self.stop is not None:
                return False
            entry = {"decision_date": self.day, "id": job_id, "at": self.now().isoformat(timespec="seconds")}
            with self.ledger.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry) + "\n")
            os.utime(self.lock_file)  # a run that is still asking is alive; its lock must not look stale
            self.calls += 1
            return True

    def halt(self, reason: str) -> None:
        with self.lock:
            self.stop = self.stop or reason


def _terminate_last_line(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        with path.open("rb+") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.write(b"\n")


def _summary(status: str, payload: dict, **extra: Any) -> dict:
    return {"status": status, "decision_date": payload["decision_date"], "jobs": len(payload["jobs"]), **extra}


@contextmanager
def single_run(path: Path) -> Iterator[bool]:
    """Yields whether this process holds the run lock. An ssh that timed out may leave its run going on the server."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and clock.time() - path.stat().st_mtime > LOCK_STALE_S:
        path.unlink(missing_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        yield False
        return
    try:
        yield True
    finally:
        path.unlink(missing_ok=True)


def check(path: Path, key_loader: Callable[[], str | None] = _server_key, now: Callable[[], datetime] = _now) -> dict:
    """Everything `run` would decide, with no network call."""
    payload = load_jobs(path)
    rows, spent = read_rows(decisions_path()), spent_calls(calls_path())
    key = key_loader()
    done = {r.get("id") for r in rows}
    return _summary("check", payload, refusal=refusal(payload, rows, spent, now(), key), key=bool(key),
                    todo=sum(job["id"] not in done for job in payload["jobs"]), not_after=payload["not_after"],
                    spent_today=spent[payload["decision_date"]], days=len(spent), logged=len(rows))


def _ask_all(payload: dict, jobs: list[dict], key: str, gate: Gate, ask: Callable[..., str], now: Callable[[], datetime],
             sleep: Callable[[float], None]) -> tuple[list[str], Counter[str]]:
    out = decisions_path()
    lock = threading.Lock()
    asked: list[str] = []
    errors: Counter[str] = Counter()

    def one(job: dict) -> None:
        error = "gave_up"
        for attempt in range(ATTEMPTS):
            if not gate.take(job["id"]):
                return
            try:
                verdict = parse_verdict(ask(job["prompt"], model=payload["model"], effort=payload["effort"],
                                            system=payload["system"], extra=TOOLS, key=key, timeout=CALL_TIMEOUT))
            except Exception as exc:  # noqa: BLE001 - any failure of one call is recorded by its label only
                error = error_label(exc)
                if error in STOP_HTTP:
                    gate.halt(error)
                    break
                sleep(5 * (attempt + 1))
                continue
            row = {**verdict, "id": job["id"], "decision_date": payload["decision_date"], "code": job["code"],
                   "group": job["group"], "score": job["score"], "asked_at": now().isoformat(timespec="seconds"),
                   "not_after": payload["not_after"], "model": payload["model"]}
            with lock, out.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                asked.append(job["id"])
            return
        with lock:
            errors[error] += 1

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, jobs))
    return asked, errors


def run(path: Path, ask: Callable[..., str] = llm.ask, key_loader: Callable[[], str | None] = _server_key,
        now: Callable[[], datetime] = _now, sleep: Callable[[float], None] = clock.sleep) -> dict:
    payload = load_jobs(path)
    with single_run(lock_path()) as held:
        if not held:
            return _summary("busy", payload, asked=0, calls=0, logged=len(read_rows(decisions_path())))
        rows, spent = read_rows(decisions_path()), spent_calls(calls_path())
        key = key_loader()
        refused = refusal(payload, rows, spent, now(), key)
        if refused:
            return _summary(refused, payload, asked=0, calls=0, logged=len(rows))
        _terminate_last_line(decisions_path())
        _terminate_last_line(calls_path())
        done = {r.get("id") for r in rows}
        jobs = [job for job in payload["jobs"] if job["id"] not in done]
        gate = Gate(payload, calls_path(), lock_path(), now)
        asked, errors = _ask_all(payload, jobs, key, gate, ask, now, sleep)
    status = "stopped" if gate.stop else "done" if not errors else "partial" if asked else "failed"
    return _summary(status, payload, asked=len(asked), failed=sum(errors.values()), calls=gate.calls, stop=gate.stop,
                    errors=dict(errors), logged=len(rows) + len(asked))


def ping(key_loader: Callable[[], str | None] = _server_key, get: Callable[..., Any] = httpx.get) -> dict:
    """One free request. Reports only the status, the key's length and whether the two models are listed."""
    key = key_loader()
    if not key:
        return {"status": "no_key"}
    if not key.isascii():
        return {"status": "bad_key", "chars": len(key)}
    try:
        r = get(MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    except httpx.HTTPError as exc:
        return {"status": "network", "error": type(exc).__name__}
    ids = {m.get("id") for m in r.json().get("data", [])} if r.status_code == 200 else set()
    return {"status": "ok" if r.status_code == 200 else "refused", "http": r.status_code, "chars": len(key),
            "models": {m: m in ids for m in (ALERT_MODEL, LIVE_MODEL)}}


def main(argv: list[str]) -> None:
    usage = "usage: llm_ask.py check <jobs.json> | run <jobs.json> | ping"
    if len(argv) == 2 and argv[1] == "ping":
        result = ping()
    elif len(argv) == 3 and argv[1] in ("check", "run"):
        result = {"check": check, "run": run}[argv[1]](Path(argv[2]))
    else:
        raise SystemExit(usage)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv)
