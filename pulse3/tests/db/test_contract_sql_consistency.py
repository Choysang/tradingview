"""Live-database check of the package field inventory (M0-E02, M0-E03; DB-A21).

integration/all_fields.json lists every column of the 80 package tables with its Postgres type and
nullability (parsed from the SQL AST by the package authors). This test compares that inventory with
the migrated database, not with the SQL text, and verifies every live foreign key targets a real
unique key. Tables added by migrations 0005+ are exempt from the inventory but not from the FK check.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

INVENTORY = Path(__file__).resolve().parents[2] / "spec_package" / "integration" / "all_fields.json"
TYPE_ALIASES = {
    "timestamptz": "timestamp with time zone",
    "char(3)": "character(3)",
    "numeric(38, 12)": "numeric(38,12)",
}


@pytest.fixture(scope="module")
def inventory():
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


@pytest.fixture
def live_columns(conn):
    rows = conn.execute(
        "SELECT n.nspname || '.' || c.relname AS tbl, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ,"
        " a.attnotnull AS notnull FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid"
        " JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped AND NOT c.relispartition"
        " AND n.nspname NOT IN ('pg_catalog','information_schema','public')").fetchall()
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        out[r["tbl"]][r["col"]] = {"type": r["typ"], "notnull": r["notnull"]}
    return out


def test_inventory_counts_match_package_statement(inventory):
    assert inventory["table_count"] == len(inventory["tables"]) == 80
    assert inventory["field_count"] == sum(len(t["fields"]) for t in inventory["tables"]) == 883


def test_every_declared_column_exists_with_declared_type_and_nullability(inventory, live_columns):
    problems = []
    for t in inventory["tables"]:
        live = live_columns.get(t["table"])
        if live is None:
            problems.append(f"{t['table']}: table missing")
            continue
        declared = {f["name"] for f in t["fields"]}
        if declared != set(live):
            problems.append(f"{t['table']}: column set differs, missing={declared - set(live)}, extra={set(live) - declared}")
        for f in t["fields"]:
            col = live.get(f["name"])
            if col is None:
                continue
            want = TYPE_ALIASES.get(f["postgres_type"], f["postgres_type"])
            if col["type"] != want:
                problems.append(f"{t['table']}.{f['name']}: type {col['type']!r} != declared {want!r}")
            if col["notnull"] != (not f["nullable"]):
                problems.append(f"{t['table']}.{f['name']}: notnull={col['notnull']} != declared nullable={f['nullable']}")
    assert not problems, "\n".join(problems[:40])


def test_declared_primary_keys_exist(inventory, conn):
    rows = conn.execute(
        "SELECT n.nspname || '.' || c.relname AS tbl,"
        " array(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(attnum, ord)"
        "       JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=u.attnum ORDER BY u.ord) AS cols"
        " FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE k.contype='p'").fetchall()
    live = {r["tbl"]: list(r["cols"]) for r in rows}
    bad = [t["table"] for t in inventory["tables"]
           if t["primary_key_columns"] and live.get(t["table"]) != t["primary_key_columns"]]
    assert not bad, bad
    assert all(t["table"] in live for t in inventory["tables"] if t["primary_key_columns"])


def _live_fks(conn):
    return conn.execute(
        "SELECT k.conname, child_n.nspname || '.' || child.relname AS child,"
        " parent_n.nspname || '.' || parent.relname AS parent, k.conrelid, k.confrelid, k.conkey, k.confkey,"
        " array(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(attnum, ord)"
        "       JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=u.attnum ORDER BY u.ord) AS cols,"
        " array(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY u(attnum, ord)"
        "       JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=u.attnum ORDER BY u.ord) AS refcols"
        " FROM pg_constraint k"
        " JOIN pg_class child ON child.oid=k.conrelid JOIN pg_namespace child_n ON child_n.oid=child.relnamespace"
        " JOIN pg_class parent ON parent.oid=k.confrelid JOIN pg_namespace parent_n ON parent_n.oid=parent.relnamespace"
        " WHERE k.contype='f'").fetchall()


def test_every_declared_foreign_key_exists_in_the_live_database(inventory, conn):
    live = {(r["child"], tuple(r["cols"]), r["parent"], tuple(r["refcols"])) for r in _live_fks(conn)}
    missing = []
    declared = 0
    for t in inventory["tables"]:
        seen = set()
        for fk in t["foreign_keys"]:
            key = (t["table"], tuple(fk["columns"]), fk["references_table"], tuple(fk["references_columns"]))
            if key in seen:
                continue
            seen.add(key)
            declared += 1
            if key not in live:
                missing.append(key)
    assert declared >= 100
    assert not missing, missing[:10]


def test_every_live_foreign_key_points_to_an_existing_unique_key(conn):
    uniques = defaultdict(set)      # referenced table oid -> {frozenset(attnums)}
    for r in conn.execute("SELECT conrelid, conkey FROM pg_constraint WHERE contype IN ('p','u')").fetchall():
        uniques[r["conrelid"]].add(frozenset(r["conkey"]))
    for r in conn.execute(
            "SELECT indrelid, indkey::int2[] AS cols FROM pg_index WHERE indisunique AND indpred IS NULL"
            " AND indexprs IS NULL").fetchall():
        uniques[r["indrelid"]].add(frozenset(r["cols"]))
    fks = _live_fks(conn)
    assert len(fks) >= 100
    dangling = [(r["child"], r["conname"], r["parent"], r["refcols"]) for r in fks
                if frozenset(r["confkey"]) not in uniques[r["confrelid"]]]
    assert not dangling, dangling


def test_numeric_and_time_policy_holds_in_every_table(conn):
    """M0-E02: money/price/ratio columns are numeric(38,12) (no binary float); instants are timestamptz."""
    rows = conn.execute(
        "SELECT n.nspname || '.' || c.relname || '.' || a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ,"
        " t.typtype, bt.typname AS basetype"
        " FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace"
        " JOIN pg_type t ON t.oid=a.atttypid LEFT JOIN pg_type bt ON bt.oid=t.typbasetype"
        " WHERE c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped"
        " AND n.nspname IN ('core','ingest','market','strategy','decision','ops','ui','execution','intelligence',"
        "                   'research','analysis')").fetchall()
    floats = [r["col"] for r in rows if r["typ"] in ("real", "double precision") or r["basetype"] in ("float4", "float8")]
    naive = [r["col"] for r in rows if r["typ"] in ("timestamp without time zone", "time without time zone")]
    assert not floats, floats
    assert not naive, naive
    numerics = {r["typ"] for r in rows if r["typ"].startswith("numeric")}
    assert numerics <= {"numeric(38,12)"}, numerics
    domains = {r["col"].split(".", 2)[-1]: r["typ"] for r in rows if r["typtype"] == "d"}
    assert {"core.finite_decimal", "core.price_raw", "core.nonnegative_amount", "core.sha256_hex"} <= set(domains.values())
