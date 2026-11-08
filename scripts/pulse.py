"""Portable local workflow. Owns only the child processes started by this invocation."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PYTHON = ROOT / "backend/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(command: list[str], cwd: Path = ROOT) -> None:
    print(f"→ {' '.join(str(part) for part in command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def pnpm() -> str:
    executable = shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm")
    if executable is None:
        raise RuntimeError("Install pnpm with: npm install --global pnpm")
    return executable


def uv_command() -> list[str]:
    executable = shutil.which("uv")
    local = Path.home() / ".local/bin" / ("uv.exe" if os.name == "nt" else "uv")
    if executable:
        return [executable]
    if local.exists():
        return [str(local)]
    if importlib.util.find_spec("uv"):
        return [sys.executable, "-m", "uv"]
    run([sys.executable, "-m", "pip", "install", "--user", "uv"])
    return [sys.executable, "-m", "uv"]


def install() -> None:
    # `akshare` is an optional extra: without it the app still runs on SIM.
    run([*uv_command(), "sync", "--project", "backend", "--extra", "akshare"])
    if not shutil.which("pnpm.cmd" if os.name == "nt" else "pnpm"):
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise RuntimeError("Node.js 22.12+ is required: https://nodejs.org/")
        run([npm, "install", "--global", "pnpm"])
    run([pnpm(), "install", "--frozen-lockfile"], FRONTEND)


def contracts(check: bool = False) -> None:
    suffix = ["--check"] if check else []
    run([str(PYTHON), "-m", "contracts.generate_types", *suffix])
    run([str(PYTHON), "-m", "contracts.export_openapi", *suffix])


def assert_available(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Port {port} is already in use. Stop its owner before starting Pulse.") from exc


def dev() -> None:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 22.12+ is required")
    assert_available(8000)
    assert_available(5173)
    env = {**os.environ, "PULSE_MODE": os.environ.get("PULSE_MODE", "SIM")}
    commands = [
        [str(PYTHON), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        [node, str(FRONTEND / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "5173", "--strictPort"],
    ]
    children: list[subprocess.Popen] = []
    try:
        for command, cwd in zip(commands, [ROOT, FRONTEND], strict=True):
            children.append(subprocess.Popen(command, cwd=cwd, env=env))
        print("Pulse starting · http://127.0.0.1:5173 · Ctrl+C stops both services", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"Service {child.pid} exited with code {child.returncode}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping Pulse…", flush=True)
    finally:
        stop(children)


def stop(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def wait_for(url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.4)
    raise RuntimeError(f"Timed out waiting for {url}. Start the service or check its logs.")


def e2e() -> None:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 22.12+ is required")
    assert_available(8000)
    assert_available(5173)
    # Tests must not write into the developer's watchlist or backtest archive.
    scratch = ROOT / ".work" / "e2e-data"
    scratch.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PULSE_MODE": os.environ.get("PULSE_MODE", "SIM"),
        "PULSE_DATA_DIR": str(scratch),
    }
    children: list[subprocess.Popen] = []
    try:
        children.append(
            subprocess.Popen(
                [str(PYTHON), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"],
                cwd=ROOT,
                env=env,
            )
        )
        children.append(
            subprocess.Popen(
                [node, str(FRONTEND / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "5173", "--strictPort"],
                cwd=FRONTEND,
                env=env,
            )
        )
        wait_for("http://127.0.0.1:8000/api/v1/health")
        wait_for("http://127.0.0.1:5173/")
        run([pnpm(), "test:e2e"], FRONTEND)
    finally:
        stop(children)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pulse local development workflow")
    parser.add_argument("action", choices=["install", "contracts", "check", "test", "e2e", "build", "dev", "sync"])
    namespace, extra = parser.parse_known_args()
    action = namespace.action
    if action == "sync":
        # Everything after `sync` (--kind, --years, --codes) goes to the warehouse CLI.
        if not PYTHON.exists():
            install()
        run([str(PYTHON), "-m", "backend.app.warehouse.sync", *extra])
        return
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if action == "install":
        install()
        contracts()
        return
    if not PYTHON.exists() or not (FRONTEND / "node_modules").exists():
        install()
        contracts()
    if action == "contracts":
        contracts()
    elif action == "check":
        contracts(check=True)
        run([pnpm(), "typecheck"], FRONTEND)
    elif action == "test":
        contracts(check=True)
        run([str(PYTHON), "-m", "pytest", "backend/tests", "-q"])
        run([pnpm(), "typecheck"], FRONTEND)
    elif action == "e2e":
        contracts(check=True)
        e2e()
    elif action == "build":
        contracts(check=True)
        run([pnpm(), "build"], FRONTEND)
    else:
        dev()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
