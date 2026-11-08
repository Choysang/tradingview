"""Historical warehouse: an append-only Parquet lake per dataset.

Why Parquet files instead of one database file: research reads (backtests,
regimes, factor studies) and the sync job's writes happen at the same time, in
different threads and sometimes different processes. Immutable files written
atomically never lock a reader out; a reader sees either the old file set or
the new one. Duplicates from re-syncs are resolved on read (newest ingest wins)
and folded away by `compact()`. Files a compaction replaces are *retired*, not
deleted: new readers stop seeing them at once, while a reader that listed them
a moment earlier can still open them; they are removed after RETIRE_AFTER.

Layout: `<data_dir>/warehouse/<dataset>/<table>/<batch>.parquet` plus
`<dataset>/docs/<kind>/<name>.json` for small JSON documents (daily reports).
Datasets: `live` (real provider history) and `sim` (deterministic synthetic
history that keeps every research path working offline, always labelled SIM).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable
from uuid import uuid4

import polars as pl

from contracts.schema import WarehouseTable
from ..config import settings

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "bars": ("code", "date"),
    "index_bars": ("symbol", "date"),
    "securities": ("code",),
    "lhb_stocks": ("code", "date", "reason"),
    # `rank` orders a stock's seats within one reason and side: several institutions
    # all appear as the same "机构专用" seat and would otherwise collapse into one row.
    "lhb_seats": ("code", "date", "side", "reason", "seat", "rank"),
    "boards": ("code", "kind", "board_id"),
    "listings": ("code",),
}
INGESTED = "ingested_at"
RETIRED = ".retired.json"
RETIRE_AFTER = 600.0  # seconds a replaced file stays on disk for in-flight readers


class Warehouse:
    def __init__(self, root: Path, dataset: str) -> None:
        self.root = root
        self.dataset = dataset
        self._lock = threading.RLock()

    # --- files -------------------------------------------------------------
    def table_dir(self, table: str) -> Path:
        return self.root / table

    def files(self, table: str) -> list[Path]:
        folder = self.table_dir(table)
        if not folder.exists():
            return []
        retired = self._retired(table)
        return sorted(path for path in folder.glob("*.parquet") if path.name not in retired)

    def _retired(self, table: str) -> dict[str, float]:
        path = self.table_dir(table) / RETIRED
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def replace(self, table: str, frame: pl.DataFrame) -> None:
        """Swap a table's content for `frame`: write it, retire the old files."""
        with self._lock:
            old = self.files(table)
            self.write(table, frame)
            self._retire(table, old)

    def _retire(self, table: str, paths: list[Path]) -> None:
        now = time.time()
        retired = self._retired(table)
        retired.update({path.name: now for path in paths})
        for name, at in list(retired.items()):
            target = self.table_dir(table) / name
            if now - at < RETIRE_AFTER and target.exists():
                continue
            try:
                target.unlink(missing_ok=True)
                del retired[name]
            except OSError:
                continue  # still open on Windows; try again next time
        folder = self.table_dir(table)
        temporary = folder / f".retired.{uuid4().hex[:6]}.tmp"
        temporary.write_text(json.dumps(retired), encoding="utf-8")
        os.replace(temporary, folder / RETIRED)

    def write(self, table: str, frame: pl.DataFrame) -> Path | None:
        """Append one immutable batch. Rows carry their ingest time so a later
        batch overrides an earlier one for the same key."""
        if frame.is_empty():
            return None
        if INGESTED not in frame.columns:
            frame = frame.with_columns(pl.lit(datetime.now(timezone.utc)).alias(INGESTED))
        folder = self.table_dir(table)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{uuid4().hex[:8]}.parquet"
        temporary = folder / f".{name}.tmp"
        frame.write_parquet(temporary, compression="zstd")
        target = folder / name
        os.replace(temporary, target)
        return target

    def replace_codes(self, table: str, frame: pl.DataFrame, codes: Iterable[str], key: str = "code") -> None:
        """Make `frame` the only rows of `codes`, whatever dates the old rows had.

        Append plus key dedupe cannot drop an old row whose date the new rows do not
        cover. The new rows are written first, then every file holding one of the
        codes is rewritten without them and retired in one step, so a crash leaves
        old and new rows side by side (the next run repeats the call), never neither."""
        wanted = sorted(set(codes))
        with self._lock:
            old = self.files(table)
            self.write(table, frame)
            touched: list[Path] = []
            for path in old:
                if not wanted or not pl.read_parquet(path, columns=[key])[key].is_in(wanted).any():
                    continue
                self.write(table, pl.read_parquet(path).filter(~pl.col(key).is_in(wanted)))
                touched.append(path)
            if touched:
                self._retire(table, touched)

    def scan(self, table: str) -> pl.LazyFrame | None:
        """Deduplicated view of a table, or None when it has no data yet."""
        files = self.files(table)
        if not files:
            return None
        lazy = pl.scan_parquet([str(path) for path in files])
        keys = TABLE_KEYS.get(table)
        if not keys:
            return lazy
        return lazy.sort(INGESTED).unique(subset=list(keys), keep="last").sort(list(keys))

    def read(self, table: str) -> pl.DataFrame | None:
        lazy = self.scan(table)
        return lazy.collect() if lazy is not None else None

    def compact(self, table: str) -> None:
        """Rewrite a table as one deduplicated file; the old batches are retired."""
        with self._lock:
            if len(self.files(table)) <= 1:
                return
            frame = self.read(table)
            if frame is not None:
                self.replace(table, frame)

    # --- documents --------------------------------------------------------
    def doc_dir(self, kind: str) -> Path:
        return self.root / "docs" / kind

    def write_doc(self, kind: str, name: str, payload: dict[str, Any]) -> Path:
        folder = self.doc_dir(kind)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{name}.json"
        temporary = folder / f".{name}.{uuid4().hex[:6]}.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def read_doc(self, kind: str, name: str) -> dict[str, Any] | None:
        path = self.doc_dir(kind) / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_docs(self, kind: str) -> list[str]:
        folder = self.doc_dir(kind)
        return sorted(path.stem for path in folder.glob("*.json")) if folder.exists() else []

    # --- introspection ----------------------------------------------------
    def version(self) -> str:
        """Changes whenever any table file changes; use it as a cache key."""
        digest = hashlib.sha1(self.dataset.encode())
        for table in TABLE_KEYS:
            for path in self.files(table):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # retired and removed between listing and stat
                digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()[:16]

    def has_bars(self) -> bool:
        return bool(self.files("bars"))

    def coverage(self) -> tuple[int, str | None, str | None]:
        """(codes, first date, last date) of the bars table."""
        lazy = self.scan("bars")
        if lazy is None:
            return 0, None, None
        row = lazy.select(
            pl.col("code").n_unique().alias("codes"),
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
        ).collect().row(0, named=True)
        return int(row["codes"]), _iso(row["start"]), _iso(row["end"])

    def tables(self) -> list[WarehouseTable]:
        out: list[WarehouseTable] = []
        for table in TABLE_KEYS:
            files = self.files(table)
            if not files:
                continue
            lazy = self.scan(table)
            columns = lazy.collect_schema().names() if lazy is not None else []
            stats = [pl.len().alias("rows")]
            key = "code" if "code" in columns else "symbol" if "symbol" in columns else None
            if key:
                stats.append(pl.col(key).n_unique().alias("codes"))
            if "date" in columns:
                stats += [pl.col("date").min().alias("start"), pl.col("date").max().alias("end")]
            row = lazy.select(stats).collect().row(0, named=True) if lazy is not None else {"rows": 0}
            out.append(
                WarehouseTable(
                    name=table,
                    rows=int(row["rows"]),
                    codes=int(row["codes"]) if "codes" in row else None,
                    start=_iso(row.get("start")),
                    end=_iso(row.get("end")),
                    files=len(files),
                    bytes=sum(path.stat().st_size for path in files if path.exists()),
                )
            )
        return out


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


# --- dataset selection --------------------------------------------------------

MIN_LIVE_CODES = 50
_INSTANCES: dict[str, Warehouse] = {}
_GUARD = threading.Lock()


def warehouse(dataset: str) -> Warehouse:
    if dataset not in ("live", "sim"):
        raise ValueError("dataset must be live or sim")
    with _GUARD:
        root = settings.data_dir / "warehouse" / dataset
        instance = _INSTANCES.get(dataset)
        # Tests point PULSE_DATA_DIR elsewhere; never reuse a handle on another root.
        if instance is None or instance.root != root:
            instance = Warehouse(root, dataset)
            _INSTANCES[dataset] = instance
        return instance


def live_ready(live: Warehouse) -> bool:
    """A whole-market backfill has finished at least once. A backfill stopped
    halfway (rate limit, network) holds whichever codes came first, a biased
    sample that research must not treat as the market."""
    return (
        live.has_bars()
        and live.coverage()[0] >= MIN_LIVE_CODES
        and live.read_doc("meta", "backfill_window") is not None
    )


def active() -> Warehouse:
    """Real history once a full backfill has finished, otherwise synthetic.

    Research must prefer real bars even while the intraday snapshot runs in SIM
    (evenings, weekends); the returned dataset name tells callers which one.
    """
    live = warehouse("live")
    if live_ready(live):
        return live
    from .synthetic import ensure_synthetic

    sim = warehouse("sim")
    ensure_synthetic(sim)
    return sim
