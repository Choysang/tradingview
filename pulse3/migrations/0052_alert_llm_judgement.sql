-- 0052 (legacy backfill lane, row #134): append-only ledger of the LLM fallback for undecided news alerts.
-- Ledger first: a 'pending' row is written (and committed) BEFORE the paid call, the call's outcome is appended as one 'result' row of
-- the same call_id afterwards. A pending row without a result row is a call whose outcome was lost (crash): it still counts against the
-- daily cap and the message is never asked again. A failed call (HTTP error, unparsable answer, a storage error) is a result row too:
-- it is the stop-for-the-day marker after a 401/403/429 and keeps the message from ever being asked twice.
-- The model's raw answer text is kept as received minus NUL characters (never an error body: those can echo credentials); the prompt only
-- as a hash. Rules: append-only (no update, no delete); a missing value stays NULL; the API key is never stored.
-- Access: only the worker writes; the API never reads this table (raw_text is model output kept for audit), so pulse_api gets no grant.
BEGIN;

CREATE TABLE notification.alert_llm_judgement (
    judgement_id      uuid PRIMARY KEY,
    seq               bigint GENERATED ALWAYS AS IDENTITY UNIQUE,   -- write order (several rows may share one asked_at)
    call_id           uuid NOT NULL,                       -- the pending row and its result row share it
    phase             text NOT NULL CHECK (phase IN ('pending', 'result')),
    message_key       text NOT NULL CHECK (message_key ~ '^[0-9a-f]{64}$'),   -- sha256(claim_id | normalized headline): one ask per message
    claim_id          uuid NOT NULL,
    claim_revision_id uuid NOT NULL,                       -- the revision that was read when the question was asked
    asked_at          timestamptz NOT NULL,
    model             text NOT NULL,
    prompt_version    text NOT NULL,
    prompt_hash       text NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
    status            text NOT NULL,                       -- pending | ok | unparsed | http_<code> | error_<ExceptionName>
    raw_text          text,                                -- the answer text of a 200 reply (NUL removed), NULL otherwise
    usage             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(usage) = 'object'),
    judgement         jsonb CHECK (judgement IS NULL OR jsonb_typeof(judgement) = 'object'),   -- the validated answer; NULL unless status = ok
    UNIQUE (message_key, phase),                           -- one pending and at most one result row per message, whatever the process
    CHECK ((phase = 'pending') = (status = 'pending')),
    CHECK ((status = 'ok') = (judgement IS NOT NULL)),
    CHECK (phase = 'result' OR (raw_text IS NULL AND judgement IS NULL))
);
CREATE INDEX alert_llm_judgement_day ON notification.alert_llm_judgement(asked_at DESC);
CREATE TRIGGER alert_llm_judgement_immutable BEFORE UPDATE OR DELETE ON notification.alert_llm_judgement
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();

-- notification's default privileges (0040) hand SELECT to the API role on every new table; this table is worker-only.
REVOKE ALL ON notification.alert_llm_judgement FROM pulse_api;
GRANT SELECT ON notification.alert_llm_judgement TO pulse_worker, pulse_coordinator;
GRANT INSERT ON notification.alert_llm_judgement TO pulse_worker;
GRANT ALL ON notification.alert_llm_judgement TO pulse_migrator;

COMMIT;
