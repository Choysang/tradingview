"""Helpers of the native news recorder tests: recorded answers, mutable MockTransport per host, in-memory sink, fake clock. Never the network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pulse.adapters.sources import native_news
from pulse.adapters.sources.http import RateLimitedClient

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2] / "fixtures"
THS, EM, CLS = native_news.ORIGIN_THS, native_news.ORIGIN_EM, native_news.ORIGIN_CLS
HOSTS = {THS: "news.10jqka.com.cn", EM: "np-listapi.eastmoney.com", CLS: "www.cls.cn"}


def recorded(name: str) -> bytes:
    return (ROOT / ("news_rec" if name.startswith("ths") else "intel/recorded") / name).read_bytes()


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class World:
    """The three hosts: ``bodies[origin]`` is what the host answers (bytes), ``status[origin]`` the HTTP status; ``requests`` counts per host."""

    def __init__(self) -> None:
        self.bodies = {THS: recorded("ths_push_p1.json"), EM: recorded("eastmoney_news_p1.json"), CLS: recorded("cls_roll_p1.json")}
        self.status = {o: 200 for o in HOSTS}
        self.requests = {o: 0 for o in HOSTS}

    def handler_for(self, origin: str):
        def handle(req: httpx.Request) -> httpx.Response:
            assert req.url.host == HOSTS[origin], f"{origin} client sent a request to {req.url.host}"
            self.requests[origin] += 1
            return httpx.Response(self.status[origin], content=self.bodies[origin], headers={"content-type": "application/json"})
        return handle

    def clients(self, clock: Clock) -> dict[str, RateLimitedClient]:
        return {o: RateLimitedClient(min_interval_seconds=0.0, transport=httpx.MockTransport(self.handler_for(o)), now=clock, sleep=lambda s: None)
                for o in HOSTS}

    # ---- edits of the recorded answers -----------------------------------------------------------------------------
    def edit(self, origin: str, fn) -> None:
        doc = json.loads(self.bodies[origin])
        rows = {THS: lambda d: d["data"]["list"], EM: lambda d: d["data"]["fastNewsList"], CLS: lambda d: d["data"]["roll_data"]}[origin](doc)
        fn(rows)
        self.bodies[origin] = json.dumps(doc, ensure_ascii=False).encode("utf-8")


class MemSink:
    """NewsSink in memory: stores versions per (origin, item); ``fail_writes`` makes the next writes raise."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], list[dict]] = {}
        self.writes: list[tuple[str, list[dict]]] = []
        self.fail_writes = 0
        self.heartbeats: list[str] = []
        self.bad: list[str] = []
        self.blocked_pages: list[str] = []
        self.open_circuits: dict[str, str] = {}
        self.claim_calls = 0

    def circuit_open(self, origin):
        return self.open_circuits.get(origin)

    def fetch(self, origin, call):
        return call()

    def bad_answer(self, origin, exc, body):
        self.bad.append(origin)

    def blocked_page(self, origin, exc):
        self.blocked_pages.append(origin)

    def heartbeat(self, origin, newest):
        self.heartbeats.append(origin)

    def previous(self, origin, item_id):
        v = self.versions.get((origin, item_id))
        return None if not v else {"content_hash": v[-1]["content_hash"], "revision_id": v[-1]["revision_id"]}

    def write(self, origin, messages, cap):
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("database down")
        self.writes.append((origin, list(messages)))
        for m in messages:
            self.versions.setdefault((origin, m["item_id"]), []).append(m)
        return len(messages)

    def claims(self):
        self.claim_calls += 1
        return {"claims_new": 0}

    def all_messages(self, origin: str | None = None) -> list[dict]:
        return [m for o, ms in self.writes for m in ms if origin in (None, o)]
