"""Native intraday recorder: main-board snapshots into ``quote_snapshots_60s``, Tencent primary, optional Sina fallback (NOT scheduled).

Why: until the M6 cutover the old system records every session and pulse3 imports its files. At the cutover this recorder takes over and
publishes the SAME dataset in the SAME frame schema (``intraday_rows.rows_to_frame`` runs the casts of ``LegacyRecordings.read_quotes``), so the
seal facts, forward logs and analysis pages need no change; only the lineage differs (batches belong to ``pulse_intraday_recorder``).
Rows: LEGACY_TRIAGE #103/#185 (recorder), #119 (second source with fallback). Ported from the old ``warehouse/intraday.py`` schedule:
09:15-09:25 full snapshot every 60 s and the 09:25:10 result (auction), the static pool every 5 s from 09:19:30 to 09:25:10, 09:30-11:30 and
13:00-15:00 full every 60 s and the hot subset every 10 s, 15:01:30 close, 15:05-15:30 after-hours fixed-price session (static pool + hot subset
every 60 s, whole board at 15:05:05, 15:15 and 15:30:05); nothing in the 11:30-13:00 break; the loop ends at ``until``.
Vendors: Tencent (qt.gtimg.cn) is the PRIMARY because the old recorder polled it and it is the only vendor that carries every legacy
column (outer/inner lots, turnover, caps, volume ratio, vwap, the vendor's own limit prices incl. ST names, after-hours fields). Sina is the
OPTIONAL fallback (opt-in, ``--fallback sina``; off by default because hq.sinajs.cn already answered 403 to the server IP on 2026-11-19, so
a second host is not assumed to be available): no outer/inner lots, turnover, caps, volume ratio or vwap, and limit prices from the
price-limit rules (NULL where unverified). Without a fallback a block of Tencent stops the run.
Cost of the 1 request/s per host limit with Tencent (80 symbols per request, the old recorder's batch; the old one ran 10 requests/s): a
full main-board snapshot (~3100 codes) takes about 39 s, so a full board plus the hot polls no longer fit every 10 s grid slot. Hot polls
are cut by their 8 s budget (about 640 names; the rest wait for the next slot, counted as ``truncated_hot``) and grid slots lost while a
full board runs are counted as ``missed_hot`` / ``missed_auction_hot`` in the summary. Nothing is hidden: the 60 s boards stay complete.
Invariants:
* requests: one client per vendor, each at most 1 request per second; the first block (HTTP 403/429/456/501, or an HTML body) or 10
  transport failures in a row take that vendor out for the rest of the session; with a fallback configured the SAME symbols are re-sliced to
  the other vendor's batch size (never above its cap) and asked from it; a block is never retried and a vendor that is out is never asked
  again; when no vendor is left the run stops (``AllSourcesDown``, exit code 2);
* point in time: ``poll_at`` of a row is when ITS batch's response arrived (the capture's ``fetched_at``), not when the board started: at
  1 request/s the last batches of a Tencent board are known 30-40 s after the first;
* every response is stored as raw evidence under its vendor id; every published part names its vendor (``parts[...]["vendor"]``);
* trading-day check on the quote dates: rows stamped today verify the day at any time (so auction rows are kept if every source is blocked
  right after); a whole-board poll after 09:25:10 whose quotes carry an earlier day means a holiday: the run stops and publishes nothing, so a
  stale session is never filed under today's date; nothing is published before the day is verified;
* values a vendor does not carry stay NULL (see ``intraday_rows``); nothing is filled from the other vendor or derived;
* unpublished rows survive a failed flush and are retried; a flush that fails at the end raises.
Flush cadence: the CLI publishes a part every 2 full cycles (2 minutes) because ``native_quotes`` (tape / actionability) reads the newest part and refuses
quotes older than 3 minutes. Every publish re-validates the whole day's members (``DataService._validate_lake_rows``), so a flush late in the day costs a
few seconds of reading; the poll loop pauses for that time.
Optional step ``limit_pools`` (``--limit-pools``, off by default): the vendor's limit-up / broken-board / limit-down pools once after the close
(15:25-15:50, like the old recorder) into the separate dataset ``limit_pools_eastmoney`` (see ``intraday_pools``); its own client, a block stops
only that host and never the quote recording.
Not covered: scheduling, restart state (a restarted run repeats the one-shot steps whose window is still open), Eastmoney pools, minute bars,
convertible bonds, news-linked names; the static pool comes from the earlier sessions this recorder (or the import) published, so it is empty on
the first day after the cutover and the hot subset then grows from the 09:30 polls.
"""
from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Any, Callable

from pulse.adapters.lake import ObjectRef
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.sources import native_intraday as N
from pulse.adapters.sources import registry, sina, tencent
from pulse.adapters.sources.http import Capture, RateLimitedClient, SourceBlocked, TransportFailure
from pulse.application import intraday_rows as R
from pulse.application.bf_live_service import CST, DATASET_QUOTES, LiveService, _z
from pulse.application.data_service import Summary
from pulse.application.intraday_pools import LimitPoolRecorder
from pulse.domain.evidence.announcements import NotData, SchemaChanged

SYMBOL_PREFIX = {"6": "sh", "0": "sz", "3": "sz"}
MAX_FAILS_IN_ROW = 10
POLL_BUDGET = {"auction": 55.0, "auction_final": 55.0, "full": 55.0, "close": 55.0, "after": 55.0, "auction_hot": 4.0, "hot": 8.0, "after_hot": 8.0}
FIRST_FULL_AFTER_AUCTION = time(9, 25, 10)
VERIFY_ROWS = 20          # whole-board rows needed before the trading-day check (a few suspended names carry old stamps)
MAX_NOTES = 40


class AllSourcesDown(SourceBlocked):
    """No quote vendor is left (every one blocked or failing); the run stopped after flushing. ``summary`` is what was recorded."""

    summary: Summary | None = None


@dataclass
class Vendor:
    name: str
    client: RateLimitedClient
    adapter: Any
    spec: Any
    batch: int
    parse: Callable[[bytes], tuple[list[dict], list[str]]]
    make_row: Callable[[dict, str, datetime, date], dict]
    fails: int = 0
    down: str | None = None


def _vendor(name: str, client: RateLimitedClient, batch: int | None) -> Vendor:
    if name == "sina":
        return Vendor(name, client, sina.SinaAdapter(client), registry.spec("sina", "hq_quote"), batch or 200, sina.parse_hq_book,
                      lambda rec, kind, poll, day: R.sina_row(rec, kind, poll, day))
    if name == "tencent":
        return Vendor(name, client, tencent.TencentAdapter(client), registry.spec("tencent", "qt_quote"), batch or 80, tencent.parse_qt_book,
                      lambda rec, kind, poll, day: R.tencent_row(rec, kind, poll))
    raise ValueError("source must be sina or tencent")


def symbol_of(code: str) -> str | None:
    prefix = SYMBOL_PREFIX.get(code[:1])
    return None if prefix is None else prefix + code


def static_pool(svc: LiveService, day: date, sessions: int = 3) -> set[str]:
    """Names that touched a limit (day high at the limit-up or day low at the limit-down) in the last ``sessions`` published sessions
    before ``day``: the recorder's own data stands in for the old recorder's warehouse bars. Empty when nothing was published yet."""
    rows = svc.conn.execute(
        "SELECT DISTINCT ON (coverage_document->>'session_date') content_hash FROM market.dataset_manifest"
        " WHERE dataset_id=%s AND manifest_state='READY' AND coverage_document->>'session_date' < %s"
        " ORDER BY coverage_document->>'session_date' DESC, manifest_version DESC LIMIT %s", (DATASET_QUOTES, day.isoformat(), sessions)).fetchall()
    out: set[str] = set()
    for r in rows:
        out |= R.static_codes(svc.lake.read_snapshot_frames(r["content_hash"]))
    return out


class IntradayRecorder:
    """One recording day. ``client`` serves the primary vendor ``source``; ``fallback_client`` (optional) the other one."""

    def __init__(self, svc: LiveService, client: RateLimitedClient, *, source: str = "tencent", fallback_client: RateLimitedClient | None = None,
                 chunk: int | None = None, sleep: Callable[[float], None] | None = None, static: set[str] | None = None,
                 limit_pools: LimitPoolRecorder | None = None, tick: float = 0.5, max_fails: int = MAX_FAILS_IN_ROW, flush_seconds: float = 600.0):
        if source not in ("sina", "tencent"):
            raise ValueError("source must be sina or tencent")
        other = "tencent" if source == "sina" else "sina"
        self.svc, self.chunk, self.sleep, self.tick, self.max_fails, self.flush_seconds = svc, chunk, sleep or _time.sleep, tick, max_fails, flush_seconds
        self.vendors = [_vendor(source, client, chunk)] + ([_vendor(other, fallback_client, chunk)] if fallback_client is not None else [])
        self.active = self.vendors[0]
        self.source = source
        self.static = set(static or ())
        self.pools = limit_pools
        self.codes: list[str] = []
        self.spec = {R.STREAM_FULL: registry.spec(N.SOURCE_ID, N.EP_QUOTES), R.STREAM_HOT: registry.spec(N.SOURCE_ID, N.EP_HOT)}
        self.hot: set[str] = set(self.static)
        self.latest: dict[str, dict] = {}
        self.state = R.Clock()
        self.cfg = R.Schedule()
        self.s = Summary("intraday-record")
        self.day: date = date.min
        self._until = time(23, 59)
        self._buf: dict[tuple[str, str], list[dict]] = {}
        self._caps: list[Capture] = []
        self._stop: str | None = None
        self._verified = False
        self._seq = 0
        self._last_flush: datetime | None = None

    # ---- time -------------------------------------------------------------------------------------------------------------
    def _cst(self) -> datetime:
        return self.svc.clock().astimezone(CST).replace(tzinfo=None)

    def _late(self) -> bool:
        return self._cst().time() >= self._until

    def _note(self, text: str) -> None:
        if len(self.s.notes) < MAX_NOTES:
            self.s.notes.append(f"{self._cst():%H:%M:%S} {text}")

    # ---- vendor routing ---------------------------------------------------------------------------------------------------
    def _down(self, v: Vendor, reason: str) -> None:
        v.down = reason
        self._note(f"{v.name} out for the session: {reason}")
        nxt = next((x for x in self.vendors if x.down is None), None)
        if nxt is None:
            raise AllSourcesDown("all quote sources unavailable: " + "; ".join(f"{x.name}: {x.down}" for x in self.vendors))
        self.active = nxt
        self.s.add("source_switches")
        self._note(f"switched to {nxt.name} for the rest of the session")

    def _request(self, remaining: list[str]) -> tuple[int, tuple[Vendor, Capture, list[dict]] | None]:
        """One batch from the active vendor, sliced to ITS batch size from the symbols still to ask for (never more than that vendor's cap,
        also after a switch: a block moves to the next vendor, the same symbols are re-sliced for it and asked again at once).
        Returns (symbols consumed, result); the result is None when the batch failed but the vendor stays in (the next poll tries again)."""
        partition = f"record:{self.day}"
        while True:
            v = self.active
            symbols = remaining[:v.batch]
            try:
                cap = self.svc.data._fetch(v.spec, partition, lambda: v.adapter.quotes(symbols, limit=v.batch))
            except SourceBlocked as exc:
                self._down(v, f"blocked ({exc})")
                continue
            except TransportFailure as exc:
                self.s.add("transport_errors")
                v.fails += 1
                if v.fails >= self.max_fails:
                    self._down(v, f"{v.fails} transport failures in a row, last: {str(exc)[:80]}")
                    continue
                return len(symbols), None
            self.s.add("requests")
            if cap.body.lstrip()[:1] == b"<":                      # an HTML page instead of quotes is a refusal, like a 403
                self.svc.data.record_attempt(v.spec, partition, "blocked", error_code="html_body")
                self._down(v, "HTML body instead of quotes (block page)")
                continue
            try:
                records, _empty = v.parse(cap.body)
            except (NotData, SchemaChanged) as exc:
                self.s.add("parse_errors")
                v.fails += 1
                self._note(f"{v.name} body not parsed: {exc}")
                if v.fails >= self.max_fails:
                    self._down(v, f"{v.fails} unparsable bodies in a row")
                    continue
                return len(symbols), None
            v.fails = 0
            return len(symbols), (v, cap, records)

    # ---- polls ------------------------------------------------------------------------------------------------------------
    def _poll(self, kind: str, codes: list[str]) -> list[dict]:
        stream = R.STREAM_HOT if kind in R.HOT_KINDS else R.STREAM_FULL
        symbols = [s for s in (symbol_of(c) for c in sorted(codes)) if s]
        started = self.svc.clock()
        rows: list[dict] = []
        pos = 0
        while pos < len(symbols):
            if self._late():
                break
            if (self.svc.clock() - started).total_seconds() > POLL_BUDGET.get(kind, 55.0):
                self.s.add(f"truncated_{kind}")
                break
            used, got = self._request(symbols[pos:])
            pos += used
            if got is None:
                continue
            v, cap, records = got
            # poll_at is when THIS batch's response arrived: at 1 request/s the last batches of a board are known tens of seconds after the first
            poll_ts = cap.fetched_at.astimezone(CST).replace(tzinfo=None)
            part = [v.make_row(rec, kind, poll_ts, self.day) for rec in records]
            self._caps.append(cap)
            self._buf.setdefault((stream, v.name), []).extend(part)
            rows += part
            self._verify(rows, min(VERIFY_ROWS, len(symbols)), board=stream == R.STREAM_FULL)
            if self._stop is not None:
                break
        self.s.add(f"polls_{kind}")
        self.s.add("rows_polled", len(rows))
        return rows

    def _verify(self, rows: list[dict], need: int, *, board: bool) -> None:
        """Trading-day check on the quote dates. Rows stamped today verify the day at any time (the auction's own rows are then kept even if a
        block follows). Rows stamped with an earlier day mean a holiday only for a whole-board poll after the auction (before it, pre-open quotes
        legitimately carry the previous day): the run stops and everything buffered is dropped."""
        if self._verified or self._stop is not None or len(rows) < need:
            return
        if R.stamped_today(rows, self.day):
            self._verified = True
            return
        if not board or self._cst().time() < FIRST_FULL_AFTER_AUCTION:
            return
        self._stop = "quotes not stamped today: not a trading day"
        self._buf.clear()
        self._caps.clear()

    def _full(self, kind: str) -> None:
        rows = self._poll(kind, self.codes)
        if not rows or self._stop is not None:
            return
        self.latest = {r["code"]: r for r in rows}
        t = self._cst().time()
        if kind in ("full", "close") and t >= R.SESSIONS[0][0]:
            self.hot, capped = R.grow_hot(self.hot, rows)
            if capped:
                self._note(f"hot set capped at {R.HOT_CAP}")
        queues = [q for q in map(R.limit_queue, rows) if q is not None]
        self.s.counts["last_limit_up_names"] = sum(1 for side, _ in queues if side == "up")
        self.s.counts["last_limit_down_names"] = sum(1 for side, _ in queues if side == "down")

    def _hot(self, kind: str) -> None:
        codes = self.static if kind == "auction_hot" else self.static | self.hot if kind == "after_hot" else self.hot
        if codes:
            self._poll(kind, sorted(codes))

    def _step(self, action: str) -> None:
        if action in ("full", "auction_final", "close"):
            self._full("auction" if action == "full" and self._cst().time() < R.SESSIONS[0][0] else action)
        elif action in R.AFTER_SHOTS:
            self._full("after")
        elif action == "pools":
            self.pools.fetch_and_publish(self.day, self.s)
        else:
            self._hot(action)

    def _advance(self, action: str, now: datetime) -> None:
        """Mark a step as taken before running it, so a failing step is not retried in a tight loop."""
        grids = {"full": ("last_full", self.cfg.full_every), "hot": ("last_hot", self.cfg.hot_every),
                 "auction_hot": ("last_auction", self.cfg.auction_every), "after_hot": ("last_after", self.cfg.after_every)}
        if action in grids:
            attr, every = grids[action]
            current = R.slot(now, every)
            lost = R.missed(getattr(self.state, attr), current, every)
            if lost:
                self.s.add(f"missed_{action}", lost)
            setattr(self.state, attr, current)
        else:
            self.state.done.add(action)

    # ---- publishing -------------------------------------------------------------------------------------------------------
    def flush(self, final: bool = False) -> None:
        """Publish the buffered rows as new parts of today's dataset version (cumulative manifest); the buffers are kept on failure."""
        svc, day = self.svc, self.day
        buffered = {k: v for k, v in self._buf.items() if v}
        if not buffered:
            return
        now = svc.clock()
        stamp = f"{now.astimezone(CST):%H%M%S}"
        batch_id = ingest_repo.create_batch(svc.conn, self.spec[R.STREAM_FULL], f"record:{day}:{stamp}", now)
        owner = f"job:{batch_id}"
        try:
            for cap in self._caps:
                svc.data.store_capture(batch_id, cap, owner)
            with svc.conn.transaction():
                ingest_repo.commit_raw(svc.conn, batch_id, svc.clock())
            prev = svc.latest_ready(DATASET_QUOTES, day)
            parts: dict[str, dict] = dict((prev["coverage_document"].get("parts") or {}) if prev else {})
            old: dict[str, ObjectRef] = {}
            if prev:
                old = {m["sha256"]: ObjectRef(m["sha256"], m["path"], m["size"], m.get("kind", "data"))
                       for m in svc.lake.read_manifest(prev["content_hash"]).members}
            members = {k: old[v["object_sha256"]] for k, v in parts.items()}
            newest = None
            added = 0
            for (stream, vendor), rows in sorted(buffered.items()):
                self._seq += 1
                name = f"{stream}/native-{vendor}-{stamp}-{self._seq:04d}.parquet"
                df = R.rows_to_frame(rows, stream=stream, part=name, day=day)
                ref = svc.lake.put_parquet(df, owner=owner)
                members[name] = ref
                parts[name] = {"stream": stream, "vendor": vendor, "source_sha256": ref.sha256, "object_sha256": ref.sha256, "rows": df.height,
                               "source_size": ref.size}
                added += df.height
                top = df["quote_at"].max()
                newest = top if newest is None or (top is not None and top > newest) else newest
            total = sum(v["rows"] for v in parts.values())
            coverage = self._coverage(day, parts, now, final)
            manifest = svc.lake.commit_manifest(DATASET_QUOTES, [members[k] for k in sorted(members)], owner=owner,
                                                meta={"row_count": total, "kind": "table", "session_date": day.isoformat(), "parts": sorted(members)})
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "NORMALIZED")
            svc.data._stage_and_publish(DATASET_QUOTES, uuid.uuid4(), batch_id, manifest, "pulse.bf_live.quote_snapshot", coverage, owner, total, now, (), ())
        except Exception:
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "FAILED", failure_code="record_error")
            svc.lake.release(owner)
            raise
        svc.data.record_attempt(self.spec[R.STREAM_FULL], str(day), "success_data", new_records=added,
                                newest_content_at=newest if newest is not None and newest <= now else None, content_expected_now=False, canonical_ready=True)
        self.s.add("rows_published", added)
        self.s.add("flushes")
        self._buf, self._caps = {}, []
        self._last_flush = now

    def _coverage(self, day: date, parts: dict[str, dict], now: datetime, final: bool) -> dict:
        vendors = sorted({p["vendor"] for p in parts.values() if p.get("vendor")})
        return {"dataset": DATASET_QUOTES, "session_date": day.isoformat(), "parts": dict(sorted(parts.items())), "imported_at": _z(now),
                "source_complete": bool(final and self._stop is None), "recorder": N.SOURCE_ID, "vendors": vendors,
                "primary": self.source, "source_switches": self.s.counts.get("source_switches", 0),
                "capability": "sampled_snapshots_estimated", "sampled_interval_seconds_nominal": 60, "not_tick_data": True,
                "px_zero_is_missing": True, "available_at_policy": "publish time (the recorder publishes about every 10 minutes)",
                "times": "poll_at / quote_at are UTC converted from Asia/Shanghai vendor times",
                "units": "lots of 100 shares (Sina shares / 100), amount yuan, caps in 100 million yuan",
                "column_availability": N._COMMON["column_availability"],
                "limit_prices": "Tencent parts: vendor values; Sina parts: versioned price-limit rules (NULL when unverified)",
                "auction_book": "09:15-09:24 rows are as received: bid1 == ask1 is the indicative price, its size the matched quantity, "
                                "bid2/ask2 size the unmatched surplus (checked on the old recording; not verified for Sina)"}

    def _flush_due(self) -> bool:
        if not self._verified or not any(self._buf.values()):
            return False
        return self._last_flush is None or (self.svc.clock() - self._last_flush).total_seconds() >= self.flush_seconds

    # ---- run ----------------------------------------------------------------------------------------------------------------
    def run(self, codes: list[str], day: date, until: time, *, cycle_seconds: float = 60.0, flush_every: int = 10) -> Summary:
        self.s = s = Summary(f"intraday-record {day} until {until}")
        self.day, self._until, self.codes = day, until, sorted(c for c in codes if symbol_of(c))
        self.cfg = replace(self.cfg, full_every=int(cycle_seconds))
        self.flush_seconds = flush_every * cycle_seconds
        failure: AllSourcesDown | None = None
        try:
            while self._stop is None:
                now = self._cst()
                if now.time() >= until:
                    break
                steps = R.due(now, self.state, self.cfg)
                if self.pools is not None and R.pools_due(now, self.state):
                    steps.append("pools")
                for action in steps:
                    self._advance(action, now)
                    try:
                        self._step(action)
                    except AllSourcesDown:
                        raise
                    except Exception as exc:                       # one bad answer must not end the day; it is counted and noted
                        s.add("step_errors")
                        self._note(f"step {action} failed: {exc!r}"[:200])
                    if self._stop is not None:
                        break
                if self._flush_due():
                    try:
                        self.flush()
                    except Exception as exc:                       # rows stay buffered and the next flush retries
                        s.add("flush_errors")
                        self._note(f"flush failed: {exc!r}"[:200])
                if not steps:
                    self.sleep(self.tick)
        except AllSourcesDown as exc:
            failure = exc
            # a block inside the very first board still leaves rows worth keeping, if they are today's
            self._verify([r for (stream, _), rows in self._buf.items() if stream == R.STREAM_FULL for r in rows], 1, board=True)
            self._stop = self._stop or f"all sources down: {exc}"
        finally:
            if self._verified:
                self.flush(final=failure is None)               # also after a block: what was captured before it is kept
        if self._stop:
            s.notes.append(f"stopped: {self._stop}")
        s.counts["hot_names"] = len(self.hot)
        if failure is not None:
            failure.summary = s
            raise failure
        return s
