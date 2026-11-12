"""Paper account and ActionabilitySnapshot (I1; M2 ledger + M3-E03 overlay).

Why: FINAL_SYSTEM_SPEC §6/§8: "can I act on this now?" is a separate immutable overlay on a frozen ranking, bound to
``release_id`` + ``account_version`` + the exact quote snapshot, and the account is the ONE unified ledger
(``execution.*``). This module opens the paper account through that ledger (no parallel ledger) and produces the overlay.
Invariants: opening an account twice is a no-op; quotes become a READY ``quotes_l1`` dataset snapshot plus
``market.quote_projection`` rows before they are referenced (the DB refuses a snapshot that is not READY at
``checked_at``); a missing quote or an unknown trigger stays ``unknown``, never eligible; the overlay never rewrites
the frozen ranking.
Not covered: order placement, broker reports, intraday trigger evaluation (the trigger is evaluated on the last
confirmed raw daily bar plus the quote as an unconfirmed observation).
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import market_repo
from pulse.adapters.postgres.ledger_repo import EventContext, LedgerRepo
from pulse.application import bindings as B
from pulse.application.data_service import DataService, ingest_repo_create_batch
from pulse.application.plan_service import store_actionability
from pulse.domain import features as F
from pulse.domain.execution import KERNEL_VERSION, LedgerReducer
from pulse.domain.features.definitions import Series
from pulse.domain.market_rules import MARKET_RULES_VERSION
from pulse.domain.market_rules.costs import user_account_2026
from pulse.domain.plans import conditions as C
from pulse.domain.portfolio.actionability import ActionabilityInput, PlanLeg, QuoteView
from pulse.domain.strategy.canonical import canonical_hash

INITIAL_CASH = "1000000"
ACCOUNT_KEY = "paper-account-main"
PAPER_ACCOUNT_ID = B.aid("account", ACCOUNT_KEY)
ACTIONABILITY_POLICY = "actionability-v1-15min"
QUOTE_DATASET = "quotes_l1"
QUOTE_POLICY = "latest-observation-per-instrument-v1"
LOT = 100


def open_paper_account(conn: psycopg.Connection, *, trading_day: date, account_id: uuid.UUID = PAPER_ACCOUNT_ID,
                       initial_cash: str = INITIAL_CASH, positions: Mapping[str, Mapping] | None = None) -> bool:
    """Open the account through the unified ledger. Returns False when it already exists. Cost profile: user_account_2026."""
    repo = LedgerRepo(conn)
    if conn.execute("SELECT 1 FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone():
        return False
    ctx = EventContext("paper-account-open", MARKET_RULES_VERSION, user_account_2026().version, KERNEL_VERSION, "none")
    repo.create_account(account_id, {"trading_day": trading_day.isoformat(), "opening_cash": initial_cash,
                                     "positions": dict(positions or {}), "sale_proceeds_reusable": True}, ctx)
    return True


# --------------------------------------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class QuoteSnapshot:
    manifest: dict
    views: dict[uuid.UUID, QuoteView]
    requested: int
    missing: tuple[str, ...]


def _dec(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def publish_quote_snapshot(svc: DataService, adapter, instrument_codes: Mapping[uuid.UUID, str], *, source_id: str = "sina") -> QuoteSnapshot:
    """Fetch quotes (<= 1 request/s through the adapter's client), then publish the latest observation per instrument."""
    conn = svc.conn
    codes = sorted(set(instrument_codes.values()))
    for i in range(0, len(codes), 50):                      # the quote endpoint takes at most 50 symbols per request
        svc.ingest_quotes(adapter, source_id, codes[i:i + 50])
    ids = [str(i) for i in instrument_codes]
    rows = conn.execute(
        "SELECT DISTINCT ON (o.entity_key) o.observation_id, o.entity_key, o.canonical_payload p, o.event_time, o.available_at, o.quality_status,"
        " o.field_quality, o.mapping_version, r.source_id, r.endpoint_id, r.source_schema_version, r.first_seen_at"
        " FROM ingest.observation o JOIN ingest.raw_object r USING (raw_object_id) WHERE o.contract_id='CanonicalQuote'"
        " AND o.entity_key = ANY(%s) AND r.source_id=%s ORDER BY o.entity_key, o.event_time DESC NULLS LAST, o.available_at DESC",
        (ids, source_id)).fetchall()
    found = {r["entity_key"] for r in rows}
    missing = tuple(sorted(codes_for for iid, codes_for in instrument_codes.items() if str(iid) not in found))
    if not rows:
        raise LookupError("no quote observations are available")
    snapshot_id = uuid.uuid4()
    versions = {r["instrument_id"]: r["v"] for r in conn.execute(
        "SELECT instrument_id, max(live_quote_version) v FROM market.quote_projection WHERE instrument_id = ANY(%s)"
        " AND quote_policy_version=%s GROUP BY 1", ([uuid.UUID(i) for i in found], QUOTE_POLICY))}
    proj, frame_rows, views = [], [], {}
    for r in rows:
        iid, p = uuid.UUID(r["entity_key"]), r["p"]
        last, bid, ask = _dec(p.get("last_price_raw")), _dec(p.get("bid_price_raw")), _dec(p.get("ask_price_raw"))
        bv, av = p.get("bid_volume_shares"), p.get("ask_volume_shares")
        td = date.fromisoformat(p["trade_date"]) if p.get("trade_date") else None
        proj.append((snapshot_id, iid, QUOTE_POLICY, versions.get(iid, 0) + 1, r["source_id"], r["endpoint_id"], r["source_schema_version"],
                     r["mapping_version"], r["observation_id"], td, r["event_time"], r["first_seen_at"], max(r["available_at"], r["first_seen_at"]),
                     last, bid if bid and bid > 0 else None, ask if ask and ask > 0 else None, bv, av, r["quality_status"], r["field_quality"]))
        frame_rows.append({"instrument_id": str(iid), "observation_id": str(r["observation_id"]), "last_price_raw": last,
                           "bid_price_raw": bid, "ask_price_raw": ask, "event_time": r["event_time"]})
        views[iid] = QuoteView(iid, last if last and last > 0 else None,
                               True if (av or 0) > 0 else None, True if (bv or 0) > 0 else None)
    frame = pl.DataFrame(frame_rows, schema={"instrument_id": pl.String, "observation_id": pl.String, "last_price_raw": pl.Decimal(38, 12),
                                             "bid_price_raw": pl.Decimal(38, 12), "ask_price_raw": pl.Decimal(38, 12),
                                             "event_time": pl.Datetime("us", "UTC")})

    def projector() -> None:
        from psycopg.types.json import Jsonb
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO market.quote_projection(quote_snapshot_id, instrument_id, quote_policy_version, live_quote_version, source_id,"
                    " endpoint_id, source_schema_version, mapping_version, observation_id, trade_date, source_event_time, first_seen_at,"
                    " available_at, currency, last_price_raw, bid_price_raw, ask_price_raw, bid_volume_shares, ask_volume_shares,"
                    " quality_status, field_quality) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CNY',%s,%s,%s,%s,%s,%s,%s)",
                    [(*t[:-1], Jsonb(t[-1])) for t in proj])

    now = svc.clock()
    spec = B.ensure_internal_registry(conn, now)
    batch_id = ingest_repo_create_batch(svc, spec, f"{QUOTE_DATASET}:{snapshot_id}", now)
    owner = f"quotes:{batch_id}"
    from pulse.adapters.postgres import ingest_repo
    with conn.transaction():
        ingest_repo.commit_raw(conn, batch_id, now)
        ingest_repo.set_batch_state(conn, batch_id, "NORMALIZED")
    cov = {"dataset": QUOTE_DATASET, "quote_policy_version": QUOTE_POLICY, "instruments": len(rows), "requested": len(codes),
           "missing_instruments": list(missing), "source": source_id,
           "freshness_note": "freshness is derived at response time from source_event_time; a daily close is never a live quote"}
    pub = svc.publish_dataset(dataset_id=QUOTE_DATASET, snapshot_id=snapshot_id, batch_id=batch_id, frames={"all": frame},
                              contract_id="CanonicalQuote", coverage=cov, owner=owner, available_watermark=max(r["available_at"] for r in rows),
                              projector=projector)
    return QuoteSnapshot(market_repo.get_manifest(conn, pub["snapshot_id"]), views, len(codes), missing)


# --------------------------------------------------------------------------------------------------------
# trigger evaluation on raw bars
# --------------------------------------------------------------------------------------------------------
class RawEvalContext:
    """EvalContext over the RAW daily series of one instrument (+ optional live quote as an unconfirmed observation).

    ``ma`` operands carry their period in ``resolution_policy_version`` as ``...;n=<period>`` (the operand has no
    parameter slot: see the proposal in docs/integration/I1_evidence.md).
    """

    def __init__(self, series: Mapping[str, list], now: datetime, quote_last: Decimal | None = None):
        self.s, self.now, self.quote_last = series, now, quote_last

    def _at(self, operand: dict, back: int):
        n = len(self.s["c"])
        i = n - 1 - back
        if i < 0:
            return None
        fid = operand["feature_id"]
        day = self.s["d"][i]
        observed = datetime(day.year, day.month, day.day, 7, 0, tzinfo=timezone.utc)
        if fid == "px.close":
            v = self.s["dc"][i] if "dc" in self.s else self.s["c"][i]
        elif fid == "ma":
            period = int(operand["resolution_policy_version"].split("n=")[1])
            ser = Series(tuple(d.isoformat() for d in self.s["d"]), *(tuple(self.s[k]) for k in ("o", "h", "l", "c", "v")))
            v = F.compute("ma", ser, {"n": period})["ma"][i]
        else:
            return None
        return None if v is None else C.Observation(Decimal(str(v)).quantize(Decimal("0.000001")), observed, True)

    def current(self, operand: dict):
        return self._at(operand, 0)

    def previous(self, operand: dict):
        return self._at(operand, 1)

    def session_kind(self):
        return None

    def event(self, event_id: str):
        return None


def account_nav(ledger: LedgerReducer, views: Mapping[uuid.UUID, QuoteView]) -> Decimal | None:
    """NAV from the ledger's own valuation (cash + receivables + marked positions). ``None`` when any held position has no
    quote price: a missing mark is unknown, never 0."""
    marks = {str(i): v.last_price_raw for i, v in views.items() if v.last_price_raw is not None}
    return ledger.snapshot_partial(marks)["nav"]


def leg_quantity(sizing: Mapping, price: Decimal | None, nav: Decimal | None) -> int | None:
    """Shares for one plan leg. ``None`` = cannot be sized (unknown NAV/price/kind); 0 = sizes to less than one lot."""
    kind = sizing.get("kind")
    if kind == "fixed_shares":
        return sizing["quantity_shares"]
    if kind == "portfolio_fraction" and price and nav is not None:
        return int(nav * Decimal(sizing["fraction"]) / price) // LOT * LOT
    return None


def plan_trigger(plan: dict, series: Mapping[str, list] | None, now: datetime) -> C.Tri:
    if not series or not series["c"]:
        return C.Tri.UNKNOWN
    return C.evaluate(plan["entry_conditions"], RawEvalContext(series, now)).value


# --------------------------------------------------------------------------------------------------------
# actionability
# --------------------------------------------------------------------------------------------------------
def build_actionability(conn: psycopg.Connection, *, release_id: uuid.UUID, ranking_id: uuid.UUID, account_id: uuid.UUID,
                        quote: QuoteSnapshot, raw_series: Mapping[uuid.UUID, Mapping[str, list]], checked_at: datetime,
                        valid_for: timedelta = timedelta(minutes=15)) -> dict:
    """Immutable overlay for the ranked instruments that carry a plan, against the account and quote snapshot."""
    from pulse.application.bindings import LedgerAccountSnapshotPort
    ledger = LedgerRepo(conn).load(account_id)
    acct = LedgerAccountSnapshotPort.of_ledger(account_id, ledger)
    entries = conn.execute(
        "SELECT e.instrument_id, e.action_rank, e.plan_id, e.plan_version, t.plan_payload FROM decision.ranking_entry e"
        " JOIN decision.trade_plan t ON t.plan_id=e.plan_id AND t.plan_version=e.plan_version WHERE e.ranking_id=%s"
        " ORDER BY e.alpha_rank", (ranking_id,)).fetchall()
    nav = account_nav(ledger, quote.views)
    plans: dict[uuid.UUID, list[PlanLeg]] = {}
    for e in entries:
        plan = e["plan_payload"]
        trig = plan_trigger(plan, raw_series.get(e["instrument_id"]), checked_at)
        view = quote.views.get(e["instrument_id"])
        price = None if view is None else view.last_price_raw
        qty = leg_quantity(plan["sizing"], price, nav)
        zone = plan.get("entry_price_zone")
        plans[e["instrument_id"]] = [PlanLeg((e["plan_id"], e["plan_version"]), "buy", qty, trig,
                                             None, None if zone is None else Decimal(zone["upper_price_raw"]))]
    inp = ActionabilityInput(
        release_id=release_id, ranking_id=ranking_id, quote_snapshot_id=quote.manifest["snapshot_id"], policy_version=ACTIONABILITY_POLICY,
        checked_at=checked_at, valid_until=checked_at + valid_for,
        ranking_order=[(e["instrument_id"], e["action_rank"]) for e in entries], plans=plans, quotes=dict(quote.views), account=acct)
    with conn.transaction():
        payload = store_actionability(conn, inp)
    return payload
