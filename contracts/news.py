"""Message and signal contracts (design 1.1 sections 5, 6.3, 7, 13.2; user modules M1-M5 of 2026-11-02).

The design's six lock-first contracts are SourceEvent, EvidenceClaim, Interpretation, MarketSnapshot,
ActionSignal and ExecutionReport. Here: MessageVersion (a SourceEvent version), EvidenceClaim,
Interpretation, ReactionWindow (60 s tape pulse + 1/3/5 min comment snapshot, module M1), ActionSignal, and
the module records FirstBoardCluster (M2), BrokenBoardCheck (M3), AuthorExpectation / ExpectationGap (M4).
ExecutionReport and the ledger live in contracts/core.py; MarketSnapshot is the recorder's quote row.

Every record carries the time it became usable (`available_at`). A decision at T reads only records with
available_at <= T: a five-minute comment snapshot cannot change a decision made one minute after the news.
Unknown is a value, never a default: an unchecked condition is None, not False.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.core import INSTRUMENT_ID, Core, KnowledgeMode, TimePrecision, _aware


class MessageVersion(Core):
    """One version of one raw item from one source (a 电报 line, an announcement, a post). Never overwritten."""

    source_id: str  # "cls.telegraph", "ths.724", "cninfo", "tgb.post"
    item_id: str
    revision_id: str
    supersedes: str | None = None
    content_hash: str
    url: str | None = None
    title: str
    body_ref: str | None = None  # immutable raw object, not the text inline
    event_time: datetime | None = None  # what the text says happened; may be unknown or in the future
    source_published_at: datetime | None = None
    received_at: datetime
    available_at: datetime
    recorded_at: datetime
    time_precision: TimePrecision = TimePrecision.UNKNOWN
    knowledge_mode: KnowledgeMode = KnowledgeMode.AS_OBSERVED

    _tz = field_validator("event_time", "source_published_at", "received_at", "available_at", "recorded_at")(_aware)

    @model_validator(mode="after")
    def _times(self) -> MessageVersion:
        if self.available_at < self.received_at:
            raise ValueError("available_at cannot precede received_at")
        return self


class ClaimType(str, Enum):
    RUMOR = "RUMOR"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    PROGRESS = "PROGRESS"
    CLARIFICATION = "CLARIFICATION"
    RETRACTION = "RETRACTION"
    OLD_NEWS = "OLD_NEWS"  # a resurfaced fact: new spread, no new fact


class FactStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    CONFIRMED = "CONFIRMED"  # an original source (announcement, policy text, first report) was found
    DISPUTED = "DISPUTED"
    DENIED = "DENIED"


class EvidenceClaim(Core):
    """One factual claim. Ten reposts of one announcement share one origin_cluster_id: one fact root."""

    claim_id: str
    event_id: str
    origin_cluster_id: str
    claim_type: ClaimType
    fact_status: FactStatus
    message_refs: tuple[str, ...]  # MessageVersion revision ids
    instruments: tuple[str, ...] = ()
    available_at: datetime

    _tz = field_validator("available_at")(_aware)

    @field_validator("instruments")
    @classmethod
    def _ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [x for x in v if not re.match(INSTRUMENT_ID, x)]
        if bad:
            raise ValueError(f"bad instrument ids {bad}")
        return v


class RelationType(str, Enum):
    """Why a stock is linked to an event. Business and equity links are kept apart from name and region links."""

    ISSUER = "ISSUER"
    BUSINESS = "BUSINESS"  # demand, supply, cost, competition through real operations
    EQUITY = "EQUITY"  # same controller, shareholder, M&A party
    INDUSTRY = "INDUSTRY"
    REGION = "REGION"
    STATE_OWNED = "STATE_OWNED"  # same local SASAC / state-owned group
    THEME = "THEME"
    NAME = "NAME"  # same characters, homophones, zodiac


class Direction(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNDETERMINED = "UNDETERMINED"


class Impact(Core):
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    relation: RelationType
    channel: str  # "demand", "cost", "supply", "competition", "financing", "regulation", "attention"
    direction: Direction
    horizon: Literal["intraday", "days", "quarter", "long"]
    exposure_share: float | None = Field(None, ge=0, le=1)  # share of the business really exposed; None = unknown
    verified_relation: bool = False  # the link itself has a dated source (not a model guess)
    evidence_refs: tuple[str, ...] = ()


class Interpretation(Core):
    """One model reading of one event, bound to its input version. A later clarification archives it."""

    interpretation_id: str
    event_id: str
    version: int = Field(ge=0)  # 0 = first read, then 30 s / 1 / 3 / 5 min reviews
    input_hash: str
    model_ref: str
    prompt_ref: str
    analysis_completed_at: datetime
    available_at: datetime
    known_background: str = ""
    new_facts: str = ""
    prior_expectation: str | None = None  # None = no data on what the market expected
    hypotheses: tuple[str, ...] = ()  # main reading first, then alternatives
    impacts: tuple[Impact, ...] = ()
    catalyst_next: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()
    superseded_by_clarification: bool = False

    _tz = field_validator("analysis_completed_at", "available_at")(_aware)


class Anchor(str, Enum):
    ORIGIN = "ORIGIN"  # from the source's publish time: early spread, for research on our delay
    FIRST_SEEN = "FIRST_SEEN"  # from our first receipt: what we could act on


class TapePulse(Core):
    """60 s level-1 reaction of one linked stock (module M1a), from the recorder's hot polls."""

    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    amount_delta_yuan: float | None = None  # traded amount in the window
    amount_vs_normal: float | None = None  # ratio to the same clock window's recent median
    active_buy_lots: float | None = None  # Δouter - Δinner lots (外盘 - 内盘), a buy-side proxy, not "主力"
    return_pct: float | None = None
    excess_vs_industry_pct: float | None = None
    samples: int = Field(0, ge=0)  # snapshots inside the window; 0 = the stock was not on a fast grid


class CommentSnapshot(Core):
    """Comments fetched by `available_at` for one window (module M1b). Missing coverage is stated, not zero."""

    platforms_covered: tuple[str, ...]
    platforms_missing: tuple[str, ...] = ()
    independent_authors: int = Field(ge=0)  # after de-duplicating reposts and repeated accounts
    mentions: dict[str, int] = {}  # instrument_id -> distinct authors mentioning it
    keywords: dict[str, int] = {}
    support: int = Field(0, ge=0)
    doubt: int = Field(0, ge=0)
    unsure: int = Field(0, ge=0)
    repeat_share: float | None = Field(None, ge=0, le=1)
    raw_refs: tuple[str, ...] = ()


class ReactionWindow(Core):
    event_id: str
    anchor: Anchor
    window_s: Literal[30, 60, 180, 300]
    window_start: datetime
    available_at: datetime
    tape: tuple[TapePulse, ...] = ()
    comments: CommentSnapshot | None = None
    tape_confirmed: bool | None = None  # None: too few fast-grid samples to say
    comments_confirmed: bool | None = None  # None: coverage too thin to say

    _tz = field_validator("window_start", "available_at")(_aware)

    @property
    def cross_validated(self) -> bool | None:
        """Both checks passed (True), either failed (False), or not enough data (None)."""
        if self.tape_confirmed is False or self.comments_confirmed is False:
            return False
        if self.tape_confirmed is None or self.comments_confirmed is None:
            return None
        return True


class SignalKind(str, Enum):
    WATCH = "WATCH"  # observation pool, e.g. a fact card with unverified impacts
    CONDITIONAL_PLAN = "CONDITIONAL_PLAN"  # waits for a trigger
    FIRED = "FIRED"  # an ActionIntent was produced; only this kind reaches the portfolio layer


class ActionSignal(Core):
    signal_id: str
    kind: SignalKind
    strategy_ref: str
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    generated_at: datetime
    valid_until: datetime
    conditions: tuple[str, ...] = ()  # executable rule ids
    evidence_refs: tuple[str, ...] = ()
    intent_id: str | None = None
    validation: Literal["unverified", "tape_only", "comments_only", "cross_validated", "rejected"] = "unverified"

    _tz = field_validator("generated_at", "valid_until")(_aware)

    @model_validator(mode="after")
    def _fired(self) -> ActionSignal:
        if (self.kind is SignalKind.FIRED) != (self.intent_id is not None):
            raise ValueError("a FIRED signal, and only a FIRED signal, carries an intent_id")
        if self.valid_until <= self.generated_at:
            raise ValueError("valid_until must follow generated_at")
        return self


# --- module records -------------------------------------------------------------------------------

class FirstBoardCluster(Core):
    """M2: 3+ first boards sharing one relation key in one morning; the next day's 1进2 funnel reads it."""

    cluster_id: str
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    relation: RelationType  # REGION, STATE_OWNED, THEME or INDUSTRY
    key: str  # e.g. "山东省青岛市", "青岛市国资委", "申万:食品加工"
    members: tuple[str, ...]  # first-board instrument ids, in sealing order
    formed_at: datetime  # when the third member sealed
    available_at: datetime

    _tz = field_validator("formed_at", "available_at")(_aware)

    @model_validator(mode="after")
    def _size(self) -> FirstBoardCluster:
        if len(set(self.members)) < 3:
            raise ValueError("a cluster needs 3 distinct first boards")
        return self


class BreakState(str, Enum):
    SEALED = "SEALED"
    BROKEN_WATCH = "BROKEN_WATCH"  # opened, checks running
    BENIGN = "BENIGN"  # a divergence the checks call benign: sell signals held back
    EXIT = "EXIT"  # sell the sellable shares
    RESEALED = "RESEALED"


class BrokenBoardCheck(Core):
    """M3: one evaluation of a held limit-up that opened. Each check is True, False or None (unknown)."""

    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    variant: Literal["tape", "strict"]  # M3-Tape (unknown catalyst does not veto) or M3-Strict (all four checks)
    at: datetime
    state: BreakState
    above_vwap: bool | None
    peers_sealed: int | None  # low-position peers in the same theme still sealed
    normal_hours: bool | None  # not a late-session break (after the cutoff)
    catalyst_pending: bool | None  # True: not landed yet; False: landed (REALIZED); None: unknown
    below_vwap_s: float = Field(0, ge=0)  # seconds continuously below VWAP
    sector_dump: bool | None = None
    reason: str = ""


class AuthorView(str, Enum):
    STRONG = "STRONG"  # 一致看好 / 加速
    WEAK = "WEAK"  # 看衰
    NUKE = "NUKE"  # 核按钮 expected
    NEUTRAL = "NEUTRAL"
    CONDITIONAL = "CONDITIONAL"  # "若竞价转强..." counts only if the condition is met


class AuthorExpectation(Core):
    """M4: one author's next-day view of one stock, from one post version published after the close."""

    author_id: str  # stable pseudonymous id
    post_ref: str  # MessageVersion revision id
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    for_session: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    view: AuthorView
    condition: str | None = None
    available_at: datetime

    _tz = field_validator("available_at")(_aware)


class GapType(str, Enum):
    POSITIVE_SURPRISE = "POSITIVE_SURPRISE"  # bearish consensus, strong auction: candidate B1
    DISAPPOINTMENT = "DISAPPOINTMENT"  # bullish consensus, weak auction: the cash-out trap
    IN_LINE = "IN_LINE"
    NO_CONSENSUS = "NO_CONSENSUS"  # fewer independent authors than required, or split views


class ExpectationGap(Core):
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    authors: int = Field(ge=0)
    consensus: AuthorView | None
    auction_gap_pct: float | None  # 09:25 match price over the previous close
    auction_amount_ratio: float | None  # auction amount over the previous day's auction amount
    gap_type: GapType
    available_at: datetime  # 09:25:05 at the earliest

    _tz = field_validator("available_at")(_aware)

