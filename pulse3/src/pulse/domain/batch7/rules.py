"""Batch 7 constants and the pre-registered judge (pre-registration 2026-11-20, sections 1-3).

Why: E097 (H1 prime) and E098 (H4 prime) are confirmed ONLY on forward samples from 2026-11-23; the judging conditions are numbers in the document and are written here once,
with the frozen-prefix hash that proves the code reads the document that was frozen (sections 1-4, the bytes before "## 5. 结果", LF, sha256 below).
Invariants: nothing here is tuned; ``judge`` returns progress only until the sample condition is met (E097: 300 filled signals and at least 60 sessions; E098: 150 and 60) or the deadline
2027-06-30 has passed (then it judges the sample it has and says it is insufficient); pass = matched excess below -0.252 % AND one-sided block-bootstrap p below 0.05; the two hypotheses are
two independent families (no Holm across them).
Not covered: computing the excess and p (``bf_batch7_*``), the section 5 write-up of a verdict (a person appends it to the document).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

SPEC_PATH = "docs/research/2026-11-20-e097-e098-preregistration.md"
SPEC_SHA256 = "2d2a8fcc18a5379b1d92989612d267292d3083ebcedbb46ec09a1e37196fed3c"
FROZEN_END_MARKER = "## 5. 结果"
COST = Decimal("0.00252")
SEED = 20261120
N_BOOT = 10000
ALPHA = 0.05
FORWARD_START = date(2026, 11, 23)                 # first decision day (event day D)
DEADLINE = date(2027, 6, 30)
HISTORY_START = date(2022, 12, 1)
PRE_END = date(2026, 6, 30)                        # robustness segments as in batch 6
HISTORY_END = date(2026, 11, 18)
MIN_SESSIONS = 60
INSTITUTION = "机构专用"
INSTITUTION_SHARE = 0.05

#: hypothesis -> (experiment, expected sign of the matched excess, holding sessions D+1 open .. D+h close, filled signals needed to judge)
HYPOTHESES: dict[str, tuple[str, int, int, int]] = {"H1p": ("E097", -1, 3, 300), "H4p": ("E098", -1, 5, 150)}


def frozen_prefix(raw: bytes) -> bytes:
    """The frozen part of the pre-registration: everything before the "## 5. 结果" heading, LF newlines."""
    b = raw.replace(b"\r\n", b"\n")
    return b[: b.index(FROZEN_END_MARKER.encode("utf-8"))]


def frozen_hash(raw: bytes) -> str:
    return hashlib.sha256(frozen_prefix(raw)).hexdigest()


@dataclass(frozen=True)
class Judgement:
    hypothesis: str
    state: str                    # "progress" | "judged"
    passed: bool | None
    sufficient: bool | None       # sample condition met (None while in progress)
    n_filled: int
    sessions: int
    excess: float | None
    p_value: float | None
    reason: str


def judge(hypothesis: str, *, n_filled: int, sessions: int, excess: float | None, p_value: float | None, today: date) -> Judgement:
    """Section 2.3 / 3: judge when the sample condition holds, or after the deadline on the sample at hand; otherwise report progress only."""
    exp, sign, hold, need = HYPOTHESES[hypothesis]
    sufficient = n_filled >= need and sessions >= MIN_SESSIONS
    if not sufficient and today < DEADLINE:
        return Judgement(hypothesis, "progress", None, None, n_filled, sessions, excess, p_value,
                         f"{n_filled}/{need} filled signals, {sessions}/{MIN_SESSIONS} sessions: not judged before both are met or {DEADLINE}")
    if excess is None or p_value is None:
        return Judgement(hypothesis, "judged", False, sufficient, n_filled, sessions, excess, p_value, "no excess or p-value could be computed: not a pass")
    ok_size = excess < -float(COST)
    ok_p = p_value < ALPHA
    passed = bool(ok_size and ok_p)
    note = "" if sufficient else f"; sample insufficient at the deadline ({n_filled}/{need} signals, {sessions}/{MIN_SESSIONS} sessions)"
    return Judgement(hypothesis, "judged", passed, sufficient, n_filled, sessions, excess, p_value,
                     f"excess {excess:+.5f} {'<' if ok_size else '>='} -{float(COST)}; p {p_value:.4f} {'<' if ok_p else '>='} {ALPHA}{note}")
