"""A fake HTTP world for the reference-data producers: recorded fixtures plus small synthetic series, a fake clock, a switchable block."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from pulse.adapters.sources.http import RateLimitedClient

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "data" / "refdata"
UTC = timezone.utc
START = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)         # 16:30 CST on a Thursday: the session is complete and daily bars are final
BARS_FROM = date(2025, 3, 1)


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def synthetic_kline(code_seed: int, days: int = 30, end: date = date(2026, 11, 18)) -> bytes:
    rows, d, price = [], end, 10.0 + code_seed
    dates = []
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    for i, day in enumerate(sorted(dates)):
        p = price + i * 0.01
        rows.append({"day": day.isoformat(), "open": f"{p:.3f}", "high": f"{p + 0.1:.3f}", "low": f"{p - 0.1:.3f}", "close": f"{p + 0.05:.3f}",
                     "volume": str(1_000_000 + i)})
    return json.dumps(rows).encode()


def factor_body(symbol: str, entries: list[tuple[str, str]]) -> bytes:
    data = ",".join(f'{{"d":"{d}", "f":"{f}"}}' for d, f in entries)
    return f'var {symbol}qfq={{"total":{len(entries)},"data":[{data}]}}'.encode()


class World:
    """Clock shared by the service, the rate limiter and the transport; ``block_after`` makes every later request a 403."""

    def __init__(self, start: datetime = START):
        self.t = start
        self.mono = 0.0
        self.requests: list[str] = []
        self.block_after: int | None = None
        self.factor_override: dict[str, bytes] = {}
        self.kline_override: dict[str, bytes] = {}
        self.index_close_bump: float = 0.0
        self.member_count = 3
        self.quotes: dict[str, dict] = {}
        self.fail_symbols: set[str] = set()
        self.unreachable_hosts: set[str] = set()                           # the connection is reset (httpx.ConnectError), not refused
        self.host_status: dict[str, int] = {}                              # a host answers this HTTP status for every request
        self.em_sz, self.em_sh = 150, 100                                  # rows of the synthetic Eastmoney list (Shenzhen, Shanghai)
        self.member_status: dict[str, int] = {}

    def now(self) -> datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += timedelta(seconds=secs)
        self.mono += secs

    def monotonic(self) -> float:
        return self.mono

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)

    def client(self) -> RateLimitedClient:
        return RateLimitedClient(min_interval_seconds=1.0, transport=httpx.MockTransport(self.handler), clock=self.monotonic,
                                 sleep=self.sleep, now=self.now)

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.requests.append(url)
        if self.block_after is not None and len(self.requests) > self.block_after:
            return httpx.Response(403, content=b"forbidden")
        parts, q = urlsplit(url), parse_qs(urlsplit(url).query)
        host = parts.hostname
        if host in self.unreachable_hosts:
            raise httpx.ConnectError("[WinError 10054] connection reset", request=req)
        if host in self.host_status:
            return httpx.Response(self.host_status[host], content=b"forbidden")
        if host == "82.push2.eastmoney.com":
            return httpx.Response(200, content=self.eastmoney_page(int(q["pn"][0]), int(q["pz"][0])))
        if host == "quotes.sina.cn":
            sym = q["symbol"][0]
            if sym in self.fail_symbols:
                return httpx.Response(500, content=b"boom")
            if sym.startswith(("sh0", "sz3")) and sym in ("sh000001", "sh000300", "sh000852", "sh000905", "sz399006"):
                return httpx.Response(200, content=self.index_body())
            if sym in self.kline_override:
                return httpx.Response(200, content=self.kline_override[sym])
            return httpx.Response(200, content=fixture("sina_adj_kline_sz000001.json") if sym == "sz000001" else synthetic_kline(int(sym[-3:])))
        if host == "finance.sina.com.cn" and parts.path.endswith("qfq.js"):
            sym = parts.path.split("/")[-2]
            if sym in self.factor_override:
                return httpx.Response(200, content=self.factor_override[sym])
            return httpx.Response(200, content=fixture("sina_adj_factor_sz000001.js") if sym == "sz000001" else factor_body(sym, [("1990-01-01", "1.0")]))
        if host == "web.ifzq.gtimg.cn":
            return httpx.Response(200, content=fixture("tencent_index_kline_sh000985.json"))
        if host == "money.finance.sina.com.cn":
            return httpx.Response(200, content=fixture("sina_board_nodes.json"))
        if host == "vip.stock.finance.sina.com.cn":
            node, page = q["node"][0], int(q["page"][0])
            if node in self.member_status:
                return httpx.Response(self.member_status[node], content=b"boom")
            if page > 1:
                return httpx.Response(200, content=b"[]")
            n = sum(map(ord, node)) % 250                                   # codes 600000..600752; boards share codes, so some stocks sit in two boards
            rows = [{"symbol": f"sh60{n * 3 + k:04d}", "code": f"60{n * 3 + k:04d}", "name": "x"} for k in range(self.member_count)]
            return httpx.Response(200, content=json.dumps(rows).encode())
        if host == "query.sse.com.cn":
            return httpx.Response(200, content=fixture("sse_list_trimmed.json"))
        if host == "www.szse.cn":
            return httpx.Response(200, content=fixture("szse_list_trimmed.xlsx"))
        if host == "qt.gtimg.cn":
            return httpx.Response(200, content=self.quote_body(q, parts))
        return httpx.Response(404, content=b"unknown")

    def eastmoney_page(self, page: int, size: int) -> bytes:
        codes = [f"{i:06d}" for i in range(1, self.em_sz + 1)] + [f"{600000 + i:06d}" for i in range(self.em_sh)]
        chunk = codes[(page - 1) * size: page * size]
        diff = [{"f12": c, "f14": f"n{c}", "f26": (1991 + int(c) % 30) * 10000 + 102} for c in chunk]
        return json.dumps({"rc": 0, "data": {"total": len(codes), "diff": diff} if chunk else None}).encode()

    def index_body(self) -> bytes:
        rows = json.loads(fixture("sina_index_kline_sh000001.json"))
        if self.index_close_bump:
            rows[-1]["close"] = f"{float(rows[-1]['close']) + self.index_close_bump:.3f}"
        return json.dumps(rows).encode()

    def quote_body(self, q, parts) -> bytes:
        symbols = parts.path.split("q=")[-1].split(",")
        lines = []
        for sym in symbols:
            spec = self.quotes.get(sym[2:])
            if spec is None:
                continue
            f = [""] * 50
            f[0], f[1], f[2], f[3], f[4], f[5], f[6] = "51", "name", sym[2:], spec["last"], spec["prev"], spec["open"], spec["lots"]
            f[30], f[33], f[34], f[35], f[36], f[37] = spec["time"], spec["high"], spec["low"], f"{spec['last']}/{spec['lots']}/1", spec["lots"], "1"
            f[38] = "1.0"
            lines.append(f'v_{sym}="{"~".join(str(x) for x in f)}";')
        return "\n".join(lines).encode("gbk")
