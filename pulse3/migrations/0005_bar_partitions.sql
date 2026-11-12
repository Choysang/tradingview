-- W02 migration 0005: monthly partitions for market.bar_projection (DB-A21, M0-E03).
-- Reason: 0001 declares bar_projection PARTITION BY RANGE(trade_date) and states "no default partition:
-- an absent partition is an explicit deploy error", but creates no partition, so every insert fails.
-- database_design.md section 2 names bar_projection as the only monthly-partitioned table.
-- The function is idempotent and is also the deployment hook that creates future months.
BEGIN;

CREATE FUNCTION market.ensure_bar_projection_partitions(p_from date, p_through date)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = market, pg_catalog, pg_temp AS $$
DECLARE
    m date := date_trunc('month', p_from)::date;
    created integer := 0;
    part text;
BEGIN
    IF p_through < p_from THEN
        RAISE EXCEPTION 'partition range is reversed: % > %', p_from, p_through;
    END IF;
    -- Serialize concurrent deployers so two sessions never race on CREATE TABLE.
    PERFORM pg_advisory_xact_lock(hashtextextended('market.bar_projection.partitions', 0));
    WHILE m <= p_through LOOP
        part := 'bar_projection_' || to_char(m, 'YYYYMM');
        IF to_regclass(format('market.%I', part)) IS NULL THEN
            EXECUTE format('CREATE TABLE market.%I PARTITION OF market.bar_projection FOR VALUES FROM (%L) TO (%L)',
                           part, m, (m + interval '1 month')::date);
            created := created + 1;
        END IF;
        m := (m + interval '1 month')::date;
    END LOOP;
    RETURN created;
END;
$$;
COMMENT ON FUNCTION market.ensure_bar_projection_partitions(date, date) IS
    'Creates missing monthly partitions of market.bar_projection covering [p_from, p_through]; returns the number created.';

-- Initial hot window 2024-01 .. 2027-12. The deploy job calls the function again for future months.
SELECT market.ensure_bar_projection_partitions(DATE '2024-01-01', DATE '2027-12-31');

COMMIT;
