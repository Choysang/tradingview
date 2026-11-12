"""Finite reference for ranking-budget continuity, not portfolio allocation.

Approved trial budgets are absolute shares of one ranking policy. They are never
renormalized over the remaining eligible candidates. Approval and eligibility are
inputs from the caller; this module neither assesses strategy evidence nor grants
production permissions.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext


ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_TRIAL_TOTAL_CAP = Decimal("0.1")


class InvalidRankingBudget(ValueError):
    """The supplied ranking policy is malformed or exceeds its trial budget."""


class BaselineUnavailable(RuntimeError):
    """A positive residual has no available baseline; publication must stop."""


class ReplacementUnavailable(RuntimeError):
    """The explicitly approved replacement is not currently eligible."""


@dataclass(frozen=True)
class TrialRankingBudget:
    baseline_id: str
    baseline_weight: Decimal
    candidate_weights: tuple[tuple[str, Decimal], ...]
    returned_to_baseline: Decimal
    trial_total_cap: Decimal


@dataclass(frozen=True)
class ApprovedPrimaryRanker:
    replacement_id: str
    approval_ref: str
    weight: Decimal = ONE


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRankingBudget(f"{field} must be a nonempty string")


def _weight(value: Decimal, field: str) -> None:
    # Do not accept float and convert it: its binary approximation is already lost.
    if not isinstance(value, Decimal):
        raise InvalidRankingBudget(f"{field} must be Decimal, not float/int/string")
    if not value.is_finite() or value < ZERO:
        raise InvalidRankingBudget(f"{field} must be finite and nonnegative")


def _exact_sum(values: Collection[Decimal]) -> Decimal:
    """Preserve all supplied decimal places, independent of caller precision."""
    if not values:
        return ZERO
    parts = [value.as_tuple() for value in values if value != ZERO]
    if not parts:
        return ZERO
    fractional_digits = max(0, *(-part.exponent for part in parts))
    integer_digits = max(1, *(len(part.digits) + part.exponent for part in parts))
    carry_digits = len(str(len(values)))
    with localcontext() as context:
        context.prec = integer_digits + fractional_digits + carry_digits + 2
        context.Emin = min(context.Emin, -fractional_digits - 2)
        context.Emax = max(context.Emax, integer_digits + carry_digits + 2)
        return sum(values, ZERO)


def allocate_trial_ranking_budget(
    *,
    baseline_id: str,
    approved_budgets: Mapping[str, Decimal],
    eligible_candidate_ids: Collection[str],
    baseline_available: bool = True,
    trial_total_cap: Decimal = DEFAULT_TRIAL_TOTAL_CAP,
) -> TrialRankingBudget:
    """Keep each eligible trial's approved share; send every residual to baseline.

    The cap is on all approved candidate budgets, including currently ineligible
    ones. It must be less than one: a full replacement is a separate operation.
    Ineligible candidates remain in the result with zero weight for traceability.
    No result is returned if its positive baseline residual cannot be supplied.
    """
    _identifier(baseline_id, "baseline_id")
    if not isinstance(approved_budgets, Mapping):
        raise InvalidRankingBudget("approved_budgets must be a mapping")
    if not isinstance(baseline_available, bool):
        raise InvalidRankingBudget("baseline_available must be bool")
    _weight(trial_total_cap, "trial_total_cap")
    if trial_total_cap >= ONE:
        raise InvalidRankingBudget("trial_total_cap must be below 1; use the replacement interface")

    for candidate_id, budget in approved_budgets.items():
        _identifier(candidate_id, "candidate_id")
        if candidate_id == baseline_id:
            raise InvalidRankingBudget("a candidate cannot have the baseline_id")
        _weight(budget, f"budget[{candidate_id}]")

    if isinstance(eligible_candidate_ids, (str, bytes)) or not isinstance(eligible_candidate_ids, Collection):
        raise InvalidRankingBudget("eligible_candidate_ids must be a collection of IDs")
    for candidate_id in eligible_candidate_ids:
        _identifier(candidate_id, "eligible_candidate_id")
    eligible = set(eligible_candidate_ids)
    if eligible - approved_budgets.keys():
        raise InvalidRankingBudget("eligibility contains a candidate without an approved budget")

    approved_total = _exact_sum(list(approved_budgets.values()))
    if approved_total > trial_total_cap:
        raise InvalidRankingBudget("total approved candidate budget exceeds trial_total_cap")

    weights = tuple(
        (candidate_id, budget if candidate_id in eligible else ZERO)
        for candidate_id, budget in sorted(approved_budgets.items())
    )
    active_total = _exact_sum([weight for _, weight in weights])
    residual = _exact_sum([ONE, active_total.copy_negate()])
    returned = _exact_sum([approved_total, active_total.copy_negate()])
    if residual > ZERO and not baseline_available:
        raise BaselineUnavailable(
            f"baseline {baseline_id!r} is unavailable with residual {residual}; "
            "candidate weights must not be renormalized"
        )
    return TrialRankingBudget(baseline_id, residual, weights, returned, trial_total_cap)


def activate_approved_primary_ranker(
    *, replacement_id: str, approval_ref: str, eligible: bool
) -> ApprovedPrimaryRanker:
    """Represent a separately approved 100% primary-ranker replacement.

    The caller must already have verified the referenced approval. A nonempty
    reference is provenance, not authentication. Trial promotion, a baseline
    outage, or a high score never invokes this operation automatically.
    """
    _identifier(replacement_id, "replacement_id")
    _identifier(approval_ref, "approval_ref")
    if not isinstance(eligible, bool):
        raise InvalidRankingBudget("eligible must be bool")
    if not eligible:
        raise ReplacementUnavailable("approved primary ranker is not currently eligible")
    return ApprovedPrimaryRanker(replacement_id, approval_ref)
