"""Webhook push: payload shapes per platform, unconfigured behaviour, failures. No network."""
from __future__ import annotations

import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.notify import push
from contracts.schema import DailyReport

URL = "https://hooks.example.test/robot/send?key=SECRET-TOKEN"


def _pick(rank: int, code: str, horizon: str = "swing", score: float = 80.0) -> dict:
    return {
        "rank": rank, "code": code, "name": f"测试{code}", "horizon": horizon, "score": score, "agreement": 3,
        "close": 10.0, "pct_chg": 1.0, "stop_loss": 9.3, "take_profit": 11.2, "holding_days": 10,
        "entry_note": "回踩介入",
        "reasons": [
            {"playbook_id": "a", "playbook_name": "趋势跟随", "weight": 0.2, "signal": 0.5, "note": "站上 20 日线"},
            {"playbook_id": "b", "playbook_name": "放量突破", "weight": 0.5, "signal": 0.9, "note": "量比 2.1"},
        ],
    }


def make_report(mode: str = "LIVE", many: bool = True) -> DailyReport:
    count = 8 if many else 1
    picks = [_pick(i, f"60000{i}", score=90 - i) for i in range(1, count + 1)]
    return DailyReport.model_validate(
        {
            "trade_date": "2026-10-30",
            "generated_at": "2026-10-30T15:40:00+08:00",
            "provenance": {"mode": mode, "source": "t", "as_of": "2026-10-30T15:40:00+08:00",
                           "trade_date": "2026-10-30", "detail": "d"},
            "regime": {"date": "2026-10-30", "trend": "bull", "liquidity": "steady", "sentiment": "climax",
                       "style": "small", "label": "牛市·情绪高潮", "explanation": ["x"]},
            "weights": [],
            "short": [{**p, "horizon": "short"} for p in picks],
            "swing": picks,
            "long": [{**p, "horizon": "long"} for p in picks],
            "overall": picks,
            "notes": [],
        }
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PULSE_PUSH_WEBHOOK", raising=False)
    monkeypatch.delenv("PULSE_PUSH_KIND", raising=False)
    push.reset_for_tests()


class Capture:
    def __init__(self, reply: dict | None = None, status: int = 200) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply = {"errcode": 0} if reply is None else reply
        self.status = status

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        return httpx.Response(self.status, json=self.reply, request=httpx.Request("POST", url))


def configure(monkeypatch, kind: str = "wecom", capture: Capture | None = None) -> Capture:
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", URL)
    monkeypatch.setenv("PULSE_PUSH_KIND", kind)
    capture = capture or Capture()
    monkeypatch.setattr(push.httpx, "post", capture.post)
    return capture


# --- status & never-send --------------------------------------------------------------------


def test_unconfigured_status_and_no_send(monkeypatch) -> None:
    capture = Capture()
    monkeypatch.setattr(push.httpx, "post", capture.post)
    state = push.status()
    assert state.configured is False and state.kind is None and "PULSE_PUSH_WEBHOOK" in state.detail
    result = push.push_report(make_report())
    assert result.configured is False and result.last_pushed is None
    assert capture.calls == []


def test_invalid_webhook_counts_as_unconfigured(monkeypatch) -> None:
    capture = Capture()
    monkeypatch.setattr(push.httpx, "post", capture.post)
    monkeypatch.setenv("PULSE_PUSH_WEBHOOK", "not a url")
    assert push.status().configured is False
    push.push_report(make_report())
    assert capture.calls == []


def test_status_never_reveals_the_webhook(monkeypatch) -> None:
    configure(monkeypatch, "feishu")
    state = push.status()
    assert state.configured and state.kind == "feishu"
    assert "SECRET-TOKEN" not in state.detail and "hooks.example.test" in state.detail


def test_unknown_kind_falls_back_to_generic_and_says_so(monkeypatch) -> None:
    configure(monkeypatch, "slack")
    state = push.status()
    assert state.kind == "generic" and "slack" in state.detail


# --- payload shapes ---------------------------------------------------------------------------


def test_wecom_payload(monkeypatch) -> None:
    capture = configure(monkeypatch, "wecom")
    result = push.push_report(make_report())
    url, payload = capture.calls[0]
    assert url == URL and set(payload) == {"msgtype", "markdown"}
    assert payload["msgtype"] == "markdown" and "content" in payload["markdown"]
    assert result.last_pushed is not None and result.configured


def test_feishu_payload(monkeypatch) -> None:
    capture = configure(monkeypatch, "feishu", Capture({"code": 0}))
    push.push_report(make_report())
    payload = capture.calls[0][1]
    assert payload["msg_type"] == "text" and "**" not in payload["content"]["text"]


def test_dingtalk_payload(monkeypatch) -> None:
    capture = configure(monkeypatch, "dingtalk")
    push.push_report(make_report())
    payload = capture.calls[0][1]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"].startswith("Pulse 每日选股") and payload["markdown"]["text"]


def test_generic_payload_is_the_report_plus_text(monkeypatch) -> None:
    capture = configure(monkeypatch, "generic")
    report = make_report()
    push.push_report(report)
    payload = capture.calls[0][1]
    assert payload["trade_date"] == "2026-10-30" and payload["overall"] and "text" in payload
    assert payload["regime"]["label"] == "牛市·情绪高潮"


def test_digest_content() -> None:
    title, body = push.build_digest(make_report())
    assert "2026-10-30" in title
    assert "牛市·情绪高潮" in body
    assert "研究用途，不构成投资建议" in body
    assert body.count("综合 Top 5") == 1
    overall = body.split("**短线 Top 3**")[0]
    assert "5. " in overall and "6. " not in overall  # five overall picks
    for label in ("短线 Top 3", "波段 Top 3", "中长线 Top 3"):
        assert label in body
    assert "评分 89.0" in body  # score
    assert "放量突破：量比 2.1" in body  # the highest weight x signal reason
    assert "止损 9.30" in body and "止盈 11.20" in body
    assert "[波段]" in body  # horizon in the overall list


def test_sim_digest_warns() -> None:
    _, body = push.build_digest(make_report(mode="SIM"))
    assert "SIM" in body


def test_long_digest_is_truncated_but_keeps_the_disclaimer() -> None:
    report = make_report()
    long_name = "超长名称" * 400
    report = report.model_copy(update={"overall": [report.overall[0].model_copy(update={"name": long_name})] * 5})
    _, body = push.build_digest(report)
    assert len(body) <= push.MAX_CHARS + 40 and "研究用途，不构成投资建议" in body and "已截断" in body


# --- failures ---------------------------------------------------------------------------------


def test_platform_rejection_raises_without_leaking_the_url(monkeypatch) -> None:
    configure(monkeypatch, "wecom", Capture({"errcode": 93000, "errmsg": "invalid webhook url"}))
    with pytest.raises(push.PushError) as caught:
        push.push_report(make_report())
    assert "93000" in str(caught.value) and "SECRET-TOKEN" not in str(caught.value)
    assert push.status().last_pushed is None


def test_http_error_and_transport_error(monkeypatch) -> None:
    configure(monkeypatch, "generic", Capture({}, status=500))
    with pytest.raises(push.PushError, match="HTTP 500"):
        push.push_report(make_report())

    def boom(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(push.httpx, "post", boom)
    with pytest.raises(push.PushError) as caught:
        push.push_report(make_report())
    assert "SECRET-TOKEN" not in str(caught.value)


# --- HTTP API ---------------------------------------------------------------------------------


@pytest.fixture
def fake_picks(monkeypatch):
    module = types.ModuleType("backend.app.playbooks.picks")
    module.latest_report = lambda: None
    generated: list[bool] = []

    def generate_report(force: bool = False):
        generated.append(force)
        return make_report()

    module.generate_report = generate_report
    module.generated = generated
    monkeypatch.setitem(sys.modules, "backend.app.playbooks.picks", module)
    import backend.app.playbooks as package

    monkeypatch.setattr(package, "picks", module, raising=False)
    return module


def test_status_endpoint_unconfigured_and_configured(monkeypatch) -> None:
    with TestClient(app) as client:
        body = client.get("/api/v1/notify/status").json()
        assert body["configured"] is False and "PULSE_PUSH_WEBHOOK" in body["detail"] and "调度器" in body["detail"]
        configure(monkeypatch, "dingtalk")
        body = client.get("/api/v1/notify/status").json()
        assert body["configured"] is True and body["kind"] == "dingtalk"
        assert "SECRET-TOKEN" not in body["detail"]


def test_test_endpoint_is_409_when_unconfigured_and_never_sends(monkeypatch, fake_picks) -> None:
    capture = Capture()
    monkeypatch.setattr(push.httpx, "post", capture.post)
    with TestClient(app) as client:
        response = client.post("/api/v1/notify/test")
    assert response.status_code == 409 and "PULSE_PUSH_WEBHOOK" in response.json()["detail"]
    assert capture.calls == [] and fake_picks.generated == []


def test_test_endpoint_generates_and_sends(monkeypatch, fake_picks) -> None:
    capture = configure(monkeypatch, "wecom")
    with TestClient(app) as client:
        response = client.post("/api/v1/notify/test")
    assert response.status_code == 200 and response.json()["last_pushed"]
    assert len(capture.calls) == 1 and fake_picks.generated == [False]


def test_test_endpoint_prefers_the_latest_report(monkeypatch, fake_picks) -> None:
    fake_picks.latest_report = lambda: make_report()
    capture = configure(monkeypatch, "generic")
    with TestClient(app) as client:
        assert client.post("/api/v1/notify/test").status_code == 200
    assert fake_picks.generated == [] and len(capture.calls) == 1


def test_test_endpoint_reports_a_platform_failure_as_502(monkeypatch, fake_picks) -> None:
    configure(monkeypatch, "wecom", Capture({"errcode": 40058, "errmsg": "bad"}))
    with TestClient(app) as client:
        response = client.post("/api/v1/notify/test")
    assert response.status_code == 502 and "40058" in response.json()["detail"]
