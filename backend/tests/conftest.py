"""Test isolation: never write into the developer's watchlist or archive."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

_TMP = Path(tempfile.mkdtemp(prefix="pulse-tests-"))
os.environ.setdefault("PULSE_DATA_DIR", str(_TMP))
os.environ.setdefault("PULSE_SCHEDULER", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_hung_live_calls_between_tests(monkeypatch):
    """A test that makes a live source hang leaves its thread behind; the next test must not see that source as busy."""
    from backend.app.market import live

    monkeypatch.setattr(live, "_PENDING", {})
