"""Core trading contracts v1 (design 1.0, sections 8.5, 9.4, 10.4, 15): the four P1 contracts.

DataView spec, ActionIntent, PortfolioState and OrderPlan / ExecutionReport, plus the event envelope they
travel in. These are the stable centre of the migration (docs/research/2026-11-02-migration-plan.md):
strategies emit ActionIntents, the portfolio layer turns them into OrderPlans against one PortfolioState,
execution answers with ExecutionReports. Nothing here imports the old screener contracts in schema.py.

Units: prices and cash in CNY as Decimal (never float equality for limit prices), quantities in shares,
times as timezone-aware datetimes stored in UTC (shown in Asia/Shanghai). Unknowns stay null.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"
INSTRUMENT_ID = r"^CN\.(SH|SZ|BJ)\.\d{6}$"  # e.g. CN.SH.600000; the type comes from master data, not the prefix
LOT = 100  # 主板买入申报单位


class Core(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return value


# --- time semantics (9.4) -------------------------------------------------------------------------

class KnowledgeMode(str, Enum):
    AS_OBSERVED = "AS_OBSERVED"  # availability = our real receive / processing time (forward runs)
    AS_PUBLISHED = "AS_PUBLISHED"  # availability = a reliable original publication time (history rebuilds)


class TimePrecision(str, Enum):
    MS = "ms"
    SECOND = "s"
    MINUTE = "min"
    DAY = "day"
    UNKNOWN = "unknown"


class Session(str, Enum):
    PRE_OPEN = "PRE_OPEN"  # 09:15-09:20, orders cancellable
    OPEN_AUCTION = "OPEN_AUCTION"  # 09:20-09:25, no cancels
    CONTINUOUS_AM = "CONTINUOUS_AM"
    CONTINUOUS_PM = "CONTINUOUS_PM"
    CLOSE_AUCTION = "CLOSE_AUCTION"  # 14:57-15:00, no cancels
    AFTER_HOURS_FIXED = "AFTER_HOURS_FIXED"  # 盘后固定价格交易, 15:05-15:30
    CLOSED = "CLOSED"


class TimeFields(Core):
    """The 8 time fields of design 1.0 section 9.4, carried by every stored event and core table row.

    event_time: when the thing happened; published_at: first public version; received_at: our collector got
    it; available_at: earliest use allowed in this knowledge mode; recorded_at: written to local storage;
    valid_from / valid_to: when a security attribute or rule held in reality; revision_id / supersedes:
    content version and what it replaces; time_precision: never claim more precision than the source has."""

    event_time: datetime
    published_at: datetime | None = None
    received_at: datetime
    available_at: datetime
    recorded_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    revision_id: str | None = None
    supersedes: str | None = None
    time_precision: TimePrecision = TimePrecision.UNKNOWN

    _tz = field_validator("event_time", "published_at", "received_at", "available_at", "recorded_at",
                          "valid_from", "valid_to")(_aware)

    @model_validator(mode="after")
    def _order(self) -> TimeFields:
        if self.available_at < self.received_at:
            raise ValueError("available_at cannot precede received_at")
        if self.published_at is not None and self.received_at < self.published_at:
            raise ValueError("received_at cannot precede published_at")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must follow valid_from")
        return self


class EventEnvelope(TimeFields):
    """One market, disclosure or system event. `sequence` is monotonic within `stream_id` only."""

    event_id: str
    stream_id: str
    sequence: int = Field(ge=0)
    event_type: str
    instrument_id: str | None = Field(None, pattern=INSTRUMENT_ID)
    session: Session | None = None
    source_id: str
    quality_flags: tuple[str, ...] = ()
    payload_ref: str


class DataViewSpec(Core):
    """What a decision at `decision_time` may see. The runtime DataView only serves rows available by then."""

    decision_time: datetime
    snapshot_id: str
    knowledge_mode: KnowledgeMode
    ruleset_id: str
    universe_id: str

    _tz = field_validator("decision_time")(_aware)


# --- intents (15.3) -------------------------------------------------------------------------------

class Action(str, Enum):
    INCREASE_LONG = "INCREASE_LONG"
    DECREASE_LONG = "DECREASE_LONG"
    EXIT_LONG = "EXIT_LONG"
    T_BUY_FIRST = "T_BUY_FIRST"  # 正 T: buy, then sell existing sellable stock
    T_SELL_FIRST = "T_SELL_FIRST"  # 倒 T: sell sellable stock, then buy back


class TriggerRef(Core):
    rule_id: str  # stable id with version, e.g. "reclaim.vwap@1"; B01/S01 are display aliases only
    status: Literal["ARMED", "FIRED", "EXPIRED", "INVALIDATED"]
    evidence_refs: tuple[str, ...] = ()


class PricePolicy(Core):
    type: Literal["bounded_limit", "at_open", "at_close", "after_hours_fixed", "market_protected"]
    limit_price: Decimal | None = Field(None, gt=0)  # the highest buy / lowest sell still worth doing
    tick_size: Decimal = Decimal("0.01")

    @model_validator(mode="after")
    def _limit(self) -> PricePolicy:
        if self.type == "bounded_limit" and self.limit_price is None:
            raise ValueError("bounded_limit needs limit_price")
        return self


class QuantityPolicy(Core):
    type: Literal["portfolio_allocated", "target_weight", "fixed_shares", "all_sellable"]
    value: Decimal | None = Field(None, ge=0)


class ActionIntent(Core):
    """A fired trigger. Not an order: the portfolio layer sizes it, execution turns it into an OrderPlan."""

    intent_id: str
    decision_time: datetime
    generated_at: datetime
    expires_at: datetime
    strategy_ref: str  # "trend.pullback@1.0.0"
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    action: Action
    horizon_sessions: int = Field(ge=1)
    trigger: TriggerRef
    price_policy: PricePolicy
    quantity_policy: QuantityPolicy
    invalidation: tuple[str, ...] = ()  # executable rule ids, never free text
    data_snapshot_id: str
    calibration_ref: str | None = None
    reason: str = ""  # display only

    _tz = field_validator("decision_time", "generated_at", "expires_at")(_aware)

    @model_validator(mode="after")
    def _times(self) -> ActionIntent:
        if not (self.decision_time <= self.generated_at < self.expires_at):
            raise ValueError("need decision_time <= generated_at < expires_at")
        return self


# --- portfolio (8.5) ------------------------------------------------------------------------------

class Position(Core):
    """One instrument's inventory in the single account ledger.

    sellable_qty counts every share that may be sold today, reserved ones included; new sells may use
    sellable_qty - reserved_sell_qty. Shares bought today sit in acquired_today until the next real session."""

    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    total_qty: int = Field(ge=0)
    sellable_qty: int = Field(ge=0)
    acquired_today: int = Field(ge=0)
    reserved_sell_qty: int = Field(0, ge=0)
    pending_buy_qty: int = Field(0, ge=0)
    cost_basis: Decimal = Field(Decimal(0), ge=0)  # total cash paid incl. fees, for attribution only

    @model_validator(mode="after")
    def _inventory(self) -> Position:
        if self.sellable_qty + self.acquired_today > self.total_qty:
            raise ValueError("sellable_qty + acquired_today exceeds total_qty")
        if self.reserved_sell_qty > self.sellable_qty:
            raise ValueError("reserved_sell_qty exceeds sellable_qty")
        return self

    @property
    def free_to_sell(self) -> int:
        return self.sellable_qty - self.reserved_sell_qty


class PortfolioState(Core):
    """The one cash and inventory truth of an account at `as_of`. Strategy sub-ledgers are attribution only."""

    account_id: str
    ledger_version: int = Field(ge=0)
    as_of: datetime
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")  # the trading session the counters refer to
    cash_available: Decimal
    cash_reserved: Decimal = Field(Decimal(0), ge=0)
    positions: tuple[Position, ...] = ()
    open_order_ids: tuple[str, ...] = ()

    _tz = field_validator("as_of")(_aware)

    @model_validator(mode="after")
    def _unique(self) -> PortfolioState:
        ids = [p.instrument_id for p in self.positions]
        if len(ids) != len(set(ids)):
            raise ValueError("one position per instrument")
        if self.cash_available < 0:
            raise ValueError("cash_available cannot be negative")
        return self

    def position(self, instrument_id: str) -> Position | None:
        return next((p for p in self.positions if p.instrument_id == instrument_id), None)


# --- orders (10.4) --------------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"  # the order may still fill
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset({OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.PARTIALLY_FILLED,
                                      OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.EXPIRED}),
    OrderStatus.ACKNOWLEDGED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING,
                                         OrderStatus.EXPIRED}),
    OrderStatus.PARTIALLY_FILLED: frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                                             OrderStatus.CANCEL_PENDING, OrderStatus.EXPIRED}),
    OrderStatus.CANCEL_PENDING: frozenset({OrderStatus.CANCELED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}),
}


def can_transition(old: OrderStatus, new: OrderStatus) -> bool:
    return new in TRANSITIONS.get(old, frozenset())


class OrderPlan(Core):
    client_order_id: str
    idempotency_key: str  # retries reuse it; the ledger books one order per key
    intent_id: str
    account_id: str
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    side: Side
    quantity: int = Field(gt=0)
    price_policy: PricePolicy
    session: Session
    time_in_force: Literal["DAY", "SESSION", "IOC"] = "DAY"
    latency_model: str | None = None  # simulated arrival delay model id, None for live

    @model_validator(mode="after")
    def _lot(self) -> OrderPlan:
        if self.side is Side.BUY and self.quantity % LOT:
            raise ValueError(f"buy quantity must be a multiple of {LOT}")
        return self


class ExecutionReport(Core):
    report_id: str
    order_id: str
    client_order_id: str
    status: OrderStatus
    order_quantity: int = Field(gt=0)
    filled_quantity: int = Field(ge=0)  # cumulative
    last_fill_quantity: int = Field(0, ge=0)
    last_fill_price: Decimal | None = Field(None, gt=0)
    fees: Decimal = Field(Decimal(0), ge=0)  # cumulative commission + stamp duty + transfer fees
    event_time: datetime
    reason: str | None = None  # reject / cancel / expiry reason code

    _tz = field_validator("event_time")(_aware)

    @model_validator(mode="after")
    def _fills(self) -> ExecutionReport:
        if self.filled_quantity > self.order_quantity:
            raise ValueError("filled more than ordered")
        if self.last_fill_quantity and self.last_fill_price is None:
            raise ValueError("a fill needs its price")
        if self.status is OrderStatus.FILLED and self.filled_quantity != self.order_quantity:
            raise ValueError("FILLED needs the full quantity")
        return self


# --- opportunity book (10.5) ----------------------------------------------------------------------

class OpportunityStatus(str, Enum):
    TRIGGERED = "TRIGGERED"  # intent fired, no order yet
    ORDERED = "ORDERED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    UNFILLED = "UNFILLED"  # the order ended without a fill
    NOT_ORDERED = "NOT_ORDERED"  # the portfolio layer declined it (cash, overlap, budget)


class UnfilledReason(str, Enum):
    LIMIT_UP_QUEUE = "LIMIT_UP_QUEUE"  # queued at the limit-up price, the queue never reached us
    ONE_WORD_BOARD = "ONE_WORD_BOARD"  # sealed at the limit all session
    PRICE_BOUND = "PRICE_BOUND"  # the market never came inside our limit price
    EXPIRED = "EXPIRED"
    NO_CASH = "NO_CASH"
    NOT_SELLABLE = "NOT_SELLABLE"  # T+1 or reserved shares
    SUSPENDED = "SUSPENDED"
    BUDGET = "BUDGET"  # declined by the portfolio policy
    REJECTED = "REJECTED"


class FillScenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    PESSIMISTIC = "PESSIMISTIC"


class OpportunityRecord(Core):
    """Every fired intent, bought or not. Strategy results are reported on candidates, triggers, orders and
    fills side by side, so a strategy whose best names cannot be bought shows it."""

    opportunity_id: str
    intent_id: str
    account_id: str
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    strategy_ref: str
    decision_time: datetime
    status: OpportunityStatus
    scenario: FillScenario | None = None  # set when the fill came from the queue simulator
    target_quantity: int = Field(ge=0)
    filled_quantity: int = Field(0, ge=0)
    avg_fill_price: Decimal | None = Field(None, gt=0)
    reference_price: Decimal | None = Field(None, gt=0)  # price used to value the missed trade, e.g. the limit
    unfilled_reason: UnfilledReason | None = None
    client_order_id: str | None = None
    closed_at: datetime | None = None

    _tz = field_validator("decision_time", "closed_at")(_aware)

    @model_validator(mode="after")
    def _consistent(self) -> OpportunityRecord:
        if self.filled_quantity > self.target_quantity:
            raise ValueError("filled more than targeted")
        if self.status in (OpportunityStatus.UNFILLED, OpportunityStatus.NOT_ORDERED) and self.unfilled_reason is None:
            raise ValueError("an unfilled opportunity needs its reason")
        if self.status is OpportunityStatus.FILLED and (self.filled_quantity != self.target_quantity or self.avg_fill_price is None):
            raise ValueError("FILLED needs the full quantity and a price")
        return self
