"""All 52 playbooks: registry shape, signals fire on synthetic data, and NO LOOK-AHEAD (truncating the panel at k gives the
same scores for every bar <= k)."""
from __future__ import annotations

import pytest

from pulse.domain.playbooks import library as L
from pulse.domain.playbooks.builder import build_views, prepare, run_signal

from .synth import panel


def test_registry_has_52_playbooks_in_8_families():
    assert len(L.REGISTRY) == 52
    fam = {}
    for p in L.REGISTRY.values():
        fam.setdefault(p.family, []).append(p.id)
    assert {k: len(v) for k, v in fam.items()} == {
        "limit_relay": 5, "trend_breakout": 7, "academic_factor": 5, "oversold": 5, "sentiment_cycle": 9, "crowd": 5,
        "factor_composite": 2, "technical": 14}


def test_unavailable_playbooks_declare_why_and_return_nothing():
    days, raws, views = panel(30, 300)
    prepare(views, days)
    for pid in ("value_pe_pb", "industry_follower", "industry_first_board", "name_cluster_follower", "smart_seat_follow", "institution_net_buy"):
        pb = L.REGISTRY[pid]
        assert pb.requires and pb.unavailable_reason
        assert all(x is None for x in run_signal(pb, views[0]))


def test_signals_fire_somewhere_and_scores_are_positive():
    days, raws, views = panel(60, 520)
    prepare(views, days)
    fired = {}
    for pid, pb in L.REGISTRY.items():
        n = 0
        for v in views:
            for s in run_signal(pb, v):
                if s is not None:
                    assert s > 0
                    n += 1
        fired[pid] = n
    silent = sorted(k for k, n in fired.items() if n == 0 and not L.REGISTRY[k].unavailable_reason)
    # rare rules may stay silent on random data, but most of the library must fire
    assert len(silent) <= 12, silent


@pytest.mark.parametrize("cut", [330, 400])
def test_no_look_ahead_truncation_equals_prefix(cut):
    days, raws, views = panel(50, 460)
    prepare(views, days)
    full = {pid: [run_signal(pb, v) for v in views] for pid, pb in L.REGISTRY.items()}
    short_days = days[:cut]
    short_raws = [type(r)(r.instrument_id, r.board_code, r.dates[:cut], r.open[:cut], r.high[:cut], r.low[:cut], r.close[:cut],
                          r.volume[:cut], r.code, None) for r in raws]
    sviews = build_views(short_raws, short_days)
    # float cap is a latest-snapshot scaled by close_t/close_last: give the short panel the SAME per-bar series
    for sv, fv in zip(sviews, views):
        sv.float_cap = None if fv.float_cap is None else list(fv.float_cap[:cut])
    prepare(sviews, short_days)
    for pid, pb in L.REGISTRY.items():
        for k, sv in enumerate(sviews):
            part = run_signal(pb, sv)
            assert part == full[pid][k][:cut], f"{pid} looks ahead on instrument {k}"


def test_web_catalog_json_is_in_sync_with_the_code():
    from pathlib import Path

    from pulse.domain.screening import export

    path = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "ext" / "b" / "catalog.json"
    assert path.read_text(encoding="utf-8") == export.render(), "run: uv run python -m pulse.domain.screening.export"
