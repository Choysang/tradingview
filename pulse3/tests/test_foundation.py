from decimal import Decimal

import pytest

from pulse import contracts
from pulse.adapters.postgres import migrate


def test_contract_counts_match_package_baseline():
    assert len(contracts.v3_schema()["$defs"]) == 142
    ops = [op for p in contracts.openapi()["paths"].values() for op in p.values()]
    assert len(ops) == 31


def test_decimal_wire_form():
    assert contracts.decimal_str(Decimal("-0.000")) == "0"
    assert contracts.decimal_str(Decimal("1E+3")) == "1000"
    assert contracts.decimal_str(Decimal("10.500")) == "10.5"


@pytest.mark.pg
def test_migrations_apply_on_real_pg(pg_dsn):
    import psycopg

    with psycopg.connect(pg_dsn) as c:
        n = c.execute(
            "select count(*) from information_schema.tables where table_schema not in"
            " ('pg_catalog','information_schema','public')"
        ).fetchone()[0]
        applied = [r[0] for r in c.execute("select name from public.schema_migration order by name")]
    assert n >= 80
    assert applied == [p.name for p in migrate.files()]
    # re-apply is a no-op
    assert migrate.apply(pg_dsn) == []
