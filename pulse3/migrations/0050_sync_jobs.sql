-- 0050 (legacy backfill lane sync): audit log of the data-sync jobs a user starts from the web (#49, #89).
-- ops.job_run carries state / lease / error code but no parameters of its own and no text of a failed run, so one row per run
-- keeps what an operator needs: the bounded parameters, who asked, start / end, the exit status and the summary text.
-- Rules: one row per job run (written at enqueue by the API, completed by the worker once); a missing value stays NULL.
BEGIN;

CREATE TABLE ops.sync_job_log (
    job_run_id    uuid PRIMARY KEY REFERENCES ops.job_run,
    kind          text NOT NULL CHECK (kind IN ('sync_daily_bars', 'sync_legacy_import', 'sync_reconcile')),
    parameters    jsonb NOT NULL,                 -- the validated, bounded request parameters
    requested_by  uuid NOT NULL,
    requested_at  timestamptz NOT NULL,
    started_at    timestamptz,                    -- set by the worker when it claims the run
    finished_at   timestamptz,
    exit_status   integer,                        -- exit code of the ingest command (0 = ok, 2 = source blocked us, 3 = source unreachable)
    summary       text,                           -- command output (bounded) or the failure message
    CHECK ((finished_at IS NULL) = (exit_status IS NULL))
);
CREATE INDEX sync_job_log_recent ON ops.sync_job_log(requested_at DESC);

GRANT SELECT ON ops.sync_job_log TO pulse_api, pulse_worker, pulse_coordinator;
GRANT INSERT ON ops.sync_job_log TO pulse_api;
GRANT INSERT, UPDATE ON ops.sync_job_log TO pulse_worker;
GRANT ALL ON ops.sync_job_log TO pulse_migrator;

COMMIT;
