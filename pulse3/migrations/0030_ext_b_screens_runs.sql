-- 0030 (ext group B, PEX10/PEX11/PEX06): saved screens (D12), frozen screen runs (D13) and their result rows.
-- ui.saved_screen is append-only per version: a save is CAS on the head version and keeps every referenced version.
-- research.screen_run is one frozen scan; only TRUE and UNKNOWN rows are stored (FALSE rows are counted, not stored, so the
-- unknown denominator stays explainable); the result is immutable once the run is 'ready'.
BEGIN;
CREATE TABLE ui.saved_screen (
    screen_id uuid NOT NULL,                 -- stable screen identity
    screen_version integer NOT NULL,         -- 1,2,3...; created with expected_screen_version=0
    principal_id uuid NOT NULL,              -- owner (from the authenticated session, never from the request)
    display_name text NOT NULL,
    ast jsonb NOT NULL,                      -- whitelisted ScreenAst (never a layout blob, never SQL)
    ast_hash core.sha256_hex NOT NULL,
    strategy_instances jsonb NOT NULL DEFAULT '[]'::jsonb,
    universe_policy_version text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (screen_id, screen_version),
    CHECK (screen_version > 0),
    CHECK (jsonb_typeof(ast) = 'object'),
    CHECK (jsonb_typeof(strategy_instances) = 'array')
);
CREATE INDEX saved_screen_owner_idx ON ui.saved_screen (principal_id, screen_id, screen_version DESC);
CREATE TRIGGER saved_screen_immutable BEFORE UPDATE OR DELETE ON ui.saved_screen
    FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();

CREATE TABLE research.screen_run (
    screen_run_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL,
    screen_id uuid,
    screen_version integer,
    ast jsonb NOT NULL,
    ast_hash core.sha256_hex NOT NULL,
    input_document jsonb NOT NULL,           -- universe policy, snapshot refs, decision_as_of, strategy instances
    input_hash core.sha256_hex NOT NULL,
    job_run_id uuid NOT NULL REFERENCES ops.job_run,
    state text NOT NULL,
    result_hash core.sha256_hex,
    error_code text,
    true_count integer,
    unknown_count integer,
    false_count integer,
    evaluated_count integer,
    capability_gaps jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    FOREIGN KEY (screen_id, screen_version) REFERENCES ui.saved_screen,
    CHECK ((screen_id IS NULL) = (screen_version IS NULL)),
    CHECK (state IN ('queued', 'running', 'ready', 'failed', 'cancelled')),
    CHECK (state <> 'ready' OR (result_hash IS NOT NULL AND true_count IS NOT NULL AND unknown_count IS NOT NULL)),
    CHECK (state <> 'failed' OR error_code IS NOT NULL)
);
CREATE INDEX screen_run_owner_idx ON research.screen_run (principal_id, created_at DESC);

CREATE TABLE research.screen_run_row (
    screen_run_id uuid NOT NULL REFERENCES research.screen_run,
    sort_position integer NOT NULL,
    instrument_id uuid NOT NULL,
    condition_value text NOT NULL,
    features jsonb NOT NULL,
    PRIMARY KEY (screen_run_id, sort_position),
    CHECK (sort_position > 0),
    CHECK (condition_value IN ('true', 'false', 'unknown'))
);
CREATE FUNCTION research.guard_screen_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'screen_run is never deleted'; END IF;
    IF OLD.state IN ('ready', 'failed', 'cancelled') THEN RAISE EXCEPTION 'closed screen_run is immutable'; END IF;
    IF NEW.screen_run_id <> OLD.screen_run_id OR NEW.principal_id <> OLD.principal_id OR NEW.ast_hash <> OLD.ast_hash
       OR NEW.input_hash <> OLD.input_hash OR NEW.job_run_id <> OLD.job_run_id THEN
        RAISE EXCEPTION 'screen_run identity and inputs are immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER screen_run_guard BEFORE UPDATE OR DELETE ON research.screen_run
    FOR EACH ROW EXECUTE FUNCTION research.guard_screen_run();
CREATE FUNCTION research.guard_screen_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'screen_run_row is immutable'; END IF;
    SELECT state INTO st FROM research.screen_run WHERE screen_run_id = NEW.screen_run_id;
    IF st NOT IN ('queued', 'running') THEN RAISE EXCEPTION 'rows can only be added while the run is open'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER screen_row_guard BEFORE INSERT OR UPDATE OR DELETE ON research.screen_run_row
    FOR EACH ROW EXECUTE FUNCTION research.guard_screen_row();

GRANT SELECT ON ui.saved_screen, research.screen_run, research.screen_run_row TO pulse_api, pulse_worker, pulse_coordinator;
GRANT INSERT ON ui.saved_screen, research.screen_run TO pulse_api;
GRANT INSERT, UPDATE ON research.screen_run TO pulse_worker;
GRANT INSERT ON research.screen_run_row TO pulse_worker;
GRANT ALL ON ui.saved_screen, research.screen_run, research.screen_run_row TO pulse_migrator;
-- registerStrategyDraft / scheduleTacticReplay write through the API principal
GRANT INSERT ON strategy.definition, strategy.instance, strategy.tag_assignment, research.strategy_parent,
    research.experiment_run TO pulse_api;
COMMIT;
