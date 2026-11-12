from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import DefaultContext, Decimal, Inexact, Rounded, ROUND_DOWN, localcontext
import unittest

from alignment import (
    AlignmentPolicy, Observation, Scope, derive_turnover_fraction, derive_vwap,
    select_asof,
)

D = Decimal
LOCAL = timezone(timedelta(hours=8))
DAY = date(2026, 11, 11)
SECURITY = "11111111-1111-4111-8111-111111111111"


def stamp(hour=9, minute=31, second=0, microsecond=0):
    return datetime(2026, 11, 11, hour, minute, second, microsecond, tzinfo=LOCAL)


def scope(field="last_price_raw"):
    unit = {"last_price_raw": "CNY/share", "turnover_amount_cny": "CNY",
            "volume_shares": "shares", "shares.float": "shares"}.get(field, "CNY/share")
    return Scope(SECURITY, "provider_a", "quotes", "1", "1", field, unit,
                 "CNY", "1m", "continuous_am", DAY, "raw", "trade_aggregation", "1", "snapshot-1")


def obs(identity="a", *, field="last_price_raw", event=None, available=None,
        value="10", revision=1, natural_key=None, **kwargs):
    event = event or stamp()
    return Observation(identity, natural_key or identity, revision, scope(field), event,
                       available or event, D(value) if value is not None else None, **kwargs)


def align(items, *, selected_scope=None, target=None, cutoff=None, policy=None):
    return select_asof(items, scope=selected_scope or scope(),
                       target_event_time=target or stamp(), decision_as_of=cutoff or stamp(),
                       policy=policy or AlignmentPolicy("state-1", "state_hold", 10))


def bars(amount="1000.001", volume="100"):
    end = stamp()
    common = dict(event=end, available=end + timedelta(seconds=1),
                  observation_window_start=end - timedelta(minutes=1),
                  observation_window_end=end, is_final=True)
    return (
        replace(obs("amount", field="turnover_amount_cny", value=amount, **common),
                scope=replace(scope("turnover_amount_cny"), endpoint_id="bars")),
        replace(obs("volume", field="volume_shares", value=volume, **common),
                scope=replace(scope("volume_shares"), endpoint_id="bars")),
    )


def share_scope():
    return replace(scope("shares.float"), source_id="capital_disclosure", endpoint_id="capital",
                   timeframe="not-applicable", session_scope="effective_state", trade_date=None,
                   currency=None, methodology_id="circulating_shares")


def shares(identity="capital-1", value="1000", **kwargs):
    return replace(obs(identity, field="shares.float", event=stamp(9, 0),
                       available=stamp(9, 1), value=value, **kwargs), scope=share_scope())


class AlignmentTests(unittest.TestCase):
    def test_only_backward_and_registered_state_fields(self):
        for direction in ("nearest", "forward"):
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, "ONLY_BACKWARD"):
                AlignmentPolicy("p", "state_hold", 10, strategy=direction)
        for field in ("volume_shares", "turnover_amount_cny", "open_price_raw",
                      "high_price_raw", "low_price_raw", "close_price_raw", "unregistered"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "NOT_REGISTERED"):
                align([], selected_scope=scope(field))
        with self.assertRaisesRegex(ValueError, "NEVER_SILENTLY"):
            AlignmentPolicy("p", "state_hold", 10, latest_invalid_policy="last_good_value")

    def test_event_and_available_cutoffs_both_apply(self):
        old = obs("old", event=stamp(9, 30, 59), value="10")
        late = obs("late", event=stamp(9, 31), available=stamp(9, 31, 1), value="11")
        future = obs("future", event=stamp(9, 31, 1), available=stamp(9, 30, 59), value="12")
        result = align([future, late, old])
        self.assertEqual(result.value, D("10"))
        self.assertEqual(result.observation.observation_id, "old")
        self.assertEqual(result.age_seconds, D("1"))
        self.assertEqual(result.observation.event_time, stamp(9, 30, 59))

    def test_revision_availability_and_invalid_latest_do_not_resurrect_old_value(self):
        original = obs("r1", natural_key="quote/0931", value="10")
        corrected = replace(original, observation_id="r2", observation_revision=2,
                            available_at=stamp(9, 31, 2), value=None, field_status="invalid")
        early = align([corrected, original])
        late = align([corrected, original], cutoff=stamp(9, 31, 3))
        self.assertEqual(early.value, D("10"))
        self.assertIsNone(late.value)
        self.assertEqual(late.field_status, "invalid")
        self.assertEqual(late.observation.observation_revision, 2)

    def test_revision_identity_wins_over_arrival_order(self):
        first = obs("r1", natural_key="same", revision=1, value="10", available=stamp(9, 31, 5))
        second = obs("r2", natural_key="same", revision=2, value="11", available=stamp(9, 31, 2))
        self.assertEqual(align([first, second], cutoff=stamp(9, 31, 6)).value, D("11"))

    def test_missing_invalid_disputed_latest_do_not_fall_back(self):
        old = obs("old", event=stamp(9, 30, 59), value="10")
        for status in ("missing", "invalid", "disputed", "not_applicable"):
            latest = obs("latest", value=None, field_status=status)
            with self.subTest(status=status):
                result = align([old, latest])
                self.assertIsNone(result.value)
                self.assertEqual(result.field_status, status)
                self.assertEqual(result.reason, "LATEST_VALUE_UNUSABLE")

    def test_scope_never_silently_changes_source_method_or_session(self):
        base = obs()
        differences = {
            "source_id": "provider_b", "endpoint_id": "different", "source_schema_version": "2",
            "mapping_version": "2", "canonical_unit": "USD/share", "currency": "USD",
            "methodology_id": "order_imbalance", "methodology_version": "2", "timeframe": "5m",
            "snapshot_id": "snapshot-2",
            "session_scope": "continuous_pm", "trade_date": date(2026, 11, 10),
            "adjustment_mode": "forward_adjusted", "instrument_id": "22222222-2222-4222-8222-222222222222",
        }
        for key, value in differences.items():
            with self.subTest(key=key):
                other = replace(base, scope=replace(base.scope, **{key: value}))
                self.assertEqual(align([other]).reason, "NO_SCOPE_MATCH")

    def test_age_boundary_is_precise_and_source_clock_is_preserved(self):
        observation = obs(event=stamp(9, 30, 50))
        at_limit = align([observation], target=stamp(9, 31), cutoff=stamp(9, 31))
        after_limit = align([observation], target=stamp(9, 31, 0, 1), cutoff=stamp(9, 31, 0, 1))
        self.assertEqual(at_limit.value, D("10"))
        self.assertIsNone(after_limit.value)
        self.assertEqual(after_limit.freshness_state, "stale")
        self.assertEqual(after_limit.age_seconds, D("10.000001"))
        self.assertEqual(observation.event_time, stamp(9, 30, 50))

    def test_timezone_equivalence_and_naive_time_rejected(self):
        observation = obs()
        result = align([observation], target=stamp().astimezone(timezone.utc))
        self.assertEqual(result.value, D("10"))
        self.assertIs(result.observation.event_time.tzinfo, LOCAL)
        with self.assertRaisesRegex(ValueError, "TIMEZONE"):
            align([observation], target=stamp().replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "TARGET_AFTER"):
            align([observation], target=stamp(9, 32))

    def test_expired_latest_state_does_not_reveal_old_open_ended_state(self):
        old = shares("old", value="1000", effective_from=stamp(8, 0))
        latest = replace(shares("new", value="2000"), effective_from=stamp(9, 5), effective_to=stamp(9, 10))
        result = align([old, latest], selected_scope=share_scope(),
                       policy=AlignmentPolicy("capital", "state_hold", 86400))
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "LATEST_STATE_EXPIRED")
        self.assertEqual(result.observation.observation_id, "new")

    def test_announced_future_state_not_applied_early(self):
        old = shares("old", effective_from=stamp(8, 0))
        future = replace(shares("future", value="2000"), effective_from=stamp(10, 0))
        result = align([future, old], selected_scope=share_scope(),
                       policy=AlignmentPolicy("capital", "state_hold", 86400))
        self.assertEqual(result.value, D("1000"))

    def test_interval_field_requires_exact_event_without_forward_fill(self):
        _, volume = bars()
        result = align([volume], selected_scope=volume.scope, target=stamp(9, 32), cutoff=stamp(9, 32),
                       policy=AlignmentPolicy("interval", "exact_event", 600))
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "EXACT_EVENT_REQUIRED")
        earlier = replace(volume, observation_id="earlier", natural_key="earlier",
                          event_time=stamp(9, 30), available_at=stamp(9, 30), effective_from=stamp(9, 29))
        exact = replace(volume, effective_from=stamp(8, 0))
        match = align([exact, earlier], selected_scope=volume.scope, cutoff=stamp(9, 31, 2),
                      policy=AlignmentPolicy("interval", "exact_event", 600))
        self.assertEqual(match.observation.observation_id, "volume")

    def test_duplicate_id_is_idempotent_but_payload_conflict_fails(self):
        observation = obs()
        self.assertEqual(align([observation, observation]).value, D("10"))
        with self.assertRaisesRegex(ValueError, "PAYLOAD_CONFLICT"):
            align([observation, replace(observation, value=D("11"))])

    def test_ambiguous_revision_and_observation_are_disputed(self):
        original = obs("a", natural_key="shared")
        same_revision = replace(original, observation_id="b", value=D("11"))
        self.assertEqual(align([original, same_revision]).reason, "AMBIGUOUS_REVISION")
        same_time = obs("other_key", value="12")
        result = align([original, same_time])
        self.assertIsNone(result.value)
        self.assertEqual(result.field_status, "disputed")
        self.assertEqual(result.reason, "AMBIGUOUS_OBSERVATION")
        latest = obs("latest", event=stamp(9, 31, 1), value="20")
        valid = align([original, same_revision, latest], target=stamp(9, 31, 1), cutoff=stamp(9, 31, 1))
        self.assertEqual(valid.value, D("20"))


class DerivedInputTests(unittest.TestCase):
    def vwap(self, amount=None, volume=None, **kwargs):
        default_amount, default_volume = bars()
        return derive_vwap(amount or default_amount, volume or default_volume,
                           decision_as_of=kwargs.pop("decision_as_of", stamp(9, 31, 2)),
                           computed_at=kwargs.pop("computed_at", stamp(9, 31, 3)),
                           definition_version="interval-vwap-1", **kwargs)

    def turnover(self, observations, *, volume=None, denominator_scope=None, **kwargs):
        return derive_turnover_fraction(volume or bars()[1], observations,
            denominator_scope=denominator_scope or share_scope(),
            denominator_policy=kwargs.pop("denominator_policy", AlignmentPolicy("capital", "state_hold", 86400)),
            decision_as_of=kwargs.pop("decision_as_of", stamp(9, 31, 2)),
            computed_at=kwargs.pop("computed_at", stamp(9, 31, 3)),
            definition_version="circulating-turnover-1", **kwargs)

    def test_vwap_hand_calculated_units_lineage_and_completion_time(self):
        result = self.vwap()
        self.assertEqual(result.value, D("10.000010000000"))
        self.assertEqual(result.canonical_unit, "CNY/share")
        self.assertEqual(result.input_observation_ids, ("amount", "volume"))
        self.assertEqual(result.available_at, stamp(9, 31, 3))
        self.assertEqual(result.field_status, "derived")

    def test_local_decimal_context_does_not_change_result(self):
        amount, volume = bars(amount="12345678.123456789012", volume="3")
        expected = D("4115226.041152263004")
        with localcontext() as ctx:
            ctx.prec = 6
            result = self.vwap(amount, volume)
        self.assertEqual(result.value, expected)
        with localcontext() as ctx:
            ctx.prec = 6
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            ctx.rounding = ROUND_DOWN
            repeating = self.vwap(*bars(amount="1", volume="3"))
        self.assertEqual(repeating.value, D("0.333333333333"))

    def test_mutated_decimal_default_context_cannot_change_calculation(self):
        original = DefaultContext.copy()
        try:
            DefaultContext.traps[Inexact] = True
            DefaultContext.traps[Rounded] = True
            DefaultContext.Emax = 3
            DefaultContext.Emin = -3
            DefaultContext.rounding = ROUND_DOWN
            result = self.vwap(*bars(amount="1000000", volume="3"))
        finally:
            for attribute in ("prec", "rounding", "Emin", "Emax", "capitals", "clamp"):
                setattr(DefaultContext, attribute, getattr(original, attribute))
            DefaultContext.traps = original.traps.copy()
            DefaultContext.flags = original.flags.copy()
        self.assertEqual(result.value, D("333333.333333333333"))

    def test_positive_derived_value_cannot_silently_underflow_to_zero(self):
        with self.assertRaisesRegex(ValueError, "RESOLUTION_UNDERFLOW"):
            self.vwap(*bars(amount="0.000000000001", volume="10000000000000"))

    def test_vwap_rejects_cross_window_source_method_currency_and_adjustment(self):
        amount, volume = bars()
        invalid = [
            replace(volume, observation_window_start=stamp(9, 29)),
            replace(volume, scope=replace(volume.scope, endpoint_id="other")),
            replace(volume, scope=replace(volume.scope, source_id="other")),
            replace(volume, scope=replace(volume.scope, methodology_version="2")),
            replace(volume, scope=replace(volume.scope, snapshot_id="other-snapshot")),
            replace(volume, scope=replace(volume.scope, currency="USD")),
            replace(volume, scope=replace(volume.scope, canonical_unit="hands")),
            replace(volume, scope=replace(volume.scope, adjustment_mode="forward_adjusted")),
            replace(volume, value=D("1.5")),
        ]
        for sample in invalid:
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                self.vwap(amount, sample)

    def test_zero_volume_is_unknown_vwap_not_zero_price(self):
        result = self.vwap(*bars(amount="0", volume="0"))
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "ZERO_VOLUME")
        self.assertEqual(result.quality_status, "partial")
        contradictory = self.vwap(*bars(amount="100", volume="0"))
        self.assertEqual(contradictory.reason, "AMOUNT_WITHOUT_VOLUME")
        self.assertEqual(contradictory.quality_status, "invalid")
        zero_amount = self.vwap(*bars(amount="0", volume="100"))
        self.assertEqual(zero_amount.reason, "VOLUME_WITHOUT_AMOUNT")
        self.assertEqual(zero_amount.quality_status, "invalid")

    def test_incomplete_future_or_invalid_inputs_cannot_be_derived(self):
        amount, volume = bars()
        with self.assertRaisesRegex(ValueError, "FINAL_BAR"):
            self.vwap(amount, replace(volume, is_final=False))
        with self.assertRaisesRegex(ValueError, "NOT_AVAILABLE"):
            self.vwap(amount, replace(volume, available_at=stamp(9, 31, 4)))
        with self.assertRaisesRegex(ValueError, "COMPUTED_BEFORE"):
            self.vwap(computed_at=stamp(9, 31, 1))
        invalid = self.vwap(amount, replace(volume, value=None, field_status="invalid"))
        self.assertIsNone(invalid.value)
        self.assertEqual(invalid.quality_status, "invalid")

    def test_then_known_share_denominator_excludes_later_correction(self):
        original = shares(natural_key="capital/2026")
        correction = replace(original, observation_id="capital-2", observation_revision=2,
                             value=D("2000"), available_at=stamp(10, 0))
        early = self.turnover([correction, original])
        later = self.turnover([original, correction], decision_as_of=stamp(10, 1), computed_at=stamp(10, 1, 1))
        self.assertEqual(early.value, D("0.100000000000"))
        self.assertEqual(early.input_observation_ids, ("volume", "capital-1"))
        self.assertEqual(later.value, D("0.050000000000"))
        self.assertEqual(later.input_observation_ids, ("volume", "capital-2"))
        self.assertEqual(early.value, D("0.100000000000"))  # old artifact remains unchanged

    def test_free_float_basis_and_wrong_security_are_not_substitutes(self):
        for wrong_scope in (
            replace(share_scope(), methodology_id="free_float_shares"),
            replace(share_scope(), instrument_id="22222222-2222-4222-8222-222222222222"),
        ):
            with self.subTest(scope=wrong_scope), self.assertRaisesRegex(ValueError, "CIRCULATING"):
                self.turnover([shares()], denominator_scope=wrong_scope)

    def test_latest_invalid_share_state_is_not_replaced_by_prior_denominator(self):
        old = shares(natural_key="capital")
        invalid = replace(old, observation_id="capital-new", observation_revision=2,
                          value=None, field_status="invalid", available_at=stamp(9, 20))
        result = self.turnover([old, invalid])
        self.assertIsNone(result.value)
        self.assertEqual(result.quality_status, "invalid")
        self.assertEqual(result.input_observation_ids, ("volume", "capital-new"))

    def test_stale_share_state_and_no_denominator_remain_missing(self):
        result = self.turnover([shares()], denominator_policy=AlignmentPolicy("capital", "state_hold", 1))
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "MAX_AGE_EXCEEDED")
        self.assertIsNone(self.turnover([]).value)

    def test_share_change_inside_bar_requires_finer_interval(self):
        old = shares("old", value="1000")
        changed = replace(shares("changed", value="2000"), effective_from=stamp(9, 30, 30))
        result = self.turnover([old, changed])
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "DENOMINATOR_CHANGED_WITHIN_WINDOW")
        self.assertEqual(result.input_observation_ids, ("volume", "changed"))

    def test_turnover_fraction_is_not_calibrated_probability(self):
        result = self.turnover([shares(value="50")])
        self.assertEqual(result.value, D("2.000000000000"))
        self.assertEqual(result.canonical_unit, "ratio")

    def test_decimal_float_and_precision_boundaries_are_explicit(self):
        with self.assertRaisesRegex(ValueError, "FINITE_DECIMAL"):
            replace(obs(), value=0.1)
        with self.assertRaisesRegex(ValueError, "38_12"):
            replace(obs(), value=D("0.0000000000001"))
        with self.assertRaisesRegex(ValueError, "38_12"):
            replace(obs(), value=D("100000000000000000000000000"))
        with self.assertRaisesRegex(ValueError, "FINITE_DECIMAL"):
            replace(obs(), value=D("NaN"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
