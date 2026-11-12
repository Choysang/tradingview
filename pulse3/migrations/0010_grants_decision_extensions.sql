-- 0010: runtime grants for tables added by 0008/0009 (worker writes plan bodies, actionability results,
-- lifecycle events; API reads them through the default SELECT privilege set in 0007).
BEGIN;
GRANT SELECT ON decision.plan_body, decision.actionability_result, strategy.lifecycle_event
    TO pulse_api, pulse_worker, pulse_coordinator;
GRANT INSERT ON decision.plan_body, decision.actionability_result, strategy.lifecycle_event TO pulse_worker;
GRANT ALL ON decision.plan_body, decision.actionability_result, strategy.lifecycle_event TO pulse_migrator;
COMMIT;
