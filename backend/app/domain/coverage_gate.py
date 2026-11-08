"""Data coverage gate for the daily bar sync (E058 conversion (c), after Sequoia-X's missing-data idea).

After a sync, coverage = main-board codes with a bar for the session / main-board codes expected to trade that
session (listed, not suspended per corp_events, not delisted). Below RETRY_BELOW the sync of the missing codes is
retried once; still below REFUSE_BELOW, no forward pick list may be generated for that session. Between the two
the list is generated and marked degraded. Not wired into the server jobs yet (needs a deploy decision); on the
lab panel 2023-11 ~ 2026-10 the proxy coverage never fell below 98.4%.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

RETRY_BELOW = 0.98
REFUSE_BELOW = 0.95


@dataclass(frozen=True)
class CoverageDecision:
    coverage: float
    retried: bool
    status: str  # ok | degraded | refuse
    missing: tuple[str, ...]

    @property
    def may_generate(self) -> bool:
        return self.status != "refuse"


def coverage(expected: Iterable[str], got: Iterable[str]) -> tuple[float, tuple[str, ...]]:
    exp = set(expected)
    if not exp:
        return 0.0, ()
    missing = tuple(sorted(exp - set(got)))
    return 1 - len(missing) / len(exp), missing


def decide(expected: Iterable[str], got: Iterable[str],
           retry: Callable[[tuple[str, ...]], Iterable[str]] | None = None) -> CoverageDecision:
    """`retry(missing)` re-syncs the missing codes and returns the codes it now has bars for."""
    exp = set(expected)
    have = set(got)
    cov, missing = coverage(exp, have)
    retried = False
    if cov < RETRY_BELOW and retry is not None and missing:
        have |= set(retry(missing))
        cov, missing = coverage(exp, have)
        retried = True
    status = "ok" if cov >= RETRY_BELOW else "degraded" if cov >= REFUSE_BELOW else "refuse"
    return CoverageDecision(round(cov, 6), retried, status, missing)
