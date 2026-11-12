"""Legacy warehouse reader: read-only historical import source (path-1 daily history), M1-E01/E02.

Inspection result (recorded in docs/data/M1_evidence.md, verified by spot checks
against Sina/Tencent on 2026-11-11):
* ``bars``: Float64 OHLC, **forward-adjusted with unknown, drifting factors** (000001
  2023-11-01 close 8.6156 vs raw 10.48; values of the same date change between
  ingests whenever a new dividend appears), ``volume`` in SHARES (equals Sina
  ``volume`` exactly, Tencent lots x 100), ``amount`` entirely NULL, ``ingested_at`` UTC.
  Hence: adjustment_mode=forward_adjusted, adjustment_version=legacy-qfq-unversioned,
  turnover missing, never presented as raw.
* ``index_bars``: index points + volume of unverified unit; kept as an index dataset.
* ``securities``: name (full-width blanks, ST prefixes), 10cm/20cm/30cm price-limit
  class (NOT the board), caps; ``listings``: list_date; ``boards``/``lhb_*``: kept as
  raw evidence for the sector/seat packages.

Invariants: files are only read; every row used keeps its source file name and
``ingested_at``; one input file = one raw object (bytes + sha256).
Not covered: writing to the legacy warehouse, deleting legacy data.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

SOURCE_ID = "legacy_warehouse"
SCHEMA_VERSION = "parquet-bars-code-date-ohlcv-amount-ingested_at-v1"
MAPPING_VERSION = "1.0.0"
ADJUSTMENT_VERSION = "legacy-qfq-unversioned"
DATASETS = ("bars", "index_bars", "securities", "listings", "boards", "lhb_seats", "lhb_stocks")
MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


def _op(source: str, target: str, op: str, **kw) -> dict:
    return {"source": source, "target": target, "op": op, **kw}


BARS_PROFILE = {
    "profile_id": "legacy_warehouse_bars_v1", "source_id": SOURCE_ID, "endpoint_id": "bars",
    "source_schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "record_kind": "bar",
    "adjustment_mode": "forward_adjusted",
    "source_semantics": {"prices": "forward adjusted, unknown factor series, float64",
                         "volume": "shares (float64, integral)", "amount": "always NULL in this warehouse",
                         "date": "session date"},
    "operations": [
        _op("code", "instrument_id", "resolve_identifier_at_trade_date"),
        _op("date", "trade_date", "parse_date_iso"),
        _op("open", "open_price", "decimal_from_float64", round_scale=12), _op("high", "high_price", "decimal_from_float64", round_scale=12),
        _op("low", "low_price", "decimal_from_float64", round_scale=12), _op("close", "close_price", "decimal_from_float64", round_scale=12),
        _op("volume", "volume_shares", "float64_require_integer", raw_unit="shares"),
        _op("amount", "turnover_amount_cny", "decimal_from_float64", raw_unit="CNY", round_scale=2),
    ],
    "recognized_unmapped": ["ingested_at", "source_file"],
}


@dataclass(frozen=True)
class LegacyWarehouse:
    root: Path

    def files(self, dataset: str) -> list[Path]:
        if dataset not in DATASETS:
            raise ValueError(f"unknown legacy dataset {dataset!r}")
        return sorted(Path(p) for p in glob.glob(str(self.root / dataset / "*.parquet")))

    def read_bars_versions(self, since: date, *, codes: list[str] | None = None, main_board_only: bool = True) -> pl.DataFrame:
        """All versions of every (code, date) >= ``since`` with the file each came from."""
        files = self.files("bars")
        if not files:
            raise FileNotFoundError(f"no legacy bars under {self.root}")
        lf = pl.scan_parquet([str(f) for f in files], include_file_paths="source_file").filter(pl.col("date") >= since)
        if codes:
            lf = lf.filter(pl.col("code").is_in(codes))
        elif main_board_only:
            lf = lf.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIXES))
        return lf.with_columns(pl.col("source_file").map_elements(lambda p: os.path.basename(p), return_dtype=pl.String)).collect()

    @staticmethod
    def distinct_versions(versions: pl.DataFrame) -> pl.DataFrame:
        """One row per distinct (code, date, OHLCV) version ordered by ingest time, with revision 1..k.

        ``revision`` counts distinct value sets per key (adjustment drift and volume precision
        refinements are revisions); identical re-ingests collapse to the earliest observation.
        """
        v = versions.sort(["code", "date", "ingested_at", "source_file"])
        v = v.unique(subset=["code", "date", "open", "high", "low", "close", "volume"], keep="first", maintain_order=True)
        v = v.sort(["code", "date", "ingested_at", "source_file"])
        return v.with_columns(pl.col("ingested_at").rank("ordinal").over(["code", "date"]).cast(pl.Int32).alias("revision"),
                              pl.len().over(["code", "date"]).alias("revision_count"))

    def read_securities(self) -> pl.DataFrame:
        files = self.files("securities")
        return pl.concat([pl.read_parquet(f).with_columns(pl.lit(f.name).alias("source_file")) for f in files])

    def read_listings(self) -> pl.DataFrame:
        files = self.files("listings")
        return pl.concat([pl.read_parquet(f) for f in files]).sort("ingested_at").unique("code", keep="last")

    def read_index_bars(self, since: date) -> pl.DataFrame:
        files = self.files("index_bars")
        lf = pl.scan_parquet([str(f) for f in files], include_file_paths="source_file").filter(pl.col("date") >= since)
        return lf.collect().sort(["symbol", "date", "ingested_at"]).unique(["symbol", "date"], keep="last")
