"""Feed circulating-cap versions point in time to the playbook panel (legacy rows #142/#144/#146).

Why: ``playbook_service.evaluate_playbook`` refused every float-cap playbook (``float_cap_history_not_point_in_time``) because the
panel scaled the latest cap snapshot over all history. ``attach_pit_caps`` replaces that with the version-by-version series of
``domain.playbooks.capseries``: a session sees only the cap versions known at that session.
Invariants: reads the READY ``instrument_circulating_cap_legacy`` manifest visible at ``decision_as_of`` (observed policy: by
``ready_at``); never mutates the cached panel (new views are built); a panel whose caps are all missing is reported as a reason, not
evaluated on nothing.
Not covered: fundamentals (PE/PB) - the legacy securities table holds them for three snapshot dates only, which is no history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

import polars as pl

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.domain.playbooks import capseries as CS


@dataclass(frozen=True)
class CapCoverage:
    versions: int
    first_as_of: str | None
    last_as_of: str | None
    instruments_with_cap: int
    first_session_with_cap: str | None
    reason: str | None


def load_versions(conn, lake: LakeStore, decision_as_of: datetime, policy: str = B.AVAILABILITY_POLICY) -> tuple[dict[str, list[CS.CapVersion]], dict | None]:
    man = B._ready_manifest(conn, B.CAP_DATASET, None if B.reconstructed(policy) else decision_as_of)
    if man is None:
        return {}, None
    df = lake.read_snapshot_frames(man["content_hash"])
    if df is None or df.height == 0:
        return {}, man
    df = df.filter(pl.col("float_cap_cny").is_not_null())
    if not B.reconstructed(policy):
        df = df.filter(pl.col("available_at") <= decision_as_of)
    out: dict[str, list[CS.CapVersion]] = {}
    for iid, as_of, cap, avail in zip(df["instrument_id"].to_list(), df["as_of"].to_list(), df["float_cap_cny"].to_list(), df["available_at"].to_list()):
        out.setdefault(iid, []).append(CS.CapVersion(date.fromisoformat(as_of), float(cap), avail))
    return out, man


def attach_pit_caps(conn, lake: LakeStore, panel, policy: str = B.AVAILABILITY_POLICY):
    """(new panel with ``float_cap`` series on every view, CapCoverage). The input panel is not changed."""
    versions, man = load_versions(conn, lake, panel.decision_as_of, policy)
    if man is None:
        return panel, CapCoverage(0, None, None, 0, None, "no_cap_dataset_visible")
    usable = None if B.reconstructed(policy) else B.policy_usable_at
    views, with_cap, first = [], 0, None
    for v in panel.views:
        vs = versions.get(v.instrument_id)
        series = CS.pit_float_cap_series(v.dates, v.close, vs, usable_at=usable) if vs else [None] * v.n
        if any(x is not None for x in series):
            with_cap += 1
            d0 = next(d for d, x in zip(v.dates, series) if x is not None)
            first = d0 if first is None or d0 < first else first
        views.append(replace(v, float_cap=series, xs={}, _cache={}))
    all_v = [x for vs in versions.values() for x in vs]
    reason = None if with_cap else "no_instrument_has_a_cap_version_usable_in_the_panel"
    cov = CapCoverage(len(all_v), min((x.as_of for x in all_v), default=None) and min(x.as_of for x in all_v).isoformat(),
                      max((x.as_of for x in all_v), default=None) and max(x.as_of for x in all_v).isoformat(), with_cap, first, reason)
    return replace(panel, views=views), cov
