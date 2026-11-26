"""E101 constants, events (class, D0, R) and the pre-registered judge (pre-registration 2026-11-25, sections 1-4).

Why: every number of the frozen document is written here once, with the frozen-prefix hash that proves the code reads the frozen text (the bytes before the "## 7. 冻结" heading,
LF newlines, 6684 bytes, sha256 below).
Readings of the frozen text (written to section 8 of the document as well; none changes a frozen setting):
  * classes: E073's regular expressions on the name with full-width colons made half-width, plus ``^美国:核心PCE物价指数`` (the first name fetched after the freeze was
    ``美国:核心PCE物价指数:同比(报告期:2026年10月)``). One event per (class, D0): several series of one release (core and headline, month and year) are one event.
  * D0 = the last A-share session whose close (15:00 Asia/Shanghai) is before the release time; R = the first session whose close is after it. Only timed releases count
    (a date-only row has no release time, so D0 is not defined).
  * forward sample: events whose D0 is on or after 2026-11-26 (the first session after the freeze) and on or before 2027-12-31. The 2026-11-25 core PCE (D0 = the freeze day) is out.
  * H1 control: the non-event days are the sessions of the same window that are neither a D0 nor an R of any class; the control of an event is the mean market return of the
    non-event days with the event's weekday. effect = mean over events of (D0 return - control); one-sided p of "effect >= 0" from the block bootstrap of (control - D0 return),
    slots = every session of the window, weight = events per D0 day.
  * H3 / H4 carry two groups; a hypothesis passes only when BOTH groups pass the size rule, and its p for Holm is the larger of the two groups' p (intersection-union).
  * Holm: family of four; a hypothesis not tested yet counts as p = 1 (H1 / H3 can be judged before H2 / H4 have 40 events with a consensus). H1 / H3 are judged once, on the
    first 40 resolved forward events; H2 / H4 once, on the first 40 resolved events with a consensus, with the stored raw p of H1 / H3 in the family.
  * an H3 event needs its calendar row known before the D0 open (point in time: the groups are bought at the D0 open); H1 does not trade and counts every event.
Not covered: computing returns and legs (``application.bf_e101``), the statistics (``stats``), writing the verdict into section 8 (a person does that).
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from pulse.domain.batch6 import evaluate as EV

SPEC_PATH = "docs/research/2026-11-25-e101-us-macro-anticipation-preregistration.md"
SPEC_SHA256 = "a140a6facc9dd680e29e2291ae1914f47a0846e38250f95d14ea5fd9ed49a2ff"
FROZEN_END_MARKER = "## 7. 冻结"
EXPERIMENT = "E101"
HYPOTHESES = ("H1", "H2", "H3", "H4")
FAMILY = 4
COST = Decimal("0.00252")
MIN_EFFECT = float(COST)
SEED = 20261125
N_BOOT = 10000
BLOCK = 5
ALPHA = 0.05
MIN_EVENTS = 40
FORWARD_START = date(2026, 11, 26)
DEADLINE = date(2027, 12, 31)
HISTORY_START = date(2023, 11, 1)
HISTORY_END = date(2026, 11, 24)
CST = timezone(timedelta(hours=8))
CLOSE = time(15, 0)
OPEN = time(9, 30)

CLASSES: tuple[tuple[str, str], ...] = (
    ("US_CORE_PCE", r"^美国:核心PCE物价指数"),
    ("US_CPI", r"^美国:(核心)?CPI"),
    ("US_NFP", r"^美国:新增非农就业人数"),
    ("FOMC", r"^美国:联邦基金利率目标:上限"),
)
_RX = tuple((c, re.compile(p)) for c, p in CLASSES)
# group definitions (section 3)
LEADER_MIN_STREAK = 3
CORE_CAP_SHARE = 0.10
CORE_RET_SHARE = 0.20
CORE_RET_SESSIONS = 20
H3_HOLD = 1                       # D0 open .. D0 close (decision day = the session before D0)
H4_HOLD = 3                       # R open .. R+2 close (decision day = the session before R)
GROUPS = ("leader", "core")


def frozen_prefix(raw: bytes) -> bytes:
    b = raw.replace(b"\r\n", b"\n")
    return b[: b.index(FROZEN_END_MARKER.encode("utf-8"))]


def frozen_hash(raw: bytes) -> str:
    return hashlib.sha256(frozen_prefix(raw)).hexdigest()


def normalise(name: str) -> str:
    return name.replace("：", ":").strip()


def classify(name: str) -> str | None:
    n = normalise(name)
    for cls, rx in _RX:
        if rx.search(n):
            return cls
    return None


def d0_r(release_at: datetime, sessions: Sequence[date]) -> tuple[date | None, date | None]:
    """(D0, R) of a timed release in ascending ``sessions``: last session closed before the release, first session closing after it."""
    if release_at.tzinfo is None:
        raise ValueError("release time needs a timezone")
    d0 = r = None
    for d in sessions:
        close = datetime.combine(d, CLOSE, CST)
        if close < release_at:
            d0 = d
        elif r is None:
            r = d
            break
    return d0, r


@dataclass(frozen=True)
class Event:
    cls: str
    d0: date
    r: date | None
    release_at: datetime
    names: tuple[str, ...]
    known_at: datetime | None          # earliest available_at of the calendar rows (None: history file, no timestamp)
    ids: tuple[str, ...] = ()           # calendar_event_id of the rows (empty for the history file)

    @property
    def key(self) -> str:
        return f"{self.d0.isoformat()}|{self.cls}"

    def known_before_open(self) -> bool:
        return self.known_at is None or self.known_at < datetime.combine(self.d0, OPEN, CST)


def build_events(rows: Sequence[Mapping], sessions: Sequence[date]) -> list[Event]:
    """Events from calendar rows (``name``, ``release_at`` aware, optional ``known_at``): classified, dated, one per (class, D0), the earliest release first."""
    acc: dict[tuple[str, date], dict] = {}
    for row in sorted(rows, key=lambda x: x["release_at"]):
        cls = classify(row["name"])
        if cls is None:
            continue
        d0, r = d0_r(row["release_at"], sessions)
        if d0 is None:
            continue
        k = (cls, d0)
        e = acc.setdefault(k, {"cls": cls, "d0": d0, "r": r, "release_at": row["release_at"], "names": [], "known_at": row.get("known_at"), "ids": set()})
        e["names"].append(normalise(row["name"]))
        if row.get("calendar_event_id"):
            e["ids"].add(row["calendar_event_id"])
        ka = row.get("known_at")
        if ka is not None and (e["known_at"] is None or ka < e["known_at"]):
            e["known_at"] = ka
    return sorted((Event(e["cls"], e["d0"], e["r"], e["release_at"], tuple(sorted(set(e["names"]))), e["known_at"], tuple(sorted(e["ids"])))
                   for e in acc.values()), key=lambda e: (e.d0, e.cls))


def in_forward(e: Event) -> bool:
    return FORWARD_START <= e.d0 <= DEADLINE


def direction(s: float | None) -> str | None:
    """Section 2: s > 0 hawkish, s < 0 dovish, 0 in line (inflation, FOMC; payrolls: more jobs than expected is hawkish, same sign)."""
    if s is None:
        return None
    return "hawkish" if s > 0 else "dovish" if s < 0 else "in_line"


@dataclass(frozen=True)
class Verdict:
    hypothesis: str
    effect: float | None
    p_raw: float | None
    p_holm: float | None
    passed: bool
    reason: str


def _size_ok(h: str, effects: Sequence[float | None]) -> bool:
    if not effects or any(x is None for x in effects):
        return False
    if h in ("H1", "H2"):
        return all(x <= -MIN_EFFECT for x in effects)          # H1: D0 below control; H2: sign(s) x gap below zero
    return all(x >= MIN_EFFECT for x in effects)               # H3 / H4: positive excess


def judge(results: Mapping[str, tuple[Sequence[float | None], float | None]]) -> dict[str, Verdict]:
    """``results``: hypothesis -> (effects of its parts, its p). Untested hypotheses are absent or have p None (counted as p = 1 in the family of four)."""
    p_raw = {h: results.get(h, ((), None))[1] for h in HYPOTHESES}
    adj = EV.holm(p_raw, family=FAMILY)
    out = {}
    for h in HYPOTHESES:
        if h not in results:
            continue
        effects, p = results[h]
        size = _size_ok(h, effects)
        pa = adj[h]
        passed = bool(size and pa is not None and pa < ALPHA)
        bits = [f"effects {', '.join('-' if x is None else f'{x:+.4%}' for x in effects)}", f"Holm p {'-' if pa is None else f'{pa:.4f}'}",
                "size ok" if size else f"size below {MIN_EFFECT:.3%} or wrong sign"]
        out[h] = Verdict(h, effects[0] if len(effects) == 1 else None, p, pa, passed, "; ".join(bits))
    return out
