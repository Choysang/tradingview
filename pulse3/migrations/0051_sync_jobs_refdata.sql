-- 0051 (legacy backfill lane sync): the fourth sync kind, sync_refdata (native reference-data worker), may be logged.
BEGIN;

ALTER TABLE ops.sync_job_log DROP CONSTRAINT sync_job_log_kind_check;
ALTER TABLE ops.sync_job_log ADD CONSTRAINT sync_job_log_kind_check
    CHECK (kind IN ('sync_daily_bars', 'sync_legacy_import', 'sync_reconcile', 'sync_refdata'));

COMMIT;
