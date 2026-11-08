from datetime import datetime

import polars as pl

from backend.app.warehouse import minute_synth as M


def snaps(rows):
    return pl.DataFrame(rows, schema=["code", "kind", "quote_ts", "price", "volume_lots", "amount_yuan"], orient="row")


def test_bars_end_on_the_minute_and_take_volume_differences():
    at = lambda h, m, s: datetime(2026, 11, 2, h, m, s)  # noqa: E731
    bars = M.synth(snaps([
        ("600000", "auction", at(9, 25, 3), 10.0, 100.0, 100000.0),  # auction: out of the session
        ("600000", "full", at(9, 30, 2), 10.0, 200.0, 200000.0),
        ("600000", "hot", at(9, 30, 32), 10.2, 300.0, 302000.0),
        ("600000", "full", at(9, 31, 2), 10.1, 400.0, 403000.0),  # 2 s after 09:31 still closes bar 0931
        ("600000", "full", at(9, 31, 2), 10.1, 400.0, 403000.0),  # the same quote twice counts once
        ("600000", "full", at(9, 33, 2), 10.3, 600.0, 609000.0),  # no snapshot for 0932: it rolls into 0933
        ("600000", "hot", at(11, 30, 52), 10.3, 700.0, 712000.0),  # after the morning's last bar
    ]))
    assert bars["minute"].to_list() == ["0930", "0931", "0933"]
    first, second, third = bars.rows(named=True)
    assert (first["lots"], first["n_snaps"]) == (200.0, 1)
    assert (second["open"], second["high"], second["low"], second["price"]) == (10.2, 10.2, 10.1, 10.1)
    assert (second["lots"], second["amount_yuan"], second["n_snaps"]) == (200.0, 203000.0, 2)
    assert third["lots"] == 200.0 and abs(third["vwap"] - 609000.0 / 60000.0) < 1e-12


def test_merge_keeps_fetched_bars_per_code():
    fetched = pl.DataFrame({"code": ["600000"], "minute": ["0930"], "price": [10.0]})
    built = pl.DataFrame({"code": ["600000", "000001"], "minute": ["0930", "0930"], "price": [9.9, 5.0],
                          "source": ["snapshot", "snapshot"]})
    out = M.merge(fetched, built)
    assert out.select("code", "price", "source").rows() == [("000001", 5.0, "snapshot"), ("600000", 10.0, "tencent")]
