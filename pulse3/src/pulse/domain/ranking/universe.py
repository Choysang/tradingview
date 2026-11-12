"""UniverseRules / user universe preference and the exclusion reasons they produce.

Why: FINAL_SYSTEM_SPEC §1.1 and RECONCILIATION §6: the default recommendation universe
is main-board SH/SZ, price <= 100 CNY, circulating market cap <= 100e8 CNY, new
listings excluded. Exchange/board rules and the user's price/cap preference are
versioned separately - a preference is not an exchange rule, and the old 200e8 research
threshold stays a historical experiment domain, never the default.
Invariants: every excluded instrument carries explicit reason codes; an unknown fact
(None) never passes a bound and never counts as zero; output order is deterministic.
Not covered: loading instrument facts (adapter), ST history reconstruction.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class UniverseRules:
    """Market-structure rules (not user preference)."""

    rules_version: str = "cn-mainboard-rules-v1"
    venues: frozenset[str] = frozenset({"XSHG", "XSHE"})
    boards: frozenset[str] = frozenset({"mainboard"})
    instrument_types: frozenset[str] = frozenset({"equity"})
    exclude_st: bool = True
    #: registered before the first experiment (spec): sessions since listing.
    new_listing_min_sessions: int = 60


@dataclass(frozen=True)
class UserUniversePreference:
    """User-configurable bounds. Not verified optimal thresholds."""

    preference_version: str = "default-pref-v1"
    price_cap_raw: Decimal | None = Decimal("100")
    circulating_market_cap_cap_cny: Decimal | None = Decimal("10000000000")


@dataclass(frozen=True)
class UniversePolicy:
    rules: UniverseRules = field(default_factory=UniverseRules)
    preference: UserUniversePreference = field(default_factory=UserUniversePreference)

    @property
    def policy_version(self) -> str:
        return f"{self.rules.rules_version}+{self.preference.preference_version}"


@dataclass(frozen=True)
class InstrumentFacts:
    """Point-in-time facts for one instrument; ``None`` means unknown."""

    instrument_id: UUID
    venue_code: str | None
    board_code: str | None
    instrument_type: str | None
    is_st: bool | None
    listed_sessions: int | None
    price_raw: Decimal | None
    circulating_market_cap_cny: Decimal | None
    is_suspended: bool | None = None


@dataclass(frozen=True)
class Exclusion:
    instrument_id: UUID
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class UniverseResult:
    policy_version: str
    included: tuple[InstrumentFacts, ...]
    exclusions: tuple[Exclusion, ...]


def _reasons(policy: UniversePolicy, f: InstrumentFacts) -> list[str]:
    r, p = policy.rules, policy.preference
    out: list[str] = []
    if f.venue_code is None:
        out.append("unknown_venue")
    elif f.venue_code not in r.venues:
        out.append(f"venue_not_in_universe:{f.venue_code}")
    if f.board_code is None:
        out.append("unknown_board")
    elif f.board_code not in r.boards:
        out.append(f"board_not_in_universe:{f.board_code}")
    if f.instrument_type is None:
        out.append("unknown_instrument_type")
    elif f.instrument_type not in r.instrument_types:
        out.append(f"instrument_type_not_in_universe:{f.instrument_type}")
    if r.exclude_st:
        if f.is_st is None:
            out.append("unknown_st_status")
        elif f.is_st:
            out.append("risk_warning_st")
    if f.listed_sessions is None:
        out.append("unknown_listing_age")
    elif f.listed_sessions < r.new_listing_min_sessions:
        out.append("new_listing")
    if f.is_suspended:
        out.append("suspended")
    if p.price_cap_raw is not None:
        if f.price_raw is None:
            out.append("unknown_price")
        elif f.price_raw > p.price_cap_raw:
            out.append("price_above_cap")
    if p.circulating_market_cap_cap_cny is not None:
        if f.circulating_market_cap_cny is None:
            out.append("unknown_circulating_market_cap")
        elif f.circulating_market_cap_cny > p.circulating_market_cap_cap_cny:
            out.append("circulating_market_cap_above_cap")
    return out


def apply_universe(policy: UniversePolicy, facts: Iterable[InstrumentFacts]) -> UniverseResult:
    included: list[InstrumentFacts] = []
    excluded: list[Exclusion] = []
    for f in sorted(facts, key=lambda x: str(x.instrument_id)):
        reasons = _reasons(policy, f)
        if reasons:
            excluded.append(Exclusion(f.instrument_id, tuple(reasons)))
        else:
            included.append(f)
    return UniverseResult(policy.policy_version, tuple(included), tuple(excluded))
