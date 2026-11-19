"""Point-in-time circulating-cap series for the playbook panel (legacy rows #142/#144/#146: float-cap playbooks).

Why: five playbooks rank stocks by circulating market cap (small_cap, high_low_switch, low_turnover_low_vol, microcap_timed,
low_risk_combo). The older panel scaled the LATEST snapshot back over the whole history (``snapshot x close_t / close_latest``),
which uses a share count and a price that were not known at ``t``; the service therefore refused to run them. This module builds
the cap of each session from the cap VERSIONS (one row per instrument and as-of date) that were known at that session.
Rules (``CAP_SERIES_VERSION``):
* a version is usable for session ``d`` when its ``as_of`` date <= ``d`` and, under the observed availability policy, it was
  recorded by the end of ``d`` (``available_at <= usable_at(d)``); under the reconstructed policy the as-of date alone decides
  (``bindings.visible_caps``);
* the newest usable version is scaled to the session by the ratio of adjusted closes ``close_d / close_ref`` where ``close_ref``
  is the last close on or before the version's as-of date; no reference close -> no cap;
* a version older than ``MAX_VERSION_AGE_DAYS`` calendar days is not trusted (the share count changes: issuance, unlocks) -> no cap;
* a session before the first usable version has NO cap (None). It is never back-filled from a later snapshot.
Invariants: pure, no IO; output has one entry per input date, ``None`` or a positive float; the entry at ``d`` depends only on
versions and closes visible at ``d`` (the truncation test checks this).
Not covered: cash-dividend and rights-issue effects on the share count between a version and the session (the adjusted-close ratio
is a total-return ratio), a free-float (扣除大股东锁定) cap.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

CAP_SERIES_VERSION = "pit-cap-series-v1"
MAX_VERSION_AGE_DAYS = 400


@dataclass(frozen=True)
class CapVersion:
    as_of: date
    float_cap_cny: float
    available_at: datetime


def pit_float_cap_series(dates: Sequence[str], closes: Sequence[float | None], versions: Sequence[CapVersion], *,
                         usable_at: Callable[[date], datetime] | None) -> list[float | None]:
    """Cap of each session (CNY) or None. ``usable_at(d)``: when the cap known for session ``d`` must have been recorded
    (None = the reconstructed policy: only the as-of date is checked)."""
    vs = sorted((v for v in versions if v.float_cap_cny is not None and v.float_cap_cny > 0), key=lambda v: (v.as_of, v.available_at))
    day_list = [date.fromisoformat(d) for d in dates]
    out: list[float | None] = []
    for i, d in enumerate(day_list):
        c = closes[i]
        if c is None or c <= 0 or not vs:
            out.append(None)
            continue
        horizon = [v for v in vs[:bisect_right([v.as_of for v in vs], d)]
                   if usable_at is None or v.available_at <= usable_at(d)]
        if not horizon:
            out.append(None)
            continue
        v = horizon[-1]
        if (d - v.as_of).days > MAX_VERSION_AGE_DAYS:
            out.append(None)
            continue
        ref = _close_on_or_before(day_list, closes, v.as_of, i)
        out.append(None if ref is None else v.float_cap_cny * c / ref)
    return out


def _close_on_or_before(days: Sequence[date], closes: Sequence[float | None], day: date, upto: int) -> float | None:
    j = bisect_right(days, day, 0, upto + 1) - 1
    while j >= 0:
        if closes[j] is not None and closes[j] > 0:
            return closes[j]
        j -= 1
    return None
