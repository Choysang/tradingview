# M5-E02 recovery drill on the target host (2026-11-13)

Host: the production Windows server (2 vCPU, 8 GB), pulse3 PostgreSQL service `pulse3-pg`, API task `Pulse3Api`.
Script: four drills run in sequence after the market close. Only pulse3 was touched; the old site (8100) kept running.

| Drill | What was done | First run (r7) | After fixes (r8) |
|---|---|---|---|
| D1 database restart | `Restart-Service pulse3-pg`, then read `/app-config.json` | **500** on the first request (stale pooled connection) | 200 (`main`) |
| D2 API process killed | `Stop-Process` on the uvicorn process | **stayed down 363 s** (the task only restarts on its own failure) | back after 11 s (supervisor loop in `run_api.ps1`) |
| D3 source interruption | `live-daily` with an unreachable proxy, then a normal run | exit 1 with a traceback; recovery run exit 0 | exit 3 `SOURCE UNREACHABLE ... cursor unchanged`; recovery run exit 0 (manifest v39, 2 requests) |
| D4 idempotent rebuild | `analysis build --as-of latest` twice | new release (inputs changed by D3) | second run `replayed: true`, same release `f068799b` |

Fixes (commit 89dfbcd): the API pool checks each connection before use (`ConnectionPool.check_connection`);
`run_api.ps1` restarts uvicorn with a 5-60 s backoff; the ingest CLI maps `TransportFailure` to exit code 3.
Regression tests: `tests/api/test_pool_recovery.py` (terminates every backend, the next request is 200) and
`tests/data/test_raw_coverage.py::test_unreachable_source_exits_3_without_traceback`.

Not covered by this drill: full-day load and latency under the intraday schedule (pulse3 has no intraday recorder yet),
disk-full and lake corruption, and a host reboot (the tasks start at boot; not exercised today).

## Raw output (r8)

```text
[05:48:31] D1 database restart
  before: api 200
  [05:48:32] service Running
  [05:48:35] after: api 200
  [05:48:36] app-config read after restart: main
[05:48:36] D2 API process killed
  killed pid 5616; api down
  [05:48:52] api 200 after 11 s (task restart policy)
[05:48:52] D3 source interruption (unreachable proxy) then recovery
  interrupted run exit 3; last line: SOURCE UNREACHABLE: ConnectError: [WinError 10061] 由于目标计算机积极拒绝，无法连接�? batch marked failed, cursor unchanged, rerun to resume
  recovery run exit 0
      manifest_version: 39
    http_requests: 2
[05:48:59] D4 idempotent rebuild
  exit 0
      "release_id": "f068799b-79a0-5316-abb8-9e21334f15b0",
      "replayed": false,
  second run exit 0
      "release_id": "f068799b-79a0-5316-abb8-9e21334f15b0",
      "replayed": true,
[05:50:41] done; api 200
```
