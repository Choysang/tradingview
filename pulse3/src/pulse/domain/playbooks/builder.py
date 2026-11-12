"""Build the InstrumentViews of a research panel and run playbook signals on them.

Why: one place assembles a panel (calendar flags, cross-sectional ranks, market-wide flags) so that the screen, the pattern
radar, the factor lab, analogs and the playbook replay read identical views. Pure: it takes plain lists, no IO.
Invariants: the calendar is the PANEL's sorted session list; per-view arrays are aligned to the view's own bars; cross
sections and market flags use same-date and earlier data only (``panel.py``); optional float-cap series are PIT-safe by
construction (latest snapshot x close_t / close_latest only needs close_t and the one snapshot value).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .library import REGISTRY
from .panel import attach_cross_sections, market_aggregates
from .spec import Playbook, XSpec
from .view import InstrumentView, calendar_flags, limit_class_pct


@dataclass(frozen=True)
class RawSeries:
    instrument_id: str
    board_code: str
    dates: tuple[str, ...]
    open: tuple
    high: tuple
    low: tuple
    close: tuple
    volume: tuple
    code: str | None = None
    #: latest snapshot float cap in CNY (None = unknown); scaled per bar by close_t / close_last to a PIT-safe series
    float_cap_latest_cny: float | None = None


def make_view(r: RawSeries, month: Mapping[str, bool], week: Mapping[str, bool]) -> InstrumentView:
    cap = None
    if r.float_cap_latest_cny is not None and r.close and r.close[-1]:
        last = r.close[-1]
        cap = [None if c is None else r.float_cap_latest_cny * c / last for c in r.close]
    return InstrumentView(
        r.instrument_id, limit_class_pct(r.board_code), r.dates, r.open, r.high, r.low, r.close, r.volume, r.code,
        tuple(month[d] for d in r.dates), tuple(week[d] for d in r.dates), {}, cap)


def build_views(series: Sequence[RawSeries], sessions: Sequence[str]) -> list[InstrumentView]:
    month, week = calendar_flags([date.fromisoformat(d) for d in sessions])
    return [make_view(r, month, week) for r in series]


def prepare(views: Sequence[InstrumentView], sessions: Sequence[str], playbooks: Sequence[Playbook] | None = None) -> None:
    """Attach the cross-sectional and market columns the given playbooks need (default: all)."""
    pbs = list(playbooks or REGISTRY.values())
    specs: dict[str, XSpec] = {p.xs.key: p.xs for p in pbs if p.xs is not None}
    attach_cross_sections(views, sessions, list(specs.values()))
    if any(p.needs_market for p in pbs):
        flags = market_aggregates(views, sessions)
        for v in views:
            v.xs["mkt.ice"] = [flags["ice"].get(d) for d in v.dates]
            v.xs["mkt.switch"] = [flags["switch"].get(d) for d in v.dates]


def run_signal(pb: Playbook, view: InstrumentView, params: Mapping[str, float] | None = None) -> list:
    """Per-bar scores (positive = fires on the completed bar). Raises nothing for unavailable playbooks: all None."""
    out = pb.signal(view, pb.default_params(dict(params or {})))
    if len(out) != view.n:
        raise AssertionError(f"{pb.id}: signal length differs from the view")
    return out
