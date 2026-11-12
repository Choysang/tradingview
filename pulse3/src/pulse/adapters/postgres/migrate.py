"""Forward-only migration runner.

Applies ``migrations/NNNN_*.sql`` in order inside one transaction each and
records name + sha256 in ``public.schema_migration``. A changed checksum of an
already applied file is an error, never a silent re-run.

Usage: python -m pulse.adapters.postgres.migrate [--dsn DSN] [--create-db]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg

from pulse import config

MIGRATIONS = Path(__file__).resolve().parents[4] / "migrations"


def files() -> list[Path]:
    return sorted(p for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def ensure_database(dsn: str) -> None:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    name = info.get("dbname", "pulse")
    admin = psycopg.conninfo.make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (name,)).fetchone():
            c.execute(f'create database "{name}"')


def apply(dsn: str) -> list[str]:
    applied: list[str] = []
    # Seed files carry their own BEGIN/COMMIT, so run them in autocommit mode
    # (simple-query protocol, whole file at once) and record afterwards.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "create table if not exists public.schema_migration("
            " name text primary key, sha256 text not null, applied_at timestamptz not null default now())"
        )
        done = {r[0]: r[1] for r in conn.execute("select name, sha256 from public.schema_migration")}
        for path in files():
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise RuntimeError(f"migration {path.name} changed after it was applied")
                continue
            text = body.decode("utf-8")
            if "BEGIN;" not in text:
                text = f"BEGIN;\n{text}\nCOMMIT;"
            conn.execute(text)
            conn.execute("insert into public.schema_migration(name, sha256) values (%s, %s)", (path.name, digest))
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=config.load().dsn)
    ap.add_argument("--create-db", action="store_true")
    a = ap.parse_args(argv)
    if a.create_db:
        ensure_database(a.dsn)
    for name in apply(a.dsn):
        print("applied", name)
    print("migrations up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
