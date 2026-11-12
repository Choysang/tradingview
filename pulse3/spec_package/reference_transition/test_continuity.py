import unittest
from decimal import Decimal, localcontext

from continuity import (
    ApprovedPrimaryRanker,
    BaselineUnavailable,
    InvalidRankingBudget,
    ReplacementUnavailable,
    activate_approved_primary_ranker,
    allocate_trial_ranking_budget,
)


D = Decimal


class RankingContinuityTests(unittest.TestCase):
    def allocate(self, budgets, eligible, **kwargs):
        return allocate_trial_ranking_budget(
            baseline_id="baseline-v1",
            approved_budgets=budgets,
            eligible_candidate_ids=eligible,
            **kwargs,
        )

    def test_no_candidates_leaves_full_baseline(self):
        result = self.allocate({}, set())
        self.assertEqual(result.baseline_weight, D("1"))
        self.assertEqual(result.candidate_weights, ())
        self.assertEqual(result.returned_to_baseline, D("0"))

    def test_one_ten_percent_trial_stays_ten_percent(self):
        result = self.allocate({"trial": D("0.1")}, {"trial"})
        self.assertEqual(dict(result.candidate_weights), {"trial": D("0.1")})
        self.assertEqual(result.baseline_weight, D("0.9"))
        self.assertEqual(result.trial_total_cap, D("0.1"))

    def test_multiple_candidates_keep_absolute_approved_budgets(self):
        result = self.allocate({"b": D("0.06"), "a": D("0.03")}, {"a", "b"})
        self.assertEqual(result.candidate_weights, (("a", D("0.03")), ("b", D("0.06"))))
        self.assertEqual(result.baseline_weight, D("0.91"))

    def test_ineligible_share_returns_to_baseline_not_other_trial(self):
        result = self.allocate({"a": D("0.04"), "b": D("0.06")}, {"a"})
        self.assertEqual(dict(result.candidate_weights), {"a": D("0.04"), "b": D("0")})
        self.assertEqual(result.baseline_weight, D("0.96"))
        self.assertEqual(result.returned_to_baseline, D("0.06"))

    def test_all_ineligible_returns_full_baseline(self):
        result = self.allocate({"a": D("0.04"), "b": D("0.06")}, set())
        self.assertEqual(result.baseline_weight, D("1"))
        self.assertEqual(result.returned_to_baseline, D("0.10"))

    def test_baseline_outage_never_renormalizes_candidates(self):
        for budgets, eligible in [({}, set()), ({"a": D("0.1")}, {"a"})]:
            with self.subTest(budgets=budgets):
                with self.assertRaisesRegex(BaselineUnavailable, "residual.*must not be renormalized"):
                    self.allocate(budgets, eligible, baseline_available=False)

    def test_cap_applies_to_approved_not_only_active_candidates(self):
        with self.assertRaisesRegex(InvalidRankingBudget, "exceeds"):
            self.allocate({"a": D("0.08"), "b": D("0.08")}, {"a"})

    def test_invalid_weight_types_and_values_are_rejected(self):
        for value in [0.1, 0, "0.1", True, D("-0.01"), D("NaN"), D("Infinity"), D("-Infinity")]:
            with self.subTest(value=value):
                with self.assertRaises(InvalidRankingBudget):
                    self.allocate({"a": value}, {"a"})

    def test_invalid_caps_including_full_replacement_are_rejected(self):
        for cap in [0.1, D("-0.1"), D("NaN"), D("Infinity"), D("1"), D("1.1")]:
            with self.subTest(cap=cap):
                with self.assertRaises(InvalidRankingBudget):
                    self.allocate({}, set(), trial_total_cap=cap)

    def test_duplicate_baseline_or_unapproved_eligibility_is_rejected(self):
        for budgets, eligible in [({"baseline-v1": D("0")}, set()), ({}, {"unknown"})]:
            with self.subTest(budgets=budgets, eligible=eligible):
                with self.assertRaises(InvalidRankingBudget):
                    self.allocate(budgets, eligible)

    def test_decimal_precision_is_independent_of_caller_context(self):
        tiny = D("1e-40")
        with localcontext() as context:
            context.prec = 6
            result = self.allocate({"tiny": tiny}, {"tiny"})
            self.assertEqual(result.baseline_weight, D("0." + "9" * 40))
            self.assertEqual(dict(result.candidate_weights)["tiny"], tiny)
            with self.assertRaises(InvalidRankingBudget):
                self.allocate({"a": D("0.05"), "b": D("0.05000000000000000000000000001")}, {"a", "b"})

    def test_zero_cap_and_zero_budget_do_not_create_weight(self):
        result = self.allocate({"a": D("0")}, {"a"}, trial_total_cap=D("0"))
        self.assertEqual(result.baseline_weight, D("1"))
        self.assertEqual(dict(result.candidate_weights), {"a": D("0")})

    def test_full_replacement_requires_the_separate_approved_interface(self):
        result = activate_approved_primary_ranker(
            replacement_id="new-primary-v2", approval_ref="promotion-decision-17", eligible=True
        )
        self.assertIsInstance(result, ApprovedPrimaryRanker)
        self.assertEqual(result.weight, D("1"))
        self.assertEqual(result.approval_ref, "promotion-decision-17")
        with self.assertRaises(InvalidRankingBudget):
            activate_approved_primary_ranker(replacement_id="new-primary-v2", approval_ref="", eligible=True)
        with self.assertRaises(ReplacementUnavailable):
            activate_approved_primary_ranker(replacement_id="new-primary-v2", approval_ref="decision-17", eligible=False)


if __name__ == "__main__":
    unittest.main()
