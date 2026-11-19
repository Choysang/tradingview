"""Cross-process host pacing (PULSE_HOST_PACING_DIR): two real processes share one request budget per host. No network (mock transport)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from pulse.adapters.sources import http as H

WORKER = r"""
import json, sys, time
import httpx
from pulse.adapters.sources.http import RateLimitedClient

host, n, interval = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
stamps = []
def handler(req):
    stamps.append(time.time())
    return httpx.Response(200, content=b"ok")
c = RateLimitedClient(min_interval_seconds=interval, transport=httpx.MockTransport(handler))
for i in range(n):
    c.request(source_id="t", endpoint_id="e", request_key=str(i), url=f"https://{host}/x")
print(json.dumps(stamps))
"""


def _run_two(env: dict, host: str = "qt.gtimg.cn", n: int = 4, interval: float = 0.3) -> list[float]:
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, host, str(n), str(interval)], stdout=subprocess.PIPE, text=True, env=env)
             for _ in range(2)]
    stamps: list[float] = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        stamps += json.loads(out.strip().splitlines()[-1])
    return sorted(stamps)


def test_two_processes_share_one_request_budget_per_host(tmp_path):
    env = {**os.environ, H.PACING_ENV: str(tmp_path / "pacing"), "PYTHONUTF8": "1"}
    stamps = _run_two(env)
    assert len(stamps) == 8
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.3 - 0.03, gaps                      # the processes never overlap within one interval (small timer slack)
    assert (tmp_path / "pacing" / "qt.gtimg.cn.lock").exists()


def test_hosts_do_not_slow_each_other(tmp_path):
    env = {**os.environ, H.PACING_ENV: str(tmp_path), "PYTHONUTF8": "1"}
    t0 = time.time()
    a = subprocess.Popen([sys.executable, "-c", WORKER, "qt.gtimg.cn", "3", "0.3"], stdout=subprocess.PIPE, text=True, env=env)
    b = subprocess.Popen([sys.executable, "-c", WORKER, "hq.sinajs.cn", "3", "0.3"], stdout=subprocess.PIPE, text=True, env=env)
    for p in (a, b):
        p.communicate(timeout=60)
    assert {f.name for f in tmp_path.iterdir()} == {"qt.gtimg.cn.lock", "hq.sinajs.cn.lock"}
    assert time.time() - t0 < 30


def test_without_the_variable_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv(H.PACING_ENV, raising=False)
    slept: list[float] = []
    H.pace_host_across_processes("https://qt.gtimg.cn/x", 1.0, slept.append)
    assert slept == [] and list(tmp_path.iterdir()) == []


def test_a_stamp_from_the_future_waits_at_most_one_interval(tmp_path, monkeypatch):
    monkeypatch.setenv(H.PACING_ENV, str(tmp_path))
    slept: list[float] = []
    H.pace_host_across_processes("https://h.example/x", 5.0, slept.append, wall=lambda: 1000.0)       # first request: stamp 1000
    H.pace_host_across_processes("https://h.example/x", 5.0, slept.append, wall=lambda: 100.0)        # the clock stepped back
    assert slept == [5.0]                                                                                # not 905 s
    assert float((tmp_path / "h.example.lock").read_bytes()[H._STAMP_AT:].decode()) == 100.0
