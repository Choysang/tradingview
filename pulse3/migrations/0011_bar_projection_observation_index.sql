-- 0011 (accepted from data package proposal P2, 2026-11-11): deleting/revising observations checks the
-- market.bar_projection foreign key per row; without this index every partition is scanned (a 386k-row
-- delete ran > 10 minutes on the dev database).
BEGIN;
CREATE INDEX IF NOT EXISTS bar_projection_observation_idx ON market.bar_projection(observation_id);
COMMIT;
