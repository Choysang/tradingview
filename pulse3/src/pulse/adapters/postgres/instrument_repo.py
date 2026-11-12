"""Instrument persistence: stable ids, source aliases with validity, dated board / ST states, resolver.

Why: ``core.instrument`` holds identity, ``core.instrument_alias`` maps what each
source calls it (point-in-time), ``core.instrument_state`` holds the dated display
name, board and risk-warning flag. The resolver answers ``(source symbol, date)``
from the alias table only - no prefix heuristics at lookup time.

Invariants: re-registering the same instrument is a no-op; a changed name / ST flag
appends a new ``state_version`` (never edits history); states carry the snapshot
date they were first observed (``evidence_ref`` says so) - they are not back-filled
before that date.
Not covered: delisting handling, code reuse after delisting (needs listing/delisting evidence).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Sequence

import psycopg

from pulse.domain.instruments.identity import InstrumentRecord, instrument_uuid

ALIAS_NS = uuid.UUID("3d9c1b77-2e4a-4f68-a1c5-9b0e6d7f8a21")
EPOCH = datetime(1990, 12, 19, tzinfo=timezone.utc)  # SSE opening day: earliest possible A-share validity start


@dataclass(frozen=True)
class StateObservation:
    code: str
    as_of: date
    name: str
    evidence_ref: str


def upsert_instruments(conn: psycopg.Connection, records: Sequence[InstrumentRecord], now: datetime,
                       source_ids: Iterable[str], anchor: str) -> int:
    """Insert identity rows and per-source aliases; returns the number of new instruments."""
    new = 0
    with conn.cursor() as cur:
        for r in records:
            cur.execute(
                "INSERT INTO core.instrument(instrument_id, instrument_type, venue_code, currency, exchange_timezone,"
                " listed_on, created_at) VALUES (%s,%s,%s,'CNY','Asia/Shanghai',%s,%s) ON CONFLICT (instrument_id) DO NOTHING",
                (r.instrument_id, r.instrument_type, r.venue_code, r.listed_on, now))
            new += cur.rowcount
            if r.listed_on is not None:
                cur.execute("UPDATE core.instrument SET listed_on=%s WHERE instrument_id=%s AND listed_on IS NULL",
                            (r.listed_on, r.instrument_id))
            for sid in source_ids:
                sym = r.symbols.get(sid)
                if sym is None:
                    continue
                eff = datetime.combine(r.listed_on, datetime.min.time(), tzinfo=timezone.utc) if r.listed_on else EPOCH
                cur.execute(
                    "INSERT INTO core.instrument_alias(alias_id, source_id, source_symbol, instrument_id, effective_from,"
                    " effective_to, available_at, revision, source_anchor) VALUES (%s,%s,%s,%s,%s,NULL,%s,1,%s)"
                    " ON CONFLICT (source_id, source_symbol, effective_from, revision) DO NOTHING",
                    (uuid.uuid5(ALIAS_NS, f"{sid}|{sym}|{eff.isoformat()}|1"), sid, sym, r.instrument_id, eff, now,
                     f"{anchor};exchange_numbering_range"))
    return new


def append_state_if_changed(conn: psycopg.Connection, r: InstrumentRecord, observed_on: date, now: datetime,
                            evidence_ref: str) -> int | None:
    """Append state_version n+1 only when name / board / risk flag differ from the latest state."""
    last = conn.execute(
        "SELECT state_version, board_code, display_name, is_risk_warning, effective_from FROM core.instrument_state"
        " WHERE instrument_id=%s ORDER BY state_version DESC LIMIT 1", (r.instrument_id,)).fetchone()
    eff = datetime.combine(observed_on, datetime.min.time(), tzinfo=timezone.utc)
    if last is not None and eff <= last["effective_from"]:
        # An observation from on/before the latest state's start (incremental re-reads of older files) cannot
        # open a newer version or close the current one; history is never rewritten backwards.
        return None
    if last is not None and (last["board_code"], last["display_name"], last["is_risk_warning"]) == (
            r.board_code, r.display_name, r.is_risk_warning):
        return None
    version = 1 if last is None else last["state_version"] + 1
    if last is not None:
        conn.execute("UPDATE core.instrument_state SET effective_to=%s WHERE instrument_id=%s AND state_version=%s AND effective_to IS NULL",
                     (eff, r.instrument_id, last["state_version"]))
    conn.execute(
        "INSERT INTO core.instrument_state(instrument_id, state_version, board_code, display_name, is_risk_warning,"
        " trading_status, effective_from, effective_to, available_at, evidence_ref)"
        " VALUES (%s,%s,%s,%s,%s,'active',%s,NULL,%s,%s)",
        (r.instrument_id, version, r.board_code, r.display_name, r.is_risk_warning, eff, now, evidence_ref))
    return version


def alias_resolver(conn: psycopg.Connection, source_id: str) -> Callable[[str, str, str | None], str]:
    """In-memory point-in-time resolver for one source (loaded once, queried per record)."""
    rows = conn.execute(
        "SELECT source_symbol, instrument_id, effective_from, effective_to FROM core.instrument_alias WHERE source_id=%s"
        " ORDER BY effective_from DESC, revision DESC", (source_id,)).fetchall()
    table: dict[str, list[tuple[datetime, datetime | None, str]]] = {}
    for r in rows:
        table.setdefault(r["source_symbol"], []).append((r["effective_from"], r["effective_to"], str(r["instrument_id"])))

    def resolve(symbol: str, trade_date: str, market: str | None) -> str:
        at = datetime.fromisoformat(trade_date).replace(tzinfo=timezone.utc)
        for eff_from, eff_to, iid in table.get(symbol, ()):
            if eff_from <= at and (eff_to is None or at < eff_to):
                return iid
        raise ValueError(f"unresolved_identifier:{source_id}:{symbol}")

    return resolve


def instrument_ids_by_code(conn: psycopg.Connection, codes: Iterable[str], venue_for: Callable[[str], str]) -> dict[str, uuid.UUID]:
    return {c: instrument_uuid(venue_for(c), c) for c in codes}


def get_instrument_wire(conn: psycopg.Connection, instrument_id: uuid.UUID) -> dict:
    """Instrument contract payload (contract def ``Instrument``)."""
    row = conn.execute(
        "SELECT i.instrument_id, i.venue_code, i.instrument_type, i.currency, i.exchange_timezone, s.display_name,"
        " s.state_version, s.trading_status, s.is_risk_warning, s.board_code FROM core.instrument i"
        " JOIN LATERAL (SELECT * FROM core.instrument_state WHERE instrument_id=i.instrument_id ORDER BY state_version DESC LIMIT 1) s ON true"
        " WHERE i.instrument_id=%s", (instrument_id,)).fetchone()
    if row is None:
        raise KeyError(str(instrument_id))
    aliases = conn.execute("SELECT source_id, source_symbol FROM core.instrument_alias WHERE instrument_id=%s ORDER BY source_id, source_symbol",
                           (instrument_id,)).fetchall()
    return {**{k: (str(v) if k == "instrument_id" else v) for k, v in row.items()},
            "source_symbols": [{"source_id": a["source_id"], "source_symbol": a["source_symbol"]} for a in aliases]}
