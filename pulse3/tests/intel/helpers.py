"""Helpers: recorded fixtures and a MockTransport that answers the intelligence sources from them (never the network)."""
from __future__ import annotations

from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / "fixtures" / "intel" / "recorded"
SPEC_PKG = ROOT / "spec_package"


def recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def transport(*, overrides: dict | None = None, calls: list | None = None) -> httpx.MockTransport:
    """Route by host/path to the recorded bodies; ``overrides`` maps a substring of the URL to (status, bytes)."""
    overrides = overrides or {}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if calls is not None:
            calls.append(url)
        for k, (status, body) in overrides.items():
            if k in url:
                return httpx.Response(status, content=body)
        if "getFastNewsList" in url:
            return httpx.Response(200, content=recorded("eastmoney_news_p1.json"), headers={"content-type": "application/json"})
        if "cls.cn/v1/roll" in url:
            return httpx.Response(200, content=recorded("cls_roll_p1.json"), headers={"content-type": "application/json"})
        if "guba.eastmoney.com/list,000001" in url:
            return httpx.Response(200, content=recorded("guba_list_000001_f.html"), headers={"content-type": "text/html"})
        if "RPT_DAILYBILLBOARD_DETAILSNEW" in url:
            f = "lhb_daily_list_20261110.json" if "2026-11-10" in url else "lhb_daily_list_empty_20261108.json"
            return httpx.Response(200, content=recorded(f), headers={"content-type": "text/plain"})
        if "RPT_BILLBOARD_DAILYDETAILSBUY" in url:
            return httpx.Response(200, content=recorded("lhb_seat_buy_20261110.json"), headers={"content-type": "text/plain"})
        if "RPT_BILLBOARD_DAILYDETAILSSELL" in url:
            return httpx.Response(200, content=recorded("lhb_seat_sell_20261110.json"), headers={"content-type": "text/plain"})
        if "fflow/daykline" in url:
            return httpx.Response(200, content=recorded("fundflow_000001.json"), headers={"content-type": "application/json"})
        return httpx.Response(404, content=b"not recorded")

    return httpx.MockTransport(handler)


def client(*, now=None, transport_=None):
    from pulse.adapters.sources.http import RateLimitedClient

    return RateLimitedClient(min_interval_seconds=0, transport=transport_ or transport(), sleep=lambda s: None, now=now)


def em_page(items: list[dict], sort_end: str | None = "1") -> bytes:
    """Synthetic Eastmoney page in the recorded shape (used only for revision / cluster scenarios)."""
    import json

    return json.dumps({"code": "1", "message": "success", "data": {"sortEnd": sort_end, "fastNewsList": items}}, ensure_ascii=False).encode()


def em_item(code: str, title: str, summary: str, show_time: str, stocks: list[str] | None = None) -> dict:
    return {"code": code, "title": title, "summary": summary, "showTime": show_time, "stockList": stocks or [], "realSort": "1", "image": []}


def cninfo_page(ann_id: str, sec_code: str, title: str, ms: int) -> bytes:
    import json

    return json.dumps({"totalAnnouncement": 1, "hasMore": False, "announcements": [{"announcementId": ann_id, "secCode": sec_code, "secName": "x",
                       "announcementTitle": title, "announcementTime": ms, "adjunctUrl": f"finalpage/2026-11-11/{ann_id}.PDF", "adjunctSize": 1,
                       "adjunctType": "PDF"}]}, ensure_ascii=False).encode()
