"""CostProfile and CostEngine: versioned, Decimal, cumulative-per-order fees.

Why: execution_standard.md section 3 and M2-E04 require fees to be a versioned
configuration (rate, direction, base, minimum, aggregation, rounding, inclusion)
and partial fills must not multiply a cumulative minimum charge.
Method: for an order the fee owed after cumulative notional N is
``sum_i round(max(rate_i * N, minimum_i))`` over applicable components; a fill's
fee is that cumulative amount minus what was already charged, so tail rounding
reconciles exactly and fill granularity cannot change the total.
Unknown components (``rate=None``) are never treated as zero: the increment is
flagged ``incomplete`` and lists them; the known part is what the ledger books and
a later ``fee.cash_adjustment`` appends the difference.
Documented values only: the SSE/SZSE A-share exchange handling fee 0.00341% per
side (EXS09/EXS10, schedule of 2026-01). Commission, stamp duty, transfer and
regulatory fees are NOT documented in the package (execution_standard.md says they
need tax/clearing notices and the customer contract) and are therefore unknown in
``a_share_documented_partial``. ``synthetic_fixed_fee`` reproduces the golden
fixtures' 5 CNY per order and is synthetic, not a market rate.
Rounding to ``settlement_scale`` (default 0.01 CNY, half-even) is an engineering
decision, unverified against any broker statement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

D = Decimal
ZERO = D(0)


@dataclass(frozen=True)
class FeeRule:
    name: str
    rate: Decimal | None  # fraction of notional; None = unknown
    sides: frozenset[str] = frozenset({"buy", "sell"})
    minimum: Decimal = ZERO  # per order, applied to the cumulative amount
    is_exchange_fee: bool = False  # skipped when commission already includes it


@dataclass(frozen=True)
class CostProfile:
    profile_id: str
    version: str
    effective_from: date | None
    effective_to: date | None  # exclusive
    rules: tuple[FeeRule, ...]
    evidence_status: str  # synthetic | documented_partial | contract
    commission_includes_exchange_fee: bool = False
    settlement_scale: Decimal = D("0.01")

    def covers(self, day: date) -> bool:
        return ((self.effective_from is None or day >= self.effective_from)
                and (self.effective_to is None or day < self.effective_to))

    def applicable(self, side: str) -> tuple[FeeRule, ...]:
        return tuple(r for r in self.rules if side in r.sides
                     and not (r.is_exchange_fee and self.commission_includes_exchange_fee))


@dataclass(frozen=True)
class FeeIncrement:
    fee: Decimal
    cumulative: Decimal
    unknown_components: tuple[str, ...]

    @property
    def status(self) -> str:
        return "incomplete" if self.unknown_components else "complete"


def synthetic_fixed_fee(per_order: str = "5") -> CostProfile:
    return CostProfile("synthetic-fixed", f"synthetic-fixed-{per_order}-per-order-v1", None, None,
                       (FeeRule("commission", ZERO, minimum=D(per_order)),), "synthetic")


def a_share_documented_partial() -> CostProfile:
    return CostProfile(
        "a-share-documented-partial", "a-share-exchange-fee-only-2026-01-v1", None, None,
        (FeeRule("exchange_handling", D("0.0000341"), is_exchange_fee=True),
         FeeRule("commission", None), FeeRule("stamp_duty", None, frozenset({"sell"})),
         FeeRule("transfer", None), FeeRule("regulatory", None)),
        "documented_partial")


def user_account_2026() -> CostProfile:
    """The user's confirmed account plan (decision 2026-11-08, docs/rebuild/07 §3.2).

    Commission 0.05% both sides with 5 CNY minimum per order (minimum is common
    practice, see decision D05); commission is taken to include the exchange
    handling fee. Stamp duty 0.05% on sells from 2023-08-28 and transfer fee
    0.001% both sides are from secondary sources, not yet checked against the
    original notices; regulatory fee is folded into commission by the broker plan.
    Evaluations also report x2 and x3 of this profile (07 §3.2).
    """
    return CostProfile(
        "user-account", "user-account-2026-11-08-v1", date(2023, 8, 28), None,
        (FeeRule("commission", D("0.0005"), minimum=D("5")),
         FeeRule("exchange_handling", D("0.0000341"), is_exchange_fee=True),
         FeeRule("stamp_duty", D("0.0005"), frozenset({"sell"})),
         FeeRule("transfer", D("0.00001"))),
        "user_account", commission_includes_exchange_fee=True)


def scaled(profile: CostProfile, factor: int) -> CostProfile:
    """Cost stress copy (x2, x3): every known rate and minimum multiplied."""
    rules = tuple(FeeRule(r.name, None if r.rate is None else r.rate * factor, r.sides, r.minimum * factor,
                          r.is_exchange_fee) for r in profile.rules)
    return CostProfile(profile.profile_id + f"-x{factor}", profile.version + f"-x{factor}", profile.effective_from,
                       profile.effective_to, rules, profile.evidence_status, profile.commission_includes_exchange_fee,
                       profile.settlement_scale)


class CostEngine:
    @staticmethod
    def cumulative_fee(profile: CostProfile, side: str, cumulative_notional: Decimal) -> tuple[Decimal, tuple[str, ...]]:
        if cumulative_notional < 0:
            raise ValueError("cumulative notional must be nonnegative")
        total, unknown = ZERO, []
        with localcontext() as ctx:
            ctx.prec = 50
            for r in profile.applicable(side):
                if r.rate is None:
                    unknown.append(r.name)
                    continue
                part = max(r.rate * cumulative_notional, r.minimum) if cumulative_notional > 0 else ZERO
                total += part.quantize(profile.settlement_scale, rounding=ROUND_HALF_EVEN)
        return total, tuple(unknown)

    @classmethod
    def increment(cls, profile: CostProfile, side: str, cumulative_notional: Decimal,
                  charged_so_far: Decimal) -> FeeIncrement:
        cum, unknown = cls.cumulative_fee(profile, side, cumulative_notional)
        if cum < charged_so_far:
            raise ValueError("cumulative fee below amount already charged; use fee.cash_adjustment")
        return FeeIncrement(cum - charged_so_far, cum, unknown)


class CostProfileRegistry:
    def __init__(self, profiles: tuple[CostProfile, ...]):
        self.profiles = profiles

    def resolve(self, day: date) -> CostProfile:
        hits = [p for p in self.profiles if p.covers(day)]
        if len(hits) != 1:
            raise LookupError(f"{len(hits)} cost profiles cover {day}; exactly one required")
        return hits[0]
