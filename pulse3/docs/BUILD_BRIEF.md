# pulse3 build brief (read first)

pulse3 is the clean v3.2 rewrite of Pulse, built strictly from the package in
`spec_package/` (unzipped `Pulse_Rewrite_Package`). The old system lives one
directory up (`../backend`, `../frontend`, `../scripts`). **Never import, copy or
read old code.** Old *data* may be read only by the legacy-import source adapter.

## Authority

`spec_package/authority_manifest.json` lists the authoritative files. Explicit
v3.2 corrections in `spec/REFERENCE_RECONCILIATION_V32.md` override older prose.
Start every package with `spec/FINAL_SYSTEM_SPEC.md`, `spec/IMPLEMENTATION_GUIDE_V3.md`,
`traceability/milestones_v32.json` (exit evidence M0-M6), then the domain files
named in your task. JSON files are UTF-8; on Windows set `PYTHONUTF8=1` or pass
`encoding="utf-8"`.

## Fixed foundation (do not change; ask the integrator)

- Python package `src/pulse`, run with `uv run ...` from `pulse3/`. Add a
  dependency with `uv add <pkg>` only if truly needed and say so in your report.
- Contract: `pulse.contracts` (`check(def_name, payload)`, `to_wire`,
  `decimal_str`, `utc_z`). Data files in `src/pulse/contracts/data/` are the
  package contracts verbatim. Never edit them. Every API payload / fixture you
  produce must pass `contracts.check`.
- Database: PostgreSQL 18.3 local at `postgresql://postgres@127.0.0.1:55432/`.
  Migrations `migrations/0001..0004` are the package DDL verbatim, applied by
  `pulse.adapters.postgres.migrate` (forward only, checksummed). Only the DB work
  package (W02) adds `migrations/0005+`. Others needing a schema change write
  `migrations/proposals/<package>_<topic>.sql` and report it; do not apply ad hoc DDL.
- `pulse.adapters.postgres.db.connect()/transaction()` open connections.
- Tests: pytest under `tests/<package>/`. Fixture `pg_dsn` (tests/conftest.py)
  gives a fresh migrated database; mark such tests `@pytest.mark.pg`. Real PG
  tests are required for persistence claims; static parsing is not evidence.

## Rules that every package keeps

- Money/price/ratio: `Decimal` (context precision 50, ROUND_HALF_EVEN), wire as
  decimal strings. Quantities: integer shares. Instants: tz-aware UTC; session
  dates in Asia/Shanghai. Missing stays `None`/NULL, never 0.
- Instruments use stable UUIDs; `600000.SH` style codes are aliases with validity.
- `available_at` is the real availability time; history rebuild uses
  `policy_usable_at` + `availability_policy_version`. Derived `available_at` =
  max(inputs' available_at, computed_at).
- One economic ledger (`pulse.domain.execution`), one contract, one migration
  sequence. No parallel ledger, no synonym fields.
- Domain modules (`src/pulse/domain/**`) are pure: no psycopg, no HTTP, no SDK.
  IO lives in `src/pulse/adapters/**`; orchestration in `src/pulse/application/**`.
- Each new module starts with a short docstring: why it exists (requirement /
  spec section), invariants, what is not covered.
- Stay inside your `allowed_paths`. Shared files belong to the integrator.

## Report format (your final message)

1. Files created/changed. 2. Commands run with exit codes and test counts.
3. Spec items / milestone evidence IDs (e.g. M2-E01) you satisfied, with the
test that proves each. 4. What is not done or only synthetic. 5. Any proposed
contract/schema change.
