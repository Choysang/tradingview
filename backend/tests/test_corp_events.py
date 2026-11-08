from datetime import date, datetime, timezone

import httpx
import polars as pl
import pytest

from backend.app.warehouse import corp_events as E

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
LIFT = [  # shapes as answered on 2026-11-02
    {"SECURITY_CODE": "002116", "SECURITY_NAME_ABBR": "中国海诚", "FREE_DATE": "2026-11-02 00:00:00", "CURRENT_FREE_SHARES": 46.0012,
     "ABLE_FREE_SHARES": 46.0012, "LIFT_MARKET_CAP": 306.367992, "FREE_RATIO": 0.000832430688, "FREE_SHARES_TYPE": "股权激励限售股份",
     "TOTAL_RATIO": 0.000822988904, "NON_FREE_SHARES": 633.9882, "BATCH_HOLDER_NUM": 1},
    {"SECURITY_CODE": "600001", "FREE_DATE": "2026-11-09 00:00:00", "CURRENT_FREE_SHARES": 1000, "FREE_RATIO": 0.2},
    {"SECURITY_CODE": "600001", "FREE_DATE": "2026-11-30 00:00:00", "CURRENT_FREE_SHARES": 1000, "FREE_RATIO": 0.3},
    {"SECURITY_CODE": "920007", "FREE_DATE": "2026-11-02 00:00:00", "CURRENT_FREE_SHARES": 180, "FREE_RATIO": 0.13},  # 北交所: out
]
STOP = [{"SECURITY_CODE": "000831", "SECURITY_NAME_ABBR": "中国稀土", "SUSPEND_START_TIME": "2026-11-02 09:30:00",
         "SUSPEND_END_TIME": "2026-11-13 15:00:00", "SUSPEND_EXPIRE": "连续停牌", "SUSPEND_REASON": "拟筹划重大资产重组",
         "PREDICT_RESUME_DATE": "2026-11-16 00:00:00"}]


def client(answers, seen):
    def handler(request):
        seen.append(dict(request.url.params))
        return answers.pop(0)
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(rows):
    return httpx.Response(200, json={"success": True, "result": {"pages": 1, "data": rows} if rows else None})


def test_sync_keeps_main_board_snapshots_with_fetch_time(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "PACE", 0)
    seen = []
    assert E.sync(30, NOW, client([ok(LIFT), ok(STOP)], seen), tmp_path) == {"unlocks": 3, "unlocks_all_boards": 4, "suspensions": 1}
    assert seen[0]["filter"] == "(FREE_DATE>='2026-11-02')(FREE_DATE<='2026-12-02')"
    assert seen[1]["filter"] == "(MARKET=\"全部\")(DATETIME='2026-11-02')"
    u = pl.read_parquet(tmp_path / "unlocks" / "2026-11-02.parquet")
    assert u.filter(pl.col("code") == "002116")["value_at_fetch_wan"][0] == pytest.approx(306.367992)
    s = pl.read_parquet(tmp_path / "suspensions" / "2026-11-02.parquet").row(0, named=True)
    assert (s["expected_resume"], s["reason"]) == (date(2026, 11, 16), "拟筹划重大资产重组")
    assert (tmp_path / "unlocks" / "2026-11-02.raw.json.gz").exists()
    p = E.unlock_pressure(u, date(2026, 11, 2), 10)
    assert p.row(0) == ("600001", 0.2, date(2026, 11, 9))  # the 11-30 batch is beyond the horizon


def test_empty_day_and_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "PACE", 0)
    assert E.sync(30, NOW, client([ok([]), ok([])], []), tmp_path)["suspensions"] == 0
    with pytest.raises(RuntimeError, match="refused"):
        E.sync(30, NOW, client([httpx.Response(429)], []), tmp_path)
