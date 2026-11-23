"""Batch 8 constants and the pre-registered judge (pre-registration 2026-11-23, sections 1-5).

Why: E099 is confirmed ONLY on forward samples from 2026-11-24; every judging number is written here once, together with the frozen-prefix hash that proves the code reads the
document that was frozen (the bytes before the "## 6. 冻结" heading, LF newlines, 4741 bytes, sha256 below).
Invariants: nothing here is tuned. Two hypotheses (H8a next-day exit, H8b entry filter) form ONE family, Holm over exactly two p-values (``family=2``, an untested one counts as p = 1);
pass = Holm-adjusted one-sided p < 0.05 AND effect <= -0.252 % (absolute size at least one round-trip cost; section 4 says "≥", so exactly -0.252 % passes).
Sample condition (section 4): judge once, on the EARLIEST prefix of resolved sessions that has at least 60 sessions and at least 150 FLAG samples; at most 120 sessions, and if the 120th
session is reached without 150 FLAG samples the result is ``inconclusive`` (no pass, no fail). Before that ``judge`` REFUSES (state ``refused``, nothing is written).
FLAG sample = a flagged limit-up-close name whose D+1 open -> D+5 close leg is filled and has a matched baseline (the sample H8a's excess is computed on).
Not covered: computing the excess and p (``bf_batch8_reads`` / ``domain.batch8.stats``), writing the verdict into section 7 of the document (a person does that).
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pulse.domain.batch6 import evaluate as EV

SPEC_PATH = "docs/research/2026-11-23-e099-lhasa-short-book-preregistration.md"
SPEC_SHA256 = "ada61331bbdd83342447acec5b9e47994280bb5a18de4fefed426c238a1f9a0e"
FROZEN_END_MARKER = "## 6. 冻结"
EXPERIMENT = "E099"
HYPOTHESES = ("H8a", "H8b")
SIGN = -1                                         # both hypotheses expect a NEGATIVE excess (H8a: flagged names do worse after D+1 open; H8b: flagged do worse than unflagged)
COST = Decimal("0.00252")
SEED = 20261123
N_BOOT = 10000
ALPHA = 0.05
HOLD = 5                                          # D+1 open .. D+5 close; also the bootstrap block length
FAMILY = 2                                        # H8a, H8b
FORWARD_START = date(2026, 11, 24)                # first decision day (event day D)
HISTORY_START = date(2023, 11, 1)
HISTORY_END = date(2026, 11, 20)
MIN_SESSIONS = 60
MAX_SESSIONS = 120
MIN_FLAG = 150
OVERLAY_SUMMARY_SESSIONS = 60                     # section 5: one summary when 60 sessions are complete
START_OVERLAY = FORWARD_START

LHASA = "拉萨"
INSTITUTION = "机构专用"
LHASA_MIN_L1, LHASA_MIN_L3 = 1, 3
TRIP_MIN_EVENTS = 5                               # settled buys of a seat before D
TRIP_MIN_SHARE_NUM, TRIP_MIN_SHARE_DEN = 3, 5     # 60 % as an exact integer ratio: trips * 5 >= n * 3
SETTLE_SESSIONS = 3                               # an event of day t is settled on the 3rd open day after t (E048's buy-next-open / sell-3-bars-later window)


def frozen_prefix(raw: bytes) -> bytes:
    """The frozen part of the pre-registration: everything before the "## 6. 冻结" heading, LF newlines."""
    b = raw.replace(b"\r\n", b"\n")
    return b[: b.index(FROZEN_END_MARKER.encode("utf-8"))]


def frozen_hash(raw: bytes) -> str:
    return hashlib.sha256(frozen_prefix(raw)).hexdigest()


@dataclass(frozen=True)
class Cut:
    state: str                    # "refused" | "judge" | "inconclusive"
    sessions: int | None          # length of the judged prefix (None while refused)
    flag_n: int                   # FLAG samples inside that prefix (all of them while refused)
    reason: str


def sample_cut(flag_per_session: Sequence[int]) -> Cut:
    """Section 4: the earliest prefix of m resolved sessions, 60 <= m <= 120, holding at least 150 FLAG samples; else refuse (too short) or inconclusive (120 reached)."""
    run = 0
    cum: list[int] = []
    for x in flag_per_session:
        run += int(x)
        cum.append(run)
    for m in range(MIN_SESSIONS, min(len(cum), MAX_SESSIONS) + 1):
        if cum[m - 1] >= MIN_FLAG:
            return Cut("judge", m, cum[m - 1], f"{cum[m - 1]} FLAG samples in the first {m} resolved sessions (need {MIN_FLAG} and {MIN_SESSIONS} sessions)")
    if len(cum) >= MAX_SESSIONS:
        return Cut("inconclusive", MAX_SESSIONS, cum[MAX_SESSIONS - 1], f"only {cum[MAX_SESSIONS - 1]} FLAG samples after {MAX_SESSIONS} sessions (need {MIN_FLAG}): inconclusive")
    total = cum[-1] if cum else 0
    return Cut("refused", None, total, f"{len(cum)}/{MIN_SESSIONS} resolved sessions and {total}/{MIN_FLAG} FLAG samples: not judged before both are met (at most {MAX_SESSIONS} sessions)")


@dataclass(frozen=True)
class Verdict:
    hypothesis: str
    effect: float | None
    p_value: float | None
    p_holm: float | None
    size_ok: bool
    significant: bool
    passed: bool
    reason: str


def judge(effects: Mapping[str, tuple[float | None, float | None]]) -> dict[str, Verdict]:
    """``{hypothesis: (effect, one-sided p)}`` -> verdicts. Holm over the family of two; a missing effect or p is never a pass."""
    adj = EV.holm({h: effects.get(h, (None, None))[1] for h in HYPOTHESES}, family=FAMILY)
    out: dict[str, Verdict] = {}
    for h in HYPOTHESES:
        eff, p = effects.get(h, (None, None))
        if eff is None or p is None:
            out[h] = Verdict(h, eff, p, None, False, False, False, "no effect or p-value could be computed: not a pass")
            continue
        size_ok = -eff >= float(COST)
        sig = adj[h] is not None and adj[h] < ALPHA
        out[h] = Verdict(h, eff, p, adj[h], bool(size_ok), bool(sig), bool(size_ok and sig),
                         f"effect {eff:+.5f} {'<=' if size_ok else '>'} -{float(COST)}; Holm p {adj[h]:.4f} {'<' if sig else '>='} {ALPHA} (raw {p:.4f})")
    return out
