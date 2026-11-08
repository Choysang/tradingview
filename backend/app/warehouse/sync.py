"""Warehouse sync jobs: backfill and daily refresh from Tencent.

`run_sync` is blocking and never raises: every failure becomes a job with
status "failed". `start_sync` runs it on a daemon thread, one job at a time.

Bars are forward-adjusted (qfq), which means every dividend rewrites all
earlier prices. The daily job reads the whole market's closing quotes in batches
of 80 (about 75 requests) and appends them as today's bars; a code goes to the
kline endpoint only when it is new, has missed days, or its published previous
close differs from the stored one (an ex-right day: the exchange publishes a
reference price), in which case its whole history is re-fetched so the lake
never mixes two adjustment bases for one stock.

Both jobs stop at once when Tencent's WAF blocks us, or after a run of
consecutive failures, keeping everything already written; a rerun resumes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import logging
import re
import sys
import threading
from time import monotonic, sleep
from typing import Callable
from uuid import uuid4

import httpx
import polars as pl

from contracts.schema import SyncJob, SyncRequest
from . import sina, tencent
from .store import Warehouse, warehouse

LOG = logging.getLogger("pulse.warehouse.sync")

WORKERS = 8
BATCH_CODES = 200
DAILY_LOOKBACK_DAYS = 45  # ~30 trading bars
READJUST_TOLERANCE = 0.005
COMPACT_ABOVE = 40
MAX_CONSECUTIVE_ERRORS = 30
CST = timezone(timedelta(hours=8))
SESSION_DONE = time(15, 5)  # after the close auction; earlier bars for today are unfinished
# The kline WAF behaves like a request quota, not a rate limit: on 2026-10-30/31 it
# refused us after ~1,000 requests at 6/s and after ~700 at 2/s, and stayed closed
# for over 30 minutes. `--wait-hours` waits it out and resumes.
PROBE_EVERY = 15 * 60.0
# Daily bars come from Sina (ratio forward adjustment, see sina.py); securities and
# closing quotes stay on Tencent. The method tag guards against mixing the two
# adjustments in one warehouse: a change forces a fresh backfill.
BAR_SOURCE = "sina"
BARS_METHOD = {"sina": "sina-ratio", "tencent": "tencent-fq"}
LEGACY_METHOD = "tencent-fq"  # warehouses written before the tag existed


def _bars():
    return sina if BAR_SOURCE == "sina" else tencent


def stored_method(store: Warehouse) -> str:
    return (store.read_doc("meta", "bars_method") or {}).get("method", LEGACY_METHOD)
TABLES = ("bars", "index_bars", "securities")

ProgressHook = Callable[[SyncJob], None]


class SyncFailure(RuntimeError):
    """The job could not produce usable data; the message is shown to the user."""


class SyncBlocked(SyncFailure):
    """The provider refused us mid-job; rerunning later resumes."""


def _check_years(years: int) -> None:
    """One Sina request returns at most MAX_BARS bars: a longer window would be cut
    silently while the recorded window claimed the full span."""
    if BAR_SOURCE == "sina" and years > sina.MAX_YEARS:
        raise SyncFailure(f"新浪日线单次最多约 {sina.MAX_YEARS} 年（{sina.MAX_BARS} 根），请把 years 设为 {sina.MAX_YEARS} 以内")


def _store() -> Warehouse:
    return warehouse("live")


def _client() -> httpx.Client:
    return tencent.new_client()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- job state ---------------------------------------------------------------------

_STATE = threading.Lock()
_JOB: SyncJob | None = None
_RUNNING = False
_THREAD: threading.Thread | None = None


def current_job() -> SyncJob | None:
    with _STATE:
        return _JOB


@dataclass
class _Tracker:
    kind: str
    hook: ProgressHook | None = None
    job: SyncJob = field(init=False)
    blocked: bool = field(default=False, init=False)  # the job stopped on a provider refusal
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.job = SyncJob(id=uuid4().hex[:8], kind=self.kind, status="running", started_at=_now(), message="准备中…")  # type: ignore[arg-type]
        self._publish()

    def _publish(self) -> None:
        global _JOB
        with _STATE:
            _JOB = self.job
        if self.hook is not None:
            try:
                self.hook(self.job)
            except Exception:  # a broken progress callback must not kill the sync
                LOG.exception("progress hook failed")

    def update(self, **fields: object) -> None:
        with self._lock:
            merged = {**self.job.model_dump(), **fields}
            total, done = int(merged["total"]), int(merged["done"])
            merged["progress"] = min(1.0, done / total) if total > 0 else 0.0
            self.job = SyncJob(**merged)
        self._publish()

    def finish(self, message: str, status: str = "done") -> None:
        with self._lock:
            merged = {**self.job.model_dump(), "status": status, "message": message, "finished_at": _now()}
            if status == "done":
                merged["progress"] = 1.0
                merged["done"] = max(int(merged["done"]), int(merged["total"]))
            self.job = SyncJob(**merged)
        self._publish()


# --- public api ---------------------------------------------------------------------

def run_sync(request: SyncRequest, on_progress: ProgressHook | None = None) -> SyncJob:
    """Blocking sync. Not guarded against a concurrent `start_sync` job."""
    return _execute(request, _Tracker(request.kind, on_progress))


def start_sync(request: SyncRequest) -> SyncJob:
    """Start a background job, or return the one already running."""
    return begin_sync(request)[0]


def begin_sync(request: SyncRequest) -> tuple[SyncJob, bool]:
    """(job, started). started is False when another job holds the slot."""
    global _RUNNING, _THREAD
    with _STATE:
        if _RUNNING and _JOB is not None:
            return _JOB, False
        _RUNNING = True
    try:
        tracker = _Tracker(request.kind)
        thread = threading.Thread(target=_background, args=(request, tracker), name="warehouse-sync", daemon=True)
        _THREAD = thread
        thread.start()
    except Exception:
        with _STATE:
            _RUNNING = False
        raise
    return tracker.job, True


def _background(request: SyncRequest, tracker: _Tracker) -> None:
    global _RUNNING
    try:
        _execute(request, tracker)
    finally:
        with _STATE:
            _RUNNING = False


def _execute(request: SyncRequest, tracker: _Tracker) -> SyncJob:
    try:
        store = _store()
        with _client() as client:
            if request.kind == "backfill":
                message = _backfill(store, request, client, tracker)
            else:
                message = _daily(store, request, client, tracker)
        _compact(store, tracker)
        _record(store, tracker.job)
        tracker.finish(message)
    except SyncFailure as exc:
        tracker.blocked = isinstance(exc, SyncBlocked)
        tracker.finish(str(exc), "failed")
    except tencent.ProviderBlocked as exc:  # refused before any stock was fetched
        tracker.blocked = True
        tracker.finish(f"{exc}；重新运行会从断点续传", "failed")
    except Exception as exc:  # network, disk or polars errors: report, never raise
        LOG.exception("sync failed")
        tracker.finish(f"同步失败：{exc}", "failed")
    return tracker.job


# --- shared steps ---------------------------------------------------------------------

def _valid_codes(codes: list[str] | None) -> list[str] | None:
    if not codes:
        return None
    clean = sorted({code.strip() for code in codes if re.fullmatch(r"\d{6}", code.strip())})
    if not clean:
        raise SyncFailure("代码列表里没有有效的 6 位股票代码")
    return clean


def session_cut(now: datetime | None = None) -> date | None:
    """Bars dated on or after this day are unfinished and must not be stored:
    today during a weekday session, otherwise None. Research signals use
    completed bars only, so a sync run at 10:30 keeps everything up to yesterday."""
    now = (now or datetime.now(CST)).astimezone(CST)
    return now.date() if now.weekday() < 5 and now.time() < SESSION_DONE else None


def _complete(frame: pl.DataFrame, cut: date | None) -> pl.DataFrame:
    return frame.filter(pl.col("date") < cut) if cut is not None and not frame.is_empty() else frame


def _securities(client: httpx.Client, codes: list[str] | None) -> pl.DataFrame:
    return tencent.fetch_quotes(codes, client) if codes else tencent.list_securities(client)


def _stored_span(store: Warehouse) -> dict[str, tuple[date, date]]:
    lazy = store.scan("bars")
    if lazy is None:
        return {}
    frame = lazy.group_by("code").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last")).collect()
    return {code: (first, last) for code, first, last in frame.iter_rows()}


def _sync_indices(
    store: Warehouse, client: httpx.Client, start: date, end: date, cut: date | None = None
) -> tuple[int, int, date | None]:
    """(indices written, errors, newest date seen)."""
    frames: list[pl.DataFrame] = []
    errors = 0
    for symbol in tencent.INDEX_TICKERS:
        try:
            frame = _complete(_bars().fetch_index(symbol, start, end, client), cut)
        except tencent.ProviderBlocked:
            raise
        except Exception as exc:
            LOG.warning("index %s failed: %s", symbol, exc)
            errors += 1
            continue
        if not frame.is_empty():
            frames.append(frame)
    if frames:
        merged = pl.concat(frames)
        store.write("index_bars", merged)
        return len(frames), errors, merged["date"].max()
    return 0, errors, None


def _compact(store: Warehouse, tracker: _Tracker) -> None:
    for table in TABLES:
        if len(store.files(table)) > COMPACT_ABOVE:
            tracker.update(message=f"正在合并 {table} 数据文件…")
            store.compact(table)


def _record(store: Warehouse, job: SyncJob) -> None:
    try:
        store.write_doc("meta", "last_sync", job.model_dump())
    except OSError:
        LOG.warning("could not record sync metadata", exc_info=True)


class _Latest:
    """Newest trading day reached in this run, shared by the worker threads."""

    def __init__(self, value: date | None) -> None:
        self._value = value
        self._lock = threading.Lock()

    def get(self) -> date | None:
        with self._lock:
            return self._value

    def offer(self, value: date | None) -> None:
        if value is None:
            return
        with self._lock:
            if self._value is None or value > self._value:
                self._value = value


class _BatchWriter:
    """Collects per-code frames and writes ~BATCH_CODES of them per Parquet file.

    With `replace`, a code's frame supersedes every stored row of that code (even
    dates it does not cover, and when it is empty): a method switch must not leave
    rows of the old adjustment behind."""

    def __init__(self, store: Warehouse, cut: date | None = None, replace: bool = False) -> None:
        self.store = store
        self.cut = cut
        self.replace = replace
        self.pending: list[pl.DataFrame] = []
        self.codes: set[str] = set()
        self.rows = 0
        self.files = 0

    def add(self, frame: pl.DataFrame, code: str | None = None) -> None:
        frame = _complete(frame, self.cut)
        if self.replace and code is not None:
            self.codes.add(code)
        if not frame.is_empty():
            self.pending.append(frame)
        if len(self.codes if self.replace else self.pending) >= BATCH_CODES:
            self.flush()

    def flush(self) -> None:
        if not self.pending and not self.codes:
            return
        merged = pl.concat(self.pending) if self.pending else pl.DataFrame(schema=tencent.BAR_SCHEMA)
        if self.replace:
            self.store.replace_codes("bars", merged, self.codes)
        else:
            self.store.write("bars", merged)
        self.rows += merged.height
        self.files += 1
        self.pending = []
        self.codes = set()


# --- backfill ---------------------------------------------------------------------------

class _Breaker:
    """Stop a job when the provider blocks us or every request keeps failing."""

    def __init__(self) -> None:
        self.run = 0
        self.blocked = False

    def trip(self, error: Exception | None) -> str | None:
        if isinstance(error, tencent.ProviderBlocked):
            self.blocked = True
            return str(error)
        self.run = self.run + 1 if error is not None else 0
        if self.run >= MAX_CONSECUTIVE_ERRORS:
            return f"连续 {self.run} 只股票请求失败，已停止（{error}）"
        return None


def _abort(futures: list, writer: "_BatchWriter", reason: str, blocked: bool = False) -> SyncFailure:
    for future in futures:
        future.cancel()
    writer.flush()
    failure = SyncBlocked if blocked else SyncFailure
    return failure(f"{reason}；已写入 {writer.rows} 行，稍后重新运行会从断点续传")


def _backfill(store: Warehouse, request: SyncRequest, client: httpx.Client, tracker: _Tracker) -> str:
    _check_years(request.years)
    today = date.today()
    start = today - timedelta(days=365 * request.years)
    wanted = _valid_codes(request.codes)

    tracker.update(message="正在获取证券列表…")
    securities = _securities(client, wanted)
    if not securities.is_empty():
        store.write("securities", securities)
    codes = wanted or securities["code"].to_list()
    if not codes:
        raise SyncFailure("没有取到任何证券，请检查网络后重试")

    cut = session_cut()
    tracker.update(message=f"正在同步 {len(tencent.INDEX_TICKERS)} 个指数…", total=len(codes), done=0)
    index_ok, index_errors, index_latest = _sync_indices(store, client, start, today, cut)

    span = _stored_span(store)
    latest = _Latest(index_latest)
    window_slack = start + timedelta(days=14)
    # A finished earlier backfill reaching back at least this far fetched every code's
    # whole window, so a code whose first bar is later simply listed later.
    method = BARS_METHOD[BAR_SOURCE]
    same_method = stored_method(store) == method  # otherwise every stored bar is refetched
    done_from = (store.read_doc("meta", "backfill_window") or {}).get("start") if same_method else None
    window_known = done_from is not None and date.fromisoformat(done_from) <= start
    # An interrupted run records the codes it fetched whole. The next run skips them
    # outright: late listings, suspended stocks and codes Tencent has no bars for
    # would otherwise never look covered and cost two requests on every resume
    # (about a third of each quota window on 2026-10-31). Tails that fall behind
    # meanwhile are the daily sync's gaps.
    partial = store.read_doc("meta", "backfill_fetched") or {}
    partial_ok = (
        bool(partial.get("codes")) and date.fromisoformat(partial["start"]) <= start
        and partial.get("method", LEGACY_METHOD) == method
    )
    fetched: set[str] = set(partial["codes"]) if partial_ok else set()
    fetched_from = date.fromisoformat(partial["start"]) if partial_ok else start

    def covered(code: str) -> bool:
        if code in fetched:
            return True
        if not same_method:
            return False
        stored = span.get(code)
        reached = latest.get()
        if stored is None or reached is None or stored[1] < reached:
            return False
        return stored[0] <= window_slack or window_known

    def remember() -> None:
        if not wanted:
            store.write_doc("meta", "backfill_fetched", {
                "start": fetched_from.isoformat(), "codes": sorted(fetched), "method": method,
            })

    blocked: list[Exception] = []  # the first refusal; later workers return it without a request

    def work(code: str) -> tuple[str, pl.DataFrame | None, Exception | None]:
        if covered(code):
            return code, None, None
        if blocked:
            return code, None, blocked[0]
        try:
            return code, _bars().fetch_bars(code, start, today, client), None
        except Exception as exc:
            if isinstance(exc, tencent.ProviderBlocked):
                blocked.append(exc)
            return code, None, exc

    writer = _BatchWriter(store, cut, replace=not same_method)  # a refetched code drops its old-method rows
    breaker = _Breaker()
    done = errors = skipped = 0
    first_error = ""
    tracker.update(message=f"正在回填 {request.years} 年日线（{len(codes)} 只）…")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, code) for code in codes]
        for future in as_completed(futures):
            if future.cancelled():
                continue
            code, frame, error = future.result()
            done += 1
            reason = breaker.trip(error)
            if reason:
                failure = _abort(futures, writer, reason, breaker.blocked)
                remember()  # after the flush: only codes whose bars are on disk
                raise failure
            if error is not None:
                errors += 1
                first_error = first_error or f"{code}：{error}"
            elif frame is None:
                skipped += 1
            else:
                frame = _complete(frame, cut)
                latest.offer(frame["date"].max() if not frame.is_empty() else None)
                writer.add(frame, code)
                fetched.add(code)
            tracker.update(
                done=done,
                errors=errors + index_errors,
                message=f"回填中 {done}/{len(codes)}，已写入 {writer.rows + sum(f.height for f in writer.pending)} 行",
            )
    writer.flush()

    pulled = len(codes) - skipped - errors
    # The method switch is complete only when no stored code still holds old-method rows.
    # A code with no stored bars that failed holds none, so it blocks nothing.
    stale = [] if same_method or wanted else sorted(code for code in codes if code not in fetched and code in span)
    if stale:
        remember()  # the next run refetches exactly these codes
        raise SyncFailure(
            f"回填未完成：{len(stale)} 只库内已有日线的股票没能按新口径（{method}）重拉"
            + (f"（例：{first_error}）" if first_error else "")
            + "；复权口径尚未切换，日更仍会拒绝，重新运行回填只会重试这些股票"
        )
    if writer.rows == 0 and errors > 0:
        raise SyncFailure(f"回填失败：{errors} 只股票全部请求失败（{first_error}）")
    if not wanted:  # a code that failed stored nothing, so it is never taken as covered
        earliest = min(start, date.fromisoformat(done_from)) if done_from else start
        store.write_doc("meta", "backfill_window", {"start": earliest.isoformat()})
        store.write_doc("meta", "bars_method", {"method": method})
        if partial_ok:  # the window now covers them; a later backfill refreshes every tail again
            store.write_doc("meta", "backfill_fetched", {"start": start.isoformat(), "codes": [], "method": method})
    parts = [
        f"回填完成：{len(codes)} 只股票，新写入 {writer.rows} 行 / {writer.files} 个文件",
        f"拉取 {pulled} 只，跳过已最新 {skipped} 只",
        f"指数 {index_ok}/{len(tencent.INDEX_TICKERS)}",
    ]
    if errors or index_errors:
        parts.append(f"失败 {errors} 只股票、{index_errors} 个指数" + (f"（例：{first_error}）" if first_error else ""))
    return "；".join(parts)


# --- daily ---------------------------------------------------------------------------------

def _readjusted(stored: dict[date, float], fresh: pl.DataFrame, last_stored: date) -> bool:
    """True when overlapping closes disagree by more than the tolerance, which
    means a dividend or split has shifted the forward-adjusted history. The
    newest stored bar is ignored: it may have been captured intraday."""
    for day, close in zip(fresh["date"].to_list(), fresh["close"].to_list()):
        if day >= last_stored:
            continue
        old = stored.get(day)
        if old and abs(close - old) / old > READJUST_TOLERANCE:
            return True
    return False


def _recent_closes(store: Warehouse, cutoff: date) -> dict[str, dict[date, float]]:
    lazy = store.scan("bars")
    out: dict[str, dict[date, float]] = {}
    if lazy is None:
        return out
    frame = lazy.filter(pl.col("date") >= cutoff).select("code", "date", "close").collect()
    for code, day, close in frame.iter_rows():
        out.setdefault(code, {})[day] = close
    return out


def plan_daily(
    quotes: pl.DataFrame, tail: dict[str, dict[date, float]]
) -> tuple[pl.DataFrame, dict[str, str]]:
    """Split stored codes by what today's closing quote tells us.

    Returns (bars to append, {code: reason} for the kline endpoint). A quote is
    appended when the code traded (volume > 0) and its published previous close
    matches the stored close of the last market day before the quote's day.
    Otherwise the code missed days ("gap") or the previous close moved, which
    happens on an ex-right day ("readjusted"). Suspended codes are skipped.
    """
    market_days = sorted({day for closes in tail.values() for day in closes})
    keep: list[dict] = []
    refetch: dict[str, str] = {}
    for row in quotes.iter_rows(named=True):
        if not row["volume"] or row["volume"] <= 0:
            continue
        before = [day for day in market_days if day < row["date"]]
        stored = tail.get(row["code"], {}).get(before[-1]) if before else None
        if stored is None or not row["prev_close"]:
            refetch[row["code"]] = "gap"
        elif abs(stored / row["prev_close"] - 1) > READJUST_TOLERANCE:
            refetch[row["code"]] = "readjusted"
        else:
            keep.append(row)
    # Kline rows carry no turnover, so every stored bar uses the same estimate
    # (features._amount_estimate); mixing in the quote's real amount would shift
    # 20-day averages and the market liquidity ratio on the days it appears.
    bars = (
        pl.DataFrame(keep, schema=tencent.QUOTE_BAR_SCHEMA)
        .select(list(tencent.BAR_SCHEMA))
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    )
    return bars, refetch


def _daily(store: Warehouse, request: SyncRequest, client: httpx.Client, tracker: _Tracker) -> str:
    _check_years(request.years)
    today = date.today()
    cutoff = today - timedelta(days=DAILY_LOOKBACK_DAYS)
    full_start = today - timedelta(days=365 * request.years)
    wanted = _valid_codes(request.codes)

    tracker.update(message="正在刷新证券快照…")
    securities = _securities(client, wanted)
    if not securities.is_empty():
        store.write("securities", securities)

    span = _stored_span(store)
    if span and stored_method(store) != BARS_METHOD[BAR_SOURCE]:
        raise SyncFailure(
            "库内日线的复权方式与当前来源不同（已改用新浪按比例前复权），日更会混用两种口径；"
            "请先运行一次全市场回填：python scripts/pulse.py sync --kind backfill --wait-hours 24"
        )
    codes = wanted or sorted(set(span) | set(securities["code"].to_list()))
    if not codes:
        raise SyncFailure("仓库里没有任何股票，请先执行回填")
    tail = _recent_closes(store, cutoff)

    cut = session_cut()
    index_lazy = store.scan("index_bars")
    index_last = index_lazy.select(pl.col("date").max()).collect().item() if index_lazy is not None else None
    # Indices restart from their own last bar, so a long pause leaves no hole.
    index_start = min(index_last, cutoff) if index_last is not None else full_start
    tracker.update(message="正在更新指数…", total=len(codes), done=0)
    index_ok, index_errors, _ = _sync_indices(store, client, index_start, today, cut)

    stored_codes = [code for code in codes if code in span]
    tracker.update(message=f"正在读取 {len(stored_codes)} 只股票的收盘快照…")
    quotes = tencent.fetch_quote_bars(stored_codes, client) if stored_codes else pl.DataFrame(schema=tencent.QUOTE_BAR_SCHEMA)
    appended, refetch = plan_daily(_complete(quotes, cut), tail)
    writer = _BatchWriter(store, cut)
    if not appended.is_empty():
        writer.add(appended)
    for code in codes:
        if code not in span:
            refetch[code] = "new"

    blocked: list[Exception] = []  # the first refusal; later workers return it without a request

    def work(code: str) -> tuple[str, pl.DataFrame | None, str, Exception | None]:
        """(code, frame, how, error) with how in new / readjusted / gap."""
        how = refetch[code]
        if blocked:
            return code, None, "error", blocked[0]
        try:
            stored = span.get(code)
            if stored is None:
                return code, _bars().fetch_bars(code, full_start, today, client), "new", None
            if how == "readjusted":
                return code, _bars().fetch_bars(code, stored[0], today, client), "readjusted", None
            if stored[1] < cutoff:
                # Paused for longer than the overlap window: no stored closes to check the
                # adjustment basis against, so take the whole history again.
                return code, _bars().fetch_bars(code, stored[0], today, client), "gap", None
            recent = _bars().fetch_bars(code, cutoff, today, client)
            if not recent.is_empty() and _readjusted(tail.get(code, {}), recent, stored[1]):
                return code, _bars().fetch_bars(code, stored[0], today, client), "readjusted", None
            return code, recent, "gap", None
        except Exception as exc:
            if isinstance(exc, tencent.ProviderBlocked):
                blocked.append(exc)
            return code, None, "error", exc

    breaker = _Breaker()
    done = errors = 0
    counts = {"new": 0, "readjusted": 0, "gap": 0}
    first_error = ""
    tracker.update(message=f"收盘快照写入 {appended.height} 只；K 线补拉 {len(refetch)} 只…", total=max(1, len(refetch)))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, code) for code in sorted(refetch)]
        for future in as_completed(futures):
            if future.cancelled():
                continue
            code, frame, how, error = future.result()
            done += 1
            reason = breaker.trip(error)
            if reason:
                raise _abort(futures, writer, reason, breaker.blocked)
            if error is not None:
                errors += 1
                first_error = first_error or f"{code}：{error}"
            else:
                counts[how] += 1
                if frame is not None:
                    writer.add(frame)
            tracker.update(done=done, errors=errors + index_errors, message=f"K 线补拉 {done}/{len(refetch)}")
    writer.flush()

    if writer.rows == 0 and errors > 0:
        raise SyncFailure(f"日更失败：{errors} 只股票全部请求失败（{first_error}）")
    parts = [
        f"日更完成：{len(codes)} 只股票，收盘快照写入 {appended.height} 只，共 {writer.rows} 行 / {writer.files} 个文件",
        f"前复权重算重拉 {counts['readjusted']} 只，新增 {counts['new']} 只，补缺口 {counts['gap']} 只",
        f"指数 {index_ok}/{len(tencent.INDEX_TICKERS)}",
    ]
    if errors or index_errors:
        parts.append(f"失败 {errors} 只股票、{index_errors} 个指数" + (f"（例：{first_error}）" if first_error else ""))
    return "；".join(parts)


# --- cli -------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Tencent daily bars into the live warehouse")
    parser.add_argument("--kind", choices=["backfill", "daily"], default="daily")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--codes", default="", help="comma separated 6-digit codes; empty means the whole market")
    parser.add_argument(
        "--wait-hours", type=float, default=0.0,
        help="after a WAF refusal, wait for the provider to reopen and resume, for at most this many hours",
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Pipes (CI, subprocess captures) are read as UTF-8; terminals keep their own codec.
            stream.reconfigure(errors="replace", **({} if stream.isatty() else {"encoding": "utf-8"}))
    request = SyncRequest(
        kind=args.kind,
        years=args.years,
        codes=[code for code in args.codes.split(",") if code.strip()] or None,
    )
    seen = {"step": -1, "message": ""}

    def show(job: SyncJob) -> None:
        step = job.done // 100 if job.total > 200 else job.done
        if job.status == "running" and step == seen["step"] and job.message[:4] == seen["message"]:
            return
        seen["step"], seen["message"] = step, job.message[:4]
        print(f"[{job.status}] {job.done}/{job.total} ({job.progress:.0%}) err={job.errors} {job.message}", flush=True)

    deadline = monotonic() + args.wait_hours * 3600
    while True:
        tracker = _Tracker(request.kind, show)
        result = _execute(request, tracker)
        print(f"[{result.status}] {result.message}", flush=True)
        if result.status == "done" or not tracker.blocked or not _wait_until_open(deadline):
            return 0 if result.status == "done" else 1


def _wait_until_open(deadline: float) -> bool:
    """Probe the kline host every PROBE_EVERY seconds until it answers or the deadline passes."""
    while monotonic() + PROBE_EVERY <= deadline:
        sleep(PROBE_EVERY)
        with _client() as client:
            if _bars().kline_open(client):
                print(f"[waiting] {datetime.now(CST):%H:%M} 接口已恢复，继续回填", flush=True)
                return True
        print(f"[waiting] {datetime.now(CST):%H:%M} 仍被拒绝，{PROBE_EVERY / 60:.0f} 分钟后再试", flush=True)
    return False


if __name__ == "__main__":
    sys.exit(main())
