"""Intelligence orchestration: news -> documents -> claims -> events; community; seats; fund flow (I3, spec FINAL §5/§7/§12).

Why: one place wires adapters, the pure intelligence domain, the model port and the repositories into the
recoverable protocol of database_design §5: raw bytes first (lake + ``ingest.raw_object`` + batch member), batch
RAW_COMMITTED, then normalization into append-only revisions, then cursor/health. Claim processing is a separate
step (``process_claims``) that re-reads the raw record, asks the model port (rules provider by default) for
enums-and-spans, validates them in code and derives numbers, origin votes, status and impact in code.

Invariants: no HTTP/SDK/psycopg here beyond the injected connection (autocommit; short explicit
transactions); the clock is injected; ``available_at`` = actual completion; the first block (403/429/456/501)
stops the run and opens the circuit via the shared health machine; a correction or withdrawal appends a
revision and the dependent claims/events get a new revision (SS-G12); unknown stays NULL/None.
Not covered: scheduling/daemons, PDF bodies, push channels.
"""
from __future__ import annotations

import io
import json
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.adapters.models.port import ManifestRef, ModelPolicy, ModelRequest, claim_extraction_policy, sha256_hex
from pulse.adapters.models.rules import LOCATOR as RULES_LOCATOR
from pulse.adapters.models.rules import RulesProvider
from pulse.adapters.models.runner import ModelRunner
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.adapters.sources import cls_news, eastmoney_news, fundflow, guba, lhb
from pulse.adapters.sources.http import Capture, SourceBlocked, TransportFailure
from pulse.application.data_service import DataService, Summary
from pulse.domain.evidence.announcements import NotData, SchemaChanged
from pulse.domain.instruments.identity import instrument_uuid, venue_for_code
from pulse.domain.intelligence import claims as claims_mod
from pulse.domain.intelligence import community, feeds, funding, impact, origin, schemas, seats, verification
from pulse.domain.intelligence.feeds import NewsRecord, stock_code_from_ref
from pulse.domain.intelligence.verification import EvidenceItem

UTC = timezone.utc
CST = ZoneInfo("Asia/Shanghai")
NEWS_SOURCES = (eastmoney_news.SOURCE_ID, cls_news.SOURCE_ID)
NEWS_TOKEN = "pulse.intel.news_claim@1"
LEGACY_SOURCE = "legacy_warehouse"
TOPIC_WORDS = ("政府补助", "回购", "减持", "增持", "分红", "权益分派", "业绩预告", "业绩快报", "中标", "合同", "订单", "重组", "收购", "诉讼", "仲裁",
               "立案", "问询函", "监管函", "质押", "股权激励", "定向增发", "可转债", "年度报告", "半年度报告", "季度报告", "控制权")
ALL_SOURCE_ROWS = {**eastmoney_news.SOURCE_ROWS, **cls_news.SOURCE_ROWS, **guba.SOURCE_ROWS, **lhb.SOURCE_ROWS, **fundflow.SOURCE_ROWS}
ALL_SPECS = [eastmoney_news.SPEC, cls_news.SPEC, guba.SPEC, *lhb.SPECS.values(), fundflow.SPEC]
_NEWS = {"eastmoney": (eastmoney_news, feeds.parse_eastmoney_news), "cls": (cls_news, feeds.parse_cls_roll)}


def _fq(status: str, raw_object_id: uuid.UUID | None, origin_: str = "primary_source", reason: str | None = None, **extra) -> dict:
    return {"status": status, "reason": reason, "raw_object_id": str(raw_object_id) if raw_object_id else None, "value_origin": origin_, **extra}


class IntelligenceService:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock: Callable[[], datetime] | None = None, runner: ModelRunner | None = None,
                 claim_policy: ModelPolicy | None = None):
        if not conn.autocommit:
            raise ValueError("IntelligenceService needs an autocommit connection; it opens explicit short transactions")
        self.conn, self.lake = conn, lake
        self.clock = clock or (lambda: datetime.now(UTC))
        self.data = DataService(conn, lake, clock=self.clock)
        self.runner = runner or ModelRunner(conn, lake, {"rules": RulesProvider()}, clock=self.clock)
        self.claim_policy = claim_policy or claim_extraction_policy("rules", RULES_LOCATOR)
        self._exists_cache: dict[uuid.UUID, bool] = {}

    # ---- bootstrap / shared protocol ------------------------------------------------------------------------------
    def bootstrap(self) -> None:
        now = self.clock()
        with self.conn.transaction():
            repo.ensure_sources(self.conn, ALL_SOURCE_ROWS, now)
            repo.ensure_intel_contracts(self.conn, now)
            for s in ALL_SPECS:
                ingest_repo.ensure_endpoint(self.conn, s, now)

    def _fetch(self, spec, partition: str, call: Callable[[], Capture]) -> Capture:
        try:
            return call()
        except SourceBlocked as exc:
            self.data.record_attempt(spec, partition, "blocked", error_code=str(exc))
            raise
        except TransportFailure as exc:
            self.data.record_attempt(spec, partition, "transport_error", error_code=str(exc)[:80])
            raise

    def _store(self, spec, batch_id: uuid.UUID, cap: Capture, owner: str, *, published_at: datetime | None = None, precision: str = "unknown") -> uuid.UUID:
        ext = {"application/json": ".json", "text/plain": ".txt", "text/html": ".html"}.get(cap.mime_type, ".bin")
        ref = self.lake.put_bytes(cap.body, owner=owner, kind="raw", suffix=ext)  # durable before the DB learns about it
        ingested = max(self.clock(), cap.fetched_at)
        with self.conn.transaction():
            rid, _ = ingest_repo.insert_raw_object(
                self.conn, source_id=cap.source_id, endpoint_id=cap.endpoint_id, source_schema_version=spec.source_schema_version,
                source_record_key=cap.request_key + "@" + cap.fetched_at.astimezone(UTC).isoformat(), content_hash=ref.sha256, storage_uri=ref.rel_path,
                mime_type=cap.mime_type, published_at=published_at, source_time_precision=precision, first_seen_at=cap.fetched_at, ingested_at=ingested)
            ingest_repo.add_batch_member(self.conn, batch_id, rid, cap.fetched_at)
        return rid

    def _bad_page(self, spec, partition: str, exc: Exception, body: bytes, s: Summary, label: str) -> None:
        text = body[:2000].decode("utf-8", errors="replace").lower()
        login = any(w in text for w in ("login", "captcha", "验证", "登录"))
        outcome = "auth_required" if (isinstance(exc, NotData) and login) else ("schema_changed" if isinstance(exc, SchemaChanged) else "parse_error")
        self.data.record_attempt(spec, partition, outcome, error_code=str(exc)[:80])
        s.add(f"pages_{outcome}")
        s.notes.append(f"{label}: {exc}; canonical paused, raw kept")

    def _finish_batch(self, spec, partition: str, batch_id: uuid.UUID, cursor: dict, watermark: datetime | None) -> None:
        with self.conn.transaction():
            ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            cur = ingest_repo.read_cursor(self.conn, spec, partition)
            ingest_repo.advance_cursor(self.conn, spec, partition, batch_id=batch_id, new_cursor=cursor, watermark=watermark,
                                       expected_version=cur["cursor_version"] if cur else 0, now=self.clock())

    def _batch(self, spec, partition: str) -> tuple[uuid.UUID, str]:
        with self.conn.transaction():
            bid = ingest_repo.create_batch(self.conn, spec, partition, self.clock())
        return bid, f"job:{bid}"

    def _transport_failed(self, batch_id: uuid.UUID, s: Summary, who: str, exc: Exception) -> None:
        """A network failure (health already recorded by _fetch): the batch is FAILED, nothing is normalized, the run goes on."""
        with self.conn.transaction():
            ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="transport_error")
        s.add("transport_errors")
        s.notes.append(f"{who}: {exc}")

    def _blocked(self, batch_id: uuid.UUID, owner: str, s: Summary, who: str) -> None:
        with self.conn.transaction():
            ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="blocked")
        s.notes.append(f"{who} blocked the client: stopped immediately (circuit OPEN)")
        self.lake.release(owner)

    def _instrument(self, code: str) -> uuid.UUID | None:
        try:
            iid = instrument_uuid(venue_for_code(code), code)
        except ValueError:
            return None
        if iid not in self._exists_cache:
            self._exists_cache[iid] = repo.instrument_exists(self.conn, iid)
        return iid if self._exists_cache[iid] else None

    # ---- news documents ---------------------------------------------------------------------------------------------
    def ingest_news(self, adapter, kind: str, *, pages: int = 1, page_size: int = 30) -> Summary:
        mod, parse = _NEWS[kind]
        spec = mod.SPEC
        s = Summary(f"news {mod.SOURCE_ID}")
        partition = "latest"
        batch_id, owner = self._batch(spec, partition)
        recs: list[tuple[NewsRecord, uuid.UUID, datetime]] = []
        cursor: str | None = None
        empty_ok = False
        try:
            for _ in range(pages):
                cap = self._fetch(spec, partition, lambda c=cursor: adapter.page(c if kind == "eastmoney" else (int(c) if c else None), page_size))
                raw_id = self._store(spec, batch_id, cap, owner)
                s.add("pages_fetched")
                try:
                    parsed = parse(cap.body)
                except (NotData, SchemaChanged) as exc:
                    self._bad_page(spec, partition, exc, cap.body, s, f"page {_ + 1}")
                    break
                recs += [(r, raw_id, cap.fetched_at) for r in parsed.records]
                empty_ok = empty_ok or parsed.empty_success
                cursor = parsed.next_cursor
                if parsed.empty_success or not cursor:
                    break
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            if not recs and not empty_ok:
                with self.conn.transaction():
                    ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="no_parsable_page")
                return s
            self._normalize_news(spec, partition, batch_id, recs, s, empty_ok)
        except SourceBlocked:
            self._blocked(batch_id, owner, s, mod.SOURCE_ID)
            raise
        except TransportFailure as exc:
            self._transport_failed(batch_id, s, mod.SOURCE_ID, exc)
        finally:
            self.lake.release(owner)
        return s

    @staticmethod
    def _record_hash(r: NewsRecord) -> str:
        return sha256_hex(json.dumps({"t": r.title, "b": r.body, "p": r.published_at.isoformat() if r.published_at else None, "s": list(r.stock_refs)},
                                     ensure_ascii=False, sort_keys=True).encode())

    def _normalize_news(self, spec, partition: str, batch_id, recs, s: Summary, empty_ok: bool) -> None:
        newest, created = None, 0
        seen: set[str] = set()
        with self.conn.transaction():
            for rec, raw_id, fetched_at in recs:
                if rec.source_record_id in seen:
                    continue
                seen.add(rec.source_record_id)
                h = self._record_hash(rec)
                prev = repo.latest_document(self.conn, rec.source_id, rec.source_record_id)
                prev_hash = ((prev["field_quality"].get("record_content_hash") or {}).get("raw_value")) if prev else None
                if prev is None:
                    revision, status = 1, "published"
                elif prev_hash == h and prev["document_status"] not in ("withdrawn", "deleted_at_source"):
                    s.add("documents_unchanged")
                    continue
                else:
                    revision, status = prev["revision"] + 1, "corrected"
                own = "财联社" if rec.own_dispatch else None
                att = origin.detect_attribution(rec.body or rec.title, own_publisher=own)
                day = rec.published_at.astimezone(CST).date().isoformat() if rec.published_at else "unknown"
                group, ostatus = origin.document_origin(source_id=rec.source_id, title=rec.title, day=day, own_dispatch=rec.own_dispatch, attribution=att)
                fq = {"published_at": _fq("observed" if rec.published_at else "missing", raw_id, reason=None if rec.published_at else "source_value_null"),
                      "title": _fq("observed", raw_id),
                      "origin": _fq("derived", raw_id, "calculation", precision_note=f"{origin.POLICY_VERSION}; attribution={att.publisher if att else None}"),
                      "record_content_hash": _fq("observed", raw_id, raw_value=h),
                      "stock_refs": _fq("observed", raw_id, raw_value=",".join(rec.stock_refs))}
                first_seen = fetched_at
                avail = max(self.clock(), first_seen)
                repo.insert_news_document(self.conn, source_id=rec.source_id, source_record_id=rec.source_record_id, revision=revision, raw_object_id=raw_id,
                                          status=status, origin_group=group, origin_status=ostatus, title=rec.title, published_at=rec.published_at,
                                          precision=rec.published_time_precision, first_seen_at=first_seen, available_at=avail, now=self.clock(),
                                          field_quality=fq)
                s.add("documents_published" if revision == 1 else "documents_corrected")
                s.add(f"origin_{ostatus}")
                created += 1
                if rec.published_at and rec.published_at <= self.clock():
                    newest = max(newest, rec.published_at) if newest else rec.published_at
        self._finish_batch(spec, partition, batch_id, {"records": len(recs), "newest": newest.isoformat() if newest else None}, newest)
        self.data.record_attempt(spec, partition, "success_data" if recs else "success_empty", new_records=created, newest_content_at=newest,
                                 content_expected_now=True, canonical_ready=bool(recs))

    def withdraw_document(self, source_id: str, source_record_id: str, *, raw_object_id: uuid.UUID, reason: str, status: str = "withdrawn") -> Summary:
        """Append a withdrawn / deleted revision (evidence from the source, never inferred here) and recompute dependent claims."""
        s = Summary(f"withdraw {source_id}/{source_record_id}")
        now = self.clock()
        with self.conn.transaction():
            prev = repo.latest_document(self.conn, source_id, source_record_id)
            if prev is None:
                raise KeyError(f"unknown document {source_id}/{source_record_id}")
            fq = {**prev["field_quality"], "withdrawal": _fq("observed", raw_object_id, reason=reason)}
            repo.insert_news_document(self.conn, source_id=source_id, source_record_id=source_record_id, revision=prev["revision"] + 1,
                                      raw_object_id=raw_object_id, status=status, origin_group=prev["origin_group"], origin_status=prev["origin_status"],
                                      title=prev["title"], published_at=prev["published_at"], precision=prev["published_time_precision"], first_seen_at=now,
                                      available_at=now, now=now, field_quality=fq, quality_status="disputed", document_id=prev["document_id"])
            s.add("document_revisions_appended")
            for cid in repo.claims_citing_documents(self.conn, [prev["document_id"]]):
                self._recompute_claim(cid, now, s)
        return s

    # ---- claims -------------------------------------------------------------------------------------------------------
    def _load_records(self, raw_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict[str, NewsRecord]]:
        out: dict[uuid.UUID, dict[str, NewsRecord]] = {}
        for rid in set(raw_ids):
            row = self.conn.execute("SELECT source_id, content_hash FROM ingest.raw_object WHERE raw_object_id=%s", (rid,)).fetchone()
            body = self.lake.read_object(row["content_hash"])
            parse = feeds.parse_eastmoney_news if row["source_id"] == eastmoney_news.SOURCE_ID else feeds.parse_cls_roll
            out[rid] = {r.source_record_id: r for r in parse(body, row["source_id"]).records}
        return out

    def process_claims(self, *, limit: int = 500, batch_size: int = 50) -> Summary:
        s = Summary("claims")
        docs = repo.unprocessed_documents(self.conn, NEWS_SOURCES, limit)
        s.counts["documents_to_process"] = len(docs)
        for i in range(0, len(docs), batch_size):
            chunk = docs[i:i + batch_size]
            recmap = self._load_records(d["raw_object_id"] for d in chunk)
            pairs, manifest, inputs = [], [], {}
            for d in chunk:
                rec = recmap[d["raw_object_id"]].get(d["source_record_id"])
                if rec is None:
                    s.add("documents_record_missing_in_raw")
                    continue
                ref = str(d["document_revision_id"])
                h = (d["field_quality"].get("record_content_hash") or {}).get("raw_value") or sha256_hex(rec.title.encode())
                manifest.append(ManifestRef("document_revision", ref, h, d["available_at"]))
                inputs[ref] = rec
                pairs.append((d, rec))
            if not pairs:
                continue
            req = ModelRequest(self.claim_policy, tuple(manifest), max(self.clock(), max(m.available_at for m in manifest)), inputs)
            res = self.runner.run(req)
            s.add("model_runs_reused" if res.reused else "model_runs_new")
            by_ref = {e["document_ref"]: e["claims"] for e in res.output["extractions"]}
            for d, rec in pairs:
                with self.conn.transaction():
                    for raw_c in by_ref.get(str(d["document_revision_id"]), []):
                        try:
                            cand = claims_mod.Candidate.from_structured(raw_c)
                            self._apply_candidate(d, rec, cand, f"{res.policy_id}@{res.policy_version}", s)
                        except claims_mod.CandidateError as exc:
                            s.add("candidates_rejected")
                            s.notes.append(f"{d['source_record_id']}: {exc}")
        return s

    def _official_links(self, view: claims_mod.ClaimView, rec: NewsRecord, day: date) -> list[dict]:
        if view.subject_key is None or not (view.category or "").startswith("company."):
            return []
        start = datetime.combine(day - timedelta(days=1), time(0), tzinfo=CST).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=2), time(0), tzinfo=CST).astimezone(UTC)
        text = rec.title + rec.body
        out = []
        for d in repo.issuer_disclosures(self.conn, view.subject_key, start, end):
            topics = [w for w in TOPIC_WORDS if w in text and w in (d["title"] or "")]
            if topics:
                out.append({"doc": d, "topics": topics})
        return out

    def _evidence_items(self, rows: list[dict]) -> list[EvidenceItem]:
        docs_cited = {r["document_revision_id"]: repo.document_revision_by_id(self.conn, r["document_revision_id"]) for r in rows}
        latest = repo.latest_revisions_of(self.conn, list({d["document_id"] for d in docs_cited.values()}))
        items = []
        for r in rows:
            d = docs_cited[r["document_revision_id"]]
            cur = latest[d["document_id"]]
            group = d["origin_group"]
            if group and group.startswith("own:"):
                group = f"outlet:{d['source_id']}"  # one outlet's several own dispatches on a claim are ONE origin, not several
            items.append(EvidenceItem(str(d["document_revision_id"]), str(d["document_id"]), d["source_id"],
                                      origin.OriginItem(group, d["origin_status"], cur["document_status"],
                                                        r["evidence_span"].get("attribution_publisher"), r["relation"]),
                                      official=d["source_id"] == "cninfo_announcements"))
        return items

    def _apply_candidate(self, doc: dict, rec: NewsRecord, cand: claims_mod.Candidate, model_version: str, s: Summary) -> None:
        view = claims_mod.derive(rec, cand)
        now = self.clock()
        pub = rec.published_at
        since = (pub or now) - timedelta(hours=claims_mod.CLUSTER_WINDOW_HOURS)
        best, best_sim = None, 0.0
        from pulse.domain.intelligence.text import similarity

        for c in repo.recent_news_claims(self.conn, NEWS_TOKEN, since):
            p = c["object_payload"]
            if claims_mod.same_cluster(p, view):
                sim = similarity(list(p.get("sketch", [])), view.sketch)
                if best is None or sim > best_sim:
                    best, best_sim = c, sim
        denial_of_existing = view.denial and best is not None and best["object_payload"].get("predicate") != "news.denial_reported"
        relation = "refutes" if denial_of_existing else "supports"
        if best is not None:
            claim_id, prior = best["claim_id"], best
            seed = best["object_payload"]["cluster"]["seed"]
            keep = [e for e in repo.claim_evidence(self.conn, prior["claim_revision_id"])
                    if repo.document_revision_by_id(self.conn, e["document_revision_id"])["document_id"] != doc["document_id"]]
            revision = prior["revision"] + 1
            s.add("claims_extended")
        else:
            seed = str(doc["document_revision_id"])
            claim_id = claims_mod.claim_uuid(view.subject_key, seed)
            prior, keep, revision = None, [], 1
            s.add("claims_new")
        span = {"field_path": "title", "start": cand.spans[0].start, "end": cand.spans[0].end, "attribution_publisher": view.attribution_publisher,
                "assertion_basis": origin.POLICY_VERSION, "raw_record_id": rec.source_record_id}
        new_ev = [(doc["document_revision_id"], 1, relation, span, claims_mod.EXTRACTION_VERSION, doc["available_at"])]
        links = self._official_links(view, rec, (pub or now).astimezone(CST).date())
        have_docs = {e["document_revision_id"] for e in keep}
        for lk in links:
            d = lk["doc"]
            if d["document_revision_id"] not in have_docs:
                new_ev.append((d["document_revision_id"], 1, "supports", {"field_path": "announcementTitle", "link_method": "issuer_day_topic_keyword_v1",
                                                                         "topics": lk["topics"], "attribution_publisher": None},
                               "news-official-link-v1", d["available_at"]))
                s.add("official_links_attached")
        rows = [{"document_revision_id": e["document_revision_id"], "evidence_span": e["evidence_span"], "relation": e["relation"]} for e in keep]
        rows += [{"document_revision_id": n[0], "evidence_span": n[3], "relation": n[2]} for n in new_ev]
        v = verification.verify(self._evidence_items(rows), view.assertion_type)
        ev_avail = [e["available_at"] for e in keep] + [n[5] for n in new_ev]
        avail = max([now] + ev_avail)
        event_time = prior["event_time"] if prior is not None and prior["event_time"] else pub
        payload = claims_mod.claim_payload(view, cluster_seed=seed, verification=v.to_payload(), impact_event=view.category is not None)
        schemas.validate("pulse.intel.news_claim", "1", payload)
        crid = repo.insert_claim_revision(self.conn, claim_id=claim_id, revision=revision, subject_kind=view.subject_kind, subject_key=view.subject_key,
                                          predicate=view.predicate, payload=payload, token=NEWS_TOKEN, assertion_type=view.assertion_type,
                                          status=v.claim_status, policy_version=verification.POLICY_VERSION, model_version=model_version,
                                          event_time=event_time, available_at=avail, now=now, quality="valid" if v.confirmation != "disputed" else "disputed")
        for e in keep:
            repo.insert_claim_evidence(self.conn, crid, e["document_revision_id"], e["evidence_ordinal"], e["relation"], e["evidence_span"],
                                       e["extraction_version"], e["available_at"], e["recorded_at"])
        for did, ordv, rel, sp, ver, av in new_ev:
            repo.insert_claim_evidence(self.conn, crid, did, ordv, rel, sp, ver, max(av, doc["available_at"]) if did == doc["document_revision_id"] else av, now)
        s.add(f"claim_status_{v.claim_status}")
        s.add(f"assertion_{view.assertion_type}")
        if view.category:
            self._append_event(claim_id, revision, crid, view, event_time, avail, now, s)
        # a superseded revision of this document may still support OTHER claims: recompute those without it
        for cid in repo.claims_citing_documents(self.conn, [doc["document_id"]]):
            if cid != claim_id:
                self._recompute_claim(cid, now, s, drop_document_id=doc["document_id"], keep_revision=doc["document_revision_id"])

    def _append_event(self, claim_id, revision, crid, view, event_time, avail, now, s: Summary) -> None:
        iid = self._instrument(view.subject_key) if view.subject_key else None
        pay = impact.map_impact(claim_id=str(claim_id), claim_revision_id=str(crid), category=view.category, headline=view.headline,
                                polarity_hint=view.polarity_hint, subject_code=view.subject_key if iid else None,
                                subject_instrument_id=str(iid) if iid else None, sector_hints=view.sector_hints,
                                event_time=event_time.isoformat() if event_time else None)
        schemas.validate("pulse.intel.impact_mapping", "1", pay)
        erid = repo.insert_event_revision(self.conn, claim_id=claim_id, revision=revision, category=view.category, event_time=event_time, payload=pay,
                                          model_version=impact.POLICY_VERSION, available_at=avail, now=now, quality="partial")
        repo.insert_event_claim(self.conn, erid, crid)
        s.add("event_revisions")

    def _recompute_claim(self, claim_id: uuid.UUID, now: datetime, s: Summary, *, drop_document_id: uuid.UUID | None = None,
                         keep_revision: uuid.UUID | None = None) -> None:
        """New claim revision from the latest document statuses (withdrawal / superseded revision); dependent event gets a new revision too."""
        prior = repo.latest_claim(self.conn, claim_id)
        if prior is None or prior["claim_schema_version"] != NEWS_TOKEN:
            return
        rows = repo.claim_evidence(self.conn, prior["claim_revision_id"])
        if drop_document_id is not None:
            rows = [r for r in rows if repo.document_revision_by_id(self.conn, r["document_revision_id"])["document_id"] != drop_document_id
                    or r["document_revision_id"] == keep_revision]
        v = verification.verify(self._evidence_items(rows), prior["assertion_type"])
        payload = {**prior["object_payload"], "verification": v.to_payload(), "recomputed_because": "document_withdrawn_or_superseded"}
        revision = prior["revision"] + 1
        crid = repo.insert_claim_revision(self.conn, claim_id=claim_id, revision=revision, subject_kind=prior["subject_kind"], subject_key=prior["subject_key"],
                                          predicate=prior["predicate"], payload=payload, token=NEWS_TOKEN, assertion_type=prior["assertion_type"],
                                          status=v.claim_status, policy_version=verification.POLICY_VERSION, model_version=prior["model_version"],
                                          event_time=prior["event_time"], available_at=max(now, prior["available_at"]), now=now, quality="partial")
        for r in rows:
            repo.insert_claim_evidence(self.conn, crid, r["document_revision_id"], r["evidence_ordinal"], r["relation"], r["evidence_span"],
                                       r["extraction_version"], r["available_at"], r["recorded_at"])
        s.add("claims_recomputed")
        ev = repo.latest_event(self.conn, claim_id)
        if ev is not None:
            pay = {**ev["analysis_payload"], "fact": {**ev["analysis_payload"]["fact"], "claim_revision_id": str(crid)}}
            erid = repo.insert_event_revision(self.conn, claim_id=claim_id, revision=revision, category=ev["event_category"], event_time=ev["event_time"],
                                              payload=pay, model_version=impact.POLICY_VERSION, available_at=max(now, ev["available_at"]), now=now, quality="partial")
            repo.insert_event_claim(self.conn, erid, crid)
            s.add("event_revisions")

    # ---- community ------------------------------------------------------------------------------------------------------
    def ingest_community(self, adapter, codes: Sequence[str], *, pages: int = 2, window_hours: int = 24) -> Summary:
        s = Summary("community guba")
        spec = guba.SPEC
        for code in codes:
            partition = code
            iid = self._instrument(code)
            if iid is None:
                s.notes.append(f"{code}: instrument not registered; skipped")
                continue
            batch_id, owner = self._batch(spec, partition)
            collected: list[tuple[feeds.GubaPost, uuid.UUID]] = []
            sampled_at = None
            fetched = 0
            try:
                for page in range(1, pages + 1):
                    cap = self._fetch(spec, partition, lambda p=page: adapter.page(code, p))
                    raw_id = self._store(spec, batch_id, cap, owner)
                    sampled_at = cap.fetched_at if sampled_at is None else max(sampled_at, cap.fetched_at)
                    s.add("pages_fetched")
                    fetched += 1
                    try:
                        parsed = feeds.parse_guba_list(cap.body)
                    except (NotData, SchemaChanged) as exc:
                        self._bad_page(spec, partition, exc, cap.body, s, f"{code} page {page}")
                        break
                    collected += [(p, raw_id) for p in parsed.posts]
                    ws = sampled_at - timedelta(hours=window_hours)
                    unpinned = [p for p, _ in collected if not p.pinned]
                    if parsed.empty_success or (unpinned and min(p.published_at for p in unpinned) <= ws):
                        break
                with self.conn.transaction():
                    ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                if not collected or sampled_at is None:
                    with self.conn.transaction():
                        ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="no_parsable_page")
                    continue
                self._normalize_community(spec, partition, batch_id, code, iid, collected, sampled_at, window_hours, fetched, s)
            except SourceBlocked:
                self._blocked(batch_id, owner, s, guba.SOURCE_ID)
                raise
            except TransportFailure as exc:
                self._transport_failed(batch_id, s, f"{guba.SOURCE_ID}/{code}", exc)
            finally:
                self.lake.release(owner)
        return s

    def _normalize_community(self, spec, partition, batch_id, code, iid, collected, sampled_at, window_hours, fetched, s: Summary) -> None:
        ws, we = community.default_window(sampled_at, window_hours)
        posts = [p for p, _ in collected]
        cov = community.coverage_status(posts, window_start=ws, sorted_by_publish_time=True)
        scope = community.Scope(f"guba_list:{code}:pages1-{fetched}", guba.SORT, ws, we, fetched, cov)
        own = {p.post_id: raw for p, raw in collected if p.bar_code == code and not p.pinned and ws <= p.published_at < we}
        s.add("posts_other_bar_or_outside_window_skipped", len({p.post_id for p in posts}) - len(own))
        new = 0
        with self.conn.transaction():
            for p, raw_id in collected:
                if p.post_id not in own:
                    continue
                th = community.title_hash(p.title)
                prev = repo.latest_post(self.conn, spec.source_id, p.post_id)
                rev = 1 if prev is None else (prev["post_revision"] + (0 if prev["analysis_payload"].get("title_hash") == th else 1))
                analysis = {"instrument_id": str(iid), "instrument_code": code, "subject_match": "own_stockbar", "bar_code": p.bar_code, "title_hash": th,
                            "duplicate_cluster": th, "metrics_basis": community.METRICS_BASIS, "click_count": p.click_count, "forward_count": p.forward_count,
                            "platform_tag_bullish_bearish": p.bullish_bearish, "list_rank_note": "newest-post order"}
                schemas.validate("pulse.intel.community_post", "1", analysis)
                community.assert_no_forbidden_labels(analysis)
                _, created = repo.insert_community_observation(
                    self.conn, source_id=spec.source_id, raw_object_id=raw_id, post_id=p.post_id, post_revision=rev, author=community.pseudonym(p.user_id),
                    created_at=p.published_at, sampled_at=sampled_at, entry=scope.entry, sort=scope.sort, window_start=ws, window_end=we, coverage=cov,
                    like_count=None, reply_count=p.comment_count, unique_participants=None, analysis=analysis, model_version=community.ANALYSIS_MODEL_VERSION,
                    available_at=max(self.clock(), sampled_at), now=self.clock(), quality="valid" if p.user_id else "partial")
                new += created
        s.add("community_observations", new)
        summary = community.summarize(posts, code, scope)
        community.assert_no_forbidden_labels(summary)
        s.notes.append("sample " + json.dumps(summary, ensure_ascii=False))
        self._finish_batch(spec, partition, batch_id, {"posts": len(posts), "coverage": cov}, max((p.published_at for p in posts), default=None))
        self.data.record_attempt(spec, partition, "success_data", new_records=new, newest_content_at=max((p.published_at for p in posts), default=None),
                                 content_expected_now=True, canonical_ready=True)

    # ---- seats ------------------------------------------------------------------------------------------------------------
    def _sessions(self, extra: Iterable[date] = ()) -> list[date]:
        rows = self.conn.execute("SELECT DISTINCT window_end FROM intelligence.seat_report").fetchall()
        return sorted({r["window_end"] for r in rows} | set(extra))

    def _persist_reports(self, reports: Sequence[seats.SeatReportRec], raw_for: Callable[[seats.SeatReportRec], uuid.UUID], s: Summary,
                         chunk: int = 300) -> None:
        existing = repo.existing_report_ids(self.conn, reports[0].source_id) if reports else {}
        sid = reports[0].source_id if reports else ""
        pending = []
        for rep in reports:
            iid = self._instrument(rep.code)
            if iid is None:
                s.add("reports_skipped_unknown_instrument")
                continue
            prev = existing.get(rep.source_report_id)
            if prev is None:
                revision = 1
            elif repo.report_content_hash(self.conn, sid, rep.source_report_id, prev) == rep.content_hash():
                s.add("reports_unchanged")
                continue
            else:
                revision = prev + 1
            if rep.extra.get("rows_skipped_no_amount"):
                s.add("rows_skipped_no_amount", rep.extra["rows_skipped_no_amount"])
            pending.append((rep, revision, iid))
        for i in range(0, len(pending), chunk):
            with self.conn.transaction():
                now = self.clock()
                for rep, revision, iid in pending[i:i + chunk]:
                    raw_id = raw_for(rep)
                    versions: dict[uuid.UUID, int] = {}
                    for r in rep.rows:
                        res = r.resolution
                        s.add(f"rows_{res.status}")
                        if res.status == "resolved" and res.seat_id not in versions:
                            first = datetime.combine(rep.window_end, time(0), tzinfo=UTC)
                            if repo.upsert_seat_entity(self.conn, seat_id=res.seat_id, canonical_name=res.canonical_name, broker_legal_name=res.broker_legal_name,
                                                       city=res.city, effective_from=first, evidence_raw_object_id=raw_id,
                                                       alias_evidence=[{"alias": r.raw_seat_name, "relation": "disclosed_name", "evidence_raw_object_id": str(raw_id),
                                                                        "effective_from_basis": "first_observed_in_disclosure", "resolver": seats.RESOLVER_VERSION}],
                                                       available_at=now, now=now):
                                s.add("seat_entities_new")
                            versions[res.seat_id] = 1
                    repo.insert_seat_report(self.conn, rep, revision=revision, raw_object_id=raw_id, instrument_id=iid, seat_versions=versions,
                                            published_at=None, now=now)
                    s.add("seat_reports_new" if revision == 1 else "seat_reports_revised")

    def ingest_lhb(self, adapter, days: Sequence[date]) -> Summary:
        s = Summary("seats eastmoney lhb")
        for d in days:
            ds = d.isoformat()
            try:
                data = {}
                for ep in ("daily_list", "seat_buy", "seat_sell"):
                    data[ep] = self._lhb_endpoint(adapter, ep, ds, s)
                    if data[ep] is None:
                        break
                    if ep == "daily_list" and not data[ep][0]:
                        s.add("days_success_empty")
                        break
                else:
                    list_rows, list_raw = data["daily_list"]
                    buy_rows, buy_raw = data["seat_buy"]
                    sell_rows, _ = data["seat_sell"]
                    reps = seats.reports_from_eastmoney(list_rows, buy_rows, sell_rows, self._sessions([d]), self.clock())
                    self._persist_reports(reps, lambda r: buy_raw if r.rows else list_raw, s)
            except SourceBlocked:
                s.notes.append("eastmoney lhb blocked the client: stopped immediately (circuit OPEN)")
                raise
        return s

    def _lhb_endpoint(self, adapter, ep: str, ds: str, s: Summary):
        spec = lhb.SPECS[ep]
        partition = f"day:{ds}"
        batch_id, owner = self._batch(spec, partition)
        rows: list[dict] = []
        first_raw = None
        parse = feeds.parse_lhb_list if ep == "daily_list" else feeds.parse_lhb_seats
        try:
            page, pages = 1, 1
            while page <= pages and page <= 20:
                cap = self._fetch(spec, partition, lambda p=page: adapter.page(ep, ds, p, 500 if ep != "daily_list" else 200))
                raw_id = self._store(spec, batch_id, cap, owner)
                first_raw = first_raw or raw_id
                s.add("pages_fetched")
                try:
                    doc = parse(cap.body)
                except (NotData, SchemaChanged) as exc:
                    self._bad_page(spec, partition, exc, cap.body, s, f"{ep} {ds} p{page}")
                    with self.conn.transaction():
                        ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                        ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="unparsable")
                    return None
                rows += list(doc.rows)
                pages = max(pages, doc.pages)
                page += 1
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            self._finish_batch(spec, partition, batch_id, {"rows": len(rows), "pages": pages}, None)
            self.data.record_attempt(spec, partition, "success_data" if rows else "success_empty", new_records=len(rows), newest_content_at=None,
                                     content_expected_now=False, canonical_ready=bool(rows))
            return rows, first_raw
        except SourceBlocked:
            self._blocked(batch_id, owner, s, lhb.SOURCE_ID)
            raise
        except TransportFailure as exc:
            self._transport_failed(batch_id, s, f"{lhb.SOURCE_ID}/{ep}/{ds}", exc)
            return None
        finally:
            self.lake.release(owner)

    def import_legacy_lhb(self) -> Summary:
        """Normalize the legacy lhb_stocks / lhb_seats raw objects (kept as raw bytes by the legacy import) into seat reports."""
        import polars as pl

        s = Summary("seats legacy import")
        rows = self.conn.execute(
            "SELECT raw_object_id, source_record_key, content_hash FROM ingest.raw_object WHERE source_id=%s AND endpoint_id='evidence_files'"
            " AND (source_record_key LIKE 'lhb_stocks/%%' OR source_record_key LIKE 'lhb_seats/%%') ORDER BY source_record_key", (LEGACY_SOURCE,)).fetchall()
        stocks, seat_df = [], []
        for r in rows:
            df = pl.read_parquet(io.BytesIO(self.lake.read_object(r["content_hash"]))).with_columns(pl.lit(str(r["raw_object_id"])).alias("_raw"))
            (stocks if r["source_record_key"].startswith("lhb_stocks/") else seat_df).append(df)
        if not stocks:
            s.notes.append("no legacy lhb raw objects registered")
            return s
        st = pl.concat(stocks).sort("ingested_at").unique(subset=["code", "date", "reason"], keep="last")
        se = pl.concat(seat_df).sort("ingested_at").unique(subset=["code", "date", "reason", "side", "rank"], keep="last") if seat_df else None
        s.counts["legacy_stock_rows"] = st.height
        s.counts["legacy_seat_rows"] = 0 if se is None else se.height
        sessions = sorted(set(st["date"].to_list()))
        raw_of: dict[tuple, str] = {}
        if se is not None:
            for r in se.select(["code", "date", "reason", "_raw"]).unique(subset=["code", "date", "reason"]).iter_rows():
                raw_of[(r[0], r[1], r[2])] = r[3]
        stock_raw = {(r[0], r[1], r[2]): r[3] for r in st.select(["code", "date", "reason", "_raw"]).iter_rows()}
        seat_rows = [] if se is None else se.drop("_raw").to_dicts()
        reps = seats.reports_from_legacy(st.drop("_raw").to_dicts(), seat_rows, sessions)
        keymap = {r.source_report_id: (r.code, r.window_end, r.disclosure_reason) for r in reps}
        self._persist_reports(reps, lambda r: uuid.UUID(raw_of.get(keymap[r.source_report_id]) or stock_raw[keymap[r.source_report_id]]), s)
        return s

    # ---- fund flow ----------------------------------------------------------------------------------------------------------
    def ingest_fundflow(self, adapter, codes: Sequence[str], *, limit: int = 10) -> Summary:
        s = Summary("fundflow eastmoney")
        spec = fundflow.SPEC
        for code in codes:
            iid = self._instrument(code)
            if iid is None:
                s.notes.append(f"{code}: instrument not registered; skipped")
                continue
            batch_id, owner = self._batch(spec, code)
            try:
                cap = self._fetch(spec, code, lambda c=code: adapter.daykline(c, limit))
                raw_id = self._store(spec, batch_id, cap, owner)
                s.add("pages_fetched")
                try:
                    parsed = feeds.parse_fundflow(cap.body)
                    if not parsed.empty_success and parsed.code != code:
                        raise SchemaChanged(f"code_mismatch:{parsed.code}")
                except (NotData, SchemaChanged) as exc:
                    self._bad_page(spec, code, exc, cap.body, s, code)
                    with self.conn.transaction():
                        ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                        ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="unparsable")
                    continue
                with self.conn.transaction():
                    ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                new = self._normalize_funding(code, iid, raw_id, parsed, cap.fetched_at, s)
                self._finish_batch(spec, code, batch_id, {"days": len(parsed.days)}, None)
                self.data.record_attempt(spec, code, "success_data" if parsed.days else "success_empty", new_records=new, newest_content_at=None,
                                         content_expected_now=False, canonical_ready=bool(parsed.days))
            except SourceBlocked:
                self._blocked(batch_id, owner, s, fundflow.SOURCE_ID)
                raise
            except TransportFailure as exc:
                self._transport_failed(batch_id, s, f"{fundflow.SOURCE_ID}/{code}", exc)
            finally:
                self.lake.release(owner)
        return s

    def _normalize_funding(self, code, iid, raw_id, parsed, fetched_at: datetime, s: Summary) -> int:
        new = 0
        now = self.clock()
        with self.conn.transaction():
            for o in funding.observations_from_days(parsed.days, code):
                day = o.window_start.astimezone(CST).date().isoformat()
                key = f"{code}|{day}"
                prev = repo.latest_funding(self.conn, fundflow.SOURCE_ID, key, o.metric_namespace, o.metric_name, o.methodology_version)
                if prev is not None and prev["canonical_value"] == o.value:
                    s.add("funding_unchanged")
                    continue
                revision = 1 if prev is None else prev["revision"] + 1
                final = funding.is_final(day, fetched_at)
                missing = o.value is None
                quality = "valid" if (not missing and final) else "partial"
                fq = {"canonical_value": _fq("missing" if missing else "observed", raw_id, reason="vendor_value_absent" if missing else None,
                                             source_id=fundflow.SOURCE_ID, raw_unit=o.unit, precision_note=None if final else "vendor day bar fetched before 16:00 CST: accumulation may be incomplete"),
                      "methodology": _fq("observed", raw_id, precision_note="provider-computed order-size classification; thresholds undisclosed; never additive across providers"),
                      "observation_window": _fq("derived", raw_id, "calculation", precision_note="trade date calendar day (vendor does not state session scope)")}
                created = repo.insert_funding(
                    self.conn, source_id=fundflow.SOURCE_ID, raw_object_id=raw_id, record_key=key, revision=revision, instrument_id=iid, namespace=o.metric_namespace,
                    name=o.metric_name, methodology_id=o.methodology_id, methodology_version=o.methodology_version, measurement_class=o.measurement_class,
                    side_convention=o.side_convention, basis=o.trade_or_order_basis, session_scope="vendor_day_bar_scope_unspecified", direction=o.market_direction,
                    window_start=o.window_start, window_end=o.window_end, raw_value=None if missing else format(o.value, "f"), raw_unit=o.unit,
                    canonical_value=o.value, canonical_unit=o.unit, currency="CNY" if o.unit == "CNY" else None,
                    missing_reason="vendor_value_absent" if missing else None, available_at=max(now, fetched_at), now=now, quality=quality, field_quality=fq)
                new += created
                s.add("funding_observations" if revision == 1 else "funding_revisions")
        return new

    # ---- reporting ----------------------------------------------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("document_revision", "claim_revision", "claim_evidence", "event_revision", "event_claim", "community_observation", "funding_observation",
                  "seat_entity_version", "seat_report", "seat_report_row"):
            out[t] = self.conn.execute(f"SELECT count(*) AS n FROM intelligence.{t}").fetchone()["n"]
        out["model_run"] = self.conn.execute("SELECT count(*) AS n FROM analysis.model_run").fetchone()["n"]
        return out
