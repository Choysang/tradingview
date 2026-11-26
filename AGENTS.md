# Pulse — agent guide

Only one program remains: **Pulse 3** in `pulse3/`. The old system (`backend/`, `frontend/`, `contracts/`, `scripts/`) was retired on 2026-11-25. Its code is deleted from the tree but kept in git history; a local archive is at `D:\zuomian\idea\pulse_legacy_local_2026-11-25.tgz`, and the server archive is in `C:\pulse3\backup\`. Do not recreate it and do not import from it.

## Where things are

- `pulse3/README.md`: how to run, test and build.
- `pulse3/docs/`:
  - `LEGACY_TRIAGE.md`: what was ported from the old system and what was retired, with reasons.
  - `m6/LEGACY_RETIREMENT.md`: the retirement procedure.
- `pulse3/src/pulse/contracts/`: the single contract source (JSON Schema). The web types are generated from it.
- `pulse3/deploy/windows/`: the server scripts and scheduled tasks. `deploy_app.ps1` deploys a round.
- `docs/research/`: pre-registrations and the experiment ledger `ledger/experiments.jsonl`. Ledger rows are append-only. Frozen pre-registrations are checked by sha256, and the packaged copies are in `pulse3/src/pulse/research_specs/`.
- `tools/publish_github.sh`: publishes a sanitized snapshot of `main` to the public repository. It never force-pushes.

## Rules that stay in force

- Research: pre-register first. The user freezes the document ("定稿"), and changing any frozen setting makes a new experiment id. Use only point-in-time data, with next-open execution. Record results whether they succeed or fail.
- Data collection: at most 1 request per second per host. Stop at the first block and never retry-storm. Never send duplicate requests from the same IP.
- LLM calls use `gpt-6-luna` only (`PULSE_LLM_ALLOWED_MODELS`). Every paid lane has a daily cap and a switch file.
- Secrets live only in the server's ACL-restricted `secrets` folder. Never print them, commit them or publish them. The public repository must never contain the server address, users, keys, `.work/` or private documents.
- Tests: run `uv run pytest -q` from `pulse3/`. Schema changes bump `x-contract-version` and `job_version`.
