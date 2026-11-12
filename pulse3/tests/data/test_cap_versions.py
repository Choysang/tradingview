"""Cap facts keep every dated version; a history rebuild reads the one known for its session (M5 reconciliation at 2026-11-09)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import polars as pl

from pulse.application.bindings import AVAILABILITY_POLICY, RECONSTRUCTED_POLICY, visible_caps

A = str(uuid.uuid4())


def _frame() -> pl.DataFrame:
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    return pl.DataFrame({"instrument_id": [A, A, A], "as_of": ["2026-11-06", "2026-11-09", "2026-11-12"],
                         "float_cap_cny": [Decimal("100"), Decimal("110"), Decimal("120")],
                         "available_at": [utc(2026, 11, 6, 10), utc(2026, 11, 9, 7, 36), utc(2026, 11, 12, 7, 36)]},
                        schema_overrides={"float_cap_cny": pl.Decimal(38, 2)})


def test_reconstructed_reads_the_version_dated_for_the_session():
    caps = visible_caps(_frame(), datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc), RECONSTRUCTED_POLICY)
    assert caps == {uuid.UUID(A): Decimal("110")}


def test_observed_reads_the_latest_recorded_version():
    assert visible_caps(_frame(), datetime(2026, 11, 13, tzinfo=timezone.utc), AVAILABILITY_POLICY) == {uuid.UUID(A): Decimal("120")}
    assert visible_caps(_frame(), datetime(2026, 11, 5, tzinfo=timezone.utc), AVAILABILITY_POLICY) == {}
