"""Limit pools, pure part: parser on the old recorder's real bodies and the 2026-11-20 captures, frame schema, schedule, comparison arithmetic."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl
import pytest

from pulse.adapters.sources import eastmoney_pools as EM
from pulse.application import intraday_pools as P
from pulse.application import intraday_rows as R
from pulse.domain.evidence.announcements import NotData, SchemaChanged

from . import support as T

UTC = timezone.utc
FETCH = datetime(2026, 11, 2, 7, 25, 3, tzinfo=UTC)


def body(name: str) -> bytes:
    return (T.POOL_FIXTURES / name).read_bytes()


def test_parser_on_the_old_recorders_bodies():
    qd, tc, zt = EM.parse_pool(body("old_zt_20261102.json"))
    assert (qd, tc, len(zt)) == ("20261102", 125, 125) and zt[0]["c"] == "000525" and zt[0]["fbt"] == 92500
    assert EM.parse_pool(body("old_zb_20261102.json"))[1:2] == (28,) and EM.parse_pool(body("old_dt_20261102.json"))[1] == 7


def test_live_captures_of_2026_11_20_have_the_same_shape():
    for key, tc in (("zt", 28), ("zb", 7), ("dt", 58)):
        qd, total, rows = EM.parse_pool(body(f"live_{key}_20261120.json"))
        assert qd == "20261120" and total == tc == len(rows)


def test_frame_units_and_schema():
    qd, _, zt = EM.parse_pool(body("old_zt_20261102.json"))
    df = P.pool_frame(zt, key="zt", day=date(2026, 11, 2), fetched_at=FETCH)
    assert df.schema == pl.Schema(P.POOL_SCHEMA) and df.height == 125
    r = df.row(0, named=True)
    assert r["code"] == "000525" and r["name"] == "红太阳" and r["price"] == Decimal("4.940") and r["fbt"] == 92500 and r["lbc"] == 2        # p / 1000
    assert r["fetched_at"] == FETCH and r["limit_up_price"] is None and r["hybk"] == "农化制品" and r["zttj"] == '{"ct": 2, "days": 2}'
    zb = P.pool_frame(EM.parse_pool(body("old_zb_20261102.json"))[2], key="zb", day=date(2026, 11, 2), fetched_at=FETCH).row(0, named=True)
    assert zb["limit_up_price"] == Decimal("26.150") and zb["price"] == Decimal("25.270") and zb["zbc"] == 2
    dt = P.pool_frame(EM.parse_pool(body("old_dt_20261102.json"))[2], key="dt", day=date(2026, 11, 2), fetched_at=FETCH).row(0, named=True)
    assert dt["fba"] == 120578104 and dt["oc"] == 2 and dt["pe"] == pytest.approx(-6.252046)


def test_html_or_odd_bodies_are_not_data():
    with pytest.raises(NotData):
        EM.parse_pool(b"<html>captcha</html>")
    with pytest.raises(NotData):
        EM.parse_pool(b'{"rc":1}')
    with pytest.raises(SchemaChanged):
        EM.parse_pool(b'{"data":{"tc":0,"pool":{}}}')
    assert EM.parse_pool(b'{"data":{"tc":0,"qdate":20261120,"pool":null}}') == ("20261120", 0, [])        # an empty pool is null


def test_url_matches_the_old_recorders_request():
    seen = []

    class C:
        def request(self, **kw):
            seen.append(kw)

    EM.EastmoneyPoolAdapter(C()).pool("dt", date(2026, 11, 20))
    assert seen[0]["url"] == ("https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0"
                              "&pagesize=10000&sort=fund:asc&date=20261120")


def test_the_pools_step_runs_once_inside_its_window():
    c = R.Clock()
    at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)            # noqa: E731
    assert not R.pools_due(at(15, 24, 59), c) and R.pools_due(at(15, 25), c) and R.pools_due(at(15, 50), c) and not R.pools_due(at(15, 50, 1), c)
    c.done.add("pools")
    assert not R.pools_due(at(15, 30), c)
    assert "pools" not in R.due(at(15, 30, 6), R.Clock())             # the shared schedule never returns it: only an enabled recorder adds it


# --- comparison arithmetic ---------------------------------------------------------------------------------------------------------------------

def facts_frame(rows):
    sch = {"code": pl.String, "sealed_at_close": pl.Boolean, "touched_limit": pl.Boolean, "broken_count": pl.Int64, "first_seal_at": pl.Datetime("ms", "UTC")}
    return pl.DataFrame(rows, schema=sch, orient="row")


def pools_frame(zt, zb=(), dt=()):
    frames = []
    for key, rows in (("zt", zt), ("zb", zb), ("dt", dt)):
        frames.append(P.pool_frame(list(rows), key=key, day=date(2026, 11, 13), fetched_at=FETCH))
    return pl.concat(frames)


def cst(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=T.CST).astimezone(UTC)


def test_comparison_counts_sets_gaps_and_breaks():
    pools = pools_frame(zt=[{"c": "000001", "fbt": 93000, "zbc": 1}, {"c": "000002", "fbt": 100000, "zbc": 0}, {"c": "000003", "fbt": 101500, "zbc": 2}],
                        zb=[{"c": "000004", "zbc": 3}, {"c": "000005", "zbc": 1}], dt=[{"c": "000009"}])
    facts = facts_frame([("000001", True, True, 1, cst(9, 30, 40)), ("000002", True, True, 0, cst(10, 0, 20)), ("000006", True, True, 0, cst(11, 0)),
                         ("000004", False, True, 2, cst(9, 40)), ("000007", False, False, 0, None)])
    r = P.compare_frames(pools, facts)
    s = r["sealed_at_close"]
    assert (s["vendor_pool"], s["seal_facts"], s["both"], s["only_vendor"], s["only_seal_facts"]) == (3, 3, 2, ["000003"], ["000006"])
    assert r["first_seal_gap_seconds"] == {"n": 2, "median": 30.0, "max": 40, "min": 20, "within_60s": 2}        # ours minus the vendor's fbt
    b = r["breaks"]
    assert (b["seal_facts_total"], b["seal_facts_names"], b["vendor_zt_pool_zbc"], b["vendor_zb_pool_zbc"], b["vendor_total_zbc"]) == (3, 2, 3, 4, 7)
    assert b["both_sealed_names"] == {"seal_facts": 1, "vendor_zbc": 1} and b["vendor_zb_pool_names"] == 2
    c = r["broken_at_close"]
    assert (c["vendor_zb_pool"], c["seal_facts_touched_not_sealed"], c["both"], c["only_vendor"]) == (2, 1, 1, ["000005"]) and r["limit_down_pool_names"] == 1
    assert "sealed at close: vendor pool 3 | seal facts 3 | both 2" in P.render({"session_date": "2026-11-13", **r})


def test_the_latest_fetch_of_a_pool_is_the_one_compared():
    old = P.pool_frame([{"c": "000001"}], key="zt", day=date(2026, 11, 13), fetched_at=FETCH)
    new = P.pool_frame([{"c": "000001"}, {"c": "000002"}], key="zt", day=date(2026, 11, 13), fetched_at=FETCH.replace(minute=40))
    r = P.compare_frames(pl.concat([old, new]), facts_frame([("000001", True, True, 0, cst(9, 30))]))
    assert r["sealed_at_close"]["vendor_pool"] == 2
