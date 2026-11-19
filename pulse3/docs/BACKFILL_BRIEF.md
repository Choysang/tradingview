# Legacy-feature backfill brief (read first, after docs/BUILD_BRIEF.md)

Goal: bring back useful features of the old Pulse site into pulse3 without lowering pulse3's rules
(user decision 2026-11-13: strict entry, clean exit). The feature list and the reasons are in
`docs/LEGACY_TRIAGE.md`; what each old feature did is described in `docs/LEGACY_FEATURE_INVENTORY.md`
(row numbers `#` match). Rebuild the behaviour from those descriptions. Do not read, import or copy
code from `../backend`, `../frontend` or `../scripts`.

## Contract rules for new reads

- A new read lives in your own router module `src/pulse/api/routes_bf_<lane>.py` with
  `router = APIRouter(prefix="/v1/x/bf/<lane>")`. `pulse.api.app` includes every `routes_bf_*` module
  automatically; do not edit `app.py`.
- Its payload is defined in your own contract file
  `src/pulse/contracts/data/backfill_<lane>.schema.json`:
  `{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:pulse:contracts:backfill:<lane>:v1",
  "x-contract-version": "1.0.0", "$defs": {...}}`. Reuse v3/ext types by absolute `$ref`
  (`urn:pulse:contracts:v3#/$defs/Decimal` etc.). Money/price/ratio are decimal strings, missing is `null`,
  instants are UTC `Z` strings. It is registered at startup by `bootstrap._seed_backfill`.
- Respond with `pulse.api.envelope.respond_backfill("<lane>", "<DefName>", data, meta(...))`. It validates the
  payload; a payload that fails the contract is a bug, never a silent pass.
- Reads that depend on a release take `release_id` and serve only READY/SUPERSEDED releases.
- Commands (writes, job triggers) go through the existing job queue / command store
  (`pulse.application.ops_service`, `PgCommandStore`) with an `Idempotency-Key`, like the ext routes.
- Do not change `v3.schema.json`, `openapi.json`, `extension.*.json` or any existing migration. A schema change
  goes in `migrations/proposals/` and is reported.

## Data rules

Point-in-time only (`available_at` <= decision cutoff); estimated values are labelled estimated; missing is
missing (never 0); one ledger; Decimal for money. Indicator or statistic values are computed on the server; the
browser only draws them.

## Web rules

- Your pages and components only; shared files (`apps/web/src/App.tsx`, `components/Shell.tsx`,
  `routes/*.tsx`, `config.ts`, `styles.css`) belong to the integrator unless your lane owns them. If you need a
  new route or nav entry, export the page component and say so in your report.
- API calls through the existing fetch helpers (see `apps/web/src/api/` and `pages/ext/*/api.ts`).
- Mock data for `pnpm dev:mock` must pass the same contract (`mock:validate`).

## Tests and report

- Python: tests in `tests/bf_<lane>/`, real PostgreSQL for anything persisted (`pg_dsn` / `fresh_dsn` fixtures).
- Web: vitest next to the existing tests; `npx tsc --noEmit` must pass for your files.
- Other lanes edit other files at the same time. A failure in a file you do not own is not yours: report it, do
  not fix it.
- Final report: files changed, commands with exit codes and test counts, which triage rows (#) are done and how
  they were verified, what is not done.

## Batch 3 (research tools) additions

Lanes: `lab`, `fwdlog`, `fusion` (see `docs/LEGACY_TRIAGE.md`, batch 3). Each lane owns `routes_bf_<lane>.py`,
`backfill_<lane>.schema.json`, `src/pulse/application/bf_<lane>_service.py` (more modules under its own names are fine),
`src/pulse/workers/<lane>.py` for its CLI, `tests/bf_<lane>/`, `apps/web/src/pages/bf/<lane>/`.

- The old lab scripts and the pre-registration documents are the SPECIFICATION of a ported tool. Read them to learn the
  exact rules; do not import them, do not copy their code. Frozen inputs that are data (factor expression strings, the mined
  pool JSON, AlphaGen expressions, seed lists, thresholds) are copied into `src/pulse/<lane>_data/` (or under the lane's
  package) with their sha256 recorded in a manifest file and in the test.
- A forward experiment keeps its pre-registered rules unchanged. It is registered in the pulse3 research registry
  (`domain/research/experiments.py`, purpose `shadow`) with the pre-registration document's sha256.
- Reconciliation is the acceptance test: a tool `reconcile` command compares pulse3 output with the old output file for every
  common day and prints each difference with a reason. Unexplained differences = not done.
- Old outputs for reconciliation: PC `.work/lab/live/*.jsonl|parquet`, `.work/lab/main/`; server `C:\pulse\logs`,
  `C:\pulse\data\...`. `.work/` and server paths are private: never commit their content, never publish it.
- Server budget: 2 CPU, 8 GB RAM shared with PostgreSQL and two web apps. A daily step must stay under 2.5 GB and 30 min.
- Do not edit `deploy/windows/*.ps1`, `app.py`, `App.tsx`, `Shell.tsx`, `routes/*.tsx`: report the daily step and the
  route you need; the integrator wires them.
