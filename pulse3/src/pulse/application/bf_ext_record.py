"""Native extended-intraday recorder: schedule, hosts, publish into ``ext_quote_snapshots`` (step 2, #104/#186; NOT scheduled before the M6 cutover).

Why: until the cutover the old system records ETFs, indices, CFFEX index futures, the A50, Hang Seng futures, CNH, gold and crude and pulse3 imports
the files (``LiveService.import_ext``). This recorder produces the same dataset (same id, contract and frame, see ``ext_quotes.EXT_FRAME_SCHEMA``)
from public endpoints with native lineage (``coverage.recorder = pulse_native_ext``; raw evidence under the sources ``native_ext_*``), so at the
cutover the source is switched and the consumers (fwdlog E077/E078/E089, seal-facts neighbours, analysis pages) change nothing.

Schedule (Asia/Shanghai, the old recorder's, ported):
* one "grid" poll per minute 5 s into the minute from 09:00 to 16:00; per host cadence, default 60 s for Tencent AND for CFFEX (the old code polled
  the exchange files every minute too; the ledger's E089 allows slowing CFFEX to 300 s if IF main-contract updates stay >= 4 min apart: that is
  ``Cadence.cffex_seconds``, effective in whole grid minutes);
* one "close" snapshot at 15:01:30 (all hosts, including the Eastmoney minute bars), one "end" fetch of the minute bars at 16:00:30;
* a weekend or a calendar holiday ends the run at once; a day whose index quotes carry no today's stamp on five polls after 09:31 ends as "not a
  trading day". ``until`` (default 16:10) is a hard stop.

Invariants:
* one request at a time (one client per host, <= 1 request/s, Eastmoney <= 1 per 3 s); a block (403/429/456/501, redirect, WAF/HTML page) closes THAT
  host for the rest of the run and is never retried, the other hosts go on; health of the endpoint is marked OPEN and the CLI exits 2;
* 20 polls' worth of failures in a row close a host too; a failure costs that request only (the next poll is the retry);
* every answer is stored as raw evidence before it is parsed (the CFFEX body with its ``#last-modified:`` first line); a refusal page is kept;
* publish: every ``flush_seconds`` a new dataset version holds the earlier parts plus the new one; the final flush merges everything into ONE part
  (sorted, unique on symbol / poll_at / kind, the LATER fetch of a minute bar wins) exactly like the old ``compact``; a failed publish keeps the buffer;
* NOTHING is published for a day until it is confirmed as a trading day: an index / ETF quote polled after 09:31 is stamped on the session date
  (the quote stamp is the authority; the calendar helper ``market_rules.holidays`` is only an early check and says "unknown" for 2026/2027). Until then
  the rows wait in memory; when the day is judged not a trading day (5 stale polls), or the run ends without a confirming quote, the buffer is dropped
  and nothing is published, also in the final flush. ``once`` follows the same rule;
* a restart on the same day continues: steps already done (close / end) are read back from the last version's coverage.
Not covered: scheduling (``deploy/windows/run_ext_record.ps1``, registered by the main thread at the cutover), tick data, the legacy file import.
"""
from __future__ import annotations

import logging
import shutil
import time as _time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import polars as pl

from pulse.adapters.lake import ObjectRef
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.sources import ext_quotes as X
from pulse.adapters.sources import registry
from pulse.adapters.sources.http import SourceBlocked, TransportFailure
from pulse.application.bf_live_service import DATASET_EXT, LiveService
from pulse.application.data_service import Summary
from pulse.domain.market_rules import holidays as H

LOG = logging.getLogger("pulse.bf_ext_record")
UTC = timezone.utc
CONTRACT_ID = "pulse.bf_live.ext_quote_snapshot"
CAPABILITY = "sampled_snapshots_estimated"

GRID_WINDOW = (time(9, 0), time(16, 0, 59))
CLOSE_WINDOW = (time(15, 1, 30), time(15, 4))
END_WINDOW = (time(16, 0, 30), GRID_WINDOW[1])
TRADING_CHECK_AFTER = time(9, 31)
STOP_AT = time(16, 10)
STALE_POLLS = 5                 # polls whose index quotes are not stamped today before the day counts as "not a trading day"
TRADING_SHARE = 0.5
MAX_FAILS_IN_ROW = 20           # per request of the host: limit = 20 * requests of that host
MAX_ERRORS = 50
MIN_FREE_GB = 5.0


@dataclass(frozen=True)
class Cadence:
    """How often each host is asked; every value is in seconds and takes effect in whole grid minutes (the grid is one minute)."""
    tencent_seconds: int = 60
    cffex_seconds: int = 60
    grid_offset_seconds: int = 5
    flush_seconds: float = 900.0
    tick_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.tencent_seconds < 60 or self.cffex_seconds < 60:
            raise ValueError("host cadence below 60 s is not supported (one request per host per grid minute at most)")
        if not 0 <= self.grid_offset_seconds < 60:
            raise ValueError("grid_offset_seconds must be within the minute")

    def every(self, host: str) -> int:
        return self.cffex_seconds if host == X.CFFEX_HOST else self.tencent_seconds


# --- schedule (pure, tested without a clock) ---------------------------------------------------------------------------------
@dataclass
class Clock:
    """What the loop has already done today."""
    last_grid: datetime | None = None
    done: set[str] = field(default_factory=set)


def slot(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0)


def due(now: datetime, clock: Clock, cadence: Cadence = Cadence()) -> list[str]:
    """Steps to run now: the minute poll once per minute from the offset on, the closing snapshot, the last fetch of the minute bars."""
    t = now.time()
    out: list[str] = []
    if GRID_WINDOW[0] <= t <= GRID_WINDOW[1] and now.second >= cadence.grid_offset_seconds and clock.last_grid != slot(now):
        out.append("grid")
    if "close" not in clock.done and CLOSE_WINDOW[0] <= t <= CLOSE_WINDOW[1]:
        out.append("close")
    if "end" not in clock.done and END_WINDOW[0] <= t <= END_WINDOW[1]:
        out.append("end")
    return out


def finished(now: datetime) -> bool:
    """The grid window is over: nothing more will become due."""
    return now.time() > GRID_WINDOW[1]


def missed(previous: datetime | None, current: datetime) -> int:
    """Minute slots skipped before ``current``; a first poll counts the minutes since the window opened."""
    base = previous or datetime.combine(current.date(), GRID_WINDOW[0]) - timedelta(minutes=1)
    return max(0, int((current - base).total_seconds() // 60) - 1)


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ExtRecorder:
    """Polls the three hosts on the old schedule and publishes the day's ``ext_quote_snapshots`` versions (NOT scheduled)."""

    def __init__(self, svc: LiveService, fetcher: X.ExtFetcher, *, cadence: Cadence | None = None, symbols: list[str] | None = None,
                 is_trading_day: Callable[[date], bool] | None = None, sleep: Callable[[float], None] | None = None, min_free_gb: float = MIN_FREE_GB):
        self.svc, self.fetcher = svc, fetcher
        self.cadence = cadence or Cadence()
        self._symbols = symbols
        # early check only: a weekend or a closure of a COMPLETE official schedule (none is entered for 2026/2027 yet: unknown -> go on);
        # the authority is the quote stamp (``confirmed``): nothing is published for a day until an index quote is stamped that day
        self.is_trading_day = is_trading_day or (lambda d: H.is_open(d) is not False)
        self.sleep = sleep or _time.sleep
        self.min_free_gb = min_free_gb
        self.specs = {h: registry.spec(*X.HOST_ENDPOINT[h]) for h in X.HOSTS}
        self.clock = Clock()
        self.buffer: list[dict[str, Any]] = []
        self.caps: list = []
        self.rows_by_host: dict[str, int] = {}
        self.latest_a_share: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.fails: dict[str, int] = {}
        self.last_host_slot: dict[str, datetime] = {}
        self.seq = 0
        self.disk_low = False
        self.restarted = False
        self.confirmed = False              # an index / ETF quote stamped on the session date was seen: only then is anything published
        self.day: date | None = None
        self.stats: dict[str, Any] = {"polls": {}, "rows": {}, "missed": 0, "failed_requests": 0, "error_count": 0, "errors": [], "blocked": {}}
        self.last_flush = self.svc.clock()

    # ---- helpers -------------------------------------------------------------------------------------------------
    def now_cst(self) -> datetime:
        """Wall clock as a naive Asia/Shanghai datetime: the only form the schedule and the row timestamps use."""
        return self.svc.clock().astimezone(X.CST).replace(tzinfo=None)

    @property
    def partition(self) -> str:
        return f"record:{self.day}"

    def plan(self, day: date) -> dict[str, list[X.Request]]:
        return X.request_plan(self._symbols if self._symbols is not None else X.all_symbols(day))

    def _error(self, text: str) -> None:
        self.stats["error_count"] += 1
        if len(self.stats["errors"]) < MAX_ERRORS:
            self.stats["errors"].append(f"{self.now_cst():%H:%M:%S} {text}"[:300])

    def _count(self, key: str, name: str, n: int = 1) -> None:
        self.stats[key][name] = self.stats[key].get(name, 0) + n

    def _blocked(self, host: str, exc: Exception | str) -> None:
        self.stats["blocked"][host] = f"{self.now_cst():%H:%M:%S} {exc}"
        LOG.error("ext record: %s stopped for the run: %s", host, exc)

    def _failed(self, host: str, exc: Exception | str, plan: dict[str, list[X.Request]]) -> None:
        self.stats["failed_requests"] += 1
        self.fails[host] = self.fails.get(host, 0) + 1
        self._error(f"{host}: {exc}")
        LOG.warning("ext record: %s request failed: %s", host, exc)
        limit = MAX_FAILS_IN_ROW * len(plan.get(host, ()))
        if self.fails[host] >= limit > 0:
            self.fetcher.close_host(host, f"{limit} failures in a row")
            self._blocked(host, f"{limit} failures in a row, last: {exc}")

    def check_disk(self) -> bool:
        """False (and sticky) once the lake's disk is below the floor."""
        if not self.disk_low and shutil.disk_usage(self.svc.lake.root).free / 1e9 < self.min_free_gb:
            self.disk_low = True
            LOG.error("ext record: free disk below %s GB, stopping", self.min_free_gb)
        return not self.disk_low

    # ---- one request / one poll ---------------------------------------------------------------------------------------
    def _request(self, req: X.Request, kind: str, poll_ts: datetime, plan: dict[str, list[X.Request]]) -> list[dict[str, Any]]:
        host, spec = req.host, self.specs[req.host]
        try:
            cap = self.svc.data._fetch(spec, self.partition, lambda: self.fetcher.fetch(req))
        except X.RefusedAnswer as exc:                      # the refusal page is evidence too
            self.caps.append(exc.capture)
            self._blocked(host, exc)
            return []
        except SourceBlocked as exc:
            self._blocked(host, exc)
            return []
        except TransportFailure as exc:
            self._failed(host, exc, plan)
            return []
        self.caps.append(cap)
        try:
            rows = X.parse_capture(host, cap.body, kind, poll_ts)
            problem = None if rows else "no quotes parsed"
        except Exception as exc:  # noqa: BLE001 - a malformed answer must not cost the other hosts their poll
            rows, problem = [], f"parse: {exc!r}"
        if problem:
            self.svc.data.record_attempt(spec, self.partition, "parse_error", error_code=problem[:60])
            self._failed(host, problem, plan)
            return []
        self.fails[host] = 0
        return rows

    def _planned(self, kind: str, plan: dict[str, list[X.Request]], poll_slot: datetime) -> list[X.Request]:
        out: list[X.Request] = []
        for host, reqs in plan.items():
            if self.fetcher.blocked(host):
                continue
            if kind == "grid":
                last = self.last_host_slot.get(host)
                if host == X.EM_HOST or (last is not None and (poll_slot - last).total_seconds() < self.cadence.every(host)):
                    continue
                self.last_host_slot[host] = poll_slot
            elif kind == "end" and host != X.EM_HOST:
                continue
            out += reqs
        return out

    def poll(self, kind: str, now: datetime | None = None, plan: dict[str, list[X.Request]] | None = None) -> list[dict[str, Any]]:
        """This step's requests, one after the other; returns the rows recorded. The minute bars are not part of the grid and the "end" step
        fetches nothing else. A refusal closes the host, so the rest of its queue in this poll is not sent."""
        now = now or self.now_cst()
        plan = plan if plan is not None else self.plan(now.date())
        if not self.check_disk():
            return []
        rows: list[dict[str, Any]] = []
        for req in self._planned(kind, plan, slot(now)):
            if self.fetcher.blocked(req.host):
                continue
            got = self._request(req, kind, now, plan)
            self.rows_by_host[req.host] = self.rows_by_host.get(req.host, 0) + len(got)
            self._count("rows", req.host, len(got))
            rows += got
        self.latest_a_share = [r for r in rows if r["asset"] in ("etf", "index")]
        self.buffer.extend(rows)
        self.seen |= {r["symbol"] for r in rows if r["price"] is not None}
        self._count("polls", kind)
        return rows

    def trading_today(self) -> bool | None:
        """Are this poll's index / ETF quotes stamped today? False on an unlisted holiday (the feed repeats the last session); None without a
        usable stamp. Overseas quotes do not count: they carry today's date on a Chinese holiday too."""
        stamps = [r["quote_ts"].date() for r in self.latest_a_share if r["quote_ts"] is not None]
        return sum(d == self.day for d in stamps) / len(stamps) >= TRADING_SHARE if stamps else None

    # ---- publish ------------------------------------------------------------------------------------------------------
    def flush(self, day: date, s: Summary, *, final: bool = False, reason: str | None = None) -> None:
        """Publish the buffered rows as a new dataset version; ``final`` merges all parts of the day into one (old ``compact``)."""
        svc = self.svc
        if not self.confirmed:              # the buffer waits (a holiday's repeated quotes must never leave this process)
            return
        prev = svc.latest_ready(DATASET_EXT, day)
        prev_parts: dict[str, dict] = dict((prev["coverage_document"].get("parts") or {}) if prev else {})
        new_df, name = None, None
        if self.buffer:
            self.seq += 1
            name = f"native/part-{self.now_cst():%H%M%S}-{self.seq:05d}.parquet"
            new_df = X.ext_frame(self.buffer, day, name).unique(subset=X.SORT_KEYS, keep="last", maintain_order=True)
        compact = final and len(prev_parts) + (new_df is not None) > 1
        if new_df is None and not compact:
            self.last_flush = svc.clock()
            return
        now = svc.clock()
        spec = self.specs[X.QUOTE_HOST]
        batch_id = ingest_repo.create_batch(svc.conn, spec, f"{self.partition}:{now:%H%M%S}", now)
        owner = f"job:{batch_id}"
        try:
            for cap in self.caps:
                svc.data.store_capture(batch_id, cap, owner)
            with svc.conn.transaction():
                ingest_repo.commit_raw(svc.conn, batch_id, svc.clock())
            members: dict[str, ObjectRef] = {}
            parts: dict[str, dict] = {}
            if compact:
                frames = ([svc.lake.read_snapshot_frames(prev["content_hash"])] if prev else []) + ([new_df] if new_df is not None else [])
                name = "native/quotes_ext.parquet"
                merged = (pl.concat(frames, how="diagonal_relaxed").unique(subset=X.SORT_KEYS, keep="last", maintain_order=True)
                          .sort(X.SORT_KEYS).with_columns(pl.lit(name).alias("source_part")))
                ref = svc.lake.put_parquet(merged, owner=owner)
                members[name] = ref
                parts[name] = {"stream": "native", "source_sha256": ref.sha256, "object_sha256": ref.sha256, "rows": merged.height, "source_size": ref.size}
            else:
                old = {}
                if prev:
                    old = {m["sha256"]: ObjectRef(m["sha256"], m["path"], m["size"], m.get("kind", "data")) for m in svc.lake.read_manifest(prev["content_hash"]).members}
                members = {k: old[v["object_sha256"]] for k, v in prev_parts.items()}
                parts = dict(prev_parts)
                ref = svc.lake.put_parquet(new_df, owner=owner)
                members[name] = ref
                parts[name] = {"stream": "native", "source_sha256": ref.sha256, "object_sha256": ref.sha256, "rows": new_df.height, "source_size": ref.size}
            total = sum(v["rows"] for v in parts.values())
            coverage = {"dataset": DATASET_EXT, "session_date": day.isoformat(), "parts": dict(sorted(parts.items())), "imported_at": _z(now),
                        "recorder": X.RECORDER_ID, "capability": CAPABILITY, "not_tick_data": True,
                        "source_complete": bool(final and reason == "done"), "end_reason": reason if final else None,
                        "available_at_policy": "publication time of this version", "times": "poll_at / quote_at are UTC converted from Asia/Shanghai times",
                        "cadence_seconds": {"tencent": self.cadence.tencent_seconds, "cffex": self.cadence.cffex_seconds,
                                            "eastmoney": "close and end steps only"},
                        "steps_done": sorted(self.clock.done), "blocked_hosts": dict(self.stats["blocked"]),
                        "units": {h: X.ENDPOINT_POLICIES[X.HOST_ENDPOINT[h][1]]["units"] for h in X.HOSTS},
                        "recorder_summary": {k: self.stats[k] for k in ("polls", "rows", "missed", "failed_requests", "error_count")}}
            manifest = svc.lake.commit_manifest(DATASET_EXT, [members[k] for k in sorted(members)], owner=owner,
                                                meta={"row_count": total, "kind": "table", "session_date": day.isoformat(), "parts": sorted(members)})
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "NORMALIZED")
            pub = svc.data._stage_and_publish(DATASET_EXT, uuid.uuid4(), batch_id, manifest, CONTRACT_ID, coverage, owner, total, now, (), ())
        except Exception:
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "FAILED", failure_code="record_error")
            svc.lake.release(owner)
            raise
        for host, n in self.rows_by_host.items():
            if n and not self.fetcher.blocked(host):          # a host closed in this run keeps its OPEN health (nothing may be recorded over it)
                svc.data.record_attempt(self.specs[host], self.partition, "success_data", new_records=n, content_expected_now=False, canonical_ready=True)
        s.add("rows_published", new_df.height if new_df is not None else 0)
        s.add("flushes")
        s.counts["manifest_version"] = pub["manifest_version"]
        s.counts["session_rows_total"] = total
        self.buffer, self.caps, self.rows_by_host = [], [], {}
        self.last_flush = svc.clock()

    # ---- the day ------------------------------------------------------------------------------------------------------------
    def _restore(self, day: date) -> None:
        """A restart on the same day: steps already done come from the last native version; a host whose endpoint health is OPEN for this day stays closed."""
        prev = self.svc.latest_ready(DATASET_EXT, day)
        if prev and prev["coverage_document"].get("recorder") == X.RECORDER_ID:
            self.clock.done = set(prev["coverage_document"].get("steps_done") or ())
            self.restarted = True
            self.confirmed = True           # that version was only published after the day had been confirmed
        now = self.svc.clock()
        for host, spec in self.specs.items():
            h = ingest_repo.load_health(self.svc.conn, spec, self.partition)
            if h.circuit == "open" and h.next_attempt_at is not None and now < h.next_attempt_at:
                self.fetcher.close_host(host, f"health circuit open until {h.next_attempt_at.isoformat()}")
                self._blocked(host, "endpoint health is OPEN from an earlier block today; not asked again")

    def _advance(self, action: str, now: datetime) -> None:
        """Mark a step as taken before running it, so a failing step is not retried in a tight loop."""
        if action == "grid":
            previous, self.clock.last_grid = self.clock.last_grid, slot(now)
            self.stats["missed"] += missed(previous, self.clock.last_grid)
        else:
            self.clock.done.add(action)

    def _end_reason(self, now: datetime, until: time) -> str | None:
        if self.disk_low:
            return "disk low"
        if finished(now):
            return "done"
        return "stop time" if now.time() >= until else None

    def _loop(self, day: date, until: time, s: Summary) -> str:
        plan = self.plan(day)
        verified, stale = False, 0
        while True:
            now = self.now_cst()
            reason = self._end_reason(now, until)
            if reason:
                return reason
            for action in due(now, self.clock, self.cadence):
                self._advance(action, now)
                try:
                    self.poll(action, now, plan)
                except Exception as exc:  # noqa: BLE001 - one bad answer must not end the day
                    LOG.exception("ext record: poll %s failed", action)
                    self._error(f"{action}: {exc!r}")
                if action == "grid" and not verified and now.time() >= TRADING_CHECK_AFTER:
                    verdict = self.trading_today()
                    verified, stale = verdict is True, stale + (verdict is False)
                    self.confirmed = self.confirmed or verified
                    if stale >= STALE_POLLS:           # one stale answer proves nothing: a cache may repeat yesterday
                        return "quotes not stamped today: not a trading day"
            if (self.svc.clock() - self.last_flush).total_seconds() >= self.cadence.flush_seconds and self.buffer:
                try:
                    self.flush(day, s)
                except Exception as exc:  # noqa: BLE001 - the rows stay in the buffer, the next flush retries
                    LOG.exception("ext record: flush failed")
                    self._error(f"flush: {exc!r}")
                    s.add("flush_errors")
                    self.last_flush = self.svc.clock()
            self.sleep(self.cadence.tick_seconds)

    def _confirm_from_buffer(self, after: time | None = TRADING_CHECK_AFTER) -> bool:
        """The buffered index / ETF quotes polled at ``after`` or later: at least half are stamped on the session date."""
        stamps = [r["quote_ts"].date() for r in self.buffer if r["asset"] in ("etf", "index") and r["quote_ts"] is not None
                  and (after is None or r["poll_ts"].time() >= after)]
        return bool(stamps) and sum(d == self.day for d in stamps) / len(stamps) >= TRADING_SHARE

    def _settle(self, reason: str, s: Summary) -> bool:
        """Before the last flush: is this session confirmed as a trading day? If not, the buffer is dropped and nothing is published."""
        if not reason.startswith("quotes not stamped") and not self.confirmed:
            self.confirmed = self._confirm_from_buffer()
        if reason.startswith("quotes not stamped"):
            self.confirmed = False
        if self.confirmed:
            return True
        n = len(self.buffer)
        self.buffer, self.caps, self.rows_by_host = [], [], {}
        if n:
            s.notes.append(f"not published: {n} rows dropped, the index quotes were not stamped {self.day} (holiday or no usable quote)")
        s.counts["rows_dropped_unconfirmed"] = n
        return False

    def _summarise(self, day: date, reason: str, s: Summary) -> None:
        s.counts.update({f"polls_{k}": v for k, v in self.stats["polls"].items()})
        s.counts.update({"rows_polled": sum(self.stats["rows"].values()), "failed_requests": self.stats["failed_requests"],
                         "missed_slots": self.stats["missed"], "hosts_blocked": len(self.stats["blocked"]), "http_requests": self.fetcher.requests_made})
        never = sorted(set(self._symbols if self._symbols is not None else X.all_symbols(day)) - self.seen)
        s.notes.append(f"end: {reason}" + (" (restarted)" if self.restarted else ""))
        if never:
            s.notes.append(f"never returned a price: {', '.join(never)}")
        s.notes += [f"blocked {h}: {why}" for h, why in self.stats["blocked"].items()]
        s.notes += self.stats["errors"][:5]

    def run(self, day: date, until: time = STOP_AT) -> Summary:
        """Record one session on the schedule until ``until`` (CST) or the end of the window."""
        s = Summary(f"ext-record {day} until {until:%H:%M}")
        self.day = day
        if not self.is_trading_day(day):
            s.notes.append(f"{day} is not a trading day: nothing recorded")
            return s
        self._restore(day)
        reason = "crash"
        try:
            reason = self._loop(day, until, s)
            if reason == "done" and not self.stats["polls"]:
                reason = "nothing recorded: started after the window"
        finally:
            try:
                if self._settle(reason, s):
                    self.flush(day, s, final=True, reason=reason)
            finally:
                self._summarise(day, reason, s)
        return s

    def once(self, day: date, *, publish: bool = True) -> tuple[Summary, list[dict[str, Any]]]:
        """Smoke test: one poll of every host now (rows are kind "once"). With ``publish`` they are written into the day's dataset."""
        s = Summary(f"ext-once {day}")
        self.day = day
        rows = self.poll("once", plan=self.plan(day))
        if publish and self._settle("once", s) is False:
            publish = False
        if publish:
            self.flush(day, s, final=True, reason="once")
        else:
            self.buffer, self.caps, self.rows_by_host = [], [], {}
        self._summarise(day, "once", s)
        return s, rows
