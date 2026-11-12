"""Synthetic acceptance cases; no live feed, production database or load claim."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import unittest

from normalization import normalize, comparable, MappingDrift
from release_store import ReleaseStore
from source_health import SourceHealth, DurableCursor

PROFILES = json.loads((Path(__file__).resolve().parents[1] / "database" /
                      "source_mapping_examples.json").read_text())["mapping_profiles"]
IID = "00000000-0000-4000-8000-000000000001"


def resolver(identifier, trade_date, market):
    if trade_date != "2026-11-10":
        raise ValueError("identity_date_not_in_fixture")
    if identifier == "000001.SZ" or (identifier == "000001" and market == "XSHE"):
        return IID
    raise ValueError("identifier_not_in_fixture")


def ctx(profile):
    return {**{k: profile[k] for k in ("source_id", "endpoint_id", "source_schema_version", "mapping_version")},
            "market": "XSHE", "trade_date": "2026-11-10", "adjustment_mode": "raw",
            "first_seen_at": "2026-11-10T07:00:01Z", "ingested_at": "2026-11-10T07:00:02Z",
            "available_at": "2026-11-10T07:00:03Z"}


class NormalizationCases(unittest.TestCase):
    def run_profile(self, index=0, changes=None, context=None):
        p = PROFILES[index]
        return normalize(p, {**p["synthetic_raw"], **(changes or {})}, context or ctx(p), resolver)

    def test_all_official_mapping_fixture_expectations(self):
        for i, profile in enumerate(PROFILES):
            with self.subTest(profile=profile["profile_id"]):
                result = self.run_profile(i)
                self.assertTrue(result["normalization_valid"])
                for key, value in profile["canonical_expected"].items():
                    self.assertEqual(result[key], value)

    def test_same_field_name_different_endpoint_units(self):
        daily, realtime = self.run_profile(0), self.run_profile(1)
        self.assertEqual(daily["volume_shares"], realtime["volume_shares"])
        self.assertEqual(Decimal(daily["turnover_amount_cny"]), Decimal(realtime["turnover_amount_cny"]))
        bad_ctx = ctx(PROFILES[1])
        with self.assertRaisesRegex(MappingDrift, "endpoint_id"):
            normalize(PROFILES[0], PROFILES[0]["synthetic_raw"], bad_ctx, resolver)

    def test_fractional_shares_not_rounded(self):
        result = self.run_profile(changes={"vol": "100.001"})
        self.assertIsNone(result["volume_shares"])
        self.assertEqual(result["field_quality"]["volume_shares"]["status"], "invalid")
        self.assertFalse(result["normalization_valid"])

    def test_null_and_zero_are_distinct(self):
        missing, zero = self.run_profile(changes={"vol": None}), self.run_profile(changes={"vol": "0", "amount": "0"})
        self.assertIsNone(missing["volume_shares"])
        self.assertEqual(missing["quality_status"], "partial")
        self.assertEqual(zero["volume_shares"], 0)
        self.assertEqual(zero["field_quality"]["volume_shares"]["status"], "observed")

    def test_zero_volume_with_nonzero_amount_is_not_valid_observation(self):
        r = self.run_profile(changes={"vol": "0"})
        self.assertEqual(r["quality_status"], "invalid")
        self.assertFalse(r["normalization_valid"])

    def test_nonfinite_negative_and_float_inputs_are_not_precise_facts(self):
        for value in ["NaN", "Infinity", "-1", 100.25, True]:
            with self.subTest(value=value):
                r = self.run_profile(changes={"amount": value})
                self.assertEqual(r["field_quality"]["turnover_amount_cny"]["status"], "invalid")

    def test_large_exact_amount_no_float_roundtrip(self):
        r = self.run_profile(changes={"amount": "9007199254740993.123"})
        self.assertEqual(Decimal(r["turnover_amount_cny"]), Decimal("9007199254740993123"))

    def test_external_decimal_context_cannot_silently_round_unit_conversion(self):
        with localcontext() as context:
            context.prec = 6
            r = self.run_profile(changes={"amount": "9007199254740993.1234567890123"})
        self.assertEqual(Decimal(r["turnover_amount_cny"]), Decimal("9007199254740993123.4567890123"))
        self.assertTrue(r["normalization_valid"])

    def test_excess_precision_and_unsafe_integer_are_explicitly_rejected(self):
        r = self.run_profile(changes={"amount": "1.1234567890123456"})
        self.assertEqual(r["field_quality"]["turnover_amount_cny"]["status"], "invalid")
        r = self.run_profile(1, changes={"vol": 9007199254740992})
        self.assertFalse(r["normalization_valid"])

    def test_adjusted_prices_cannot_enter_raw_mapping(self):
        context = {**ctx(PROFILES[2]), "adjust": "qfq"}
        with self.assertRaisesRegex(MappingDrift, "adjusted"):
            self.run_profile(2, context=context)

    def test_required_field_rename_pauses_mapping(self):
        raw = deepcopy(PROFILES[0]["synthetic_raw"])
        raw["volume"] = raw.pop("vol")
        with self.assertRaisesRegex(MappingDrift, "missing_mapped_keys"):
            normalize(PROFILES[0], raw, ctx(PROFILES[0]), resolver)

    def test_new_field_is_retained_without_schema_invention(self):
        r = self.run_profile(changes={"vendor_future_field": "unknown"})
        self.assertEqual(r["extra_fields"], {"vendor_future_field": "unknown"})
        self.assertEqual(r["raw"]["vendor_future_field"], "unknown")
        self.assertTrue(r["normalization_valid"])

    def test_missing_bare_code_market_is_unresolved(self):
        context = ctx(PROFILES[2]); context.pop("market")
        r = self.run_profile(2, context=context)
        self.assertIsNone(r["instrument_id"])
        self.assertFalse(r["normalization_valid"])

    def test_ohlc_contradiction_is_invalid(self):
        r = self.run_profile(changes={"high": "9.00"})
        self.assertEqual(r["field_quality"]["high_price_raw"]["status"], "invalid")

    def test_times_and_finality_are_not_invented(self):
        r = self.run_profile(1)
        self.assertIsNone(r["event_time"])
        self.assertIsNone(r["published_at"])
        self.assertFalse(r["is_final"])
        c = {**ctx(PROFILES[0]), "finalization_confirmed": True, "finalization_evidence_id": "close-audit-1"}
        self.assertTrue(self.run_profile(context=c)["is_final"])

    def test_availability_and_date_contradictions_rejected(self):
        with self.assertRaisesRegex(ValueError, "availability"):
            self.run_profile(context={**ctx(PROFILES[0]), "available_at": "2026-11-10T06:00:00Z"})
        with self.assertRaisesRegex(MappingDrift, "trade_date_context_conflict"):
            self.run_profile(context={**ctx(PROFILES[0]), "trade_date": "2026-11-09"})

    def test_same_units_different_observation_scope_not_cross_validated(self):
        base = {**self.run_profile(), "timeframe": "1d", "session_scope": "continuous",
                "event_time": "2026-11-10T07:00:00Z", "adjustment_version": "raw-v1"}
        self.assertTrue(comparable(base, deepcopy(base)))
        self.assertFalse(comparable(base, {**base, "session_scope": "continuous_plus_afterhours"}))
        self.assertFalse(comparable(base, {**base, "event_time": None}))

    def test_revised_observation_preserves_prior_lineage(self):
        prior, revised = self.run_profile(), self.run_profile(changes={"amount": "101.00"})
        self.assertNotEqual(prior["raw_payload_hash"], revised["raw_payload_hash"])
        self.assertEqual(prior["raw"]["amount"], "100.25")
        self.assertEqual(prior["field_quality"]["turnover_amount_cny"]["lineage"]["available_at"], ctx(PROFILES[0])["available_at"])


class PublicationCases(unittest.TestCase):
    def setUp(self):
        self.s = ReleaseStore()
        self.manifest = {"snapshot": "pit-1", "strategy_bundle": "bundle-1", "universe": "main-board-v1"}
        self.required = [("global_ranking", "ALL"), ("sector_ranking", "semiconductors"), ("stock_workspace", IID)]

    def tearDown(self):
        self.s.db.close()

    def prepared(self, rid="r1", as_of="2026-11-10T06:00:00Z", include=3):
        self.s.stage(rid, "short-main-board", as_of, self.manifest, self.required)
        for kind, scope in self.required[:include]:
            self.s.put_view(rid, kind, scope, self.manifest, {"example": rid})

    def test_partial_ranking_set_cannot_become_active(self):
        self.prepared(include=2)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.s.publish("r1")
        self.assertIsNone(self.s.active("short-main-board"))
        self.assertEqual(self.s.changes_after(), [])

    def test_pointer_and_outbox_rollback_together(self):
        self.prepared()
        with self.assertRaises(RuntimeError):
            self.s.publish("r1", fail_after_pointer=True)
        self.assertIsNone(self.s.active("short-main-board"))
        self.assertEqual(self.s.changes_after(), [])
        self.assertTrue(self.s.publish("r1"))

    def test_all_read_models_share_explicit_release(self):
        self.prepared(); self.s.publish("r1")
        reads = [self.s.read_view("short-main-board", kind, scope, "r1") for kind, scope in self.required]
        self.assertEqual({v["release_id"] for v in reads}, {"r1"})

    def test_newer_active_release_not_overwritten_by_late_job(self):
        self.prepared(); self.s.publish("r1")
        self.prepared("r2", "2026-11-10T06:01:00Z"); self.s.publish("r2", "r1")
        self.assertFalse(self.s.publish("r1"))
        self.prepared("old", "2026-11-10T05:00:00Z")
        with self.assertRaisesRegex(ValueError, "older_analysis"):
            self.s.publish("old", "r2")
        self.assertEqual(self.s.active("short-main-board"), "r2")

    def test_compare_and_swap_detects_concurrent_publication(self):
        self.prepared(); self.s.publish("r1")
        self.prepared("r2", "2026-11-10T06:01:00Z")
        with self.assertRaisesRegex(ValueError, "compare_and_swap"):
            self.s.publish("r2", None)

    def test_same_cutoff_older_job_cannot_refresh_expected_pointer_to_win(self):
        self.prepared("old_job")
        self.manifest = {**self.manifest, "snapshot": "new-input"}
        self.prepared("new_job")
        self.s.publish("new_job")
        with self.assertRaisesRegex(ValueError, "calculation_base_changed"):
            self.s.publish("old_job", expected_active="new_job")
        self.assertEqual(self.s.active("short-main-board"), "new_job")

    def test_retry_is_idempotent_and_changed_content_is_not(self):
        self.prepared()
        self.assertFalse(self.s.stage("r1", "short-main-board", "2026-11-10T06:00:00Z", self.manifest, self.required))
        with self.assertRaisesRegex(ValueError, "release_id_content_conflict"):
            self.s.stage("r1", "short-main-board", "2026-11-10T06:00:00Z", {"other": 1}, self.required)
        with self.assertRaisesRegex(ValueError, "projection_content_conflict"):
            self.s.put_view("r1", *self.required[0], self.manifest, {"changed": True})

    def test_projection_from_different_input_snapshot_rejected(self):
        self.prepared(include=0)
        with self.assertRaisesRegex(ValueError, "input_manifest_mismatch"):
            self.s.put_view("r1", *self.required[0], {"snapshot": "future"}, {})

    def test_consumer_effect_and_receipt_rollback_together(self):
        self.prepared(); self.s.publish("r1")
        event = self.s.changes_after()[0]["event"]
        with self.assertRaises(RuntimeError):
            self.s.consume("web", event, fail_after_effect=True)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM effects").fetchone()[0], 0)
        self.assertTrue(self.s.consume("web", event))
        self.assertFalse(self.s.consume("web", event))
        self.assertTrue(self.s.consume("sector-cache", event))
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM effects").fetchone()[0], 2)

    def test_same_event_id_with_changed_content_is_not_silent_dedupe(self):
        self.prepared(); self.s.publish("r1")
        event = self.s.changes_after()[0]["event"]
        self.s.consume("web", event)
        with self.assertRaisesRegex(ValueError, "event_id_content_conflict"):
            self.s.consume("web", {**event, "release_id": "different"})

    def test_missed_push_can_be_replayed_by_cursor(self):
        self.prepared(); self.s.publish("r1")
        first = self.s.changes_after()[0]["sequence"]
        self.prepared("r2", "2026-11-10T06:01:00Z"); self.s.publish("r2", "r1")
        missed = self.s.changes_after(first)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["event"]["release_id"], "r2")
        self.assertEqual(self.s.read_view("short-main-board", *self.required[0], "r1")["release_id"], "r1")

    def test_staged_or_wrong_context_view_not_exposed(self):
        self.prepared()
        with self.assertRaises(ValueError):
            self.s.read_view("short-main-board", *self.required[0], "r1")
        self.s.publish("r1")
        with self.assertRaises(ValueError):
            self.s.read_view("other-context", *self.required[0], "r1")


class HealthCases(unittest.TestCase):
    def test_quiet_news_can_have_healthy_transport(self):
        h = SourceHealth(); h.transport_success(100)
        self.assertEqual(h.state(110, push_expected=False), {"transport": "healthy", "freshness": "unknown", "poll_due": False})

    def test_http_success_does_not_make_old_quotes_fresh(self):
        h = SourceHealth(); h.transport_success(100, 50)
        self.assertEqual(h.state(110, content_required_after=90)["freshness"], "stale")

    def test_calendar_closed_market_is_not_a_missing_quote(self):
        h = SourceHealth(); h.transport_success(100, 50)
        self.assertEqual(h.state(110, content_required_after=90, content_expected_now=False)["freshness"], "not_expected")

    def test_push_silence_triggers_poll_and_recovery_keeps_content_clock(self):
        h = SourceHealth(); h.transport_success(100, 95); h.last_push_heartbeat = 100
        self.assertTrue(h.state(200)["poll_due"])
        self.assertEqual(h.state(500)["transport"], "unavailable")
        h.transport_success(501)
        self.assertEqual(h.state(502)["transport"], "healthy")
        self.assertEqual(h.last_content_at, 95)

    def test_disabled_source_is_not_claimed_as_running(self):
        self.assertEqual(SourceHealth(enabled=False).state(100)["transport"], "disabled")
        self.assertFalse(SourceHealth(enabled=False).state(100)["poll_due"])

    def test_failed_raw_write_never_advances_checkpoint(self):
        c = DurableCursor()
        with self.assertRaisesRegex(ValueError, "durable"):
            c.commit(1, 100, raw_persisted=False)
        self.assertEqual(c.committed_sequence, 0)
        c.commit(1, 100, True)
        self.assertFalse(c.commit(1, 100, True))

    def test_gap_requires_backfill_before_checkpoint_advance(self):
        c = DurableCursor(100)
        with self.assertRaisesRegex(ValueError, "gap"):
            c.commit(103, 110, True)
        c.commit(101, 102, True); c.commit(103, 110, True)
        self.assertEqual(c.committed_sequence, 110)

    def test_invalid_clock_does_not_mutate_health(self):
        h = SourceHealth()
        with self.assertRaises(ValueError):
            h.transport_success(100, 101)
        self.assertIsNone(h.last_success)

    def test_future_push_heartbeat_cannot_mask_silence(self):
        h = SourceHealth(); h.transport_success(100); h.last_push_heartbeat = 1000
        with self.assertRaisesRegex(ValueError, "heartbeat_clock"):
            h.state(110)


if __name__ == "__main__":
    unittest.main(verbosity=2)
