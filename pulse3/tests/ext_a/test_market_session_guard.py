"""Session phases and the ``actionability`` exit code without a fresh recording (no database).

Evidence behind it: the Pulse3Tape task ran ``actionability --scope focus`` every 5 minutes through the lunch break of 2026-11-23 and got exit 5 seventeen
times (11:35-12:55: no recording exists between 11:30 and 13:00), and on 2026-11-24 also at 09:30 and 13:00 (the first board of a session is not
published yet). A feed that is really stale while the market trades must still exit 5.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulse.application import native_quotes as NQ
from pulse.domain.market_rules import holidays as H
from pulse.domain.market_rules import sessions as S
from pulse.workers import analysis

THU = (2026, 11, 12)                 # an ordinary Thursday
SAT = (2026, 11, 14)
NATIONAL_DAY = (2026, 10, 1)         # closed by the official 2026 schedule
EXC = NQ.NoFreshRecording("no fresh recording: the newest native quote was polled at 03:30:48Z, older than 180 s")


def cst(*ymd, hm: str, sec: int = 0) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(*ymd, h, m, sec, tzinfo=S.CST)


@pytest.mark.parametrize("ymd, hm, trading, label", [
    (THU, "09:29", False, "before open"), (THU, "09:30", True, "open"), (THU, "11:29", True, "open"),
    (THU, "11:30", False, "lunch"), (THU, "12:59", False, "lunch"), (THU, "13:00", True, "open"),
    (THU, "14:59", True, "open"), (THU, "15:00", False, "after close"), (THU, "15:05", False, "after close"), (THU, "23:00", False, "after close"),
    (SAT, "10:00", False, "weekend"), (NATIONAL_DAY, "10:00", False, "holiday"),
])
def test_phase_at_boundaries(ymd, hm, trading, label):
    p = S.phase_at(cst(*ymd, hm=hm))
    assert (p.trading, p.label) == (trading, label)


def test_session_start_is_the_opening_instant_of_the_current_session():
    assert S.phase_at(cst(*THU, hm="10:00")).session_start == cst(*THU, hm="09:30")
    assert S.phase_at(cst(*THU, hm="14:00")).session_start == cst(*THU, hm="13:00")
    assert S.phase_at(cst(*THU, hm="12:00")).session_start is None


def test_utc_input_is_read_in_shanghai_time():
    assert S.phase_at(datetime(2026, 11, 12, 3, 40, tzinfo=timezone.utc)).label == "lunch"      # 11:40 CST
    with pytest.raises(ValueError):
        S.phase_at(datetime(2026, 11, 12, 11, 40))


def test_a_weekday_of_a_year_without_a_complete_schedule_is_trading_so_a_stale_feed_still_fails():
    assert H.is_open(datetime(2027, 1, 4).date()) is None                       # 2027 is MISSING in the official schedules
    assert S.phase_at(cst(2027, 1, 4, hm="10:00")).trading is True
    assert S.phase_at(cst(2027, 1, 4, hm="10:00")).label == "open"


@pytest.mark.parametrize("ymd, hm, why", [
    (THU, "11:35", "lunch"), (THU, "12:55", "lunch"), (THU, "09:10", "before open"), (THU, "15:30", "after close"),
    (SAT, "10:00", "weekend"), (NATIONAL_DAY, "10:00", "holiday"),
])
def test_no_fresh_recording_outside_the_trading_minutes_skips_cleanly(ymd, hm, why, capsys):
    assert analysis._no_fresh_recording(EXC, cst(*ymd, hm=hm)) == 0
    out = capsys.readouterr()
    assert f"market closed: {why}" in out.out and "nothing published" in out.out
    assert out.err == ""                                                          # stderr would become an error record in the PowerShell 5.1 task log


@pytest.mark.parametrize("ymd, hm, sec", [(THU, "09:30", 0), (THU, "09:32", 59), (THU, "13:00", 2), (THU, "13:02", 59)])
def test_the_first_three_minutes_of_a_session_wait_for_the_first_board(ymd, hm, sec, capsys):
    assert analysis._no_fresh_recording(EXC, cst(*ymd, hm=hm, sec=sec)) == 0
    out = capsys.readouterr()
    assert "market opening" in out.out and out.err == ""


@pytest.mark.parametrize("ymd, hm, sec", [
    (THU, "09:33", 0), (THU, "10:13", 0), (THU, "11:29", 59), (THU, "13:03", 0), (THU, "14:59", 59),
    ((2027, 1, 4), "10:00", 0),
])
def test_a_stale_feed_during_trading_minutes_still_fails_loudly(ymd, hm, sec, capsys):
    assert analysis._no_fresh_recording(EXC, cst(*ymd, hm=hm, sec=sec)) == 5
    out = capsys.readouterr()
    assert "no fresh recording" in out.err and "nothing published" in out.err and out.out == ""


def test_the_grace_is_exactly_the_recording_max_age():
    just_inside = cst(*THU, hm="09:30") + NQ.MAX_AGE - timedelta(seconds=1)
    assert analysis._no_fresh_recording(EXC, just_inside) == 0
    assert analysis._no_fresh_recording(EXC, cst(*THU, hm="09:30") + NQ.MAX_AGE) == 5
