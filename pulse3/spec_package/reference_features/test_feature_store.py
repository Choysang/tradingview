import asyncio
import gc
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone

from feature_store import FeatureKey, SingleFlightFeatureStore, parameter_digest


def key(**overrides):
    values = dict(feature_id="MACD", definition_version="v1", implementation_digest="sha256:code",
                  parameter_digest=parameter_digest({"fast": 12, "slow": 26, "signal": 9}),
                  instrument_ids=("XSHG:600000",), timeframe="1d", adjustment_mode="raw",
                  snapshot_id="snapshot-A", input_revision="r1", input_schema_version="bars-v1",
                  decision_as_of=datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc),
                  availability_policy_version="available-at-v1", session_policy_version="continuous-v1",
                  dependency_digest="sha256:deps", universe_policy_version="mainboard-v1",
                  calendar_version="calendar-v1", adjustment_version="actions-v1",
                  market_rules_version="rules-v1", numeric_policy_version="decimal28-v1")
    values.update(overrides)
    return FeatureKey(**values)


class FeatureKeyTests(unittest.TestCase):
    def test_canonical_parameters_and_parameter_change(self):
        self.assertEqual(parameter_digest({"b": [2, {"x": True}], "a": 1}),
                         parameter_digest({"a": 1, "b": [2, {"x": True}]}))
        self.assertNotEqual(parameter_digest({"window": 10}), parameter_digest({"window": 11}))

    def test_parameter_digest_rejects_ambiguous_or_nonfinite_parameters(self):
        for parameters in ({1: "value"}, {"x": float("nan")}, {"x": float("inf")}, {"x": {1, 2}}):
            with self.subTest(parameters=repr(parameters)):
                with self.assertRaises((TypeError, ValueError)):
                    parameter_digest(parameters)

    def test_every_version_dimension_changes_the_key(self):
        original = key()
        variants = []
        for field in fields(FeatureKey):
            if field.name == "instrument_ids":
                value = ("XSHG:600001",)
            elif field.name == "decision_as_of":
                value = original.decision_as_of + timedelta(seconds=1)
            else:
                value = getattr(original, field.name) + ":changed"
            variant = replace(original, **{field.name: value})
            with self.subTest(dimension=field.name):
                self.assertNotEqual(original, variant)
            variants.append(variant)
        self.assertEqual(len(set([original, *variants])), len(fields(FeatureKey)) + 1)

    def test_no_implicit_or_mutable_key_dimensions(self):
        for values in ({"snapshot_id": ""}, {"instrument_ids": ["XSHG:600000"]},
                       {"instrument_ids": ()}, {"instrument_ids": ("X", "X")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                key(**values)

    def test_parameter_mutation_cannot_mutate_existing_key_identity(self):
        parameters = {"nested": {"windows": [12, 26]}}
        original_digest = parameter_digest(parameters)
        original_key = key(parameter_digest=original_digest)
        original_hash = hash(original_key)
        parameters["nested"]["windows"][0] = 13
        self.assertEqual(hash(original_key), original_hash)
        self.assertEqual(original_key.parameter_digest, original_digest)
        self.assertNotEqual(parameter_digest(parameters), original_digest)
        with self.assertRaises(FrozenInstanceError):
            original_key.snapshot_id = "changed"
        with self.assertRaises(AttributeError):
            original_key.__dict__["snapshot_id"] = "changed"

    def test_decision_time_requires_timezone_and_normalizes_same_instant(self):
        utc_key = key()
        local_key = key(decision_as_of=datetime(2026, 11, 10, 10, 0,
                                                tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(utc_key, local_key)
        self.assertEqual(local_key.decision_as_of.tzinfo, timezone.utc)
        for invalid in (datetime(2026, 11, 10, 2, 0), "2026-11-10T02:00:00Z", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                key(decision_as_of=invalid)


class SingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SingleFlightFeatureStore()

    async def asyncTearDown(self):
        await self.store.aclose()

    async def wait_joined(self, expected):
        async def poll():
            while (await self.store.stats()).joined < expected:
                await asyncio.sleep(0)
        await asyncio.wait_for(poll(), timeout=2)

    async def test_100_concurrent_same_key_compute_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return ("immutable", 42)

        waiters = [asyncio.create_task(self.store.get(key(), compute)) for _ in range(100)]
        await entered.wait()
        await self.wait_joined(99)
        release.set()
        results = await asyncio.gather(*waiters)
        self.assertEqual(calls, 1)
        self.assertEqual(results, [("immutable", 42)] * 100)
        self.assertEqual((await self.store.stats()).started, 1)
        self.assertEqual((await self.store.stats()).inflight_entries, 0)

    async def test_cancelling_creator_waiter_does_not_cancel_other_waiter(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = False

        async def compute():
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return 7

        first = asyncio.create_task(self.store.get(key(), compute))
        await entered.wait()
        second = asyncio.create_task(self.store.get(key(), compute))
        await self.wait_joined(1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, 7)
        self.assertFalse(cancelled)

    async def test_all_waiters_cancel_but_later_waiter_joins_original_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return 9

        waiter = asyncio.create_task(self.store.get(key(), compute))
        await entered.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        late = asyncio.create_task(self.store.get(key(), compute))
        await self.wait_joined(1)
        release.set()
        self.assertEqual(await late, 9)
        self.assertEqual(calls, 1)

    async def test_shared_failure_is_not_cached_and_retry_succeeds(self):
        release = asyncio.Event()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            await release.wait()
            if calls == 1:
                raise ValueError("temporary data failure")
            return 123

        waiters = [asyncio.create_task(self.store.get(key(), compute)) for _ in range(5)]
        await self.wait_joined(4)
        release.set()
        errors = await asyncio.gather(*waiters, return_exceptions=True)
        self.assertTrue(all(isinstance(error, ValueError) for error in errors))
        self.assertEqual((await self.store.stats()).completed_entries, 0)
        self.assertEqual(await self.store.get(key(), compute), 123)
        self.assertEqual(calls, 2)

    async def test_failure_after_all_waiters_cancel_has_no_unobserved_exception(self):
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unexpected = []
        loop.set_exception_handler(lambda _loop, context: unexpected.append(context))

        async def compute():
            entered.set()
            await release.wait()
            raise ValueError("late producer failure")

        try:
            waiter = asyncio.create_task(self.store.get(key(), compute))
            await entered.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()

            async def finished():
                while (await self.store.stats()).inflight_entries:
                    await asyncio.sleep(0)

            await asyncio.wait_for(finished(), 2)
            await asyncio.sleep(0)  # Run the producer's exception-observer callback.
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(unexpected, [])
            self.assertEqual((await self.store.stats()).completed_entries, 0)
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_different_snapshots_compute_independently(self):
        both_started, release = asyncio.Event(), asyncio.Event()
        starts = []

        async def compute(snapshot):
            starts.append(snapshot)
            if len(starts) == 2:
                both_started.set()
            await release.wait()
            return snapshot

        first = asyncio.create_task(self.store.get(key(snapshot_id="A"), lambda: compute("A")))
        second = asyncio.create_task(self.store.get(key(snapshot_id="B"), lambda: compute("B")))
        await asyncio.wait_for(both_started.wait(), 2)
        self.assertEqual((await self.store.stats()).inflight_entries, 2)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), ["A", "B"])

    async def test_same_snapshot_different_decision_time_does_not_reuse_future_result(self):
        later_key = key(decision_as_of=datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc))
        earlier_key = key(decision_as_of=datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc))

        async def later():
            return ("news available at 03:00",)

        async def earlier():
            return ("only earlier visible inputs",)

        self.assertEqual(await self.store.get(later_key, later), ("news available at 03:00",))
        self.assertEqual(await self.store.get(earlier_key, earlier), ("only earlier visible inputs",))
        self.assertEqual((await self.store.stats()).started, 2)

    async def test_none_is_a_cached_success(self):
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return None

        self.assertIsNone(await self.store.get(key(), compute))
        self.assertIsNone(await self.store.get(key(), compute))
        self.assertEqual(calls, 1)

    async def test_mutable_results_are_isolated_from_producer_waiters_and_cache(self):
        source = {"values": [1, 2]}
        release = asyncio.Event()

        async def compute():
            await release.wait()
            return source

        first = asyncio.create_task(self.store.get(key(), compute))
        second = asyncio.create_task(self.store.get(key(), compute))
        await self.wait_joined(1)
        release.set()
        a, b = await asyncio.gather(first, second)
        a["values"].append(99)
        source["values"].append(88)
        self.assertEqual(b, {"values": [1, 2]})
        self.assertEqual(await self.store.get(key(), compute), {"values": [1, 2]})
        self.assertEqual((await self.store.stats()).started, 1)

    async def test_bounded_lru_evicts_least_recently_used_result(self):
        store = SingleFlightFeatureStore(max_entries=2)
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return calls

        try:
            for snapshot in ("A", "B", "A", "C", "B"):
                await store.get(key(snapshot_id=snapshot), compute)
            self.assertEqual(calls, 4)
            self.assertEqual((await store.stats()).completed_entries, 2)
        finally:
            await store.aclose()

    async def test_shutdown_cancels_producer_and_rejects_new_work(self):
        entered = asyncio.Event()

        async def compute():
            entered.set()
            await asyncio.Event().wait()

        waiter = asyncio.create_task(self.store.get(key(), compute))
        await entered.wait()
        await self.store.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual((await self.store.stats()).inflight_entries, 0)
        with self.assertRaises(RuntimeError):
            await self.store.get(key(), compute)

    async def test_concurrent_shutdowns_do_not_interrupt_producer_cleanup(self):
        entered, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleaned = False

        async def compute():
            nonlocal cleaned
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
                cleaned = True

        waiter = asyncio.create_task(self.store.get(key(), compute))
        await entered.wait()
        first_close = asyncio.create_task(self.store.aclose())
        await cleaning.wait()
        second_close = asyncio.create_task(self.store.aclose())
        await asyncio.sleep(0)
        release_cleanup.set()
        await asyncio.gather(first_close, second_close)
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(cleaned)
        self.assertEqual((await self.store.stats()).inflight_entries, 0)

    async def test_cancelling_shutdown_waiter_does_not_abort_shutdown(self):
        entered, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleaned = False

        async def compute():
            nonlocal cleaned
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
                cleaned = True

        waiter = asyncio.create_task(self.store.get(key(), compute))
        await entered.wait()
        first_close = asyncio.create_task(self.store.aclose())
        await cleaning.wait()
        first_close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_close
        release_cleanup.set()
        await self.store.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(cleaned)
        self.assertEqual((await self.store.stats()).inflight_entries, 0)


if __name__ == "__main__":
    unittest.main()
