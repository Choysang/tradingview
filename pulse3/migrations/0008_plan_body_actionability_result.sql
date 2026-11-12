-- 0008 (accepted from decision package proposal 2026-11-11): typed plan bodies and actionability results.
-- 1) Typed T / grid plan bodies referenced by TradePlan.validation.result_ref = 'planbody:<kind>:<sha256>'.
-- 2) The immutable ActionabilitySnapshot result referenced by decision.actionability_snapshot.result_ref
--    ('actionability:<sha256>'), needed for paging entries pinned to actionability_id.
BEGIN;
CREATE TABLE decision.plan_body (
    body_hash core.sha256_hex PRIMARY KEY,      -- content address of the typed body
    body_kind text NOT NULL,                    -- t_plan | grid_plan
    body_version integer NOT NULL,              -- re-anchoring creates a new version, never an update
    supersedes_hash core.sha256_hex REFERENCES decision.plan_body,
    body_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (body_kind IN ('t_plan','grid_plan')),
    CHECK (body_version > 0),
    CHECK ((body_version = 1) = (supersedes_hash IS NULL))
);
CREATE TRIGGER plan_body_immutable BEFORE UPDATE OR DELETE ON decision.plan_body
    FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();

CREATE TABLE decision.actionability_result (
    actionability_id uuid PRIMARY KEY REFERENCES decision.actionability_snapshot,
    result_payload jsonb NOT NULL               -- the ActionabilitySnapshot contract object, incl. account_action_rank
);
CREATE TRIGGER actionability_result_immutable BEFORE UPDATE OR DELETE ON decision.actionability_result
    FOR EACH ROW EXECUTE FUNCTION research.reject_immutable_mutation();
COMMIT;
