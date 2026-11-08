"""The 10-day shadow (scripts/lab/forward_book_h10.py) differs from the frozen main book only in its hold."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[2] / "scripts" / "lab"
sys.path.insert(0, str(LAB))

import forward_book as F  # noqa: E402
import forward_book_h10 as S  # noqa: E402


def test_shadow_changes_only_the_hold() -> None:
    assert (F.HOLD, S.HOLD) == (5, 10)
    assert (S.SCORES, S.SLOTS, S.OFFER, S.LOOK, S.LOW, S.FROZEN) == (F.SCORES, F.SLOTS, F.OFFER, F.LOOK, F.LOW, F.FROZEN)
    assert S.LOG != F.LOG


def test_frozen_rule_constants() -> None:
    # frozen 2026-11-01 (R7-4): hold 5, 20 slots, top 40, gate 20 days / 0.25, fusion_static40_safe
    assert (F.SCORES, F.HOLD, F.SLOTS, F.OFFER, F.LOOK, F.LOW) == ("fusion_static40_safe.parquet", 5, 20, 40, 20, 0.25)
    assert hashlib.sha256((LAB / "forward_book.py").read_bytes()).hexdigest()  # file readable
