-- Pulse v3 PostgreSQL ledger migration seed. Not a full OMS or statutory ledger.
-- Requires PostgreSQL with xid8 and pg_current_xact_id(). Execute in one migration.
-- Cross-domain instrument/order IDs intentionally have no fabricated FK target.
BEGIN;
CREATE SCHEMA IF NOT EXISTS execution;

CREATE TABLE execution.account_head (
    account_id uuid PRIMARY KEY,
    base_currency char(3) NOT NULL DEFAULT 'CNY' CHECK (base_currency = 'CNY'),
    current_sequence bigint NOT NULL DEFAULT 0 CHECK (current_sequence >= 0),
    trading_date date NOT NULL,
    cash_reuse_policy text NOT NULL CHECK (cash_reuse_policy IN ('sale_proceeds_reusable','settled_only')),
    ledger_policy_version text NOT NULL,
    numeric_policy_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE execution.account_event (
    account_id uuid NOT NULL REFERENCES execution.account_head(account_id),
    account_sequence bigint NOT NULL CHECK (account_sequence >= 0),
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    source_event_id text NOT NULL,
    source_id text NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    effective_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    market_rules_version text NOT NULL,
    cost_policy_version text NOT NULL,
    execution_policy_version text NOT NULL,
    input_manifest_id text NOT NULL,
    ledger_policy_version text NOT NULL,
    numeric_policy_version text NOT NULL,
    correction_of_event_id uuid,
    PRIMARY KEY(account_id, account_sequence),
    CHECK ((account_sequence = 0) = (event_type = 'opening')),
    UNIQUE(account_id, event_id),
    UNIQUE(account_id, source_id, source_event_id),
    FOREIGN KEY(account_id, correction_of_event_id) REFERENCES execution.account_event(account_id,event_id)
);
CREATE INDEX account_event_effective_idx ON execution.account_event(account_id,effective_at,account_sequence);

CREATE TABLE execution.journal_entry (
    account_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    event_id uuid NOT NULL,
    account_sequence bigint NOT NULL,
    currency char(3) NOT NULL CHECK(currency = 'CNY'),
    effective_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ledger_policy_version text NOT NULL,
    numeric_policy_version text NOT NULL,
    reversal_of_entry_id uuid,
    created_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
    PRIMARY KEY(account_id,entry_id),
    UNIQUE(account_id,event_id),
    FOREIGN KEY(account_id,event_id) REFERENCES execution.account_event(account_id,event_id),
    FOREIGN KEY(account_id,account_sequence) REFERENCES execution.account_event(account_id,account_sequence),
    FOREIGN KEY(account_id,reversal_of_entry_id) REFERENCES execution.journal_entry(account_id,entry_id)
);
CREATE INDEX journal_entry_effective_idx ON execution.journal_entry(account_id,effective_at,account_sequence);

CREATE TABLE execution.journal_line (
    account_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    line_no smallint NOT NULL CHECK(line_no > 0),
    ledger_account text NOT NULL,
    instrument_id uuid,
    currency char(3) NOT NULL CHECK(currency = 'CNY'),
    debit numeric(38,12) NOT NULL DEFAULT 0,
    credit numeric(38,12) NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id,entry_id,line_no),
    FOREIGN KEY(account_id,entry_id) REFERENCES execution.journal_entry(account_id,entry_id),
    CHECK(debit::text NOT IN ('NaN','Infinity','-Infinity') AND credit::text NOT IN ('NaN','Infinity','-Infinity')),
    CHECK(debit >= 0 AND credit >= 0 AND ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)))
);
CREATE INDEX journal_account_idx ON execution.journal_line(account_id,ledger_account,currency);

CREATE FUNCTION execution.freeze_journal_creation_xid() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_xid := pg_current_xact_id();
    -- event_id and sequence must resolve to the SAME account event.
    IF NOT EXISTS (SELECT 1 FROM execution.account_event e WHERE e.account_id=NEW.account_id
       AND e.event_id=NEW.event_id AND e.account_sequence=NEW.account_sequence) THEN
        RAISE EXCEPTION 'journal event/sequence mismatch';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER journal_creation_xid BEFORE INSERT ON execution.journal_entry
FOR EACH ROW EXECUTE FUNCTION execution.freeze_journal_creation_xid();

CREATE FUNCTION execution.check_new_journal_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent execution.journal_entry%ROWTYPE;
BEGIN
    SELECT * INTO STRICT parent FROM execution.journal_entry
    WHERE account_id=NEW.account_id AND entry_id=NEW.entry_id;
    IF parent.created_xid <> pg_current_xact_id() THEN
        RAISE EXCEPTION 'cannot append lines to a committed journal entry';
    END IF;
    IF NEW.currency <> parent.currency THEN
        RAISE EXCEPTION 'journal currency mismatch';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER journal_new_line BEFORE INSERT ON execution.journal_line
FOR EACH ROW EXECUTE FUNCTION execution.check_new_journal_line();

CREATE FUNCTION execution.check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint; d numeric; c numeric;
BEGIN
    SELECT count(*),coalesce(sum(debit),0),coalesce(sum(credit),0)
      INTO n,d,c FROM execution.journal_line
      WHERE account_id=NEW.account_id AND entry_id=NEW.entry_id;
    IF n < 2 OR d <> c THEN
        RAISE EXCEPTION 'unbalanced or empty journal entry % lines=% debit=% credit=%',NEW.entry_id,n,d,c;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER journal_balance_on_entry AFTER INSERT ON execution.journal_entry
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION execution.check_entry_balanced();
CREATE CONSTRAINT TRIGGER journal_balance_on_line AFTER INSERT ON execution.journal_line
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION execution.check_entry_balanced();

CREATE FUNCTION execution.reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append correction/reversal event; history is immutable';
END $$;
CREATE TRIGGER account_event_immutable BEFORE UPDATE OR DELETE ON execution.account_event
FOR EACH ROW EXECUTE FUNCTION execution.reject_history_mutation();
CREATE TRIGGER journal_entry_immutable BEFORE UPDATE OR DELETE ON execution.journal_entry
FOR EACH ROW EXECUTE FUNCTION execution.reject_history_mutation();
CREATE TRIGGER journal_line_immutable BEFORE UPDATE OR DELETE ON execution.journal_line
FOR EACH ROW EXECUTE FUNCTION execution.reject_history_mutation();

CREATE TABLE execution.position_projection (
    account_id uuid NOT NULL REFERENCES execution.account_head(account_id),
    instrument_id uuid NOT NULL,
    quantity bigint NOT NULL CHECK(quantity >= 0),
    sellable_quantity bigint NOT NULL CHECK(sellable_quantity >= 0),
    reserved_sell_quantity bigint NOT NULL CHECK(reserved_sell_quantity >= 0),
    cost_basis numeric(38,12) NOT NULL CHECK(cost_basis >= 0 AND cost_basis::text NOT IN ('NaN','Infinity','-Infinity')),
    applied_sequence bigint NOT NULL,
    PRIMARY KEY(account_id,instrument_id),
    FOREIGN KEY(account_id,applied_sequence) REFERENCES execution.account_event(account_id,account_sequence),
    CHECK(reserved_sell_quantity <= sellable_quantity AND sellable_quantity <= quantity),
    CHECK(quantity <> 0 OR cost_basis = 0)
);

CREATE TABLE execution.quantity_lot (
    account_id uuid NOT NULL REFERENCES execution.account_head(account_id),
    lot_id uuid NOT NULL,
    instrument_id uuid NOT NULL,
    originating_event_id uuid NOT NULL,
    acquired_on date NOT NULL,
    eligible_on date NOT NULL,
    remaining_quantity bigint NOT NULL CHECK(remaining_quantity >= 0),
    applied_sequence bigint NOT NULL,
    PRIMARY KEY(account_id,lot_id),
    FOREIGN KEY(account_id,originating_event_id) REFERENCES execution.account_event(account_id,event_id),
    FOREIGN KEY(account_id,applied_sequence) REFERENCES execution.account_event(account_id,account_sequence),
    CHECK(eligible_on >= acquired_on)
);
CREATE INDEX quantity_eligibility_idx ON execution.quantity_lot(account_id,instrument_id,eligible_on) WHERE remaining_quantity > 0;

CREATE TABLE execution.order_reservation (
    account_id uuid NOT NULL REFERENCES execution.account_head(account_id),
    order_id uuid NOT NULL,
    instrument_id uuid NOT NULL,
    side text NOT NULL CHECK(side IN('buy','sell')),
    original_quantity bigint NOT NULL CHECK(original_quantity > 0),
    remaining_quantity bigint NOT NULL CHECK(remaining_quantity >= 0),
    reserved_cash numeric(38,12) NOT NULL CHECK(reserved_cash >= 0 AND reserved_cash::text NOT IN ('NaN','Infinity','-Infinity')),
    status text NOT NULL CHECK(status IN('active','filled','cancelled','expired')),
    applied_sequence bigint NOT NULL,
    PRIMARY KEY(account_id,order_id),
    FOREIGN KEY(account_id,applied_sequence) REFERENCES execution.account_event(account_id,account_sequence),
    CHECK(remaining_quantity <= original_quantity),
    CHECK(side <> 'sell' OR reserved_cash = 0),
    CHECK(status = 'active' OR (remaining_quantity = 0 AND reserved_cash = 0))
);
CREATE INDEX active_reservations_idx ON execution.order_reservation(account_id,instrument_id) WHERE status='active';

CREATE VIEW execution.trial_balance AS
SELECT account_id,ledger_account,currency,sum(debit-credit) AS debit_balance
FROM execution.journal_line GROUP BY account_id,ledger_account,currency;

COMMENT ON TABLE execution.account_event IS 'Canonical account events only. Raw broker messages land separately; reconcile source identities and append corrections.';
COMMENT ON TABLE execution.position_projection IS 'Rebuildable projection. Cash and cost truth comes from journal plus quantity events; applied_sequence identifies a complete account version.';
COMMENT ON COLUMN execution.order_reservation.reserved_cash IS 'Commitment, not an expense and not a second economic asset. Last resource check and reservation occur under account lock in one transaction.';
COMMENT ON VIEW execution.trial_balance IS 'Cost-basis trial balance, not marked NAV or withdrawable cash. Valuation and cash-use policies produce those projections.';
COMMIT;
