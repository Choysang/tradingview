from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.domain import board_rules as R
from contracts.news import (
    ActionSignal, Anchor, AuthorExpectation, AuthorView, BreakState, EvidenceClaim, FirstBoardCluster, GapType,
    MessageVersion, ReactionWindow, RelationType, SignalKind,
)

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=CST)
A, B, C, D = "CN.SZ.002695", "CN.SH.600600", "CN.SH.601010", "CN.SZ.000001"


def test_message_version_and_claim_ids():
    m = MessageVersion(source_id="cls.telegraph", item_id="1", revision_id="1@1", content_hash="h", title="t",
                       received_at=T0, available_at=T0 + timedelta(milliseconds=50), recorded_at=T0 + timedelta(seconds=1))
    assert m.event_time is None  # unknown stays unknown
    with pytest.raises(ValidationError):
        MessageVersion(**{**m.model_dump(), "available_at": T0 - timedelta(seconds=1)})
    with pytest.raises(ValidationError):
        EvidenceClaim(claim_id="c", event_id="e", origin_cluster_id="o", claim_type="ANNOUNCEMENT", fact_status="CONFIRMED",
                      message_refs=("1@1",), instruments=("002695",), available_at=T0)


def test_cross_validation_is_three_valued():
    base = dict(event_id="e", anchor=Anchor.FIRST_SEEN, window_s=300, window_start=T0, available_at=T0 + timedelta(minutes=5))
    assert ReactionWindow(**base, tape_confirmed=True, comments_confirmed=True).cross_validated is True
    assert ReactionWindow(**base, tape_confirmed=True, comments_confirmed=None).cross_validated is None  # thin coverage
    assert ReactionWindow(**base, tape_confirmed=False, comments_confirmed=None).cross_validated is False


def test_only_fired_signals_carry_an_intent():
    base = dict(signal_id="s", strategy_ref="news.first_spread@1", instrument_id=A, generated_at=T0,
                valid_until=T0 + timedelta(minutes=5))
    assert ActionSignal(**base, kind=SignalKind.WATCH).intent_id is None
    with pytest.raises(ValidationError):
        ActionSignal(**base, kind=SignalKind.FIRED)
    with pytest.raises(ValidationError):
        ActionSignal(**base, kind=SignalKind.WATCH, intent_id="i")


def test_first_board_clusters_need_three_morning_seals():
    q = (RelationType.REGION, "山东省青岛市")
    boards = [R.FirstBoard(A, T0, (q,)), R.FirstBoard(B, T0 + timedelta(minutes=20), (q,)),
              R.FirstBoard(C, T0.replace(hour=13, minute=10), (q,)), R.FirstBoard(D, T0 + timedelta(minutes=40), (q,))]
    got = R.clusters(boards)
    assert [b.instrument_id for b in got[q]] == [A, B, D]  # the afternoon seal is not 早盘
    assert R.clusters(boards[:2] + boards[2:3]) == {}
    with pytest.raises(ValidationError):
        FirstBoardCluster(cluster_id="x", session_date="2026-11-02", relation=RelationType.REGION, key="青岛",
                          members=(A, A, B), formed_at=T0, available_at=T0)


def test_funnel_rejects_unknowns():
    assert R.funnel_ok(True, 30.0, 5.0, True)
    assert not R.funnel_ok(True, None, 5.0, True)  # free float unknown
    assert not R.funnel_ok(None, 30.0, 5.0, True)
    assert not R.funnel_ok(True, 30.0, 8.0, True)  # gap above the band
    assert not R.funnel_ok(True, 70.0, 5.0, True)


def test_break_state_machine():
    step = lambda prev, at, **kw: R.break_step(prev, at, instrument_id=A, **{  # noqa: E731
        "sealed": False, "above_vwap": True, "below_vwap_s": 0.0, "peers_sealed": 3, "catalyst_pending": True,
        "sector_dump": False, **kw})
    s = step(BreakState.SEALED, T0)
    assert s.state is BreakState.BENIGN and s.variant == "strict"
    assert step(BreakState.SEALED, T0, catalyst_pending=None).state is BreakState.BROKEN_WATCH  # strict: unknown is not benign
    # M3-Tape: an unknown catalyst does not veto the three tape checks; a landed one does
    assert step(BreakState.SEALED, T0, catalyst_pending=None, variant="tape").state is BreakState.BENIGN
    assert step(BreakState.SEALED, T0, catalyst_pending=False, variant="tape").state is BreakState.BROKEN_WATCH
    assert step(BreakState.SEALED, T0, catalyst_pending=None, peers_sealed=1, variant="tape").state is BreakState.BROKEN_WATCH
    assert step(BreakState.BENIGN, T0, below_vwap_s=95.0, above_vwap=False, variant="tape").state is BreakState.EXIT
    assert step(BreakState.SEALED, T0.replace(hour=14, minute=30)).state is BreakState.BROKEN_WATCH  # late break
    assert step(BreakState.BENIGN, T0, above_vwap=False, below_vwap_s=90.0).state is BreakState.EXIT
    assert step(BreakState.BENIGN, T0, sector_dump=True).state is BreakState.EXIT
    assert step(BreakState.BENIGN, T0, sealed=True).state is BreakState.RESEALED
    assert step(BreakState.EXIT, T0, sealed=True).state is BreakState.EXIT  # an exit is not taken back


def _view(author: str, view: AuthorView, minutes: int = 0) -> AuthorExpectation:
    return AuthorExpectation(author_id=author, post_ref=f"{author}@{minutes}", instrument_id=A, for_session="2026-11-03",
                             view=view, available_at=T0 + timedelta(minutes=minutes))


def test_consensus_counts_independent_latest_views():
    n, v = R.consensus([_view("a", AuthorView.STRONG), _view("a", AuthorView.STRONG, 5), _view("b", AuthorView.STRONG)])
    assert (n, v) == (2, None)  # one author twice is still two authors: below the minimum
    n, v = R.consensus([_view("a", AuthorView.NUKE), _view("b", AuthorView.WEAK), _view("c", AuthorView.WEAK),
                        _view("d", AuthorView.CONDITIONAL)])
    assert (n, v) == (3, AuthorView.WEAK)
    n, v = R.consensus([_view("a", AuthorView.STRONG), _view("a", AuthorView.WEAK, 10), _view("b", AuthorView.STRONG),
                        _view("c", AuthorView.STRONG), _view("d", AuthorView.NUKE)])
    assert (n, v) == (4, None)  # a's later bearish view replaces the bullish one: 2 against 2, no two-thirds side


def test_expectation_gap():
    assert R.expectation_gap(AuthorView.WEAK, 3.0, 1.4) is GapType.POSITIVE_SURPRISE
    assert R.expectation_gap(AuthorView.WEAK, 3.0, 0.8) is GapType.IN_LINE  # no volume behind the gap
    assert R.expectation_gap(AuthorView.STRONG, 0.5, 2.0) is GapType.DISAPPOINTMENT
    assert R.expectation_gap(None, 5.0, 2.0) is GapType.NO_CONSENSUS


def test_shadow_entries_three_times():
    e = R.shadow_entries(T0)
    assert [x.tag for x in e] == ["T+15s", "T+60s", "T+300s"]
    assert [(x.decide_at - T0).total_seconds() for x in e] == [15, 60, 300]
    assert R.shadow_fires(e[0], None, None)  # instant: tags only
    assert R.shadow_fires(e[1], True, None) and not R.shadow_fires(e[1], None, None)
    assert not R.shadow_fires(e[2], True, None) and R.shadow_fires(e[2], True, True)


def test_catalyst_status_from_the_calendar():
    from datetime import date
    from contracts.calendar import CalendarEvent
    seen = T0 - timedelta(days=3)
    ev = lambda **kw: CalendarEvent(**{"event_id": "e", "revision": 0, "name": "n", "category": "conference", "region": "CN",  # noqa: E731
                                       "status": "official_scheduled", "event_date": date(2026, 11, 5), "event_end_date": date(2026, 11, 10),
                                       "precision": "date_range", "source_url": "u", "first_seen_at": seen, "instruments": (A,), **kw})
    before, during, after = T0, datetime(2026, 11, 6, 10, tzinfo=CST), datetime(2026, 11, 11, 9, tzinfo=CST)
    assert R.catalyst_pending([ev()], A, before) is True
    assert R.catalyst_pending([ev()], A, during) is None  # under way, no result yet
    assert R.catalyst_pending([ev()], A, after) is False  # window over: REALIZED
    assert R.catalyst_pending([ev(result_known_at=during)], A, during + timedelta(minutes=1)) is False
    assert R.catalyst_pending([ev()], B, before) is None  # not linked
    assert R.catalyst_pending([ev(first_seen_at=T0 + timedelta(hours=1))], A, T0) is None  # not known yet at T0
    assert R.catalyst_pending([ev(), ev(event_id="f", event_date=date(2026, 10, 1), event_end_date=None)], A, before) is True
    assert R.catalyst_pending([ev(category="macro_release")], A, before) is None  # a linked CPI release is no catalyst
    assert R.catalyst_pending([ev(instruments=())], A, before) is None  # a synced row before any theme mapping


def test_three_legs_compound_to_the_total():
    x = R.legs(10.0, 10.3, 9.9, 10.5)
    assert round((1 + x["entry_to_close"] / 100) * (1 + x["overnight"] / 100) * (1 + x["next_open_to_exit"] / 100) - 1, 10) == round(x["total"] / 100, 10)
    assert x["overnight"] < 0
