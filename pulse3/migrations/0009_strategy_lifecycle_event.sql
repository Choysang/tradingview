-- 0009 (accepted from decision package proposal 2026-11-11): strategy lifecycle events and league result enum.
-- 1) Lifecycle history for steps that have no experiment (discovered->specified->data_ready):
--    research.promotion_decision requires evaluation_experiment_id NOT NULL, so those moves
--    currently have no durable old/new/reason record.
-- 2) League outcome enum: the spec result 'insufficient_evidence' (and 'execution_failed') have no slot in
--    research.league_pairing.outcome; today they are stored as 'pending' with a reason_ref prefix.
BEGIN;
CREATE TABLE strategy.lifecycle_event (
    event_id uuid PRIMARY KEY,
    instance_id uuid NOT NULL REFERENCES strategy.instance,
    from_lifecycle text NOT NULL,
    to_lifecycle text NOT NULL,
    reason text NOT NULL,
    policy_version text NOT NULL,
    revival_ref text,
    promotion_id uuid REFERENCES research.promotion_decision,  -- set when an experiment-backed decision caused it
    decided_at timestamptz NOT NULL,
    CHECK (from_lifecycle IN ('discovered','specified','data_ready','screened','validated','shadow','active','paused','retired')),
    CHECK (to_lifecycle IN ('discovered','specified','data_ready','screened','validated','shadow','active','paused','retired'))
);
CREATE TRIGGER lifecycle_event_immutable BEFORE UPDATE OR DELETE ON strategy.lifecycle_event
    FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();

ALTER TABLE research.league_pairing DROP CONSTRAINT league_pairing_outcome_check;
ALTER TABLE research.league_pairing ADD CONSTRAINT league_pairing_outcome_check
    CHECK (outcome IN ('pending','left','right','tie','bye','incomparable','insufficient_evidence','execution_failed'));
-- contract side: v3.schema.json League...pairings.outcome enum must gain the same two values (Contract Owner).
COMMIT;
