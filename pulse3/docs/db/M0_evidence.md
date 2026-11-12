# W02 database evidence (M0-E02, M0-E03, M1-E03, M4-E03)

Environment: PostgreSQL 18.3 (`show server_version` = 18.3) at `127.0.0.1:55432`, Windows 11, Python 3.12 via `uv run`.
Every test below is marked `@pytest.mark.pg` and runs against a freshly created, fully migrated database
(`pg_dsn` fixture); concurrency tests use separate connections/threads against the same server.

## Commands and results

| Command (from `pulse3/`, `PYTHONUTF8=1`) | Exit | Result |
|---|---|---|
| `uv run pytest tests/db -q` (run three times in a row) | 0 | 75 passed each run |
| `uv run pytest tests -q` (whole repository incl. other packages, at time of writing) | 0 | 258 passed, 48 subtests passed |
| `uv run python -m pulse.adapters.postgres.migrate --dsn <fresh db>` (via `tests/db/test_migrations.py`) | 0 | 7 migrations applied in order, second run is a no-op |

Per file: test_release 18, test_outbox 14, test_jobs 11, test_commands 8, test_contract_sql_consistency 6,
test_roles 6, test_partitions 5, test_manifest_lifecycle 4, test_migrations 3 (= 75).

## Migrations added (package files 0001-0004 untouched)

* `0005_bar_partitions.sql` - monthly partitions of `market.bar_projection` 2024-01..2027-12 plus the
  idempotent definer function `market.ensure_bar_projection_partitions` for future months. Reason: 0001
  declares the partitioned parent and forbids a default partition, so no insert could succeed.
* `0006_delivery_release_jobs.sql` - `ops.outbox_stream`/`ops.outbox_position` (commit-ordered, gap-free
  per-scope delivery log, assigned by a definer trigger; backfills pre-existing outbox rows),
  outbox content immutability, `decision.release_optional_component` (explicit degradation policy),
  `ops.job_run_resource` (JobReceipt resource id + write-once, lease-fenced result), job-run state machine
  guard (terminal rows immutable, legal transitions, epoch/attempt coupling), command-receipt immutability.
* `0007_runtime_roles.sql` - NOLOGIN roles `pulse_migrator`, `pulse_api`, `pulse_worker`,
  `pulse_coordinator`; SELECT default privileges; publish functions executable only by the coordinator.

## Evidence IDs covered and the tests that prove them

| Evidence | Requirement slice | Test(s) |
|---|---|---|
| M0-E03 | PG version + real run log | this file; `test_foundation.py::test_migrations_apply_on_real_pg` |
| M0-E03 | core -> ledger -> sources_sectors -> integration order, clean build and upgrade build | `test_migrations.py` (order, repeatability, checksum guard, upgrade from 0001-0004 with backfill) |
| M0-E03 | monthly partitions | `test_partitions.py` (provisioned window, routing and boundaries, missing month is an explicit error, pruning to one partition, concurrent deployers) |
| M0-E03 | role permissions | `test_roles.py` (api read-mostly, worker cannot publish/move pointer, coordinator end to end, every table readable) |
| M0-E03 | unique keys / FKs / key constraints, concurrency constraints | `test_contract_sql_consistency.py` (883 columns, PKs, 100+ FKs, every live FK targets a real unique key), `test_release.py` concurrency tests, `test_jobs.py`, `test_outbox.py` |
| M0-E02 | one numeric/time policy across SQL | `test_contract_sql_consistency.py::test_numeric_and_time_policy_holds_in_every_table` (no float columns, no naive timestamps, only numeric(38,12) and the four package domains) and the column-by-column type/nullability comparison with `integration/all_fields.json` |
| M1-E03 | staged / COMMITTED_UNPUBLISHED / READY state records | `test_manifest_lifecycle.py` |
| M1-E03 | missing required component never yields READY; publish CAS | `test_release.py::test_publish_requires_every_required_component`, `::test_database_rejects_incomplete_ready_even_if_repository_bypassed`, `::test_stale_stage_cannot_replace_newer`, `::test_two_publishers_of_one_stage_yield_one_pointer`, `::test_two_stages_race_older_one_loses_and_pointer_is_consistent`, `::test_begin_stage_waits_for_slot_lock_then_captures_committed_pointer`, `::test_component_write_racing_publication_cannot_alter_ready_release` (DB-A12, DB-A13) |
| M1-E03 | pin/lease and GC vs publish | NOT covered: the package schema has no pin/lease/GC objects (see below) |
| M4-E03 | JobReceipt <-> resource/run id and query operation | `test_jobs.py::test_enqueue_is_idempotent_and_receipt_matches_contract` (validated with `contracts.check("JobReceipt"/"JobRun")`, restart-safe lookup) |
| M4-E03 | completed/failed/cancelled and duplicate command records | `test_jobs.py` (retry budget, LEASE_EXPIRED, cancel, concurrency key, parallel claimers), `test_commands.py` |
| M4-E03 | expired worker cannot overwrite newer result | `test_jobs.py::test_expired_worker_cannot_overwrite_newer_result`, `::test_database_itself_fences_when_repository_is_bypassed` |
| DB-A10/A11 | outbox ack loss, parallel holes | `test_outbox.py::test_duplicate_delivery_runs_effect_once_and_hash_mismatch_conflicts`, `::test_out_of_order_completion_keeps_contiguous_checkpoint`, `::test_stream_sequence_follows_commit_order_not_start_order`, `::test_parallel_workers_of_one_consumer_apply_each_event_once` |

## Not claimed

* Object pins/leases and GC-vs-publish coordination (M1-E03 third bullet): no table exists for them; belongs
  to the storage adapter package.
* Backoff for retried jobs (`scheduled_for` is immutable in the package schema), cron expansion.
* Rollback publication (package defines forward publication only), restore/reconciliation drill (DB-A22),
  throughput/mixed-workload capacity (DB-A20).
* `core.api_binding` / `core.contract_version` seed rows: tests insert minimal rows; the real registry load
  belongs to W01/integrator.
