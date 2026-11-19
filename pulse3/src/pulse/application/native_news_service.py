"""PostgreSQL sink of the native news recorder: message versions -> document revisions -> claims (lane C, rows #131/#187).

Why: the native recorder must land in the SAME document store as the legacy import (``intelligence.document_revision``), through the
same ``IntelligenceService`` protocol (raw bytes to the lake first, batch RAW_COMMITTED, then append-only revisions), so claim
extraction and the alert scan read native news without any change. Only the source id differs: ``native_news_recorder`` /
``news_messages`` instead of ``legacy_news_recorder``; ``source_record_id`` is the same ``<origin>:<item id>``, the raw object is the same
lossless ``legacy-news-revision-v1`` JSON (one per message version), ``first_seen_at`` = our poll's response time and ``available_at``
= the write time.
Invariants: autocommit connection, short explicit transactions (as ``IntelligenceService``); one batch per write with the partition
``poll:<origin>`` (health and cursors are per feed); a stored revision is never rewritten (the recorder asks ``previous`` before it
sends a version, and ``_normalize_news`` skips an unchanged record hash anyway); a block records health "blocked" (circuit OPEN) and
the recorder stops that host.
Not covered: the legacy import (``LegacyNewsImporter``, lane A), the alert scan.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Callable

from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.adapters.sources import legacy_recordings as LR
from pulse.adapters.sources import native_news, registry
from pulse.adapters.sources.http import Capture
from pulse.application.data_service import Summary
from pulse.application.intelligence_service import IntelligenceService


class NativeNewsWriter(IntelligenceService):
    """``NewsSink`` over the pulse3 document store. Use one instance per recorder process."""

    def bootstrap(self) -> None:
        self.data.bootstrap()                              # registers every registry endpoint, including native_news_recorder/news_messages
        super().bootstrap()

    @property
    def spec(self):
        return registry.spec(native_news.SOURCE_ID, native_news.EP_MESSAGES)

    @staticmethod
    def partition(origin: str) -> str:
        return f"poll:{origin}"

    # ---- NewsSink --------------------------------------------------------------------------------------------------
    def circuit_open(self, origin: str) -> str | None:
        """Why this feed must not be polled now (an open circuit inside its cool-down), else None."""
        st = ingest_repo.load_health(self.conn, self.spec, self.partition(origin))
        if st.circuit == "open" and st.next_attempt_at and self.clock() < st.next_attempt_at:
            return f"{st.last_error_code or 'blocked'} until {st.next_attempt_at.isoformat()}"
        return None

    def fetch(self, origin: str, call: Callable[[], Capture]) -> Capture:
        return self._fetch(self.spec, self.partition(origin), call)          # a block / transport failure is recorded in health, then re-raised

    def bad_answer(self, origin: str, exc: Exception, body: bytes) -> None:
        self.data.record_attempt(self.spec, self.partition(origin), "parse_error", error_code=str(exc)[:80])

    def blocked_page(self, origin: str, exc: Exception) -> None:
        """An HTML shell answered with HTTP 200: health "blocked" (circuit OPEN) exactly as a 403/429 would leave it."""
        self.data.record_attempt(self.spec, self.partition(origin), "blocked", error_code="html_page")

    def heartbeat(self, origin: str, newest: datetime | None) -> None:
        """A successful answer with nothing new: contact is healthy; freshness follows the newest item the feed itself shows."""
        self.data.record_attempt(self.spec, self.partition(origin), "success_data", new_records=0, newest_content_at=newest, content_expected_now=True,
                                 canonical_ready=False)

    def previous(self, origin: str, item_id: str) -> dict | None:
        """Content hash and revision id of the stored latest state of an item, read back from its raw object; None if never stored.

        The native store is asked first; across the cutover the item may only exist in the legacy import's store
        (``legacy_news_recorder``, same ``<origin>:<item id>``), whose latest state then counts as the previous state: an unchanged
        item is not stored again and a changed one supersedes the legacy revision. The hash is recomputed from the stored title and
        body (the recorder's own formula), never trusted from the file the legacy recorder wrote."""
        record_id = f"{origin}:{item_id}"
        for source_id in (native_news.SOURCE_ID, LR.NEWS_SOURCE_ID):
            row = repo.latest_document(self.conn, source_id, record_id)
            if row is None:
                continue
            raw = self.conn.execute("SELECT content_hash FROM ingest.raw_object WHERE raw_object_id=%s", (row["raw_object_id"],)).fetchone()
            doc = json.loads(self.lake.read_object(raw["content_hash"]).decode("utf-8"))
            digest = hashlib.sha256((str(doc.get("title") or "") + "\n" + str(doc.get("body") or "")).encode("utf-8")).hexdigest()
            revision_id = doc.get("revision_id") or f"{item_id}@{digest[:12]}"
            return {"content_hash": digest, "revision_id": revision_id, "revision": row["revision"], "source_id": source_id}
        return None

    def write(self, origin: str, messages: list[dict], cap: Capture) -> int:
        """Raw object per message version, then document revisions; returns the number of revisions created."""
        s = Summary(f"native-news {origin}")
        partition = self.partition(origin)
        batch_id, owner = self._batch(self.spec, partition)
        try:
            row = []
            for m in messages:
                nr = LR.parse_recorded_news(LR.recorded_news_bytes(m), native_news.SOURCE_ID).records[0]
                key = f"{nr.source_record_id}:{m['revision_id']}"
                version_cap = Capture(native_news.SOURCE_ID, native_news.EP_MESSAGES, key, cap.url, "GET", None, cap.status, LR.recorded_news_bytes(m),
                                      "application/json", m["received_at"])
                rid = self._store(self.spec, batch_id, version_cap, owner, published_at=nr.published_at, precision=nr.published_time_precision)
                row.append((nr, rid, m["received_at"]))
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            self._normalize_news(self.spec, partition, batch_id, row, s, False)
        except Exception:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="write_error")
            raise
        finally:
            self.lake.release(owner)
        return s.counts.get("documents_published", 0) + s.counts.get("documents_corrected", 0)

    def claims(self) -> dict[str, int]:
        """The same claim step the legacy import runs (``process_new_claims``): every document without a claim gets one."""
        total: dict[str, int] = {}
        for _ in range(40):
            part = self.process_claims(limit=500)
            for k, v in part.counts.items():
                total[k] = total.get(k, 0) + v
            if part.counts.get("documents_to_process", 0) < 500:
                break
        return total
