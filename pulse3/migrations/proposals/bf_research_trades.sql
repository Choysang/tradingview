-- PROPOSAL (not applied, not numbered): per-experiment replay equity and trades, lane `research` (docs/LEGACY_TRIAGE.md #17 #20 #18 #24).
-- Today these are stored as one immutable ops.ext_artifact per completed playbook experiment (contract urn:pulse:contracts:backfill:research:v1,
-- body.version = 'playbook-replay-v1'), found by body->>'experiment_id' with no index. Apply this migration, then backfill from the artifacts
-- (INSERT ... SELECT from jsonb_array_elements(body->'nav') / body->'trades') and switch bf_research_service to the tables.
-- Also proposed: research.experiment_run has no creation column, so the archive sorts by COALESCE(completed_at, started_at, context.created_at);
-- `created_at` below would make "newest first" exact for queued / running rows.

ALTER TABLE research.experiment_run ADD COLUMN created_at timestamptz;             -- NULL for rows registered before this migration
CREATE INDEX experiment_run_created_idx ON research.experiment_run (COALESCE(completed_at, started_at, created_at) DESC, experiment_id DESC);

CREATE TABLE research.replay_equity (
    experiment_id uuid NOT NULL REFERENCES research.experiment_run,
    trading_day   date NOT NULL,
    nav           numeric(38, 12),                       -- NULL = not computable that day (never 0); net of the user cost profile
    PRIMARY KEY (experiment_id, trading_day)
);

CREATE TABLE research.replay_trade (
    experiment_id    uuid NOT NULL REFERENCES research.experiment_run,
    trade_no         integer NOT NULL CHECK (trade_no > 0),           -- order of entry inside the replay
    instrument_id    uuid NOT NULL,
    entry_day        date NOT NULL,
    exit_day         date,
    quantity         bigint NOT NULL CHECK (quantity > 0),
    entry_price      numeric(38, 12) NOT NULL,
    exit_price       numeric(38, 12),
    gross_return     numeric(38, 12),                                 -- exit/entry - 1, gross of fees (fees are in the equity curve)
    holding_sessions integer CHECK (holding_sessions >= 0),
    exit_reason      text,
    regime_at_entry  jsonb,                                           -- {session, trend, liquidity, sentiment, style, basis{}}; NULL = no regime known
    PRIMARY KEY (experiment_id, trade_no),
    CHECK ((exit_day IS NULL) = (exit_price IS NULL)),
    CHECK (exit_day IS NULL OR exit_day > entry_day)
);
CREATE INDEX replay_trade_entry_idx ON research.replay_trade (experiment_id, entry_day DESC);

-- completed experiments are immutable: rows may only be added while the run is not completed
CREATE FUNCTION research.guard_replay_rows() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END IF;
    SELECT run_state INTO st FROM research.experiment_run WHERE experiment_id = NEW.experiment_id;
    IF st IN ('completed', 'failed', 'cancelled') THEN RAISE EXCEPTION 'rows can only be added while the experiment is open'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER replay_equity_guard BEFORE INSERT OR UPDATE OR DELETE ON research.replay_equity FOR EACH ROW EXECUTE FUNCTION research.guard_replay_rows();
CREATE TRIGGER replay_trade_guard  BEFORE INSERT OR UPDATE OR DELETE ON research.replay_trade  FOR EACH ROW EXECUTE FUNCTION research.guard_replay_rows();

GRANT SELECT ON research.replay_equity, research.replay_trade TO pulse_api, pulse_worker, pulse_coordinator;
GRANT INSERT ON research.replay_equity, research.replay_trade TO pulse_worker;
GRANT ALL ON research.replay_equity, research.replay_trade TO pulse_migrator;
