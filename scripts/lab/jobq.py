"""Exclusive job queue for heavy lab runs: one job at a time, each gets every core, nothing preempts it.

    python jobq.py add <name> [--env K=V ...] -- <program> [args...]   # enqueue (runs in scripts/lab)
    python jobq.py run                                                # drain the queue (single runner)
    python jobq.py ls                                                 # show pending / running / done

Jobs are JSON files in .work/lab/queue/ (NNNN_<name>.pending -> .running -> .done / .failed); each job's
output goes to .work/lab/queue/<NNNN_name>.log. A lock file keeps a second runner from starting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
Q = HERE.parents[1] / ".work" / "lab" / "queue"
Q.mkdir(parents=True, exist_ok=True)
LOCK = Q / "runner.lock"


def add(argv: list[str]) -> None:
    name, rest = argv[0], argv[1:]
    env = {}
    while rest and rest[0] == "--env":
        k, v = rest[1].split("=", 1)
        env[k] = v
        rest = rest[2:]
    if not rest or rest[0] != "--":
        raise SystemExit("usage: jobq.py add <name> [--env K=V ...] -- <program> [args...]")
    n = 1 + max((int(p.name[:4]) for p in Q.glob("[0-9][0-9][0-9][0-9]_*") if p.name[:4].isdigit()), default=0)
    job = Q / f"{n:04d}_{name}.pending"
    job.write_text(json.dumps({"cmd": rest[1:], "env": env, "added": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    print(job.name)


def run() -> None:
    if LOCK.exists():
        pid = LOCK.read_text().strip()
        raise SystemExit(f"a runner already holds {LOCK} (pid {pid}); remove it only if that process is gone")
    LOCK.write_text(str(os.getpid()))
    try:
        while True:
            pending = sorted(Q.glob("*.pending"))
            if not pending:
                print("queue empty", flush=True)
                return
            job = pending[0]
            spec = json.loads(job.read_text(encoding="utf-8"))
            stem = job.name[: -len(".pending")]
            running = job.with_suffix(".running")
            job.rename(running)
            t0 = time.perf_counter()
            print(f"[jobq] start {stem}: {' '.join(spec['cmd'])[:160]}", flush=True)
            with open(Q / f"{stem}.log", "w", encoding="utf-8") as log:
                code = subprocess.call(spec["cmd"], cwd=HERE, env={**os.environ, **spec["env"], "PYTHONIOENCODING": "utf-8"},
                                       stdout=log, stderr=subprocess.STDOUT)
            running.rename(Q / f"{stem}.{'done' if code == 0 else 'failed'}")
            print(f"[jobq] {'done' if code == 0 else f'FAILED ({code})'} {stem} in {time.perf_counter() - t0:.0f}s", flush=True)
    finally:
        LOCK.unlink(missing_ok=True)


def ls() -> None:
    for p in sorted(Q.glob("[0-9][0-9][0-9][0-9]_*")):
        if p.suffix != ".log":
            print(p.name)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ls"
    {"add": lambda: add(sys.argv[2:]), "run": run, "ls": ls}[cmd]()
