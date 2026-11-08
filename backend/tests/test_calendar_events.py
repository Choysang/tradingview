import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.warehouse import calendar_events as C
from contracts.calendar import CalendarEvent, Precision

UTC = timezone.utc
SEED = Path(__file__).resolve().parents[2] / "docs/research/seeds/calendar_seed_2026-11-02.json"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


@pytest.mark.skipif(not SEED.exists(), reason="seed file not in the checkout")
def test_seed_imports_once_and_revises_on_change(tmp_path: Path):
    store = tmp_path / "events.jsonl"
    assert C.import_seed(SEED, store, NOW) == (18, 0)
    assert C.import_seed(SEED, store, NOW + timedelta(hours=1)) == (0, 0)  # unchanged: nothing appended
    ev = C.load(store)
    nvda = ev["US-NVDA-Q3-FY2027"]
    assert nvda.event_at == datetime(2026, 11, 18, 5, 20, tzinfo=timezone(timedelta(hours=8))).astimezone(nvda.event_at.tzinfo)
    assert nvda.first_seen_at == NOW and nvda.schedule_known_at is None  # import time, nothing back-filled
    assert ev["CN-CIIE-2026"].event_at is None and ev["CN-CIIE-2026"].precision is Precision.DATE_RANGE
    data = json.loads(SEED.read_text(encoding="utf-8"))
    data["events"][2]["event_local_end_date"] = "2026-11-11"  # a reschedule
    changed = tmp_path / "seed2.json"
    changed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert C.import_seed(changed, store, NOW + timedelta(days=1)) == (0, 1)
    latest = C.load(store)["CN-CIIE-2026"]
    assert (latest.revision, latest.supersedes, latest.event_end_date) == (1, 0, date(2026, 11, 11))


def test_expiry_rules_roll_over_holidays():
    weekdays = lambda d: d.weekday() < 5  # noqa: E731
    got = C.expiries(2026, 11, weekdays)
    assert got == {"index_futures_options": date(2026, 11, 20), "etf_options": date(2026, 11, 25)}
    assert C.expiries(2026, 12, weekdays) == {"index_futures_options": date(2026, 12, 18), "etf_options": date(2026, 12, 23)}
    holiday = lambda d: weekdays(d) and d != date(2026, 11, 20)  # noqa: E731
    assert C.expiries(2026, 11, holiday)["index_futures_options"] == date(2026, 11, 23)


def test_calendar_event_refuses_invented_minutes():
    base = dict(event_id="x", revision=0, name="n", category="conference", region="CN", status="official_scheduled",
                event_date=date(2026, 11, 5), source_url="u", first_seen_at=NOW)
    CalendarEvent(**base, precision="date_range")
    with pytest.raises(ValidationError):
        CalendarEvent(**base, precision="date", event_at=NOW)
    with pytest.raises(ValidationError):
        CalendarEvent(**base, precision="minute")
    ev = CalendarEvent(**base, precision="date")
    assert not ev.usable_at(NOW - timedelta(seconds=1)) and ev.usable_at(NOW)


EM_ROWS = [  # shapes as answered on 2026-11-02
    {"START_DATE": "2026-11-09 09:30:00", "END_DATE": "2026-11-09 09:30:00", "FE_CODE": "1", "FE_NAME": "中国:CPI:同比(报告期:2026年10月)", "FE_TYPE": "经济数据"},
    {"START_DATE": "2026-11-03 11:30:00", "END_DATE": "2026-11-03 11:30:00", "FE_CODE": "2", "FE_NAME": "澳大利亚：央行公布利率决议(报告期:2026年11月)", "FE_TYPE": "经济数据"},
    {"START_DATE": "2026-11-05 00:00:00", "END_DATE": "2026-11-10 00:00:00", "FE_CODE": "3", "FE_NAME": "第九届中国国际进口博览会", "FE_TYPE": "行业会议", "CITY": "上海市"},
    {"START_DATE": "2026-11-11 23:30:00", "END_DATE": "2026-11-11 23:30:00", "FE_CODE": "4", "FE_NAME": "美国EIA原油库存:变动值", "FE_TYPE": "经济数据"},
    {"START_DATE": "2026-11-12 23:30:00", "END_DATE": "2026-11-12 23:30:00", "FE_CODE": "4", "FE_NAME": "美国EIA原油库存:变动值", "FE_TYPE": "经济数据"},
    {"START_DATE": "2026-11-03 00:00:00", "END_DATE": "2026-11-03 00:00:00", "FE_CODE": "5", "FE_NAME": "苹果将于北京时间11月3日公布第四财季财报", "FE_TYPE": None},
    {"START_DATE": "2026-11-13 16:30:00", "END_DATE": "2026-11-13 16:30:00", "FE_CODE": "6", "FE_NAME": "中国香港：香港:GDP:当季值", "FE_TYPE": "经济数据", "CITY": "中国香港"},
    {"START_DATE": "2026-11-13 14:30:00", "END_DATE": "2026-11-13 14:30:00", "FE_CODE": "6", "FE_NAME": "中国香港：香港:GDP:当季值", "FE_TYPE": "经济数据", "CITY": "中国香港"},
    {"START_DATE": "2026-11-18 15:00:00", "END_DATE": "2026-11-18 15:00:00", "FE_CODE": "7", "FE_NAME": "英国:CPI:同比", "FE_TYPE": "经济数据", "CITY": "英国"},
]


def _em_client(pages, seen):
    import httpx

    def handler(request):
        seen.append(dict(request.url.params))
        return pages.pop(0)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sync_maps_precision_region_and_revises(tmp_path: Path):
    import httpx
    store, seen = tmp_path / "events.jsonl", []
    ok = lambda rows: httpx.Response(200, json={"success": True, "result": {"pages": 1, "data": rows}})  # noqa: E731
    assert C.sync(30, store, NOW, _em_client([ok(EM_ROWS)], seen)) == (8, 0)
    assert seen[0]["reportName"] == "RPT_CPH_FECALENDAR" and seen[0]["filter"] == "(END_DATE>='2026-11-02')(START_DATE<'2026-12-02')"
    ev = C.load(store)
    cpi = ev["EM-1-20261109"]
    assert (cpi.precision, cpi.region, cpi.category.value) == (Precision.MINUTE, "CN", "macro_release")
    assert cpi.event_at == datetime(2026, 11, 9, 1, 30, tzinfo=UTC)
    assert ev["EM-2-20261103"].category.value == "overseas_policy" and ev["EM-2-20261103"].region == "GLOBAL"
    expo = ev["EM-3-20261105"]  # 00:00:00 means date only: never a minute
    assert (expo.precision, expo.event_at, expo.event_end_date, expo.region) == (Precision.DATE_RANGE, None, date(2026, 11, 10), "CN")
    assert {"EM-4-20261111", "EM-4-20261112"} <= set(ev) and ev["EM-5-20261103"].category.value == "overseas_market"
    gdp = ev["EM-6-20261113"]  # one release at two minutes: the earliest, approximate, both in notes
    assert (gdp.precision, gdp.event_at.astimezone(UTC).hour, gdp.region) == (Precision.APPROX_MINUTE, 6, "HK") and "14:30, 16:30" in gdp.notes
    assert ev["EM-7-20261118"].region == "GLOBAL"  # the place column names the country: 英国 is not CN
    assert C.sync(30, store, NOW + timedelta(hours=1), _em_client([ok(EM_ROWS[::-1])], [])) == (0, 0)  # stable
    moved = [{**EM_ROWS[0], "START_DATE": "2026-11-09 10:00:00", "END_DATE": "2026-11-09 10:00:00"}]
    assert C.sync(30, store, NOW + timedelta(days=1), _em_client([ok(EM_ROWS[1:]), ], [])) == (0, 0)
    assert C.sync(30, store, NOW + timedelta(days=1), _em_client([ok(moved)], [])) == (0, 1)
    assert C.load(store)["EM-1-20261109"].revision == 1


def test_sync_stops_on_a_refusal(tmp_path: Path):
    import httpx
    with pytest.raises(RuntimeError, match="refused"):
        C.sync(30, tmp_path / "e.jsonl", NOW, _em_client([httpx.Response(429)], []))
    with pytest.raises(RuntimeError, match="non-JSON"):
        C.sync(30, tmp_path / "e.jsonl", NOW, _em_client([httpx.Response(200, text="<html>")], []))
    assert not (tmp_path / "e.jsonl").exists()
