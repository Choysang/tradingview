"""Persistence split by access pattern.

User state (watchlist, backtest archive) is small and written concurrently by
the API and test runs, so it lives in SQLite: multi-process safe, single file,
no server. The historical feature snapshot is read-only and stays in DuckDB,
which handles the columnar file without a write lock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
from typing import Any

import duckdb

from ..config import settings

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path=None, history_path=None) -> None:
        self._lock = threading.RLock()
        self._path = path or settings.store_db
        self._history_path = history_path or settings.history_db
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                code TEXT NOT NULL,
                total_return REAL NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._db.commit()
        self._features: dict[str, dict[str, Any]] | None = None

    # --- watchlist -------------------------------------------------------
    def load_watchlist(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT code FROM watchlist ORDER BY position, added_at, code"
            ).fetchall()
        return [row[0] for row in rows]

    def save_watchlist(self, codes: list[str]) -> tuple[list[str], str]:
        unique = list(dict.fromkeys(code.strip() for code in codes if code.strip()))
        stamp = now_iso()
        with self._lock:
            self._db.execute("DELETE FROM watchlist")
            self._db.executemany(
                "INSERT INTO watchlist (code, added_at, position) VALUES (?, ?, ?)",
                [(code, stamp, index) for index, code in enumerate(unique)],
            )
            self._db.commit()
        return unique, stamp

    def watchlist_updated_at(self) -> str:
        with self._lock:
            row = self._db.execute("SELECT max(added_at) FROM watchlist").fetchone()
        return row[0] if row and row[0] else now_iso()

    # --- backtest archive ------------------------------------------------
    def save_run(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO backtest_runs "
                "(run_id, created_at, strategy_id, code, total_return, mode, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["run_id"],
                    payload["created_at"],
                    payload["request"]["strategy_id"],
                    payload["request"]["code"],
                    float(payload["total_return"]),
                    payload["provenance"]["mode"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._db.commit()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, created_at, strategy_id, code, total_return, mode "
                "FROM backtest_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        keys = ("run_id", "created_at", "strategy_id", "code", "total_return", "mode")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM backtest_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    # --- read-only historical features -----------------------------------
    def historical_features(self) -> dict[str, dict[str, Any]]:
        """Optional enrichment from a precomputed feature snapshot."""
        if self._features is not None:
            return self._features
        features: dict[str, dict[str, Any]] = {}
        if self._history_path.exists():
            try:
                reader = duckdb.connect(str(self._history_path), read_only=True)
                try:
                    rows = reader.execute(
                        "SELECT code, yesterday_limit_streak, prev_growth_streak, "
                        "prev_cum_return, zt_count_1y, seal_success_rate_1y, "
                        "max_streak_1y, high_60d FROM historical_features"
                    ).fetchall()
                finally:
                    reader.close()
            except duckdb.Error:
                rows = []
            for code, streak, growth, cum, zt, success, longest, high in rows:
                features[str(code)] = {
                    "prev_limit": int(streak or 0),
                    "prev_growth": int(growth or 0),
                    "prev_cum": float(cum or 0.0),
                    "zt_count_1y": int(zt or 0),
                    "seal_success_rate_1y": float(success or 0.0),
                    "max_streak_1y": int(longest or 0),
                    "high_60d": float(high or 0.0),
                }
        self._features = features
        return features

    def close(self) -> None:
        with self._lock:
            self._db.close()


storage = Storage()
