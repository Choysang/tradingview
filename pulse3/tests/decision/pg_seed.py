"""Direct-SQL prerequisites for decision PG tests (releases, manifests, instruments, taxonomy).

The release writer / data plane are other work packages; these helpers only create the rows
the decision tables reference, using the package DDL exactly (no schema shortcuts).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.postgres import db

from .helpers import NS, T0

HASH = "a" * 64


def sid(name: str) -> UUID:
    return uuid5(NS, "pg:" + name)


def conn(dsn: str) -> psycopg.Connection:
    return db.connect(dsn)


def seed_base(dsn: str, n_instruments: int = 12) -> list[UUID]:
    """Contract/source/endpoint + instruments + the prov.concept taxonomy (idempotent)."""
    ids = [sid(f"instrument{i}") for i in range(n_instruments)]
    from .helpers import uid

    ids = [uid(f"inst{i}") for i in range(n_instruments)]  # same ids the pure helpers use
    with conn(dsn) as c, c.transaction():
        c.execute(
            """INSERT INTO core.contract_version(contract_id,schema_version,schema_document,schema_hash,compatibility_policy,created_at)
               VALUES ('fixture.dataset','1.0.0',%s,%s,'strict',%s) ON CONFLICT DO NOTHING""",
            (Jsonb({"type": "object"}), HASH, T0),
        )
        c.execute("INSERT INTO core.source(source_id,display_name,provider_uri,default_origin_group,created_at) "
                  "VALUES ('fixture','fixture','urn:fixture',NULL,%s) ON CONFLICT DO NOTHING", (T0,))
        c.execute(
            """INSERT INTO core.source_endpoint(source_id,endpoint_id,source_schema_version,mapping_version,contract_id,schema_version,
                   mapping_hash,enabled,credential_ref,transport_kind,poll_interval_seconds,stale_after_seconds,policy_document,created_at)
               VALUES ('fixture','ep','1','1','fixture.dataset','1.0.0',%s,true,NULL,'batch',NULL,3600,%s,%s) ON CONFLICT DO NOTHING""",
            (HASH, Jsonb({}), T0),
        )
        for i in ids:
            c.execute("INSERT INTO core.instrument(instrument_id,instrument_type,venue_code,currency,exchange_timezone,listed_on,created_at) "
                      "VALUES (%s,'equity','XSHE','CNY','Asia/Shanghai','2010-01-04',%s) ON CONFLICT DO NOTHING", (i, T0))
        c.execute("INSERT INTO core.taxonomy(taxonomy_id,taxonomy_version,taxonomy_kind,owner_source_id,definition,available_at) "
                  "VALUES ('prov.concept',3,'concept',NULL,%s,%s) ON CONFLICT DO NOTHING", (Jsonb({}), T0))
        for node in ("ai", "chip"):
            c.execute("INSERT INTO core.taxonomy_node(taxonomy_id,taxonomy_version,node_id,parent_node_id,display_name,definition) "
                      "VALUES ('prov.concept',3,%s,NULL,%s,%s) ON CONFLICT DO NOTHING", (node, node, Jsonb({})))
    return ids


def make_manifest(c: psycopg.Connection, name: str, *, ready: bool = True, ready_at: datetime = T0, dataset: str = "fixture_ds") -> UUID:
    """A READY (or INGESTING) dataset manifest with its own batch."""
    snap, batch = sid("manifest:" + name), sid("batch:" + name)
    c.execute(
        """INSERT INTO ingest.batch(batch_id,source_id,endpoint_id,source_schema_version,mapping_version,partition_key,batch_state,
               received_count,started_at,raw_committed_at) VALUES (%s,'fixture','ep','1','1',%s,'NORMALIZED',0,%s,%s)
           ON CONFLICT DO NOTHING""", (batch, name, T0, T0))
    version = c.execute("SELECT coalesce(max(manifest_version),0)+1 AS v FROM market.dataset_manifest WHERE dataset_id=%s", (dataset,)).fetchone()["v"]
    c.execute(
        """INSERT INTO market.dataset_manifest(snapshot_id,dataset_id,manifest_version,batch_id,manifest_state,storage_uri,content_hash,
               contract_id,schema_version,row_count,available_watermark,coverage_document,created_at,ready_at)
           VALUES (%s,%s,%s,%s,%s,'file:///m',%s,'fixture.dataset','1.0.0',0,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (snap, dataset, version, batch, "READY" if ready else "INGESTING", HASH, T0, Jsonb({}), T0, ready_at if ready else None),
    )
    return snap


def make_release(c: psycopg.Connection, name: str, *, publish: bool = True, published_at: datetime | None = None) -> UUID:
    """A release with one READY component 'ranking'; published (READY) in the same transaction."""
    rid = sid("release:" + name)
    pub = published_at or (T0 + timedelta(seconds=1))
    c.execute(
        """INSERT INTO decision.release(release_id,release_state,decision_as_of,availability_policy_version,universe_policy_version,
               market_rules_version,required_components,release_hash,created_at,published_at)
           VALUES (%s,'BUILDING',%s,'observed-v1','u1','mr-1',%s,NULL,%s,NULL)""",
        (rid, T0, ["ranking"], T0),
    )
    c.execute(
        """INSERT INTO decision.release_component(release_id,component_key,component_state,artifact_ref,artifact_hash,snapshot_id,
               input_version_hash,computed_at,valid_until) VALUES (%s,'ranking','READY','art://r',%s,NULL,%s,%s,NULL)""",
        (rid, HASH, HASH, T0),
    )
    if publish:
        publish_release(c, rid, pub)
    return rid


def publish_release(c: psycopg.Connection, rid: UUID, pub: datetime) -> None:
    c.execute("UPDATE decision.release SET release_state='READY',release_hash=%s,published_at=%s WHERE release_id=%s", (HASH, pub, rid))


UTC = timezone.utc
