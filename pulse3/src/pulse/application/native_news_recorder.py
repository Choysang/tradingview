"""Native 7x24 news recorder core: poll Tonghuashun / Eastmoney / CLS, dedupe, keep revisions, hand messages to a sink (lane C).

Why: until the M6 cutover the old recorder (``backend/app/warehouse/news.py``) polls the three feeds and pulse3 imports its files
(``LegacyNewsImporter``). This is pulse3's own replacement, written and tested now, switched on only at the cutover: it polls the
same feeds at the old cadence (Tonghuashun 10 s, Eastmoney 30 s, CLS 30 s, 08:00-22:00 Asia/Shanghai) and produces the SAME
message records the legacy import produces (same keys, ``legacy-news-revision-v1`` raw object), so the document store, claim
extraction and the alert scan downstream need no change.

Message semantics (identical to the old recorder and to ``legacy_recordings.read_news``):
* one message per (origin, item id) VERSION; the content state is ``sha256(title + "\\n" + body)``; a new item is revision
  ``<item>@<hash12>`` with ``supersedes`` None; a changed state is a new revision that supersedes the previous revision id;
  an unchanged state is not recorded again (across polls and across restarts: the sink is asked for the stored state of an item
  the first time this process sees it);
* ``received_at`` = the response time of OUR poll (the only time trading may use, exactly the legacy file's ``received_at``);
  ``source_published_at`` / ``source_modified_at`` = the feed's own times, kept as aware UTC (``source_time_zone`` = "tz-aware":
  the feeds give epoch seconds or Asia/Shanghai text, so no zone has to be guessed as in the old naive files);
* ``part`` / ``part_sha256`` = the line of the day's raw answer file that carried the item and the sha256 of that answer's bytes.

Invariants: one ``RateLimitedClient`` per host (<= 1 request/s per host); a block (403/429/456/501 -> ``SourceBlocked``, or an HTML page
answered with HTTP 200) stops THAT host for the rest of the day at once, is never retried and is remembered in ``blocked.json`` so a restart the same day does not
touch the host again; 3 consecutive bad answers (HTML, vendor error, timeout, 5xx) also stop a feed for the day; a failed write
leaves the in-memory state unchanged, so the next poll tries the same items again; raw answers are kept (gzip JSON lines) only
for polls that produced new versions, before the sink sees the messages. Nothing here reads or writes the legacy data tree.
Not covered: scheduling (``deploy/windows/run_news_record.ps1`` is NOT registered before the cutover), guba follow-up fetches and
the old ``watch.json``, alert scanning and delivery (``pulse.workers.alerts``), and any HTTP retry.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from pulse.adapters.sources import cls_news, eastmoney_news, native_news, ths_news
from pulse.adapters.sources.http import Capture, RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.news_items import BadAnswer, BlockedPage

LOG = logging.getLogger("pulse.native_news")
UTC = timezone.utc
CST = ZoneInfo("Asia/Shanghai")
HEARTBEAT_SECONDS = 60.0  # an answer with nothing new still tells the health state the feed is alive, at most this often


@dataclass(frozen=True)
class Feed:
    origin: str                         # the old recorder's source id: "ths.724" / "em.724" / "cls.telegraph"
    every: int                          # seconds between polls
    fetch: Callable[[], Capture]        # one request through the host's own rate-limited client
    parse: Callable[[bytes], list[dict]]


class NewsSink(Protocol):
    """Where messages go. ``NativeNewsWriter`` (PostgreSQL document store) in production, an in-memory fake in unit tests."""

    def circuit_open(self, origin: str) -> str | None: ...
    def fetch(self, origin: str, call: Callable[[], Capture]) -> Capture: ...
    def bad_answer(self, origin: str, exc: Exception, body: bytes) -> None: ...
    def blocked_page(self, origin: str, exc: Exception) -> None: ...
    def heartbeat(self, origin: str, newest: datetime | None) -> None: ...
    def previous(self, origin: str, item_id: str) -> dict | None: ...
    def write(self, origin: str, messages: list[dict], cap: Capture) -> int: ...
    def claims(self) -> dict[str, int]: ...


def content_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()


def default_feeds(clients: dict[str, RateLimitedClient]) -> list[Feed]:
    """The three feeds at the old recorder's cadence; ``clients`` has one client per host (key = origin id)."""
    ths, em, cls = (clients[native_news.ORIGIN_THS], clients[native_news.ORIGIN_EM], clients[native_news.ORIGIN_CLS])
    return [Feed(native_news.ORIGIN_THS, native_news.POLL_EVERY_SECONDS[native_news.ORIGIN_THS], lambda: ths_news.ThsNewsAdapter(ths).page(1, 30),
                 ths_news.recorder_items),
            Feed(native_news.ORIGIN_EM, native_news.POLL_EVERY_SECONDS[native_news.ORIGIN_EM], lambda: eastmoney_news.EastmoneyNewsAdapter(em).page(None, 30),
                 eastmoney_news.recorder_items),
            Feed(native_news.ORIGIN_CLS, native_news.POLL_EVERY_SECONDS[native_news.ORIGIN_CLS], lambda: cls_news.ClsNewsAdapter(cls).page(None, 20),
                 cls_news.recorder_items)]


def build_message(origin: str, item: dict, digest: str, prior_revision: str | None, received: datetime, part: str, part_sha256: str) -> dict:
    """One message in exactly the shape ``LegacyRecordings.read_news`` returns (so ``recorded_news_bytes`` serializes both alike)."""
    return {"origin_source_id": origin, "item_id": item["item_id"], "revision_id": f"{item['item_id']}@{digest[:12]}", "supersedes": prior_revision,
            "content_hash": digest, "title": str(item["title"] or ""), "body": str(item["body"] or ""), "url": item["url"],
            "source_published_at": item["source_published_at"], "source_modified_at": item["source_modified_at"], "received_at": received,
            "codes": list(item["codes"]), "comments": item["comments"], "level": item["level"], "source_time_zone": "tz-aware",
            "part": part, "part_sha256": part_sha256}


@dataclass
class FeedState:
    feed: Feed
    last: datetime | None = None
    fails: int = 0
    stopped: str | None = None
    polls: int = 0
    new: int = 0
    revisions: int = 0
    heartbeat_at: datetime | None = None


@dataclass
class NewsRecorder:
    day_dir: Path
    sink: NewsSink
    feeds: list[Feed]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = _time.sleep
    break_after: int = native_news.BREAK_AFTER
    run_claims: bool = True
    known: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)   # (origin, item) -> (content hash, revision id)
    errors: list[str] = field(default_factory=list)
    claim_totals: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        (self.day_dir / "raw").mkdir(parents=True, exist_ok=True)
        self.states = [FeedState(f) for f in self.feeds]
        marker = self._blocked_today()
        for st in self.states:
            if st.feed.origin in marker:
                st.stopped = f"blocked earlier today: {marker[st.feed.origin]}"
            else:
                why = self.sink.circuit_open(st.feed.origin)
                if why:
                    st.stopped = f"circuit open: {why}"

    # ---- one poll --------------------------------------------------------------------------------------------------
    def poll(self, st: FeedState) -> int:
        """One request of one feed; returns the number of message versions written. Never raises for a bad answer or a block."""
        origin = st.feed.origin
        st.last, st.polls = self.clock(), st.polls + 1
        try:
            cap = self.sink.fetch(origin, st.feed.fetch)
        except SourceBlocked as exc:
            self._block(st, str(exc))
            return 0
        except TransportFailure as exc:
            self._bad(st, exc)
            return 0
        try:
            items = st.feed.parse(cap.body)
        except BlockedPage as exc:                         # an HTML shell with HTTP 200 is a block in disguise: stop the host now, no retry
            self.sink.blocked_page(origin, exc)
            self._block(st, "html_page")
            return 0
        except BadAnswer as exc:
            self.sink.bad_answer(origin, exc, cap.body)
            self._bad(st, exc)
            return 0
        st.fails = 0
        fresh = self._fresh(origin, items)
        if not fresh:
            self._idle(st, items)
            return 0
        received = cap.fetched_at
        part, sha = self._keep_raw(origin, cap)
        msgs = [build_message(origin, it, h, None if prior is None else prior[1], received, part, sha) for it, h, prior in fresh]
        written = self.sink.write(origin, msgs, cap)
        for m in msgs:                                    # only after the sink took them: a failed write is retried by the next poll
            self.known[(origin, m["item_id"])] = (m["content_hash"], m["revision_id"])
            if m["supersedes"] is None:
                st.new += 1
            else:
                st.revisions += 1
        if self.run_claims and written:
            for k, v in self.sink.claims().items():
                self.claim_totals[k] = self.claim_totals.get(k, 0) + v
        st.heartbeat_at = received
        return len(msgs)

    def _fresh(self, origin: str, items: list[dict]) -> list[tuple[dict, str, tuple[str, str] | None]]:
        out, seen = [], set()
        for it in items:
            if it["item_id"] in seen:
                continue
            seen.add(it["item_id"])
            digest = content_hash(it["title"] or "", it["body"] or "")
            prior = self.known.get((origin, it["item_id"]))
            if prior is None:                              # first sight in this process: ask the store (restart safety)
                stored = self.sink.previous(origin, it["item_id"])
                prior = (stored["content_hash"], stored["revision_id"]) if stored else None
                if prior is not None:
                    self.known[(origin, it["item_id"])] = prior
            if prior is not None and prior[0] == digest:
                continue
            out.append((it, digest, prior))
        return out

    def _idle(self, st: FeedState, items: list[dict]) -> None:
        now = self.clock()
        if st.heartbeat_at is not None and (now - st.heartbeat_at).total_seconds() < HEARTBEAT_SECONDS:
            return
        st.heartbeat_at = now
        published = [it["source_published_at"] for it in items if it["source_published_at"] is not None and it["source_published_at"] <= now]
        self.sink.heartbeat(st.feed.origin, max(published) if published else None)

    def _keep_raw(self, origin: str, cap: Capture) -> tuple[str, str]:
        sha = hashlib.sha256(cap.body).hexdigest()
        line = json.dumps({"received_at": cap.fetched_at.astimezone(UTC).isoformat(), "url": cap.url, "sha256": sha,
                           "body": cap.body.decode("utf-8", errors="replace")}, ensure_ascii=False)
        with gzip.open(self.day_dir / "raw" / f"{origin}.jsonl.gz", "at", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return f"raw/{origin}.jsonl.gz#{sha[:12]}", sha

    # ---- failure handling ------------------------------------------------------------------------------------------
    def _block(self, st: FeedState, reason: str) -> None:
        st.stopped = f"blocked: {reason}"
        self._error(f"{st.feed.origin} {st.stopped}: host stopped for the day, no retry")
        marker = self._blocked_today() | {st.feed.origin: reason}
        tmp = self.day_dir / "blocked.json.tmp"
        tmp.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.day_dir / "blocked.json")

    def _bad(self, st: FeedState, exc: Exception) -> None:
        st.fails += 1
        LOG.warning("news: %s bad answer %d/%d: %s", st.feed.origin, st.fails, self.break_after, exc)
        if st.fails >= self.break_after:
            st.stopped = f"{self.break_after} bad answers in a row: {exc}"
            self._error(f"{st.feed.origin} stopped for the day: {st.stopped}")

    def _blocked_today(self) -> dict[str, str]:
        try:
            doc = json.loads((self.day_dir / "blocked.json").read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _error(self, text: str) -> None:
        LOG.error("news: %s", text)
        if len(self.errors) < 50:
            self.errors.append(f"{self.clock().astimezone(CST):%H:%M:%S} {text}"[:300])

    # ---- loops -----------------------------------------------------------------------------------------------------
    def poll_safely(self, st: FeedState) -> None:
        try:
            self.poll(st)
        except Exception as exc:  # noqa: BLE001 - one bad write or answer must not end the day; the next poll retries the same items
            LOG.exception("news: poll %s failed", st.feed.origin)
            self._error(f"{st.feed.origin}: {type(exc).__name__}: {exc}")

    def once(self) -> str:
        """Every live feed exactly once (smoke run, ignores the daily window)."""
        for st in self.states:
            if not st.stopped:
                self.poll_safely(st)
        return self.finish("once")

    def run(self, *, start: time, until: time) -> str:
        reason = "until"
        try:
            while True:
                now = self.clock()
                t = now.astimezone(CST).time()
                if t >= until:
                    break
                if t < start:
                    self.sleep(30.0)
                    continue
                live = [st for st in self.states if not st.stopped]
                if not live:
                    reason = "all feeds stopped"
                    break
                for st in live:
                    if st.last is None or (now - st.last).total_seconds() >= st.feed.every:
                        self.poll_safely(st)
                self.sleep(0.5)
        finally:
            self.finish(reason)
        return reason

    @property
    def blocked(self) -> bool:
        return any((st.stopped or "").startswith("blocked") for st in self.states)

    def finish(self, reason: str) -> str:
        doc = {"end": reason, "finished_at": self.clock().astimezone(CST).isoformat(timespec="seconds"),
               "feeds": {st.feed.origin: {"polls": st.polls, "new": st.new, "revisions": st.revisions, "stopped": st.stopped} for st in self.states},
               "claims": self.claim_totals, "errors": self.errors}
        try:
            (self.day_dir / "summary.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            LOG.error("news: summary not written: %s", exc)
        return reason


def day_dir_for(root: Path, day: date) -> Path:
    return root / day.isoformat()
