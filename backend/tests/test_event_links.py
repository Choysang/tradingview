from datetime import date, datetime, timezone

import polars as pl

from backend.app.domain import board_rules as R
from backend.app.domain import event_links as L
from contracts.calendar import CalendarEvent

NOW = datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
SW = pl.DataFrame({"code": ["600001", "000002", "300003", "600004", "600005"],
                   "sw2": ["半导体", "半导体", "半导体", "汽车零部件", "自动化设备"],
                   "joined": ["2020-01-01", "2026-12-01", "2020-01-01", "2020-01-01", None]})


def ev(name, category="conference", **kw):
    return CalendarEvent(event_id=name, revision=0, name=name, category=category, region="CN", status="official_scheduled",
                         event_date=date(2026, 11, 4), precision="date", source_url="u", first_seen_at=NOW, **kw)


def test_members_are_main_board_and_point_in_time():
    m = L.members(SW, date(2026, 11, 2))
    assert m["半导体"] == {"600001"}  # 000002 joins in December, 300003 is ChiNext


def test_link_fills_instruments_and_themes_and_skips_broad_events():
    m = L.members(SW, date(2026, 11, 2))
    out = L.link([ev("第二十四届中国半导体封装测试技术与市场年会"), ev("AUTO TECH China 2026 广州国际自动驾驶与具身智能技术展览"),
                  ev("美国:CPI:同比(半导体无关)", category="macro_release"), ev("APEC第三十三次领导人非正式会议")], m)
    assert out[0].instruments == ("600001",) and out[0].themes == ("半导体",)
    assert out[1].instruments == ("600004", "600005") and out[1].themes == ("机器人", "智能汽车")
    assert out[2].instruments == () and out[3].instruments == ()
    assert R.catalyst_pending(out, "600001", NOW) is True and R.catalyst_pending(out, "600002", NOW) is None
