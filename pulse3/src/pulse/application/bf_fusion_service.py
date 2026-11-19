"""Fusion backfill service: round-5 static40 score, its forward books and forward experiments on the server (legacy triage batch 3).

Why: the PC task ``PulseDailyForward`` computes the frozen fusion score and records the main book, the 10-day shadow, the monitor,
the X20 basket and OS20. After the M6 cutover the PC is gone; this service does the same day with the same frozen rules inside
pulse3 and keeps the records as immutable, content-addressed datasets (``publish_derived``).
Data flow of one day (``FusionService.daily``): READY research snapshot (forward-adjusted bars, point in time at the cutoff) +
legacy float caps + industry + listing dates -> ``domain.fusion.panel`` -> factor pass (``scorer.run_factors``) -> scores of the
last rows -> reproduction gate (last 60 sessions, top-40 overlap >= 95% against the stored scores) -> promote ->
append-only as-of scores -> books, monitor, X20, OS20 -> log rows (``fusion_forward_log``).
Datasets (all derived, contract ``pulse.analysis_artifact``; each publish is a new READY snapshot holding the full table):
* ``fusion_scores_current``: (code, date, score), the promoted scores; the old rows older than the gate window are kept;
* ``fusion_scores_asof``: (code, date, score, computed_on, run_id), first computed score of a date wins, never rewritten;
* ``fusion_forward_log``: (log, date, written_on, run_id, payload JSON); logs ``main``, ``h10``, ``x20``, ``monitor``, ``os20``,
  ``os20_signals``, ``run``; append-only, the latest row of a (log, date) wins.
Invariants: nothing is invented (a missing input stops the run with a reason, never a 0); a stopped run leaves every dataset as it
was and writes one ``run`` row; rerunning the same day appends nothing; the as-of scores of a date are never replaced.
Not covered: scheduling (``deploy/windows`` belongs to the integrator), factor mining, any paper-account ledger.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
import psycopg

from pulse.adapters.lake import LakeStore
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.domain.fusion import books as BK
from pulse.domain.fusion import forward as FW
from pulse.domain.fusion import panel as PN
from pulse.domain.fusion import scorer as SC

LANE = "fusion"
DS_CURRENT, DS_ASOF, DS_LOG, DS_IC = "fusion_scores_current", "fusion_scores_asof", "fusion_forward_log", "fusion_ic_history"
CONTRACT = "pulse.analysis_artifact"
SECTOR_DATASET = "sector_membership_legacy_boards_industry"
ASOF_START = date(2026, 10, 30)          # first decision date with an as-of score (the book was frozen on 2026-11-01)
REPRO_DAYS, REPRO_MIN, REPRO_MIN_DAYS = 60, 0.95, 20
HISTORY_START = date(2023, 11, 1)        # the old panel starts here; the first score is on 2024-08-20
MIN_ROWS = 400                           # sessions a daily window needs: 250-bar windows + 61 scored rows + the recursive CGO warm-up
CST = timezone(timedelta(hours=8))
LOGS = ("main", "h10", "x20", "monitor", "os20", "os20_signals", "run")
BOOK_FILE = {"main": BK.MAIN, "h10": BK.H10}
SCORE_NAME = "fusion_static40_safe"
IMPLEMENTATION = "fusion-static40-safe-v1"


class FusionStop(RuntimeError):
    """The run cannot continue; ``code`` is the stable reason that goes into the ``run`` row."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def peak_memory_mb() -> float | None:
    """Peak resident memory of this process in MB (None when the platform gives no figure)."""
    try:
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(kb / 1024, 1)
    except ImportError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / 1048576, 1)
    except Exception:  # noqa: BLE001 - a figure for the report only
        return None
    return None


# ---------------------------------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Loaded:
    long: pl.DataFrame                   # main-board rows with every derived column and ``industry``
    session: date
    cutoff: datetime
    refs: dict[str, Any]                 # manifest ids / hashes of the inputs
    names: dict[str, str]                # code -> display name
    notes: list[str] = field(default_factory=list)


def _latest_manifest(conn: psycopg.Connection, dataset: str, cutoff: datetime | None) -> dict | None:
    return B._ready_manifest(conn, dataset, cutoff)


def load_inputs(conn: psycopg.Connection, lake: LakeStore, cutoff: datetime | None = None) -> Loaded:
    """Read the research bars visible at ``cutoff`` (default now) and everything the panel needs, point in time."""
    cutoff = cutoff or datetime.now(timezone.utc)
    ri = B.load_research_inputs(conn, lake, cutoff)
    metas, listed = B.load_instrument_meta(conn, cutoff)
    rows = [(str(iid), m.code, m.display_name, listed.get(iid)) for iid, m in metas.items() if m.code and m.board_code == "main"]
    meta = pl.DataFrame(rows, schema={"instrument_id": pl.String, "code": pl.String, "display_name": pl.String, "list_date": pl.Date}, orient="row")
    refs: dict[str, Any] = {"research_snapshot": str(ri.manifest["snapshot_id"]), "research_hash": ri.manifest["content_hash"],
                            "research_manifest_version": ri.manifest["manifest_version"], "policy": ri.availability_policy}
    cap_man = _latest_manifest(conn, B.CAP_DATASET, cutoff)
    if cap_man is None:
        raise FusionStop("cap_dataset_missing", "no READY instrument_circulating_cap_legacy snapshot at the cutoff")
    caps = lake.read_snapshot_frames(cap_man["content_hash"])
    refs["cap_snapshot"] = str(cap_man["snapshot_id"])
    caps = (caps.filter(pl.col("available_at") <= cutoff).sort(["as_of", "available_at"]).unique("instrument_id", keep="last")
            .select("instrument_id", (pl.col("float_cap_cny").cast(pl.Float64) / 1e8).alias("float_cap_100m"),
                    pl.col("total_cap_cny").is_not_null().alias("has_total_cap")))
    bars = (ri.bars.join(meta.select("instrument_id", "code", "list_date"), on="instrument_id", how="inner")
            .join(caps, on="instrument_id", how="left")
            .with_columns(pl.col("has_total_cap").fill_null(False), pl.col("trade_date").alias("date"))
            .select(PN.BAR_COLUMNS))
    long = PN.build_long(bars)
    sec = _latest_manifest(conn, SECTOR_DATASET, cutoff)
    notes = list(ri.notes)
    if sec is None:
        long = long.with_columns(pl.lit(None, dtype=pl.String).alias("industry"))
        notes.append("industry snapshot missing: OS20 and the matched baselines treat every stock as industry NA")
    else:
        refs["sector_snapshot"] = str(sec["snapshot_id"])
        mem = lake.read_snapshot_frames(sec["content_hash"])
        # latest membership; a stock listed in two boards keeps the lowest node_id (the PC's pick)
        mem = (mem.filter((pl.col("available_at") <= cutoff) & (pl.col("effective_from") <= cutoff)).sort(["effective_from", "node_id"], descending=[False, True])
               .unique("instrument_id", keep="last").join(meta.select("instrument_id", "code"), on="instrument_id")
               .select("code", pl.col("node_id").alias("industry")))
        long = long.join(mem, on="code", how="left")
    names = dict(zip(meta["code"].to_list(), meta["display_name"].to_list()))
    return Loaded(long, ri.session_date, cutoff, refs, names, notes)


# ---------------------------------------------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------------------------------------------
LOG_SCHEMA = {"log": pl.String, "date": pl.String, "written_on": pl.Date, "run_id": pl.String, "payload": pl.String}
CURRENT_SCHEMA = {"code": pl.String, "date": pl.Date, "score": pl.Float64}
ASOF_SCHEMA = {"code": pl.String, "date": pl.Date, "score": pl.Float64, "computed_on": pl.Date, "run_id": pl.String}
IC_SCHEMA = {"factor": pl.String, "date": pl.Date, "ic": pl.Float64}


class Store:
    """Reads and appends the three datasets of the lane through the lake manifests (``publish_derived``)."""

    def __init__(self, svc: DataService):
        self.svc, self.conn, self.lake = svc, svc.conn, svc.lake

    def read(self, dataset: str, schema: dict, cutoff: datetime | None = None) -> pl.DataFrame:
        man = _latest_manifest(self.conn, dataset, cutoff)
        if man is None:
            return pl.DataFrame(schema=schema)
        df = self.lake.read_snapshot_frames(man["content_hash"])
        return pl.DataFrame(schema=schema) if df is None else df

    def publish(self, dataset: str, frame: pl.DataFrame, extra: dict) -> dict | None:
        """New READY snapshot with ``frame``; an identical frame is not published twice (returns the existing manifest)."""
        buf = io.BytesIO()
        frame.write_parquet(buf)
        digest = hashlib.sha256(buf.getvalue()).hexdigest()
        hit = B.find_derived(self.conn, dataset, "frame_sha256", digest)
        if hit is not None:
            return hit
        cov = {"dataset": dataset, "frame_sha256": digest, "rows": frame.height, "implementation": IMPLEMENTATION, **extra}
        return B.publish_derived(self.svc, dataset, frame, cov, contract_id=CONTRACT, watermark=self.svc.clock())

    # log helpers -----------------------------------------------------------------------------------------------
    def log_rows(self, name: str, cutoff: datetime | None = None, frame: pl.DataFrame | None = None, *, by_date: bool = True) -> list[dict]:
        """Payloads of one log in date order (rows appended later come later for the same date); ``by_date=False`` keeps the order
        they were written in (the ``run`` log: its date is the session, not the time of the run)."""
        df = frame if frame is not None else self.read(DS_LOG, LOG_SCHEMA, cutoff)
        sel = df.filter(pl.col("log") == name)
        return [json.loads(p) for p in (sel.sort("date", maintain_order=True) if by_date else sel)["payload"].to_list()]


def log_frame(rows: list[tuple[str, str, date, str, dict]]) -> pl.DataFrame:
    return pl.DataFrame([(a, b, c, d, json.dumps(e, ensure_ascii=False, sort_keys=True)) for a, b, c, d, e in rows],
                        schema=LOG_SCHEMA, orient="row")


def sha16(df: pl.DataFrame) -> str:
    buf = io.BytesIO()
    df.sort(["date", "code"]).write_parquet(buf)
    return hashlib.sha256(buf.getvalue()).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------------------------
# computation helpers (pure given a panel)
# ---------------------------------------------------------------------------------------------------------------
def scores_by_day(frame: pl.DataFrame) -> dict[date, tuple[list[str], np.ndarray]]:
    out: dict[date, tuple[list[str], np.ndarray]] = {}
    for (d,), g in frame.sort(["date", "code"]).partition_by("date", as_dict=True, maintain_order=True).items():
        out[d] = (g["code"].to_list(), g["score"].to_numpy())
    return out


def top40(frame: pl.DataFrame, d: date) -> set[str]:
    g = frame.filter(pl.col("date") == d)
    return set(g.sort(["score", "code"], descending=[True, False])["code"].head(40).to_list())


def reproduction(old: pl.DataFrame, new: pl.DataFrame) -> dict:
    """Mean and minimum top-40 overlap of two score tables over the last ``REPRO_DAYS`` dates both hold."""
    both = sorted(set(old["date"].unique().to_list()) & set(new["date"].unique().to_list()))[-REPRO_DAYS:]
    ov = [len(top40(old, d) & top40(new, d)) / 40 for d in both]
    return {"days": len(both), "mean_overlap": round(sum(ov) / len(ov), 6) if ov else None,
            "min_overlap": round(min(ov), 6) if ov else None, "old_last": str(old["date"].max()) if old.height else None,
            "new_last": str(new["date"].max()) if new.height else None}


def grid_of(panel: PN.Panel) -> dict[str, np.ndarray]:
    return {k: panel.f[k] for k in ("open", "close", "is_limit_down", "open_at_limit", "open_at_floor")}


def score_rows_frame(panel: PN.Panel, scores: dict[int, np.ndarray]) -> pl.DataFrame:
    rows = list(SC.score_frame_rows(panel, scores))
    return pl.DataFrame({"code": [r[0] for r in rows], "date": [r[1] for r in rows], "score": [r[2] for r in rows]},
                        schema=CURRENT_SCHEMA)


def ic_matrix(hist: pl.DataFrame, names: list[str], dates: list[date]) -> np.ndarray:
    """[F, T] float32 of the stored daily ICs (NaN where none is stored)."""
    out = np.full((len(names), len(dates)), np.nan, dtype=np.float32)
    if hist.height == 0:
        return out
    fi, di = {n: i for i, n in enumerate(names)}, {d: i for i, d in enumerate(dates)}
    h = hist.filter(pl.col("factor").is_in(names) & pl.col("date").is_in(dates) & pl.col("ic").is_not_null())
    r = np.fromiter((fi[f] for f in h["factor"].to_list()), dtype=np.int64, count=h.height)
    c = np.fromiter((di[d] for d in h["date"].to_list()), dtype=np.int64, count=h.height)
    out[r, c] = h["ic"].cast(pl.Float64).to_numpy().astype(np.float32)
    return out


def first_score_row(panel: PN.Panel) -> int:
    """Row of the first scoring day (2024-08-20) in a panel that reaches back to it."""
    return next(i for i, d in enumerate(panel.dates) if d >= SC.FIRST_SCORE_DATE)


def new_ic_rows(panel: PN.Panel, hist: pl.DataFrame) -> list[int]:
    """Rows whose 5-day label is complete (up to ``T - 7``), that the IC history does not hold yet, from the first IC row on."""
    have = set(hist["date"].unique().to_list()) if hist.height else set()
    return [r for r in range(panel.T - SC.PURGE) if panel.dates[r] >= SC.FIRST_IC_DATE and panel.dates[r] not in have]


def compute_scores(panel: PN.Panel, *, tail_start: int, hist: pl.DataFrame | None = None, progress=None
                   ) -> tuple[pl.DataFrame, SC.FactorRun, dict, pl.DataFrame]:
    """Factor pass: evaluate every factor on the panel, rank and take the IC only on the rows that need it (new IC days and the tail),
    score the tail rows with the stored IC history completed by the new ICs. Returns (scores, run, info, new IC rows)."""
    hist = hist if hist is not None else pl.DataFrame(schema=IC_SCHEMA)
    facs = SC.frozen_factors()
    names = [n for n, _ in facs]
    t0, c0 = time.perf_counter(), time.process_time()
    fresh = new_ic_rows(panel, hist)
    rows = sorted(set(fresh) | set(range(max(tail_start, 0), panel.T)))
    run = SC.run_factors(panel, facs, tail_start=tail_start, rows=rows, progress=progress)
    cal = sorted(set(hist["date"].unique().to_list()) | set(panel.dates)) if hist.height else list(panel.dates)
    ic = ic_matrix(hist, names, cal)
    at = {d: i for i, d in enumerate(cal)}
    for r in fresh:                                   # a stored IC is never replaced
        ic[:, at[panel.dates[r]]] = run.ic[:, r]
    sc = SC.score_rows(run, panel, ic=ic, ic_dates=cal, first_ic_date=SC.FIRST_IC_DATE, first_score_date=SC.FIRST_SCORE_DATE)
    frame = score_rows_frame(panel, sc)
    new_ic = pl.DataFrame({"factor": [n for _ in fresh for n in names], "date": [panel.dates[r] for r in fresh for _ in names],
                           "ic": [None if not np.isfinite(run.ic[i, r]) else float(run.ic[i, r]) for r in fresh for i in range(len(names))]},
                          schema=IC_SCHEMA)
    info = {"factors": len(facs), "skipped": run.skipped, "seconds": round(time.perf_counter() - t0, 1),
            "cpu_seconds": round(time.process_time() - c0, 1), "ic_rows_computed": len(fresh), "rows_ranked": len(rows),
            "rows": [min(sc) if sc else None, max(sc) if sc else None]}
    return frame, run, info, new_ic


def merge_scores(old: pl.DataFrame, new: pl.DataFrame) -> pl.DataFrame:
    """Promotion: every row of ``old`` older than the first date of ``new`` plus all of ``new``."""
    if new.height == 0:
        return old
    first = new["date"].min()
    return pl.concat([old.filter(pl.col("date") < first), new]).sort(["date", "code"])


def append_asof(asof: pl.DataFrame, current: pl.DataFrame, *, computed_on: date, run_id: str) -> pl.DataFrame:
    """Rows of dates not archived yet and after the last archived date (``ASOF_START`` on the first run): first computed wins."""
    have = set(asof["date"].unique().to_list())
    start = max(have) if have else ASOF_START - timedelta(days=1)
    add = current.filter((pl.col("date") > start) & ~pl.col("date").is_in(list(have))).with_columns(
        pl.lit(computed_on).alias("computed_on"), pl.lit(run_id).alias("run_id")).select(list(ASOF_SCHEMA))
    return pl.concat([asof, add]) if add.height else asof


def effective_scores(asof: pl.DataFrame, current: pl.DataFrame, start: date) -> pl.DataFrame:
    """The scores forward experiments read: the archived (first computed) ones; the current ones only fill dates not archived."""
    a = asof.filter(pl.col("date") >= start).select("code", "date", "score")
    cur = current.filter((pl.col("date") >= start) & ~pl.col("date").is_in(a["date"].unique().to_list())).select("code", "date", "score")
    return pl.concat([a, cur])


def coverage_by_day(long: pl.DataFrame) -> dict[date, tuple[float, str]]:
    by: dict[date, set[str]] = {}
    for (d,), g in long.select("code", "date").partition_by("date", as_dict=True).items():
        by[d] = set(g["code"].to_list())
    return FW.coverage_status(by)


# ---------------------------------------------------------------------------------------------------------------
# the daily step
# ---------------------------------------------------------------------------------------------------------------
class FusionService:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock=None):
        self.conn, self.lake = conn, lake
        self.svc = DataService(conn, lake, clock=clock) if clock else DataService(conn, lake)
        self.store = Store(self.svc)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def today(self) -> date:
        return self.clock().astimezone(CST).date()

    # -- one day ---------------------------------------------------------------------------------------------------
    def daily(self, cutoff: datetime | None = None, *, progress=None, dry_run: bool = False) -> dict:
        """Compute and append everything the new session allows. Returns a summary; raises ``FusionStop`` for a refusal that
        was already recorded as a ``run`` row."""
        run_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        log_all = self.store.read(DS_LOG, LOG_SCHEMA)
        today = self.today()
        summary: dict[str, Any] = {"run_id": run_id}

        def stop(code: str, detail: str = "", session: date | None = None) -> None:
            row = {"status": "stopped", "code": code, "detail": detail, "session": session.isoformat() if session else None,
                   "refs": summary.get("refs")}
            if not dry_run:
                self.store.publish(DS_LOG, pl.concat([log_all, log_frame([("run", (session or today).isoformat(), today, run_id, row)])]),
                                   {"kind": "log", "last_run": run_id})
            raise FusionStop(code, detail)

        try:
            loaded = load_inputs(self.conn, self.lake, cutoff)
        except (FusionStop, LookupError) as exc:
            stop(exc.code if isinstance(exc, FusionStop) else "inputs_missing", str(exc))
        long = loaded.long
        cur = self.store.read(DS_CURRENT, CURRENT_SCHEMA)
        asof = self.store.read(DS_ASOF, ASOF_SCHEMA)
        new_logs: list[tuple[str, str, date, str, dict]] = []
        summary.update({"session": loaded.session.isoformat(), "notes": loaded.notes, "refs": loaded.refs})

        cov = coverage_by_day(long)
        if loaded.session in cov and cov[loaded.session][1] == "refuse":
            stop("coverage_refused", f"session {loaded.session} coverage {cov[loaded.session][0]:.3f} < {FW.COVERAGE_REFUSE}", loaded.session)
        panel = PN.to_panel(long)
        summary["panel"] = {"dates": panel.T, "codes": panel.N, "first": panel.dates[0].isoformat(), "last": panel.dates[-1].isoformat()}
        if panel.T < MIN_ROWS:
            stop("history_too_short", f"the research snapshot holds {panel.T} sessions from {panel.dates[0]}; a daily run needs {MIN_ROWS} "
                                      f"(import the legacy warehouse with an earlier --since)", loaded.session)
        last_cur = cur["date"].max() if cur.height else None
        if last_cur is not None and last_cur >= panel.dates[-1]:
            summary["scores"] = "up to date"
        else:
            if cur.height == 0:
                stop("scores_missing", "no promoted scores yet: run `fusion bootstrap` once to seed the history", loaded.session)
            row_of_last = panel.dates.index(last_cur) if last_cur in panel.dates else None
            tail = panel.T - REPRO_DAYS - 1 if row_of_last is None else min(panel.T - REPRO_DAYS - 1, row_of_last - REPRO_DAYS + 1)
            tail = max(first_score_row(panel), tail)
            hist = self.store.read(DS_IC, IC_SCHEMA)
            if hist.height == 0:
                stop("scores_missing", "no IC history: run `fusion bootstrap` once to seed it", loaded.session)
            if hist["date"].max() < panel.dates[0]:
                stop("ic_history_gap", f"the IC history ends {hist['date'].max()}, before the window starts {panel.dates[0]}", loaded.session)
            new, run, info, new_ic = compute_scores(panel, tail_start=tail, hist=hist, progress=progress)
            chk = reproduction(cur, new)
            summary["scores"] = {**info, "reproduction": chk}
            if chk["days"] < REPRO_MIN_DAYS or chk["mean_overlap"] is None or chk["mean_overlap"] < REPRO_MIN:
                stop("reproduction_failed", json.dumps(chk), loaded.session)
            cur = merge_scores(cur, new)
            asof = append_asof(asof, cur, computed_on=today, run_id=run_id)
            if not dry_run:
                self.store.publish(DS_CURRENT, cur, {"kind": "current", "last_date": str(cur["date"].max()), "run_id": run_id, **loaded.refs})
                self.store.publish(DS_ASOF, asof, {"kind": "asof", "last_date": str(asof["date"].max()), "run_id": run_id})
                if new_ic.height:
                    self.store.publish(DS_IC, pl.concat([hist, new_ic]), {"kind": "ic", "last_date": str(new_ic["date"].max()), "run_id": run_id})
            del run
        summary["books"] = self._books(loaded, panel, cur, asof, log_all, new_logs, run_id, today)
        summary["experiments"] = self._experiments(loaded, panel, cur, asof, log_all, new_logs, run_id, today)
        summary["seconds"] = round(time.perf_counter() - t0, 1)
        summary["peak_memory_mb"] = peak_memory_mb()
        new_logs.append(("run", loaded.session.isoformat(), today, run_id, {"status": "done", **{k: summary[k] for k in ("session", "seconds", "peak_memory_mb")},
                                                                           "scores": summary.get("scores"), "refs": loaded.refs}))
        if not dry_run and new_logs:
            self.store.publish(DS_LOG, pl.concat([log_all, log_frame(new_logs)]), {"kind": "log", "last_run": run_id, **loaded.refs})
        return summary

    # -- seed ------------------------------------------------------------------------------------------------------
    def bootstrap(self, cutoff: datetime | None = None, *, progress=None, dry_run: bool = False) -> dict:
        """One-off: score every row from the first scoring day on and publish them as the promoted scores. Needs bars from
        ``HISTORY_START``. The as-of archive starts with the dates from ``ASOF_START`` on (computed today, flagged ``seed`` in the run
        row): the PC archived those days while it was running, here they are the first computation pulse3 has."""
        run_id = uuid.uuid4().hex[:12]
        loaded = load_inputs(self.conn, self.lake, cutoff)
        panel = PN.to_panel(loaded.long)
        if panel.dates[0] > HISTORY_START + timedelta(days=7):
            raise FusionStop("history_too_short", f"the research snapshot starts {panel.dates[0]}; import the legacy warehouse with --since {HISTORY_START}")
        frame, run, info, new_ic = compute_scores(panel, tail_start=first_score_row(panel), progress=progress)
        today = self.today()
        asof = append_asof(self.store.read(DS_ASOF, ASOF_SCHEMA), frame, computed_on=today, run_id=run_id)
        out = {"run_id": run_id, "rows": frame.height, "dates": frame["date"].n_unique(), "last": str(frame["date"].max()),
               "asof_dates": asof["date"].n_unique() if asof.height else 0, "scores": info, "peak_memory_mb": peak_memory_mb(), "refs": loaded.refs}
        if not dry_run:
            self.store.publish(DS_CURRENT, frame, {"kind": "current", "seed": True, "last_date": out["last"], "run_id": run_id, **loaded.refs})
            self.store.publish(DS_ASOF, asof, {"kind": "asof", "seed": True, "last_date": str(asof["date"].max()), "run_id": run_id})
            self.store.publish(DS_IC, new_ic, {"kind": "ic", "seed": True, "last_date": str(new_ic["date"].max()), "run_id": run_id})
            log = self.store.read(DS_LOG, LOG_SCHEMA)
            row = {"status": "done", "seed": True, "session": loaded.session.isoformat(), "scores": info, "peak_memory_mb": out["peak_memory_mb"], "refs": loaded.refs}
            self.store.publish(DS_LOG, pl.concat([log, log_frame([("run", loaded.session.isoformat(), today, run_id, row)])]), {"kind": "log", "last_run": run_id})
        return out

    # -- books -----------------------------------------------------------------------------------------------------
    def _books(self, loaded: Loaded, panel: PN.Panel, cur: pl.DataFrame, asof: pl.DataFrame, log_all: pl.DataFrame, new_logs: list,
               run_id: str, today: date) -> dict:
        out: dict[str, Any] = {}
        sc = scores_by_day(cur)
        digest = sha16(cur)
        grid = grid_of(panel)
        for key, rule in BOOK_FILE.items():
            done = {r["date"] for r in self.store.log_rows(key, frame=log_all)}
            gated, raw, w, sig = BK.run_book(rule, panel.dates, panel.codes, grid, sc)
            row = BK.decision_row(rule, gated, raw, w, sig, scores_digest=digest)
            if row["date"] in done:
                out[key] = {"date": row["date"], "status": "already logged"}
                continue
            new_logs.append((key, row["date"], today, run_id, row))
            out[key] = {"date": row["date"], "status": "logged", "holdings": len(row["holdings"]), "nav": row["nav"]}
        return out

    # -- forward experiments ---------------------------------------------------------------------------------------
    def _experiments(self, loaded: Loaded, panel: PN.Panel, cur: pl.DataFrame, asof: pl.DataFrame, log_all: pl.DataFrame,
                     new_logs: list, run_id: str, today: date) -> dict:
        long = loaded.long
        days = panel.dates
        cov = coverage_by_day(long)
        out: dict[str, Any] = {}
        # monitor (E068)
        mon_panel = FW.retradable_frame(long, FW.MONITOR_PANEL_FROM)
        s_mon = effective_scores(asof, cur, FW.MONITOR_START)
        daily = FW.monitor_daily(mon_panel, s_mon)
        latest = FW.latest_by_date(self.store.log_rows("monitor", frame=log_all))
        rows = FW.monitor_new_rows(daily, days, cov, latest)
        for r in rows:
            new_logs.append(("monitor", r["date"], today, run_id, r))
        allrows = sorted({**latest, **{r["date"]: r for r in rows}}.values(), key=lambda r: r["date"])
        out["monitor"] = {"new": len(rows), **FW.monitor_summary(allrows)}
        # X20 (E085)
        x_panel = FW.retradable_frame(long, FW.X20_PANEL_FROM)
        pool = FW.x20_pool(x_panel, effective_scores(asof, cur, FW.X20_START))
        path = FW.x20_path(x_panel, pool)
        done = {r["date"] for r in self.store.log_rows("x20", frame=log_all)}
        new = [r for r in path if r["date"] not in done]
        for r in new:
            new_logs.append(("x20", r["date"], today, run_id, r))
        out["x20"] = {"days": len(path), "new": len(new), "last": path[-1] if path else None}
        # OS20 (E081)
        out["os20"] = self._os20(long, days, cov, log_all, new_logs, run_id, today)
        return out

    def _os20(self, long: pl.DataFrame, days: list[date], cov, log_all: pl.DataFrame, new_logs: list, run_id: str, today: date) -> dict:
        recent = FW.retradable_frame(long, FW.OS20_PANEL_FROM)
        out_ = FW.add_style(FW.outcomes(recent), recent)
        sig_rows = self.store.log_rows("os20_signals", frame=log_all)
        have = {r["date"] for r in sig_rows}
        pos = {d: i for i, d in enumerate(days)}
        ready = [d for d in days if d >= FW.OS20_START and d.isoformat() not in have and pos[d] + 1 < len(days)]
        frozen = {date.fromisoformat(r["date"]): r["lists"] for r in sig_rows}
        if ready:
            lists = FW.os20_signal_lists(recent, out_).filter(pl.col("date").is_in(ready))
            for (d,), g in lists.partition_by("date", as_dict=True).items():
                payload = {"date": d.isoformat(), "lists": [[a, b] for a, b in zip(g["code"].to_list(), g["group"].to_list())]}
                new_logs.append(("os20_signals", d.isoformat(), today, run_id, payload))
                frozen[d] = payload["lists"]
        sig = pl.DataFrame([(a, d, b) for d, lst in frozen.items() for a, b in lst],
                           schema={"code": pl.String, "date": pl.Date, "group": pl.String}, orient="row")
        latest = FW.latest_by_date(self.store.log_rows("os20", frame=log_all))
        added: list[dict] = []
        if sig.height:
            wide = FW.os20_wide(FW.os20_daily_excess(sig, out_))
            mkt = FW.market20(out_, recent)
            for r in wide.iter_rows(named=True):
                d = r["date"]
                if pos[d] + FW.OS20_HOLD >= len(days):
                    continue                                   # label not complete yet
                bad = [x.isoformat() for x in days[pos[d] + 1: pos[d] + FW.OS20_HOLD + 1] if cov.get(x, (1.0, "ok"))[1] == "refuse"]
                prev = latest.get(d.isoformat())
                if not FW.needs_row(prev, bool(bad)):
                    continue
                row = {**FW.os20_row(r, mkt), "flagged": bool(bad), "coverage_fail_days": bad}
                if prev is not None:
                    row["replaces_flagged"] = True
                new_logs.append(("os20", d.isoformat(), today, run_id, row))
                added.append(row)
        rows = sorted({**latest, **{r["date"]: r for r in added}}.values(), key=lambda r: r["date"])
        return {"signal_dates": len(frozen), "new_signal_dates": len(ready), "new_rows": len(added),
                **FW.os20_summary([r for r in rows if not r["flagged"]])}
