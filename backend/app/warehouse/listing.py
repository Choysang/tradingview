"""Listing dates (上市日期) from the exchanges' own stock lists, into the warehouse table `listings`.

Shanghai main board (`stock_info_sh_name_code`, 主板A股) and Shenzhen A shares (`stock_info_sz_name_code`, A股列表)
through AkShare: two requests in all. Rows: code, list_date (pl.Date). A code missing from both lists stays
absent, and the research panel then falls back to counting the warehouse's bars (see playbooks/panel.py).
Usage: python -m backend.app.warehouse.listing [--from-file listings.parquet] [--to-file out.parquet]
(the exchanges refuse the server's network, so the list can be fetched elsewhere with --to-file and stored on
the server, which stays the warehouse's only writer, with --from-file)
"""
from __future__ import annotations

import logging
import socket
import sys

import polars as pl

from .store import Warehouse, warehouse

LOG = logging.getLogger("pulse.warehouse.listing")
SCHEMA = {"code": pl.Utf8, "list_date": pl.Date}


def fetch() -> pl.DataFrame:
    import akshare as ak

    socket.setdefaulttimeout(30)  # AkShare's requests carry no timeout
    sh = ak.stock_info_sh_name_code(symbol="主板A股")
    sz = ak.stock_info_sz_name_code(symbol="A股列表")
    rows = pl.concat([
        pl.DataFrame({"code": sh["证券代码"].astype(str).tolist(), "list_date": sh["上市日期"].astype(str).tolist()}),
        pl.DataFrame({"code": sz["A股代码"].astype(str).tolist(), "list_date": sz["A股上市日期"].astype(str).tolist()}),
    ])
    return (rows.with_columns(pl.col("code").str.zfill(6), pl.col("list_date").str.slice(0, 10).str.to_date(strict=False))
            .drop_nulls().unique("code", keep="last").sort("code"))


def sync(store: Warehouse, frame: pl.DataFrame | None = None) -> int:
    frame = fetch() if frame is None else frame
    if frame.height < 1000:  # a partial answer would leave most stocks on the fallback; keep the old table
        raise RuntimeError(f"only {frame.height} listing dates fetched; not stored")
    store.replace("listings", frame.select(list(SCHEMA)))
    return frame.height


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="listing")
    ap.add_argument("--from-file")
    ap.add_argument("--to-file")
    a = ap.parse_args(argv)
    if a.to_file:
        frame = fetch()
        frame.write_parquet(a.to_file)
        LOG.info("listings: %d codes written to %s", frame.height, a.to_file)
        return 0
    given = pl.read_parquet(a.from_file).select(pl.col("code").cast(pl.Utf8), pl.col("list_date").cast(pl.Date)) if a.from_file else None
    n = sync(warehouse("live"), given)
    LOG.info("listings: %d codes", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
