-- W02 migration 0006: commit-ordered delivery log, release optional-component policy, job result binding,
-- and missing immutability/state-machine guards found while exercising 0001-0004 on PostgreSQL 18.
-- Package DDL (0001-0004) stays byte-exact; every addition below cites its reason.
BEGIN;

-- ---------------------------------------------------------------------------------------------
-- 1. Per-scope delivery log for ops.outbox (backs pollUpdates, DB-A10/DB-A11, M1-E03/M4-E03).
-- Reason: ops.outbox has only event_id (uuid), and the spec forbids "id > max-seen" as a commit
-- watermark (sequence values are not commit-ordered). api_bindings.json pollUpdates lists the open item
-- "scope-specific delivery index and retention/reset". The stream counter row is updated inside the
-- inserting transaction, so its row lock is held until COMMIT: sequence numbers are assigned and
-- become visible in commit order with no holes inside a stream. Scope = aggregate_type:aggregate_id.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE ops.outbox_stream (
    stream_name text PRIMARY KEY,                          -- delivery scope, aggregate_type:aggregate_id
    stream_epoch text NOT NULL DEFAULT '1',                -- bumped when the sequence space is rebuilt
    last_sequence bigint NOT NULL DEFAULT 0,               -- highest committed sequence in this epoch
    retained_from_sequence bigint NOT NULL DEFAULT 1,      -- lowest sequence still replayable
    epoch_started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (last_sequence >= 0 AND retained_from_sequence >= 1 AND retained_from_sequence <= last_sequence + 1)
);
CREATE TABLE ops.outbox_position (
    stream_name text NOT NULL REFERENCES ops.outbox_stream,
    stream_epoch text NOT NULL,
    stream_sequence bigint NOT NULL,
    event_id uuid NOT NULL UNIQUE REFERENCES ops.outbox(event_id),   -- an event lives in exactly one stream
    PRIMARY KEY (stream_name, stream_epoch, stream_sequence),
    CHECK (stream_sequence > 0)
);

-- Backfill rows that existed before this migration (none on a fresh database), in recorded order.
-- Runs before the trigger exists so nothing is assigned twice.
DO $$
DECLARE r record; ep text; seq bigint;
BEGIN
    FOR r IN SELECT event_id, aggregate_type || ':' || aggregate_id AS s FROM ops.outbox ORDER BY recorded_at, event_id LOOP
        INSERT INTO ops.outbox_stream(stream_name) VALUES (r.s) ON CONFLICT (stream_name) DO NOTHING;
        UPDATE ops.outbox_stream SET last_sequence = last_sequence + 1
          WHERE stream_name = r.s RETURNING stream_epoch, last_sequence INTO ep, seq;
        INSERT INTO ops.outbox_position(stream_name, stream_epoch, stream_sequence, event_id) VALUES (r.s, ep, seq, r.event_id);
    END LOOP;
END;
$$;

CREATE FUNCTION ops.assign_outbox_position() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog, pg_temp AS $$
DECLARE s text := NEW.aggregate_type || ':' || NEW.aggregate_id; ep text; seq bigint;
BEGIN
    INSERT INTO ops.outbox_stream(stream_name) VALUES (s) ON CONFLICT (stream_name) DO NOTHING;
    UPDATE ops.outbox_stream SET last_sequence = last_sequence + 1
      WHERE stream_name = s RETURNING stream_epoch, last_sequence INTO ep, seq;
    INSERT INTO ops.outbox_position(stream_name, stream_epoch, stream_sequence, event_id) VALUES (s, ep, seq, NEW.event_id);
    RETURN NULL;
END;
$$;
CREATE TRIGGER outbox_assign_position AFTER INSERT ON ops.outbox
    FOR EACH ROW EXECUTE FUNCTION ops.assign_outbox_position();

-- Rebuilding a stream starts a new epoch; clients holding an old cursor must refetch.
CREATE FUNCTION ops.rotate_outbox_epoch(p_stream text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ops, pg_catalog, pg_temp AS $$
DECLARE ep text := left(gen_random_uuid()::text, 8);
BEGIN
    UPDATE ops.outbox_stream SET stream_epoch = ep, last_sequence = 0, retained_from_sequence = 1,
           epoch_started_at = clock_timestamp()
     WHERE stream_name = p_stream;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown outbox stream %', p_stream; END IF;
    RETURN ep;
END;
$$;

-- Reason: outbox content is the business fact; only delivery bookkeeping may change ("same ID, different
-- content must conflict"). 0001 has no guard, so an UPDATE could silently rewrite a delivered event.
CREATE FUNCTION ops.guard_outbox_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.event_id, NEW.event_type, NEW.schema_version, NEW.aggregate_type, NEW.aggregate_id,
           NEW.aggregate_version, NEW.event_ordinal, NEW.occurred_at, NEW.recorded_at, NEW.producer_version,
           NEW.payload, NEW.payload_hash)
       IS DISTINCT FROM
       ROW(OLD.event_id, OLD.event_type, OLD.schema_version, OLD.aggregate_type, OLD.aggregate_id,
           OLD.aggregate_version, OLD.event_ordinal, OLD.occurred_at, OLD.recorded_at, OLD.producer_version,
           OLD.payload, OLD.payload_hash) THEN
        RAISE EXCEPTION 'Outbox event content is immutable; only publication bookkeeping may change';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER outbox_content_immutable BEFORE UPDATE ON ops.outbox
    FOR EACH ROW EXECUTE FUNCTION ops.guard_outbox_content();

-- ---------------------------------------------------------------------------------------------
-- 2. Optional release components with an explicit degradation policy (IMPLEMENTATION_GUIDE section 4).
-- Reason: decision.release only declares required_components. A component outside that list had no
-- declared policy, so "optional" meant "unchecked". Publication now needs a recorded policy per
-- optional component; undeclared components are rejected by the repository.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE decision.release_optional_component (
    release_id uuid NOT NULL REFERENCES decision.release,
    component_key text NOT NULL,
    degradation_policy text NOT NULL,   -- ALLOW_MISSING: MISSING may ship degraded; ALLOW_ANY: MISSING/FAILED/STALE may
    PRIMARY KEY (release_id, component_key),
    CHECK (degradation_policy IN ('ALLOW_MISSING','ALLOW_ANY')),
    CHECK (component_key <> '')
);
CREATE FUNCTION decision.guard_optional_component() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; r decision.release%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.release_id <> OLD.release_id THEN
        RAISE EXCEPTION 'An optional-component policy cannot move to a different release';
    END IF;
    IF TG_OP = 'DELETE' THEN target_id := OLD.release_id; ELSE target_id := NEW.release_id; END IF;
    -- Same lock the PublishService takes, so a policy cannot change under a publication in flight.
    SELECT * INTO r FROM decision.release WHERE release_id = target_id FOR UPDATE;
    IF FOUND THEN
        IF r.release_state <> 'BUILDING' THEN
            RAISE EXCEPTION 'Optional-component policy is frozen once the release left BUILDING';
        END IF;
        IF TG_OP <> 'DELETE' AND NEW.component_key = ANY (r.required_components) THEN
            RAISE EXCEPTION 'Component % is required; it cannot also be optional', NEW.component_key;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER optional_component_guard BEFORE INSERT OR UPDATE OR DELETE ON decision.release_optional_component
    FOR EACH ROW EXECUTE FUNCTION decision.guard_optional_component();

-- ---------------------------------------------------------------------------------------------
-- 3. Job run state machine, resource binding and fenced results (M4-E03).
-- Reason: ops.job_run has lease_epoch but no resource id (JobReceipt.resource_id), no result slot, and
-- 0004's guard only blocks moving the epoch backwards. A terminal run could still be reopened by a stale
-- writer, and a result had no place to be fenced. Heartbeat expiry uses heartbeat_at + the definition's
-- timeout_seconds, so no extra lease column is needed.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE ops.job_run_resource (
    job_run_id uuid PRIMARY KEY REFERENCES ops.job_run,
    resource_type text NOT NULL,                  -- experiment/export/release_stage/... the run produces
    resource_id uuid NOT NULL,                    -- JobReceipt.resource_id, fixed at enqueue time
    result_lease_epoch bigint,                    -- the lease that produced the result (fencing proof)
    output_ref text,
    output_hash core.sha256_hex,
    result_recorded_at timestamptz,
    CHECK (resource_type <> ''),
    CHECK ((result_lease_epoch IS NULL) = (output_ref IS NULL)
       AND (output_ref IS NULL) = (output_hash IS NULL)
       AND (output_hash IS NULL) = (result_recorded_at IS NULL))
);
CREATE INDEX job_run_resource_by_resource ON ops.job_run_resource(resource_type, resource_id);

CREATE FUNCTION ops.guard_job_run_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.run_state IN ('succeeded','failed','cancelled') THEN
        RAISE EXCEPTION 'Job run % is terminal (%) and immutable', OLD.job_run_id, OLD.run_state;
    END IF;
    IF NEW.run_state <> OLD.run_state THEN
        IF NOT ((OLD.run_state = 'queued' AND NEW.run_state IN ('running','cancelled'))
             OR (OLD.run_state = 'running' AND NEW.run_state IN ('queued','succeeded','failed','cancelled'))) THEN
            RAISE EXCEPTION 'Illegal job run transition % -> %', OLD.run_state, NEW.run_state;
        END IF;
        IF NEW.run_state IN ('succeeded','failed','cancelled') AND NEW.lease_epoch <> OLD.lease_epoch THEN
            RAISE EXCEPTION 'A terminal transition keeps the lease epoch of the holder';
        END IF;
    END IF;
    IF NEW.run_state = 'running' THEN
        IF NEW.lease_epoch > OLD.lease_epoch THEN           -- a new claim (first or after lease expiry)
            IF NEW.attempt_count <> OLD.attempt_count + 1 THEN
                RAISE EXCEPTION 'A new lease epoch must count exactly one more attempt';
            END IF;
        ELSIF OLD.run_state = 'queued' THEN
            RAISE EXCEPTION 'Claiming a queued run requires a new lease epoch';
        ELSIF NEW.attempt_count <> OLD.attempt_count THEN
            RAISE EXCEPTION 'Attempt count changes only together with the lease epoch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_run_state_machine BEFORE UPDATE ON ops.job_run
    FOR EACH ROW EXECUTE FUNCTION ops.guard_job_run_state();

CREATE FUNCTION ops.guard_job_run_resource() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run ops.job_run%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Job resource bindings are permanent'; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.job_run_id, NEW.resource_type, NEW.resource_id)
           IS DISTINCT FROM ROW(OLD.job_run_id, OLD.resource_type, OLD.resource_id) THEN
            RAISE EXCEPTION 'A job run keeps its resource binding';
        END IF;
        IF OLD.output_ref IS NOT NULL THEN
            RAISE EXCEPTION 'A recorded job result is write-once';
        END IF;
    END IF;
    IF NEW.output_ref IS NOT NULL THEN
        -- Fencing proof at the database: the result must come from the lease that still owns the run.
        SELECT * INTO run FROM ops.job_run WHERE job_run_id = NEW.job_run_id;
        IF run.run_state <> 'succeeded' OR run.lease_epoch <> NEW.result_lease_epoch THEN
            RAISE EXCEPTION 'Result lease epoch % does not own job run % (state %, epoch %)',
                NEW.result_lease_epoch, NEW.job_run_id, run.run_state, run.lease_epoch;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_run_resource_guard BEFORE INSERT OR UPDATE OR DELETE ON ops.job_run_resource
    FOR EACH ROW EXECUTE FUNCTION ops.guard_job_run_resource();

-- ---------------------------------------------------------------------------------------------
-- 4. Command receipts are immutable until expiry (INTEGRATION_GUIDE section 6).
-- Reason: 0004 declares the retention window but nothing stops an UPDATE of the stored response or a
-- DELETE of a live receipt, which would break "same key, same answer".
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION ops.guard_command_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Command receipts are immutable';
    END IF;
    IF OLD.expires_at > clock_timestamp() THEN
        RAISE EXCEPTION 'An unexpired command receipt cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER command_receipt_guard BEFORE UPDATE OR DELETE ON ops.command_receipt
    FOR EACH ROW EXECUTE FUNCTION ops.guard_command_receipt();

COMMIT;
