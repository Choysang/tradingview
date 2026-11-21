"""Read side of the fusion lane: status, top scores of a day, the two books and the three forward experiments.

Why: the API (``api/routes_bf_fusion``) and the web page only translate these functions into the ``backfill_fusion`` contract; the
numbers come from the append-only datasets written by ``bf_fusion_service.FusionService.daily``.
Invariants: a read never computes; an empty dataset gives empty lists and null dates (never zeros); ratios and NAVs leave as
``Decimal`` (the contract writes them as strings); the frozen-data manifest is shipped with the code and compared with the files.
Not covered: writing (the worker CLI), the registry rows of the experiments.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Any

import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres.instrument_repo import CODE_SOURCES_SQL
from pulse.application.bf_fusion_service import (ASOF_SCHEMA, BOOK_FILE, CURRENT_SCHEMA, DS_ASOF, DS_CURRENT, DS_LOG, IMPLEMENTATION,
                                                 LOG_SCHEMA, SCORE_NAME, Store)
from pulse.application.data_service import DataService
from pulse.domain.fusion import forward as FW
from pulse.domain.fusion import scorer as SC

PREREG = {
    "main": ("E036/E040", "docs/research/2026-11-01-round7-plan.md"),
    "h10": ("E065", "docs/research/2026-11-07-batch1-preregistration.md"),
    "monitor": ("E068", "docs/research/2026-11-07-batch2-preregistration.md"),
    "x20": ("E085", "docs/research/2026-11-08-batch4-preregistration.md"),
    "os20": ("E081", "docs/research/2026-11-08-os20-shock-rebound-preregistration.md"),
}
LABELS = {"main": "Frozen main book: hold 5 days, 20 slots, top 40, curve gate 20 d / 0.25",
          "h10": "Shadow book: hold 10 days, same score, the gate keeps 1 of 2 new names",
          "monitor": "Forward monitor: rank IC, decile spread and matched excess of the frozen score",
          "x20": "X20 basket: drop the lowest 20% of scores, rebalance every 10 sessions, against the equal-weight pool",
          "os20": "OS20: 20-day shock-type oversold rebound, 10-day matched excess"}


def dec(x: Any, places: int = 6) -> Decimal | None:
    """A float as a decimal; NaN, inf and None stay missing."""
    if x is None:
        return None
    x = float(x)
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return Decimal(repr(round(x, places)))


def _flat(d: dict) -> dict:
    """A summary with only decimals, integers, booleans or null as values (nested dicts get dotted keys)."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in _flat(v).items():
                out[f"{k}.{k2}"] = v2
        elif isinstance(v, bool) or v is None or isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = dec(v, 4)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def _manifest() -> dict:
    return json.loads((SC.DATA / "manifest.json").read_text(encoding="utf-8"))


def _store(conn: psycopg.Connection, lake: LakeStore) -> Store:
    return Store(DataService(conn, lake))


def _names(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT a.source_symbol AS code, s.display_name FROM core.instrument_alias a JOIN LATERAL ("
        "SELECT display_name FROM core.instrument_state st WHERE st.instrument_id=a.instrument_id "
        "ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true WHERE a.source_id " + CODE_SOURCES_SQL).fetchall()
    return {r["code"]: r["display_name"] for r in rows}


def read_status(conn: psycopg.Connection, lake: LakeStore) -> dict:
    st = _store(conn, lake)
    log, cur, asof = st.read(DS_LOG, LOG_SCHEMA), st.read(DS_CURRENT, CURRENT_SCHEMA), st.read(DS_ASOF, ASOF_SCHEMA)
    runs = st.log_rows("run", frame=log, by_date=False)
    last = None
    if runs:
        meta = log.filter(pl.col("log") == "run").tail(1).to_dicts()[0]
        r = runs[-1]
        last = {"run_id": meta["run_id"], "written_on": meta["written_on"], "status": r.get("status", "done"), "code": r.get("code"),
                "detail": (r.get("detail") or None), "session": r.get("session"), "seconds": dec(r.get("seconds"), 1),
                "peak_memory_mb": dec(r.get("peak_memory_mb"), 1)}

    def span(df: pl.DataFrame) -> dict:
        return {"rows": df.height, "dates": df["date"].n_unique() if df.height else 0,
                "first_date": df["date"].min() if df.height else None, "last_date": df["date"].max() if df.height else None}

    manifest = _manifest()
    books = []
    for key in BOOK_FILE:
        rows = st.log_rows(key, frame=log)
        books.append({"key": key, "last_date": rows[-1]["date"] if rows else None, "rows": len(rows)})
    return {"score_name": SCORE_NAME, "implementation": IMPLEMENTATION, "current": span(cur), "asof": span(asof), "last_run": last,
            "books": books, "frozen_data": [{"file": k, "sha256": v} for k, v in sorted(manifest["files"].items())],
            "preregistrations": [{"key": k, "ledger_id": v[0], "document": v[1], "sha256": manifest["preregistrations"].get(k)}
                                 for k, v in PREREG.items()]}


def read_scores(conn: psycopg.Connection, lake: LakeStore, day: date | None, limit: int = 40) -> dict:
    st = _store(conn, lake)
    cur, asof = st.read(DS_CURRENT, CURRENT_SCHEMA), st.read(DS_ASOF, ASOF_SCHEMA)
    dates = sorted(set(cur["date"].unique().to_list()) | set(asof["date"].unique().to_list()))
    rule = "static40, level-safe, main board"
    if not dates:
        return {"date": None, "kind": None, "available_dates": [], "rows": [], "rule": rule}
    d = day or dates[-1]
    archived = d in set(asof["date"].unique().to_list())
    src = asof if archived else cur
    g = src.filter(pl.col("date") == d).sort(["score", "code"], descending=[True, False]).head(limit)
    names = _names(conn)
    return {"date": d, "kind": ("asof" if archived else "current") if g.height else None, "available_dates": dates[-120:], "rule": rule,
            "rows": [{"rank": i + 1, "code": c, "display_name": names.get(c), "score": dec(s, 6)}
                     for i, (c, s) in enumerate(zip(g["code"].to_list(), g["score"].to_list()))]}


def read_books(conn: psycopg.Connection, lake: LakeStore) -> dict:
    st = _store(conn, lake)
    log = st.read(DS_LOG, LOG_SCHEMA)
    out = []
    for key, rule in BOOK_FILE.items():
        rows = st.log_rows(key, frame=log)
        out.append({"key": key, "label": LABELS[key], "rule": rule.gate_label, "ledger_id": PREREG[key][0], "rows": [
            {"date": r["date"], "gate_w": dec(r["gate_w"], 4), "new_buys_allowed": r["new_buys_allowed"], "try_buy": r["try_buy"],
             "offer_top10": r["offer_top10"], "holdings": r["holdings"], "nav": dec(r["nav"], 4), "ungated_nav": dec(r["ungated_nav"], 4),
             "nav_since_freeze_pct": dec(r.get("nav_since_freeze_pct"), 3), "scores_sha256_16": r["scores_sha256_16"]} for r in rows]})
    return {"books": out}


def read_experiments(conn: psycopg.Connection, lake: LakeStore) -> dict:
    st = _store(conn, lake)
    log = st.read(DS_LOG, LOG_SCHEMA)
    mon = sorted(FW.latest_by_date(st.log_rows("monitor", frame=log)).values(), key=lambda r: r["date"])
    os20 = sorted(FW.latest_by_date(st.log_rows("os20", frame=log)).values(), key=lambda r: r["date"])
    x20 = st.log_rows("x20", frame=log)
    manifest = _manifest()

    def head(key: str) -> dict:
        return {"key": key, "label": LABELS[key], "ledger_id": PREREG[key][0], "document": PREREG[key][1],
                "document_sha256": manifest["preregistrations"].get(key)}

    return {"experiments": [
        {**head("monitor"), "summary": _flat(FW.monitor_summary(mon)) if mon else {}, "rows": [
            {"date": r["date"], "n": r["n"], "ic": dec(r["ic"], 4), "decile_spread": dec(r["decile_spread"], 3),
             "top_ex": dec(r["top_ex"], 3), "top_sv": dec(r["top_sv"], 3), "flagged": bool(r["flagged"]),
             "coverage_fail_days": r["coverage_fail_days"]} for r in mon]},
        {**head("x20"), "summary": {}, "rows": [
            {"date": r["date"], "x20": dec(r["x20"]), "ew": dec(r["ew"]), "excess_pct": dec(r["excess_pct"], 3), "n_x20": r["n_x20"],
             "n_ew": r["n_ew"], "trade_day": bool(r.get("trade_day")), "turnover_x20": dec(r.get("turnover_x20"), 4),
             "turnover_ew": dec(r.get("turnover_ew"), 4)} for r in x20]},
        {**head("os20"), "summary": _flat(FW.os20_summary([r for r in os20 if not r["flagged"]])) if os20 else {}, "rows": [
            {"date": r["date"], "mkt20": dec(r.get("mkt20"), 3), "flagged": bool(r["flagged"]),
             "coverage_fail_days": r["coverage_fail_days"],
             "groups": [{"group": g, "n": r[f"n_{g}"], "ex": dec(r.get(f"ex_{g}"), 3), "raw": dec(r.get(f"raw_{g}"), 3)}
                        for g in FW.OS20_GROUPS]} for r in os20]}]}
