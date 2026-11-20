"""The news recorder's ``record`` command skips weekends and official exchange closures; an unknown weekday is still polled."""
from __future__ import annotations

from datetime import date

from pulse.domain.market_rules import holidays as H
from pulse.workers import rec_news


def test_skip_reason_covers_weekends_and_complete_year_closures_but_not_unknown_days():
    assert "weekend" in rec_news.skip_reason(date(2026, 11, 21))
    assert "exchange holiday" in rec_news.skip_reason(date(2026, 2, 17))      # Spring Festival weekday of the SSE 2026 notice
    assert "exchange holiday" in rec_news.skip_reason(date(2026, 10, 1))
    assert rec_news.skip_reason(date(2026, 11, 20)) is None                   # an ordinary open weekday
    assert H.is_open(date(2027, 1, 4)) is None and rec_news.skip_reason(date(2027, 1, 4)) is None, "unknown keeps polling"


def test_record_on_a_closure_exits_before_any_database_or_network_use(monkeypatch, capsys):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("the recorder must not touch config or the database on a closure day")

    from datetime import datetime, timezone

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 2, 17, 1, 0, tzinfo=timezone.utc)           # 09:00 Shanghai on a Spring Festival Tuesday

    monkeypatch.setattr(rec_news, "datetime", FakeDatetime)
    monkeypatch.setattr(rec_news, "config", Boom())
    assert rec_news.run(["record"]) == 0
    assert "exchange holiday" in capsys.readouterr().out
