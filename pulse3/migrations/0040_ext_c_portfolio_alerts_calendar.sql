-- 0040 (extension group C): persistence for product-extension cards PEX05, PEX07, PEX08, PEX12, PEX13, PEX14, PEX15.
-- Objects (D-ids of spec_package/review_v32/product_extension_cards.json):
--   D04 decision.plan_revision_metadata   D05 decision.plan_proposal
--   D08 intelligence.cluster_revision (membership of event clusters, SQL form of pulse.cluster_membership)
--   D09 calendar.snapshot / calendar.event_revision / calendar.event_link (versioned calendar events, PIT links)
--   D10 intelligence.seat_profile (published conditional seat statistics)
--   D15 access.account_grant              D16 execution.broker_import (+ broker_fill_fact: raw facts first)
--   D17 research.opportunity_event        D18 notification.alert_rule(+head)  D19 notification.alert_intent
--   D20 notification.delivery_attempt     D21 notification.destination
--   D23 ui.workspace_revision             D24 ops.export_request              D25 ops.job_run_argument
--   D27 execution.account_readmodel       ops.ext_artifact (immutable store behind every ArtifactRef of these cards)
--   execution.tracked_account / tracked_book_day (LEGACY_FORWARD paper books: registered on the ONE ledger)
-- Rules: append-only versions, no money in float, missing stays NULL, secrets only by reference.
BEGIN;

CREATE SCHEMA IF NOT EXISTS access;
CREATE SCHEMA IF NOT EXISTS notification;
CREATE SCHEMA IF NOT EXISTS calendar;

CREATE OR REPLACE FUNCTION ops.ext_c_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (%)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

-- ---- immutable artifacts (ArtifactRef targets) -----------------------------------------------------------------------
CREATE TABLE ops.ext_artifact (
    ref                text PRIMARY KEY,
    content_hash       text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    contract_id        text NOT NULL,
    schema_version     text NOT NULL,
    owner_principal_id uuid,                              -- NULL = public system artifact
    scope_account_id   uuid,                              -- readable by every principal with a read grant on this account
    body               jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER ext_artifact_immutable BEFORE UPDATE OR DELETE ON ops.ext_artifact
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();

-- ---- D15 account access ------------------------------------------------------------------------------------------------
CREATE TABLE access.account_grant (
    principal_id  uuid NOT NULL,
    account_id    uuid NOT NULL REFERENCES execution.account_head(account_id),
    permission    text NOT NULL CHECK (permission IN ('read', 'plan', 'import')),
    grant_version integer NOT NULL CHECK (grant_version > 0),
    valid_from    timestamptz NOT NULL,
    revoked_at    timestamptz,
    PRIMARY KEY (principal_id, account_id, permission, grant_version),
    CHECK (revoked_at IS NULL OR revoked_at >= valid_from)
);
CREATE TRIGGER account_grant_immutable BEFORE UPDATE OR DELETE ON access.account_grant
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();

-- ---- D16 broker import: raw report first, normalized facts, then ledger events -----------------------------------------
CREATE TABLE execution.broker_import (
    import_id                 uuid PRIMARY KEY,
    account_id                uuid NOT NULL REFERENCES execution.account_head(account_id),
    principal_id              uuid NOT NULL,
    raw_object_id             uuid NOT NULL REFERENCES ingest.raw_object(raw_object_id),
    mapping_version           text NOT NULL,
    request_hash              text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status                    text NOT NULL CHECK (status IN ('received', 'normalized', 'reconciliation_pending', 'applied', 'invalid_format')),
    applied_event_ids         uuid[] NOT NULL DEFAULT '{}',
    reconciliation_result_id  uuid REFERENCES ops.reconciliation_result(reconciliation_id),
    new_account_version       bigint,
    detail                    jsonb NOT NULL DEFAULT '{}',
    received_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (account_id, raw_object_id, mapping_version, request_hash)
);
CREATE TABLE execution.broker_fill_fact (
    account_id          uuid NOT NULL REFERENCES execution.account_head(account_id),
    source_id           text NOT NULL,
    source_execution_id text NOT NULL,
    content_hash        text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    import_id           uuid NOT NULL REFERENCES execution.broker_import(import_id),
    fill                jsonb NOT NULL CHECK (jsonb_typeof(fill) = 'object'),
    status              text NOT NULL CHECK (status IN ('applied', 'pending_order', 'pending_reconciliation', 'conflict', 'correction')),
    reason_code         text,
    account_event_id    uuid,
    received_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, source_id, source_execution_id, content_hash)
);
CREATE INDEX broker_fill_fact_import ON execution.broker_fill_fact(import_id);

-- ---- forward books: paper accounts on the unified ledger, labelled as under test ---------------------------------------
CREATE TABLE execution.tracked_account (
    account_id      uuid PRIMARY KEY REFERENCES execution.account_head(account_id),
    book_key        text NOT NULL UNIQUE,
    tracking_kind   text NOT NULL CHECK (tracking_kind = 'LEGACY_FORWARD'),
    label           text NOT NULL,
    status_label    text NOT NULL DEFAULT '检验中' CHECK (status_label = '检验中'),
    ranking_weight  text NOT NULL DEFAULT 'none' CHECK (ranking_weight = 'none'),
    rule            jsonb NOT NULL,
    frozen_on       date NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE execution.tracked_book_day (
    account_id       uuid NOT NULL REFERENCES execution.tracked_account(account_id),
    trade_date       date NOT NULL,
    release_id       uuid NOT NULL REFERENCES decision.release(release_id),
    ranking_id       uuid,
    planned_buys     jsonb NOT NULL DEFAULT '[]',
    holdings         jsonb NOT NULL DEFAULT '[]',
    nav_cny          numeric(38,12),
    cash_cny         numeric(38,12),
    missing_marks    integer NOT NULL DEFAULT 0 CHECK (missing_marks >= 0),
    account_version  bigint NOT NULL,
    computed_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, trade_date)
);

-- ---- D17 opportunities (observations of intents; no second cash ledger) ------------------------------------------------
CREATE TABLE research.opportunity_event (
    opportunity_id      uuid NOT NULL,
    opportunity_version integer NOT NULL CHECK (opportunity_version > 0),
    account_id          uuid REFERENCES execution.account_head(account_id),
    experiment_id       uuid,
    instrument_id       uuid NOT NULL,
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    intent_artifact     text NOT NULL REFERENCES ops.ext_artifact(ref),
    scenario_id         text NOT NULL,
    state               text NOT NULL CHECK (state IN ('triggered', 'not_ordered', 'ordered', 'partially_filled', 'filled', 'unfilled', 'closed')),
    target_quantity     bigint NOT NULL CHECK (target_quantity >= 0),
    filled_quantity     bigint NOT NULL CHECK (filled_quantity >= 0 AND filled_quantity <= target_quantity),
    reason_code         text,
    account_event_ids   uuid[] NOT NULL DEFAULT '{}',
    available_at        timestamptz NOT NULL,
    PRIMARY KEY (opportunity_id, opportunity_version),
    CHECK (account_id IS NOT NULL OR experiment_id IS NOT NULL)
);
CREATE TRIGGER opportunity_event_immutable BEFORE UPDATE OR DELETE ON research.opportunity_event
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE INDEX opportunity_event_account ON research.opportunity_event(account_id, available_at);

-- ---- D18-D21 notification ---------------------------------------------------------------------------------------------
CREATE TABLE notification.alert_rule (
    rule_id       uuid NOT NULL,
    principal_id  uuid NOT NULL,
    rule_version  integer NOT NULL CHECK (rule_version > 0),
    body          jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    content_hash  text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    updated_at    timestamptz NOT NULL,
    PRIMARY KEY (rule_id, rule_version)
);
CREATE TRIGGER alert_rule_immutable BEFORE UPDATE OR DELETE ON notification.alert_rule
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE TABLE notification.alert_rule_head (            -- the CAS pointer; disabling = a new version with enabled=false
    rule_id         uuid PRIMARY KEY,
    principal_id    uuid NOT NULL,
    current_version integer NOT NULL CHECK (current_version > 0),
    FOREIGN KEY (rule_id, current_version) REFERENCES notification.alert_rule(rule_id, rule_version)
);
CREATE TABLE notification.destination (
    destination_id      uuid NOT NULL,
    principal_id        uuid NOT NULL,
    destination_version integer NOT NULL CHECK (destination_version > 0),
    channel             text NOT NULL CHECK (channel IN ('in_app', 'webhook')),
    endpoint_secret_ref text NOT NULL CHECK (endpoint_secret_ref !~* '^[a-z]+://'),   -- a reference, never the URL
    enabled             boolean NOT NULL,
    label               text NOT NULL,
    updated_at          timestamptz NOT NULL,
    PRIMARY KEY (destination_id, destination_version)
);
CREATE TRIGGER destination_immutable BEFORE UPDATE OR DELETE ON notification.destination
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE TABLE notification.alert_intent (
    alert_id             uuid PRIMARY KEY,
    principal_id         uuid NOT NULL,
    rule_id              uuid NOT NULL,
    rule_version         integer NOT NULL,
    tier                 text NOT NULL CHECK (tier IN ('P0', 'P1', 'P2', 'RULE')),
    dedup_key            text NOT NULL,
    based_on_release_id  uuid,
    evidence_artifact    text NOT NULL REFERENCES ops.ext_artifact(ref),
    raised_at            timestamptz NOT NULL,
    expires_at           timestamptz NOT NULL CHECK (expires_at > raised_at),
    supersedes           uuid REFERENCES notification.alert_intent(alert_id),
    outcome_artifact     text REFERENCES ops.ext_artifact(ref),
    source_claim_revision_id uuid,
    UNIQUE (principal_id, dedup_key)
);
CREATE INDEX alert_intent_owner_time ON notification.alert_intent(principal_id, raised_at DESC);
CREATE TABLE notification.delivery_attempt (
    attempt_id          uuid PRIMARY KEY,
    alert_id            uuid NOT NULL REFERENCES notification.alert_intent(alert_id),
    destination_id      uuid NOT NULL,
    attempt_number      integer NOT NULL CHECK (attempt_number > 0),
    status              text NOT NULL CHECK (status IN ('pending', 'sending', 'accepted', 'confirmed', 'failed', 'unknown', 'expired')),
    provider_message_id  text,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz,
    error_code          text,
    UNIQUE (alert_id, destination_id, attempt_number)
);
CREATE TABLE notification.scan_cursor (
    cursor_name   text PRIMARY KEY,
    last_available_at timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

-- ---- D09 calendar -----------------------------------------------------------------------------------------------------
CREATE TABLE calendar.snapshot (
    snapshot_id   uuid PRIMARY KEY,
    created_at    timestamptz NOT NULL,
    source_id     text NOT NULL,
    event_count   integer NOT NULL CHECK (event_count >= 0),
    content_hash  text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$')
);
CREATE TABLE calendar.event_revision (
    calendar_event_id uuid NOT NULL,
    revision          integer NOT NULL CHECK (revision > 0),
    source_id         text NOT NULL,
    source_event_key  text NOT NULL,
    name              text NOT NULL,
    category          text NOT NULL,
    region            text NOT NULL,
    scheduled_at      timestamptz,
    scheduled_date    date,
    time_precision    text NOT NULL CHECK (time_precision IN ('minute', 'approx_minute', 'date', 'date_range')),
    status            text NOT NULL CHECK (status IN ('scheduled', 'postponed', 'cancelled', 'occurred', 'corrected', 'rule_computed')),
    expected          jsonb,
    actual            jsonb,
    instrument_id     uuid,
    observation_id    uuid,
    raw_object_id     uuid REFERENCES ingest.raw_object(raw_object_id),
    content_hash      text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    available_at      timestamptz NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    notes             jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (calendar_event_id, revision),
    CHECK ((time_precision IN ('minute', 'approx_minute')) = (scheduled_at IS NOT NULL)),
    CHECK (scheduled_at IS NOT NULL OR scheduled_date IS NOT NULL)
);
CREATE TRIGGER calendar_event_revision_immutable BEFORE UPDATE OR DELETE ON calendar.event_revision
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE INDEX calendar_event_when ON calendar.event_revision(scheduled_date, scheduled_at);
CREATE TABLE calendar.event_link (                       -- event -> industry; stocks are resolved point-in-time at read time
    calendar_event_id uuid NOT NULL,
    revision          integer NOT NULL,
    link_rule_version text NOT NULL,
    taxonomy_id       text NOT NULL,
    taxonomy_version  integer NOT NULL,
    node_id           text NOT NULL,
    industry_hint     text NOT NULL,
    theme             text,
    PRIMARY KEY (calendar_event_id, revision, link_rule_version, node_id),
    FOREIGN KEY (calendar_event_id, revision) REFERENCES calendar.event_revision(calendar_event_id, revision)
);

-- ---- D08 cluster membership / D10 seat profile ------------------------------------------------------------------------
CREATE TABLE intelligence.cluster_revision (
    cluster_id        uuid NOT NULL,
    revision          integer NOT NULL CHECK (revision > 0),
    membership_hash   text NOT NULL CHECK (membership_hash ~ '^[0-9a-f]{64}$'),
    claim_revision_ids   uuid[] NOT NULL,
    document_revision_ids uuid[] NOT NULL,
    event_revision_ids   uuid[] NOT NULL,
    instrument_ids    uuid[] NOT NULL DEFAULT '{}',
    independent_origin_count integer NOT NULL CHECK (independent_origin_count >= 0),
    available_at      timestamptz NOT NULL,
    clustering_policy_version text NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (cluster_id, revision),
    UNIQUE (cluster_id, membership_hash)
);
CREATE TRIGGER cluster_revision_immutable BEFORE UPDATE OR DELETE ON intelligence.cluster_revision
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE TABLE intelligence.seat_profile (
    seat_id          uuid NOT NULL,
    seat_version     integer NOT NULL,
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    decision_as_of   timestamptz NOT NULL,
    policy_version   text NOT NULL,
    artifact_ref     text NOT NULL REFERENCES ops.ext_artifact(ref),
    sample_count     integer NOT NULL CHECK (sample_count >= 0),
    limitations      jsonb NOT NULL,
    report_revision_ids uuid[] NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (seat_id, seat_version, window_start, window_end, decision_as_of, policy_version),
    FOREIGN KEY (seat_id, seat_version) REFERENCES intelligence.seat_entity_version(seat_id, seat_version)
);

-- ---- D04 / D05 plans ----------------------------------------------------------------------------------------------------
CREATE TABLE decision.plan_revision_metadata (
    plan_id           uuid NOT NULL,
    plan_version      integer NOT NULL,
    principal_id      uuid,
    origin            text NOT NULL CHECK (origin IN ('system', 'user_edit')),
    change_reason     text NOT NULL,
    trigger_event_id  uuid,
    source_proposal_id uuid,
    PRIMARY KEY (plan_id, plan_version),
    FOREIGN KEY (plan_id, plan_version) REFERENCES decision.trade_plan(plan_id, plan_version),
    CHECK ((origin = 'user_edit') = (principal_id IS NOT NULL))
);
CREATE TRIGGER plan_revision_metadata_immutable BEFORE UPDATE OR DELETE ON decision.plan_revision_metadata
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE TABLE decision.plan_proposal (
    proposal_id              uuid PRIMARY KEY,
    principal_id             uuid NOT NULL,
    instrument_id            uuid NOT NULL,
    based_on_release_id      uuid NOT NULL REFERENCES decision.release(release_id),
    base_plan_id             uuid,
    base_plan_version        integer,
    account_id               uuid REFERENCES execution.account_head(account_id),
    expected_account_version bigint,
    request_hash             text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    request_artifact         text NOT NULL REFERENCES ops.ext_artifact(ref),
    job_run_id               uuid NOT NULL REFERENCES ops.job_run(job_run_id),
    model_run_id             uuid,
    state                    text NOT NULL CHECK (state IN ('queued', 'running', 'compiled', 'rejected', 'superseded')),
    reason_code              text,
    compiled_plan_id         uuid,
    compiled_plan_version    integer,
    updated_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((base_plan_id IS NULL) = (base_plan_version IS NULL)),
    CHECK ((account_id IS NULL) = (expected_account_version IS NULL)),
    CHECK ((compiled_plan_id IS NULL) = (compiled_plan_version IS NULL)),
    CHECK (state <> 'compiled' OR compiled_plan_id IS NOT NULL)
);

-- ---- D23 workspace revisions / D24 exports / D25 job arguments -----------------------------------------------------------
CREATE TABLE ui.workspace_revision (
    principal_id       uuid NOT NULL,
    object_kind        text NOT NULL CHECK (object_kind IN ('layout', 'watchlist', 'annotation')),
    object_id          uuid NOT NULL,
    object_version     integer NOT NULL CHECK (object_version > 0),
    operation          text NOT NULL CHECK (operation IN ('upsert', 'delete')),
    body_contract_id   text NOT NULL,
    body_schema_version text NOT NULL,
    body_artifact      text REFERENCES ops.ext_artifact(ref),
    recorded_at        timestamptz NOT NULL,
    PRIMARY KEY (principal_id, object_kind, object_id, object_version),
    CHECK ((operation = 'delete') = (body_artifact IS NULL))
);
CREATE TRIGGER workspace_revision_immutable BEFORE UPDATE OR DELETE ON ui.workspace_revision
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();
CREATE TABLE ops.export_request (
    export_id            uuid PRIMARY KEY,
    principal_id         uuid NOT NULL,
    input_snapshot_refs  jsonb NOT NULL,
    field_projection     jsonb NOT NULL,
    format               text NOT NULL CHECK (format IN ('parquet', 'csv', 'png')),
    export_spec_artifact text NOT NULL REFERENCES ops.ext_artifact(ref),
    job_run_id           uuid NOT NULL REFERENCES ops.job_run(job_run_id),
    output_artifact      text REFERENCES ops.ext_artifact(ref),
    expires_at           timestamptz NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ops.job_run_argument (
    job_run_id            uuid PRIMARY KEY REFERENCES ops.job_run(job_run_id),
    principal_id          uuid NOT NULL,
    parameter_contract_id text NOT NULL,
    parameter_schema_version text NOT NULL,
    parameter_artifact    text NOT NULL REFERENCES ops.ext_artifact(ref),
    parameter_hash        text NOT NULL CHECK (parameter_hash ~ '^[0-9a-f]{64}$'),
    input_snapshot_refs   jsonb NOT NULL,
    release_id            uuid
);
CREATE TRIGGER job_run_argument_immutable BEFORE UPDATE OR DELETE ON ops.job_run_argument
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();

-- a deleted workspace id can never come back (tombstone identity is kept)
CREATE FUNCTION ui.ext_c_forbid_reuse() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE k text := TG_ARGV[0]; oid uuid;
BEGIN
    IF k = 'layout' THEN oid := NEW.layout_id; ELSIF k = 'watchlist' THEN oid := NEW.watchlist_id; ELSE oid := NEW.annotation_id; END IF;
    IF EXISTS (SELECT 1 FROM ui.workspace_revision WHERE object_kind = k AND object_id = oid AND operation = 'delete') THEN
        RAISE EXCEPTION 'deleted workspace id % cannot be reused', oid USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER layout_no_reuse BEFORE INSERT ON ui.workspace_layout FOR EACH ROW EXECUTE FUNCTION ui.ext_c_forbid_reuse('layout');
CREATE TRIGGER watchlist_no_reuse BEFORE INSERT ON ui.watchlist FOR EACH ROW EXECUTE FUNCTION ui.ext_c_forbid_reuse('watchlist');
CREATE TRIGGER annotation_no_reuse BEFORE INSERT ON ui.annotation FOR EACH ROW EXECUTE FUNCTION ui.ext_c_forbid_reuse('annotation');

-- ---- D27 account read models --------------------------------------------------------------------------------------------
CREATE TABLE execution.account_readmodel (
    account_id              uuid NOT NULL REFERENCES execution.account_head(account_id),
    account_version         bigint NOT NULL CHECK (account_version >= 0),
    projection_kind         text NOT NULL CHECK (projection_kind IN ('orders', 'performance')),
    query_context_hash      text NOT NULL CHECK (query_context_hash ~ '^[0-9a-f]{64}$'),
    projection_contract_id  text NOT NULL,
    projection_schema_version text NOT NULL,
    window_start            timestamptz NOT NULL,
    window_end              timestamptz NOT NULL,
    knowledge_as_of         timestamptz NOT NULL,
    input_snapshot_refs     jsonb NOT NULL,
    policy_hash             text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    result_artifact         text NOT NULL REFERENCES ops.ext_artifact(ref),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, account_version, projection_kind, query_context_hash)
);
CREATE TRIGGER account_readmodel_immutable BEFORE UPDATE OR DELETE ON execution.account_readmodel
    FOR EACH ROW EXECUTE FUNCTION ops.ext_c_immutable();

-- ---- grants (runtime roles: API reads everywhere and writes its own commands; worker writes derived rows) -----------------
GRANT USAGE ON SCHEMA access, notification, calendar TO pulse_api, pulse_worker, pulse_coordinator;
GRANT USAGE, CREATE ON SCHEMA access, notification, calendar TO pulse_migrator;
GRANT SELECT ON ALL TABLES IN SCHEMA access, notification, calendar TO pulse_api, pulse_worker, pulse_coordinator;
GRANT ALL ON ALL TABLES IN SCHEMA access, notification, calendar TO pulse_migrator;
ALTER DEFAULT PRIVILEGES IN SCHEMA access, notification, calendar GRANT SELECT ON TABLES TO pulse_api, pulse_worker, pulse_coordinator;
ALTER DEFAULT PRIVILEGES IN SCHEMA access, notification, calendar GRANT ALL ON TABLES TO pulse_migrator;
GRANT SELECT ON ops.ext_artifact, execution.broker_import, execution.broker_fill_fact, execution.tracked_account,
    execution.tracked_book_day, research.opportunity_event, intelligence.cluster_revision, intelligence.seat_profile,
    decision.plan_revision_metadata, decision.plan_proposal, ui.workspace_revision, ops.export_request,
    ops.job_run_argument, execution.account_readmodel TO pulse_api, pulse_worker, pulse_coordinator;
GRANT ALL ON ops.ext_artifact, execution.broker_import, execution.broker_fill_fact, execution.tracked_account,
    execution.tracked_book_day, research.opportunity_event, intelligence.cluster_revision, intelligence.seat_profile,
    decision.plan_revision_metadata, decision.plan_proposal, ui.workspace_revision, ops.export_request,
    ops.job_run_argument, execution.account_readmodel TO pulse_migrator;
-- API commands of these cards
GRANT INSERT ON ops.ext_artifact, decision.plan_proposal, ui.workspace_revision, ops.export_request, ops.job_run_argument,
    notification.alert_rule, notification.alert_rule_head, notification.destination TO pulse_api;
GRANT UPDATE ON notification.alert_rule_head TO pulse_api;
GRANT INSERT ON execution.broker_import, execution.broker_fill_fact TO pulse_api;
GRANT UPDATE (status, applied_event_ids, reconciliation_result_id, new_account_version, detail) ON execution.broker_import TO pulse_api;
GRANT INSERT, UPDATE ON intelligence.seat_profile TO pulse_api;
-- derived rows are written by workers
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA access, notification, calendar TO pulse_worker;
GRANT INSERT, UPDATE ON ops.ext_artifact, execution.broker_import, execution.broker_fill_fact, execution.tracked_account,
    execution.tracked_book_day, research.opportunity_event, intelligence.cluster_revision, intelligence.seat_profile,
    decision.plan_revision_metadata, decision.plan_proposal, ops.export_request, execution.account_readmodel TO pulse_worker;

COMMIT;
