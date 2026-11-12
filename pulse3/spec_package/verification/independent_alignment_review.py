"""Independent boundary probes, not production or database tests.

Does not import the author's test fixtures. Every numerical input is synthetic.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DefaultContext, Inexact, Rounded, localcontext
from pathlib import Path
import hashlib
import json
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "reference_alignment"))
from alignment import (  # noqa: E402
    AlignmentPolicy, Observation, Scope, derive_turnover_fraction, derive_vwap, select_asof,
)

D = Decimal
TZ = timezone(timedelta(hours=8))
DAY = date(2026, 11, 11)
INSTRUMENT = "11111111-1111-4111-8111-111111111111"


def t(hour=9, minute=31, second=0, microsecond=0):
    return datetime(2026, 11, 11, hour, minute, second, microsecond, tzinfo=TZ)


def scope(field="last_price_raw", **kw):
    values = dict(
        instrument_id=INSTRUMENT, source_id="audit_a", endpoint_id="audit_bars",
        source_schema_version="1", mapping_version="1", field_path=field,
        canonical_unit={"last_price_raw": "CNY/share", "turnover_amount_cny": "CNY",
                        "volume_shares": "shares", "shares.float": "shares"}[field],
        currency="CNY", timeframe="1m", session_scope="continuous_am", trade_date=DAY,
        adjustment_mode="raw", methodology_id="trade_aggregation", methodology_version="1",
        snapshot_id="audit-snapshot-1",
    )
    values.update(kw)
    return Scope(**values)


def obs(identity, value, *, field="last_price_raw", event=None, available=None,
        revision=1, natural_key=None, selected_scope=None, **kw):
    event = event or t()
    return Observation(identity, natural_key or identity, revision,
                       selected_scope or scope(field), event, available or event,
                       D(value) if value is not None else None, **kw)


def select(rows, *, selected_scope=None, target=None, cutoff=None, mode="state_hold", age=100000):
    return select_asof(rows, scope=selected_scope or scope(), target_event_time=target or t(),
                       decision_as_of=cutoff or t(9, 32),
                       policy=AlignmentPolicy("audit-policy-1", mode, age))


def bar_pair(amount="10", volume="3"):
    common = dict(event=t(), available=t(9, 31, 1), observation_window_start=t(9, 30),
                  observation_window_end=t(), is_final=True)
    return (obs("amount", amount, field="turnover_amount_cny", **common),
            obs("volume", volume, field="volume_shares", **common))


def vwap(amount="10", volume="3", *, computed=None):
    a, v = bar_pair(amount, volume)
    return derive_vwap(a, v, decision_as_of=t(9, 31, 2), computed_at=computed or t(9, 31, 4),
                       definition_version="audit-vwap-1")


def capital(value="100", *, identity="capital", event=None, available=None, **kw):
    return obs(identity, value, field="shares.float", event=event or t(9, 0),
               available=available or t(9, 1),
               selected_scope=scope("shares.float", source_id="capital_provider", endpoint_id="capital",
                                    snapshot_id="capital-snapshot-1", timeframe="not-applicable",
                                    session_scope="effective_state", trade_date=None, currency=None,
                                    methodology_id="circulating_shares"), **kw)


def turnover(capitals, *, cutoff=None, computed=None):
    volume = bar_pair(volume="10")[1]
    return derive_turnover_fraction(volume, capitals, denominator_scope=capital().scope,
                                    denominator_policy=AlignmentPolicy("audit-capital-1", "state_hold", 86400),
                                    decision_as_of=cutoff or t(9, 31, 2),
                                    computed_at=computed or t(9, 31, 3), definition_version="audit-turnover-1")


class IndependentBoundaries(unittest.TestCase):
    def test_process_default_context_does_not_change_fixed_numeric_policy(self):
        previous = DefaultContext.copy()
        try:
            DefaultContext.traps[Inexact] = True
            DefaultContext.traps[Rounded] = True
            DefaultContext.Emax = 4
            DefaultContext.Emin = -4
            self.assertEqual(vwap().value, D("3.333333333333"))
            observation = obs("a", "10", event=t(9, 30, 59, 999999))
            self.assertEqual(select([observation]).age_seconds, D("0.000001"))
        finally:
            DefaultContext.Emax, DefaultContext.Emin = previous.Emax, previous.Emin
            for signal in DefaultContext.traps:
                DefaultContext.traps[signal] = previous.traps[signal]

    def test_tiny_positive_vwap_cannot_be_valid_zero_price(self):
        try:
            result = vwap("0.000000000001", "10000000000000")
        except ValueError:
            return  # Explicit numeric-domain rejection is also an honest policy.
        self.assertFalse(result.value == 0 and result.quality_status == "valid")
        self.assertIsNone(result.value)
        self.assertIsNotNone(result.reason)

    def test_revision_moving_event_forward_does_not_resurrect_old_revision(self):
        predecessor = obs("preceding", "9", event=t(9, 30, 59))
        r1 = obs("r1", "10", natural_key="corrected", revision=1)
        r2 = replace(r1, observation_id="r2", observation_revision=2,
                     event_time=t(9, 31, 1), available_at=t(9, 31, 2), value=D("11"))
        result = select([predecessor, r1, r2])
        self.assertEqual(result.observation.observation_id, "preceding")
        self.assertEqual(result.value, D("9"))

    def test_later_revision_can_only_affect_later_decision_cutoff(self):
        r1 = obs("r1", "10", natural_key="same")
        r2 = replace(r1, observation_id="r2", observation_revision=2,
                     available_at=t(10, 0), value=D("12"))
        early = select([r1, r2], cutoff=t(9, 32))
        late = select([r1, r2], cutoff=t(10, 1))
        self.assertEqual(early.value, D("10"))
        self.assertEqual(late.value, D("12"))
        self.assertEqual(early.observation.available_at, t())

    def test_exact_event_uses_event_clock_not_state_effective_sort(self):
        earlier = obs("earlier", "9", event=t(9, 30), effective_from=t(9, 30))
        target = obs("target", "10", event=t(), effective_from=t(8, 0))
        result = select([earlier, target], mode="exact_event")
        self.assertEqual(result.observation.observation_id, "target")
        self.assertEqual(result.value, D("10"))

    def test_equal_numeric_values_from_different_snapshot_are_not_one_vwap(self):
        amount, volume = bar_pair()
        volume = replace(volume, scope=replace(volume.scope, snapshot_id="audit-snapshot-2"))
        with self.assertRaises(ValueError):
            derive_vwap(amount, volume, decision_as_of=t(9, 32), computed_at=t(9, 32, 1),
                        definition_version="audit-vwap-1")

    def test_late_invalid_retraction_is_visible_without_last_good_fallback(self):
        old = obs("old", "10", event=t(9, 30, 59))
        now = obs("now", "11", natural_key="now")
        retract = replace(now, observation_id="retracted", observation_revision=2,
                          available_at=t(9, 31, 3), field_status="invalid", value=None)
        self.assertEqual(select([old, now, retract], cutoff=t(9, 31, 2)).value, D("11"))
        later = select([old, now, retract], cutoff=t(9, 31, 4))
        self.assertIsNone(later.value)
        self.assertEqual(later.observation.observation_id, "retracted")

    def test_scope_noise_does_not_win_by_higher_revision(self):
        selected = obs("selected", "10")
        wrong = [replace(selected, observation_id="wrong" + str(i), observation_revision=100,
                         value=D("900"), scope=replace(selected.scope, **change))
                 for i, change in enumerate([
                     {"source_id": "other"}, {"session_scope": "continuous_pm"},
                     {"trade_date": date(2026, 11, 10)}, {"snapshot_id": "other_snapshot"},
                     {"methodology_version": "2"}, {"mapping_version": "2"}])]
        result = select([*wrong, selected])
        self.assertEqual(result.value, D("10"))
        self.assertEqual(result.observation.observation_id, "selected")

    def test_obsolete_revision_conflict_does_not_block_new_unambiguous_state(self):
        old_a = obs("old-a", "8", natural_key="old-conflict", event=t(9, 30))
        old_b = replace(old_a, observation_id="old-b", value=D("9"))
        current = obs("current", "10", event=t())
        result = select([old_a, old_b, current])
        self.assertEqual(result.value, D("10"))
        self.assertEqual(result.observation.observation_id, "current")

    def test_conflicting_top_revision_is_not_cured_by_time_filtering(self):
        before = obs("a", "10", natural_key="conflict", event=t())
        after = replace(before, observation_id="b", event_time=t(9, 32), value=D("11"))
        result = select([before, after])
        self.assertIsNone(result.value)
        self.assertEqual(result.field_status, "disputed")

    def test_expiry_boundary_is_exclusive_and_no_previous_state_reappears(self):
        predecessor = obs("old", "9", event=t(9, 29), effective_from=t(9, 29))
        expiring = obs("current", "10", event=t(9, 30), effective_from=t(9, 30), effective_to=t())
        result = select([predecessor, expiring])
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "LATEST_STATE_EXPIRED")

    def test_future_effective_known_state_does_not_apply_early(self):
        current = capital("100", effective_from=t(8, 0))
        future = capital("200", identity="new", effective_from=t(10, 0))
        result = select([current, future], selected_scope=current.scope)
        self.assertEqual(result.value, D("100"))

    def test_derived_availability_is_completion_and_retains_input_snapshots(self):
        result = vwap(computed=t(11, 0))
        self.assertEqual(result.available_at, t(11, 0))
        self.assertEqual(result.decision_as_of, t(9, 31, 2))
        self.assertEqual(result.input_snapshot_ids, ("audit-snapshot-1", "audit-snapshot-1"))

    def test_late_share_disclosure_is_not_backdated(self):
        state = capital("100", available=t(10, 0), effective_from=t(8, 0))
        before = turnover([state])
        after = turnover([state], cutoff=t(10, 1), computed=t(10, 1, 1))
        self.assertIsNone(before.value)
        self.assertEqual(after.value, D("0.100000000000"))
        self.assertEqual(after.available_at, t(10, 1, 1))
        self.assertEqual(after.input_snapshot_ids, ("audit-snapshot-1", "capital-snapshot-1"))

    def test_share_transition_within_window_requires_declared_handling(self):
        prior = capital("100", effective_from=t(8, 0))
        current = capital("200", identity="new", event=t(9, 30, 30),
                          available=t(9, 30, 31), effective_from=t(9, 30, 30))
        result = turnover([prior, current])
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "DENOMINATOR_CHANGED_WITHIN_WINDOW")

    def test_full_38_12_amount_under_restrictive_caller_context(self):
        amount = "99999999999999999999999999.999999999999"
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.Emax = 3
            ctx.Emin = -3
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            result = vwap(amount, "1")
        self.assertEqual(result.value, D(amount))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IndependentBoundaries)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    payload = {
        "scope": "Independent synthetic unit boundary tests; no database, real API, source, performance or profitability tested.",
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "passed": result.wasSuccessful(),
        "alignment_sha256": hashlib.sha256((BASE / "reference_alignment/alignment.py").read_bytes()).hexdigest(),
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (BASE / "verification/independent_alignment_result.json").write_text(json.dumps(payload, indent=2) + "\n")
    sys.exit(0 if result.wasSuccessful() else 1)
