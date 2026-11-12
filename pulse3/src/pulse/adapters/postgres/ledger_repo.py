"""PostgreSQL persistence of the one economic ledger (migration 0002, schema ``execution``).

Why: M2-E02 / execution_standard.md section 2 - one physical account is driven by a
single sequence; event, journal, quantity lots, reservations, position projection
and the new account version are written in one short transaction under a row lock
on ``account_head``. The pure ``LedgerReducer`` decides every state change; this
module only maps its result to rows.
Invariants:
* ``account_head`` is locked ``FOR UPDATE`` for the whole append; the compare-and-swap
  on ``current_sequence`` makes the account version monotonic, so two writers that
  validated against the same version cannot both commit;
* a duplicate ``event_id`` with the same canonical digest is a no-op, with a
  different digest an ``EventConflict``; a reducer rejection writes nothing;
* instants are stored as ``timestamptz`` and re-read as UTC, so every persisted
  event is first normalized to a UTC ISO instant (the opening journal keeps the
  reducer's own text); replay from the stored events rebuilds the same ledger;
* no empty journal is written (pure splits, reservations, zero-asset opening are
  events only), as execution_standard.md section 4 specifies; journal balance is
  enforced by the migration's deferred constraint triggers.
Mapping: readable fixture ids are mapped to stable uuid5 values per account (real
uuids pass through); the original text is kept in ``source_event_id`` and payload.
Not covered: raw broker message storage, outbox, trade-bust restatements, crash
recovery drills beyond transaction atomicity, non-CNY accounts.
"""
from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Iterator, Mapping

import psycopg
from psycopg.types.json import Jsonb

from pulse.domain.execution import LedgerReducer
from pulse.domain.execution.numeric import NUMERIC_POLICY_VERSION, POLICY_VERSION, canonical

_NS = uuid.UUID("6f0f3a52-9a1c-5d0e-9b7e-0c3f6e2a7d11")  # fixed namespace for readable-id mapping


class AccountVersionConflict(RuntimeError):
    """The caller's expected account version is stale."""


class EventConflict(RuntimeError):
    """Same event id redelivered with different content."""


class EventRejected(ValueError):
    """The reducer refused the event; nothing was written."""


@dataclass(frozen=True)
class EventContext:
    """Version stamps persisted with every event (all NOT NULL in the schema)."""
    source_id: str
    market_rules_version: str
    cost_policy_version: str
    execution_policy_version: str
    input_manifest_id: str


@dataclass(frozen=True)
class AppendResult:
    applied: bool  # False: exact redelivery
    sequence: int


def stable_uuid(account_id: uuid.UUID, kind: str, text: str) -> uuid.UUID:
    try:
        return uuid.UUID(text)
    except ValueError:
        return uuid.uuid5(_NS, f"{account_id}:{kind}:{text}")


def instrument_uuid(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(text)
    except ValueError:
        return uuid.uuid5(_NS, f"instrument:{text}")


def normalize_event(event: Mapping) -> dict:
    e = deepcopy(dict(event))
    when = datetime.fromisoformat(e["effective_at"])
    if when.utcoffset() is None:
        raise EventRejected("timezone required")
    e["effective_at"] = when.astimezone(timezone.utc).isoformat()
    return e


def _digest(event: Mapping) -> str:
    return hashlib.sha256(canonical(event).encode()).hexdigest()


def _rows(account: uuid.UUID, ledger: LedgerReducer) -> dict[tuple, tuple]:
    """Projection rows implied by the reducer state, keyed by (table, pk)."""
    rows: dict[tuple, tuple] = {}
    for sym, h in ledger.holdings.items():
        inst = instrument_uuid(sym)
        rows[("pos", inst)] = (h.quantity, ledger.sellable(sym), ledger.reserved_sell(sym), h.cost)
        for lot in h.lots:
            origin = stable_uuid(account, "event", lot.origin_event_id or "opening")
            lid = stable_uuid(account, "lot", f"{lot.origin_event_id}:{sym}")
            rows[("lot", lid)] = (inst, origin, lot.acquired_on, lot.eligible_on, lot.quantity)
    for oid, o in ledger.orders.items():
        status = "cancelled" if o.cancelled else ("filled" if o.remaining == 0 else "active")
        rows[("ord", stable_uuid(account, "order", oid))] = (
            instrument_uuid(o.instrument_id), o.side, o.quantity, o.remaining, o.reserved_cash, status)
    return rows


class AccountSession:
    """A locked account inside an open transaction (use via ``LedgerRepo.locked``)."""

    def __init__(self, repo: "LedgerRepo", account_id: uuid.UUID, ledger: LedgerReducer):
        self._repo, self.account_id, self.ledger = repo, account_id, ledger

    @property
    def version(self) -> int:
        return self.ledger.account_version

    def append(self, event: Mapping, ctx: EventContext) -> AppendResult:
        conn, acct = self._repo.conn, self.account_id
        ev = normalize_event(event)
        event_uuid = stable_uuid(acct, "event", ev["event_id"])
        digest = _digest(ev)
        row = conn.execute("select payload_digest from execution.account_event where account_id=%s and event_id=%s",
                           (acct, event_uuid)).fetchone()
        if row is not None:
            if row["payload_digest"] != digest:
                raise EventConflict(f"event {ev['event_id']} redelivered with different content")
            return AppendResult(False, self.version)
        before_rows = _rows(acct, self.ledger)
        n_entries = len(self.ledger.entries)
        try:
            self.ledger.apply(ev)
        except (ValueError, KeyError, TypeError, AssertionError) as exc:
            raise EventRejected(str(exc.args[0]) if exc.args else type(exc).__name__) from exc
        self._repo._write_event(acct, ev, event_uuid, digest, ctx)
        for entry in self.ledger.entries[n_entries:]:
            self._repo._write_entry(acct, entry, stable_uuid(acct, "event", entry.event_id))
        self._repo._write_projection(acct, before_rows, _rows(acct, self.ledger), ev["sequence"])
        self._repo._advance_head(acct, ev["sequence"], self.ledger.day, ev["sequence"] - 1)
        return AppendResult(True, ev["sequence"])


class LedgerRepo:
    def __init__(self, conn: psycopg.Connection):
        """``conn`` must be autocommit: every public operation owns its transaction."""
        if not conn.autocommit:
            raise ValueError("LedgerRepo requires an autocommit connection (db.connect(dsn, autocommit=True))")
        self.conn = conn
        self._cache: dict[uuid.UUID, tuple[int, LedgerReducer]] = {}

    # -- account lifecycle ----------------------------------------------------------
    def create_account(self, account_id: uuid.UUID, initial: Mapping, ctx: EventContext) -> None:
        reuse = "sale_proceeds_reusable" if initial.get("sale_proceeds_reusable", True) else "settled_only"
        with self.conn.transaction():
            self.conn.execute(
                "insert into execution.account_head(account_id,current_sequence,trading_date,cash_reuse_policy,"
                "ledger_policy_version,numeric_policy_version) values (%s,0,%s,%s,%s,%s)",
                (account_id, initial["trading_day"], reuse, POLICY_VERSION, NUMERIC_POLICY_VERSION))
            ledger = LedgerReducer(**initial)
            opening = {"event_id": "opening", "sequence": 0, "kind": "opening",
                       "effective_at": ledger.entries[0].effective_at, "payload": dict(initial)}
            event_uuid = stable_uuid(account_id, "event", "opening")
            self._write_event(account_id, normalize_event(opening), event_uuid, _digest(normalize_event(opening)), ctx)
            for entry in ledger.entries:
                self._write_entry(account_id, entry, event_uuid)
            self._write_projection(account_id, {}, _rows(account_id, ledger), 0)

    @contextmanager
    def locked(self, account_id: uuid.UUID, expected_version: int | None = None) -> Iterator[AccountSession]:
        with self.conn.transaction():
            head = self.conn.execute("select current_sequence from execution.account_head where account_id=%s for update",
                                     (account_id,)).fetchone()
            if head is None:
                raise LookupError(f"unknown account {account_id}")
            current = head["current_sequence"]
            if expected_version is not None and expected_version != current:
                raise AccountVersionConflict(f"expected {expected_version}, account is at {current}")
            cached = self._cache.get(account_id)
            ledger = deepcopy(cached[1]) if cached and cached[0] == current else self.load(account_id)
            session = AccountSession(self, account_id, ledger)
            self._cache.pop(account_id, None)  # only a committed state may be cached
            yield session
        self._cache[account_id] = (session.version, deepcopy(session.ledger))

    def append(self, account_id: uuid.UUID, event: Mapping, ctx: EventContext, *,
               expected_version: int | None = None) -> AppendResult:
        with self.locked(account_id, expected_version) as session:
            return session.append(event, ctx)

    # -- reads / rebuild ------------------------------------------------------------
    def events(self, account_id: uuid.UUID) -> list[dict]:
        out = []
        for r in self.conn.execute("select account_sequence,event_type,source_event_id,payload,effective_at "
                                   "from execution.account_event where account_id=%s order by account_sequence",
                                   (account_id,)):
            out.append({"event_id": r["source_event_id"], "sequence": r["account_sequence"], "kind": r["event_type"],
                        "effective_at": r["effective_at"].astimezone(timezone.utc).isoformat(), "payload": r["payload"]})
        return out

    def load(self, account_id: uuid.UUID) -> LedgerReducer:
        """Rebuild the reducer purely from the stored canonical events."""
        events = self.events(account_id)
        if not events or events[0]["kind"] != "opening":
            raise LookupError(f"account {account_id} has no opening event")
        ledger = LedgerReducer(**events[0]["payload"])
        for e in events[1:]:
            ledger.apply(e)
        return ledger

    def rebuild_projections(self, account_id: uuid.UUID) -> None:
        """Drop and re-derive position/lot/reservation rows by replaying the events."""
        with self.locked(account_id):
            for table in ("position_projection", "quantity_lot", "order_reservation"):
                self.conn.execute(f"delete from execution.{table} where account_id=%s", (account_id,))
            events = self.events(account_id)
            ledger = LedgerReducer(**events[0]["payload"])
            self._write_projection(account_id, {}, _rows(account_id, ledger), 0)
            for e in events[1:]:
                before = _rows(account_id, ledger)
                ledger.apply(e)
                self._write_projection(account_id, before, _rows(account_id, ledger), e["sequence"])

    def projection_snapshot(self, account_id: uuid.UUID) -> dict[str, list[tuple]]:
        q = {
            "position": "select instrument_id,quantity,sellable_quantity,reserved_sell_quantity,cost_basis,applied_sequence "
                        "from execution.position_projection where account_id=%s order by instrument_id",
            "lot": "select lot_id,instrument_id,originating_event_id,acquired_on,eligible_on,remaining_quantity,applied_sequence "
                   "from execution.quantity_lot where account_id=%s order by lot_id",
            "reservation": "select order_id,instrument_id,side,original_quantity,remaining_quantity,reserved_cash,status,applied_sequence "
                           "from execution.order_reservation where account_id=%s order by order_id",
        }
        return {k: [tuple(r.values()) for r in self.conn.execute(sql, (account_id,))] for k, sql in q.items()}

    def trial_balance(self, account_id: uuid.UUID) -> dict[str, Decimal]:
        return {r["ledger_account"]: r["debit_balance"] for r in self.conn.execute(
            "select ledger_account,debit_balance from execution.trial_balance where account_id=%s", (account_id,))}

    # -- row writers ----------------------------------------------------------------
    def _write_event(self, acct, ev, event_uuid, digest, ctx: EventContext) -> None:
        corr = None
        if ev["kind"] == "fee.cash_adjustment":  # correction references the original fill fact
            found = self.conn.execute(
                "select event_id from execution.account_event where account_id=%s and event_type='fill.booked' "
                "and payload->>'fill_id'=%s", (acct, ev["payload"]["related_fill_id"])).fetchone()
            corr = found["event_id"] if found else None
        self.conn.execute(
            "insert into execution.account_event(account_id,account_sequence,event_id,event_type,source_event_id,source_id,"
            "payload_digest,payload,effective_at,market_rules_version,cost_policy_version,execution_policy_version,"
            "input_manifest_id,ledger_policy_version,numeric_policy_version,correction_of_event_id) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (acct, ev["sequence"], event_uuid, ev["kind"], ev["event_id"], ctx.source_id, digest, Jsonb(ev["payload"]),
             ev["effective_at"], ctx.market_rules_version, ctx.cost_policy_version, ctx.execution_policy_version,
             ctx.input_manifest_id, POLICY_VERSION, NUMERIC_POLICY_VERSION, corr))

    def _write_entry(self, acct, entry, event_uuid) -> None:
        if not entry.postings:
            return  # no empty journal: event-only changes (reserve, split, session, zero-asset opening)
        entry_id = stable_uuid(acct, "entry", entry.event_id)
        self.conn.execute(
            "insert into execution.journal_entry(account_id,entry_id,event_id,account_sequence,currency,effective_at,"
            "ledger_policy_version,numeric_policy_version) values (%s,%s,%s,%s,'CNY',%s,%s,%s)",
            (acct, entry_id, event_uuid, entry.sequence, entry.effective_at, POLICY_VERSION, NUMERIC_POLICY_VERSION))
        for n, p in enumerate(entry.postings, start=1):
            inst = instrument_uuid(p.account.rsplit(":", 1)[1]) if p.account.startswith("asset:security_cost:") else None
            self.conn.execute(
                "insert into execution.journal_line(account_id,entry_id,line_no,ledger_account,instrument_id,currency,"
                "debit,credit) values (%s,%s,%s,%s,%s,'CNY',%s,%s)",
                (acct, entry_id, n, p.account, inst, p.debit, p.credit))

    def _write_projection(self, acct, before: dict, after: dict, seq: int) -> None:
        for key, vals in after.items():
            if before.get(key) == vals:
                continue
            table, pk = key
            if table == "pos":
                q, c, r, cost = vals
                self.conn.execute(
                    "insert into execution.position_projection(account_id,instrument_id,quantity,sellable_quantity,"
                    "reserved_sell_quantity,cost_basis,applied_sequence) values (%s,%s,%s,%s,%s,%s,%s) "
                    "on conflict (account_id,instrument_id) do update set quantity=excluded.quantity,"
                    "sellable_quantity=excluded.sellable_quantity,reserved_sell_quantity=excluded.reserved_sell_quantity,"
                    "cost_basis=excluded.cost_basis,applied_sequence=excluded.applied_sequence",
                    (acct, pk, q, c, r, cost, seq))
            elif table == "lot":
                inst, origin, acquired, eligible, qty = vals
                self.conn.execute(
                    "insert into execution.quantity_lot(account_id,lot_id,instrument_id,originating_event_id,acquired_on,"
                    "eligible_on,remaining_quantity,applied_sequence) values (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "on conflict (account_id,lot_id) do update set remaining_quantity=excluded.remaining_quantity,"
                    "applied_sequence=excluded.applied_sequence",
                    (acct, pk, inst, origin, acquired, eligible, qty, seq))
            else:
                inst, side, orig, rem, cash, status = vals
                self.conn.execute(
                    "insert into execution.order_reservation(account_id,order_id,instrument_id,side,original_quantity,"
                    "remaining_quantity,reserved_cash,status,applied_sequence) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "on conflict (account_id,order_id) do update set remaining_quantity=excluded.remaining_quantity,"
                    "reserved_cash=excluded.reserved_cash,status=excluded.status,applied_sequence=excluded.applied_sequence",
                    (acct, pk, inst, side, orig, rem, cash, status, seq))
        for key, vals in before.items():  # lots that were fully consumed leave the reducer; keep the row at 0
            if key[0] == "lot" and key not in after:
                self.conn.execute("update execution.quantity_lot set remaining_quantity=0,applied_sequence=%s "
                                  "where account_id=%s and lot_id=%s", (seq, acct, key[1]))

    def _advance_head(self, acct, seq: int, trading_day, expected_prev: int) -> None:
        cur = self.conn.execute(
            "update execution.account_head set current_sequence=%s,trading_date=%s,updated_at=clock_timestamp() "
            "where account_id=%s and current_sequence=%s", (seq, trading_day, acct, expected_prev))
        if cur.rowcount != 1:
            raise AccountVersionConflict("account version moved during append")
