-- W02 migration 0007: runtime roles and grants (M0-E03 "new schemas, roles and permissions").
-- Reason: 0004 says "Runtime roles must use Coordinator/Registry services (or these functions), not direct
-- pointer DML. Deployment must grant only the intended service entry points". This migration is that
-- deployment policy. Roles are NOLOGIN group roles; operators grant them to concrete login roles:
--   pulse_migrator     owner-level DDL role used only by the migration runner
--   pulse_api          read-mostly: SELECT everywhere, user data (ui.*), command receipts, job enqueue, outbox append
--   pulse_worker       ingest/analysis/research writes, outbox append, consumer inbox/checkpoint, job execution;
--                      builds BUILDING releases and components but can never move a publication pointer
--   pulse_coordinator  the PublishService: release slot/stage/publication DML and the two publish functions
-- New tables created by later migrations get SELECT via default privileges; any write grant is added
-- explicitly in that migration and checked by tests/db/test_roles.py.
BEGIN;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['pulse_migrator','pulse_api','pulse_worker','pulse_coordinator'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END;
$$;

GRANT USAGE ON SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    TO pulse_api, pulse_worker, pulse_coordinator;
GRANT USAGE, CREATE ON SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    TO pulse_migrator;
GRANT ALL ON ALL TABLES IN SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    TO pulse_migrator;
GRANT SELECT ON ALL TABLES IN SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    TO pulse_api, pulse_worker, pulse_coordinator;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    GRANT SELECT ON TABLES TO pulse_api, pulse_worker, pulse_coordinator;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, ingest, market, strategy, decision, ops, ui, execution, intelligence, research, analysis
    GRANT ALL ON TABLES TO pulse_migrator;

-- Functions are executable by PUBLIC by default; the publication entry points and DDL helpers are not.
REVOKE EXECUTE ON FUNCTION decision.begin_release_stage(uuid, text, uuid, core.sha256_hex) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION decision.publish_release_stage(uuid, core.sha256_hex, uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION market.ensure_bar_projection_partitions(date, date) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ops.rotate_outbox_epoch(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decision.begin_release_stage(uuid, text, uuid, core.sha256_hex) TO pulse_coordinator, pulse_migrator;
GRANT EXECUTE ON FUNCTION decision.publish_release_stage(uuid, core.sha256_hex, uuid) TO pulse_coordinator, pulse_migrator;
GRANT EXECUTE ON FUNCTION market.ensure_bar_projection_partitions(date, date) TO pulse_worker, pulse_migrator;
GRANT EXECUTE ON FUNCTION ops.rotate_outbox_epoch(text) TO pulse_migrator;

-- ---- pulse_api ----------------------------------------------------------------------------
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ui TO pulse_api;          -- layouts, watchlists, annotations
GRANT INSERT, DELETE ON ops.command_receipt TO pulse_api;                        -- idempotent commands
GRANT INSERT ON ops.outbox TO pulse_api;                                         -- same-transaction events
GRANT INSERT ON ops.job_run, ops.job_run_resource TO pulse_api;                  -- enqueue + JobReceipt
GRANT UPDATE (run_state, completed_at) ON ops.job_run TO pulse_api;              -- cancel a queued run

-- ---- pulse_worker -------------------------------------------------------------------------
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA core, ingest, market, strategy, decision, ui, execution,
    intelligence, research, analysis TO pulse_worker;
-- A worker builds releases; only the coordinator may publish or touch the pointer tables.
REVOKE INSERT, UPDATE ON decision.release_slot, decision.release_stage, decision.release_publication FROM pulse_worker;
REVOKE UPDATE ON decision.release FROM pulse_worker;
GRANT UPDATE (release_state, required_components) ON decision.release TO pulse_worker;   -- FAILED/lock only; READY needs hash+time
GRANT DELETE ON market.bar_projection, market.quote_projection TO pulse_worker;           -- rebuildable hot projections
GRANT INSERT ON ops.outbox TO pulse_worker;
GRANT UPDATE (published_at, publication_attempts, next_attempt_at) ON ops.outbox TO pulse_worker;
GRANT INSERT ON ops.consumer_inbox TO pulse_worker;
GRANT INSERT, UPDATE ON ops.consumer_checkpoint TO pulse_worker;
GRANT DELETE ON ops.outbox_position TO pulse_worker;                                      -- retention of the delivery log
GRANT UPDATE (retained_from_sequence) ON ops.outbox_stream TO pulse_worker;
GRANT INSERT, UPDATE ON ops.job_run, ops.job_run_resource TO pulse_worker;
GRANT INSERT, DELETE ON ops.command_receipt TO pulse_worker;
GRANT INSERT, UPDATE ON ops.reconciliation_result, ops.reconciliation_member, ops.data_issue TO pulse_worker;

-- ---- pulse_coordinator --------------------------------------------------------------------
GRANT INSERT, UPDATE ON decision.release TO pulse_coordinator;
GRANT INSERT, UPDATE, DELETE ON decision.release_optional_component TO pulse_coordinator;
GRANT INSERT, UPDATE ON decision.release_component TO pulse_coordinator;
GRANT INSERT, UPDATE ON decision.release_slot TO pulse_coordinator;
GRANT INSERT ON decision.release_stage, decision.release_publication TO pulse_coordinator;
GRANT INSERT ON ops.outbox TO pulse_coordinator;

COMMIT;
