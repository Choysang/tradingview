"""Builders of vendor bodies and a two-host fake world for the native intraday recorder tests (mock transports only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import json

import httpx

DAY = date(2026, 11, 12)
UTC = timezone.utc
CST = timezone(timedelta(hours=8))
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rec_intraday"
POOL_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "rec_pools"
POOL_HOST = "push2ex.eastmoney.com"
POOL_FILES = {"getTopicZTPool": "old_zt_20261102.json", "getTopicZBPool": "old_zb_20261102.json", "getTopicDTPool": "old_dt_20261102.json"}
Level = tuple[float, int]      # (price, LOTS); a level without an order is (0, 0)


@dataclass
class Stock:
    """Logical state of one stock; both vendor bodies are rendered from it, so the two renderings must normalise to the same row."""
    code: str
    name: str
    prev: float
    price: float
    bids: list[Level] = field(default_factory=lambda: [(0.0, 0)] * 5)
    asks: list[Level] = field(default_factory=lambda: [(0.0, 0)] * 5)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    lots: int = 1000
    amount: float = 1_000_000.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    outer: int = 600
    inner: int = 400
    turnover: float = 1.5
    float_cap: float = 100.0
    total_cap: float = 120.0
    volume_ratio: float = 1.1
    vwap: float = 10.0
    after_lots: int = 0
    after_wan: float = 0.0

    @property
    def prefix(self) -> str:
        return "sh" if self.code[0] == "6" else "sz"


def sina_line(st: Stock, when: datetime, trailer: str | None = None) -> str:
    f = [st.name, f"{st.open or st.price:.3f}", f"{st.prev:.3f}", f"{st.price:.3f}", f"{st.high or st.price:.3f}", f"{st.low or st.price:.3f}",
         f"{st.bids[0][0]:.3f}", f"{st.asks[0][0]:.3f}", str(st.lots * 100), f"{st.amount:.3f}"]
    for (px, lots) in st.bids:
        f += [str(lots * 100), f"{px:.3f}"]
    for (px, lots) in st.asks:
        f += [str(lots * 100), f"{px:.3f}"]
    f += [when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S"), "00"]
    if trailer:
        f.append(trailer)
    return f'var hq_str_{st.prefix}{st.code}="{",".join(f)}";'


def tencent_line(st: Stock, when: datetime) -> str:
    f = [""] * 62
    f[0], f[1], f[2], f[3], f[4], f[5] = "51", st.name, st.code, f"{st.price:.2f}", f"{st.prev:.2f}", f"{st.open or st.price:.2f}"
    f[6], f[7], f[8] = str(st.lots), str(st.outer), str(st.inner)
    for i, (px, lots) in enumerate(st.bids):
        f[9 + 2 * i], f[10 + 2 * i] = f"{px:.2f}", str(lots)
    for i, (px, lots) in enumerate(st.asks):
        f[19 + 2 * i], f[20 + 2 * i] = f"{px:.2f}", str(lots)
    f[30] = when.strftime("%Y%m%d%H%M%S")
    f[33], f[34] = f"{st.high or st.price:.2f}", f"{st.low or st.price:.2f}"
    f[35] = f"{st.price:.2f}/{st.lots}/{int(st.amount)}"
    f[38], f[44], f[45] = f"{st.turnover:.2f}", f"{st.float_cap:.2f}", f"{st.total_cap:.2f}"
    f[47], f[48], f[49], f[51] = f"{st.limit_up:.2f}", f"{st.limit_down:.2f}", f"{st.volume_ratio:.2f}", f"{st.vwap:.2f}"
    f[58], f[59] = f"{st.after_wan:.4f}", str(st.after_lots)
    return f'v_{st.prefix}{st.code}="{"~".join(f)}";'


def sina_body(stocks: list[Stock], when: datetime, trailer: str | None = None) -> bytes:
    return "\n".join(sina_line(s, when, trailer) for s in stocks).encode("gbk")


def tencent_body(stocks: list[Stock], when: datetime) -> bytes:
    return ("\n".join(tencent_line(s, when) for s in stocks) + "\nv_pv_none_match=\"1\";").encode("gbk")


def sealed(code: str = "000001", name: str = "平安银行") -> Stock:
    """A board sealed at the 10% limit: queue 5000 lots on the bid, nothing on the ask."""
    return Stock(code, name, 10.0, 11.0, bids=[(11.0, 5000), (10.99, 100), (0, 0), (0, 0), (0, 0)], limit_up=11.0, limit_down=9.0,
                 high=11.0, low=10.2, asks=[(0.0, 0)] * 5)


def plain(code: str = "600000", name: str = "浦发银行") -> Stock:
    return Stock(code, name, 10.0, 10.5, bids=[(10.49, 200), (10.48, 300), (10.47, 100), (10.46, 100), (10.45, 50)],
                 asks=[(10.51, 300), (10.52, 100), (10.53, 100), (10.54, 100), (10.55, 80)], limit_up=11.0, limit_down=9.0, high=10.6, low=10.4)


class World:
    """A fake clock plus two vendor hosts. ``block`` / ``fail`` map a host to the request number (counted per host) from which it answers
    403 / 500; ``stale`` stamps the quotes with an earlier day."""

    def __init__(self, start_utc: datetime, stocks: list[Stock]):
        self.t, self.mono, self.stocks = start_utc, 0.0, stocks
        self.requests: dict[str, list[tuple[float, int]]] = {"hq.sinajs.cn": [], "qt.gtimg.cn": [], POOL_HOST: []}
        self.pool_fail_first = 0                                   # the first N pool requests answer 500
        self.pool_mode = "ok"                                      # ok | html (a block page with status 200) | stale (the last session's qdate)
        self.block: dict[str, int] = {}
        self.fail: dict[str, int] = {}
        self.stale = False
        self.cap_symbols: int | None = None                       # the vendor silently answers only the first N symbols of a request
        self.trailer: str | None = None
        self.log: list[tuple[datetime, str, list[str]]] = []      # (time, host, symbols asked) of every request

    def now(self) -> datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += timedelta(seconds=secs)
        self.mono += secs

    def monotonic(self) -> float:
        return self.mono

    def count(self, host: str) -> int:
        return len(self.requests[host])

    def pool_handler(self, req: httpx.Request) -> httpx.Response:
        """The old recorder's real 2026-11-02 pool bodies, with qdate set to the requested date (what the vendor does on a session)."""
        self.log.append((self.t, POOL_HOST, [req.url.path.strip("/")]))
        self.requests[POOL_HOST].append((self.mono, 0))
        n = len(self.requests[POOL_HOST])
        if POOL_HOST in self.block and n >= self.block[POOL_HOST]:
            return httpx.Response(403, content=b"forbidden")
        if n <= self.pool_fail_first:
            return httpx.Response(500, content=b"oops")
        if self.pool_mode == "html":
            return httpx.Response(200, content=b"<html>captcha</html>", headers={"content-type": "text/html"})
        doc = json.loads((POOL_FIXTURES / POOL_FILES[req.url.path.strip("/")]).read_text(encoding="utf-8"))
        if self.pool_mode != "stale":
            doc["data"]["qdate"] = int(req.url.params["date"])
        return httpx.Response(200, content=json.dumps(doc, ensure_ascii=False).encode("utf-8"), headers={"content-type": "application/json"})

    def gaps(self, host: str) -> list[float]:
        times = [t for t, _ in self.requests[host]]
        return [b - a for a, b in zip(times, times[1:])]

    def handler(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == POOL_HOST:
            return self.pool_handler(req)
        want = str(req.url).split("list=" if host == "hq.sinajs.cn" else "q=")[1].split(",")
        self.log.append((self.t, host, want))
        if self.cap_symbols is not None:
            want = want[:self.cap_symbols]
        self.requests[host].append((self.mono, 0))
        n = len(self.requests[host])
        if host in self.block and n >= self.block[host]:
            return httpx.Response(403, content=b"forbidden")
        if host in self.fail and n >= self.fail[host]:
            return httpx.Response(500, content=b"oops")
        when = self.t.astimezone(CST).replace(tzinfo=None)
        if self.stale:
            when -= timedelta(days=1)
        wanted = set(want)
        chosen = [s for s in self.stocks if f"{s.prefix}{s.code}" in wanted]
        body = sina_body(chosen, when, self.trailer) if host == "hq.sinajs.cn" else tencent_body(chosen, when)
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})
