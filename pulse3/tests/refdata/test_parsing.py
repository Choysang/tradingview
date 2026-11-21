"""Reference-data adapters: parsing, units, the ratio adjustment, block detection (offline, recorded fixtures only)."""
from __future__ import annotations

import io
import zipfile
from datetime import date

import httpx
import pytest

from pulse.adapters.sources import ref_listing as L
from pulse.adapters.sources import ref_market as M
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked
from pulse.domain.evidence.announcements import NotData, SchemaChanged

from .support import fixture

# Values of the legacy warehouse (server snapshot of 2026-11-18, ``bars`` of 000001, latest ingest per date): the native ratio
# adjustment of the recorded Sina kline + qfq factor file reproduces them bit for bit.
LEGACY_000001 = {
    date(2025, 3, 27): (10.229429263780673, 10.265416701823877, 10.211435544759071, 10.247422982802275, 55334940.0),
    date(2025, 9, 10): (10.89514361318849, 10.941545417333243, 10.867302530701638, 10.922984695675343, 79266839.0),
    date(2026, 1, 28): (10.36411766554795, 10.411485662191222, 10.250434473604097, 10.269381672261405, 143876603.0),
    date(2026, 6, 8): (10.392538463533914, 10.51569525480642, 10.38306486420526, 10.449380059505838, 109092646.0),
}


def test_kline_parse_units_and_order():
    rows = M.parse_kline(fixture("sina_adj_kline_sz000001.json"))
    assert len(rows) == 400 and rows[0]["date"] == date(2025, 3, 27) and rows[-1]["date"] == date(2026, 11, 18)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert rows[0] == {"date": date(2025, 3, 27), "open": 11.37, "high": 11.41, "low": 11.35, "close": 11.39, "volume": 55334940.0,   # shares, raw prices
                       "text": {"open": "11.370", "high": "11.410", "low": "11.350", "close": "11.390", "volume": "55334940"}}      # exact vendor strings


def test_factor_file_is_sorted_ascending_and_ends_at_one():
    f = M.parse_factors(fixture("sina_adj_factor_sz000001.js"))
    assert [d for d, _ in f] == sorted(d for d, _ in f) and f[-1] == (date(2026, 9, 24), 1.0) and len(f) == 33
    assert M.parse_factors(b"var x=") == [] and M.parse_factors(b"") == []          # no object -> no factors (not an error)


def test_ratio_adjustment_reproduces_the_legacy_warehouse_bit_for_bit():
    adj = {r["date"]: r for r in M.adjust_rows(M.parse_kline(fixture("sina_adj_kline_sz000001.json")),
                                               M.parse_factors(fixture("sina_adj_factor_sz000001.js")))}
    for day, (o, h, lo, c, v) in LEGACY_000001.items():
        r = adj[day]
        assert (r["open"], r["high"], r["low"], r["close"], r["volume"]) == (o, h, lo, c, v), day
    last = adj[date(2026, 11, 18)]
    assert last["close"] == 12.2                                   # the latest factor is 1.0: the newest bar equals the raw one


def test_adjustment_is_a_ratio_not_a_subtraction():
    rows = M.parse_kline(fixture("sina_adj_kline_sz000001.json"))
    adj = M.adjust_rows(rows, M.parse_factors(fixture("sina_adj_factor_sz000001.js")))
    # inside one factor segment every daily return equals the raw one (Tencent's subtractive qfq would not keep this)
    seg = [(r, a) for r, a in zip(rows, adj) if date(2025, 10, 15) <= r["date"] < date(2026, 6, 12)]
    for (r0, a0), (r1, a1) in zip(seg, seg[1:]):
        assert a1["close"] / a0["close"] == pytest.approx(r1["close"] / r0["close"], rel=1e-12)
    jan = next(a for r, a in zip(rows, adj) if r["date"] == date(2026, 1, 28))
    raw_jan = next(r for r in rows if r["date"] == date(2026, 1, 28))
    assert jan["close"] == raw_jan["close"] / 1.0555650131575                       # factor in force from 2025-10-15


def test_adjust_without_factors_is_identity_and_before_first_factor_uses_the_earliest():
    rows = [{"date": date(2024, 1, 2), "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1.0}]
    assert M.adjust_rows(rows, [])[0]["close"] == 10.5
    out = M.adjust_rows(rows, [(date(2025, 1, 1), 2.0), (date(2026, 1, 1), 1.0)])
    assert out[0]["close"] == 5.25


def test_kline_rejects_bad_shapes_and_drops_unusable_rows():
    with pytest.raises(NotData):
        M.parse_kline(b"<html>captcha</html>")
    with pytest.raises(SchemaChanged):
        M.parse_kline(b'[{"day":"2026-01-02","open":"1"}]')
    assert M.parse_kline(b"null") == [] and M.parse_kline(b"") == []
    rows = M.parse_kline(b'[{"day":"2026-01-02","open":"0","high":"1","low":"1","close":"1","volume":"5"},'
                         b'{"day":"2026-01-05","open":"1","high":"1","low":"1","close":"1","volume":"5"}]')
    assert [r["date"] for r in rows] == [date(2026, 1, 5)]          # a zero price is dropped, never repaired


def test_index_units_sina_points_and_tencent_lots():
    sina = M.parse_kline(fixture("sina_index_kline_sh000001.json"))
    assert sina[-1]["close"] == 3587.832 and sina[-1]["volume"] == 42393483200.0
    tc = M.parse_tencent_index(fixture("tencent_index_kline_sh000985.json"), "sh000985")
    assert tc[0]["date"] == date(2026, 10, 8) and tc[0]["close"] == 5560.01
    assert tc[0]["volume"] == 974518436.0 * 100                    # lots x 100 = shares, exactly the legacy 000985 rows (9.7452e10)
    with pytest.raises(SchemaChanged):
        M.parse_tencent_index(fixture("tencent_index_kline_sh000985.json"), "sh999999")
    with pytest.raises(NotData):
        M.parse_tencent_index(b"<html>", "sh000985")


def test_board_list_and_member_pages():
    boards = M.parse_nodes(fixture("sina_board_nodes.json"))
    assert sum(b.kind == "industry" for b in boards) == 48 and sum(b.kind == "concept" for b in boards) == 213
    assert len({b.board_id for b in boards}) == len(boards)
    assert any(b.board_id == "new_blhy" and b.kind == "industry" for b in boards) and all(b.name for b in boards)
    codes, n = M.parse_members(fixture("sina_board_members_new_blhy_p1.json"))
    assert n == 19 and len(codes) == 19 and all(len(c) == 6 and c.isdigit() for c in codes) and n < M.PAGE_SIZE   # a short page ends the board
    dup = M.parse_members(b'[{"code":"600001"},{"code":"600001"},{"symbol":"sz000002"},{"code":"123456"},{"code":"x"}]')
    assert dup == (["600001", "000002"], 5)                         # repeats dropped, symbol fallback, bond-like and junk codes filtered
    with pytest.raises(SchemaChanged):
        M.parse_nodes(b'["x", []]')
    with pytest.raises(NotData):
        M.parse_members(b"<html>")


def test_listing_sse_json_and_szse_xlsx():
    sse = L.parse_sse(fixture("sse_list_trimmed.json"))
    assert len(sse) == 25 and sse[0] == {"code": "600000", "list_date": date(1999, 11, 10), "name": "浦发银行"}
    szse = L.parse_szse(fixture("szse_list_trimmed.xlsx"))
    assert len(szse) == 25 and szse[0] == {"code": "000001", "list_date": date(1991, 4, 3), "name": "平安银行"}
    assert L.parse_date("19901219") == date(1990, 12, 19) and L.parse_date("2000-01-04 00:00:00") == date(2000, 1, 4)
    assert L.parse_date("36526") == date(2000, 1, 1) and L.parse_date("-") is None and L.parse_date("") is None   # unreadable -> NULL, never today
    with pytest.raises(NotData):
        L.parse_szse(b"not a zip")
    with pytest.raises(SchemaChanged):
        L.parse_sse(b'{"result": [{"A_STOCK_CODE": "600000"}]}')


def test_szse_header_drift_is_detected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">'
                   '<c r="A1" t="inlineStr"><is><t>代码</t></is></c></row></sheetData></worksheet>')
    with pytest.raises(SchemaChanged):
        L.parse_szse(buf.getvalue())


@pytest.mark.parametrize("status,body", [(302, b""), (200, b"<html>verify</html>")])
def test_html_or_redirect_for_a_data_call_is_a_block(status, body):
    t = {"n": 0.0}
    client = RateLimitedClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body, headers={"location": "https://x"})),
                               clock=lambda: t["n"], sleep=lambda s: None)
    with pytest.raises(SourceBlocked):
        M.SinaRefAdapter(client).kline("sz000001", 30)
    assert client.circuit_open
    with pytest.raises(SourceBlocked, match="circuit_open"):         # no further request leaves
        M.SinaRefAdapter(client).factors("sz000001")
    assert client.requests_made == 1


def test_datalen_is_capped_and_requests_name_the_old_hosts():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, content=b"[]")

    client = RateLimitedClient(transport=httpx.MockTransport(handler), clock=lambda: 0.0, sleep=lambda s: None)
    M.SinaRefAdapter(client).kline("sz000001", 99999)
    assert seen[0].startswith("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData") and "datalen=1023" in seen[0]
    assert M.bars_needed(date(2022, 1, 1), date(2026, 11, 19)) == 1023 and M.bars_needed(date(2026, 11, 1), date(2026, 11, 19)) == 33
