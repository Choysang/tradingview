"""BF live lane: import the old system's recordings, derive seal facts, serve the seal reads, record natively (not scheduled).

Why: until the M6 cutover the old system records every session (intraday 60 s snapshots, 7x24 news, extended intraday). pulse3
imports those files as a declared legacy source (``adapters.sources.legacy_recordings``) and builds its facts from them; its own
recorder (:class:`IntradayRecorder`) is written and tested but not scheduled, so the two systems never double their requests to
Sina/Tencent. Rows: LEGACY_TRIAGE #103/#185, #131/#187, #132/#188, #104/#186, #5/#124.

Datasets (lake manifest + ``market.dataset_manifest``, one version per import, the latest READY version of a session is the whole
session): ``quote_snapshots_60s`` (cumulative over the session's parts), ``ext_quote_snapshots``, ``legacy_guba_samples``,
``legacy_alerts_comparison`` (comparison only), ``intraday_seal_facts`` (derived, depends on the snapshot manifest it was built from).
Invariants:
* point in time: rows keep ``poll_at`` / ``quote_at`` in UTC; ``available_at`` of a dataset row is the IMPORT time;
* incremental and idempotent: a source part is identified by name and sha256; a re-run with nothing new publishes nothing and
  creates no batch; news revisions append only past what is stored;
* every source file is also stored byte-exact as a raw object (news: one lossless JSON object per message revision);
* the old files are only read; a part that cannot be read yet is reported and retried by the next run;
* seal facts are ESTIMATED (``estimated_from_60s_snapshots``): see ``domain.market_state.seal``.
Not covered: scheduling (deploy/windows), the alert rules themselves (``alert_service``), tick data.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.lake import LakeStore, ManifestRef, ObjectRef
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres import intelligence_repo as repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.sources import legacy_recordings as LR
from pulse.adapters.sources import registry, sina, tencent
from pulse.adapters.sources.http import Capture, RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.legacy_recordings import LegacyRecordings, PartUnreadable, SchemaMismatch, SourcePart
from pulse.application.data_service import DataService, Summary
from pulse.application.intelligence_service import IntelligenceService
from pulse.domain.intelligence.feeds import NewsRecord
from pulse.domain.instruments.identity import instrument_uuid, parse_display_name, venue_for_code
from pulse.domain.market_rules import Board, Exchange, InstrumentSpec, PriceLimitRegistry, RuleNotVerified
from pulse.domain.market_state import seal as S

UTC = timezone.utc
CST = S.CST
DATASET_QUOTES = "quote_snapshots_60s"
DATASET_EXT = "ext_quote_snapshots"
DATASET_GUBA = "legacy_guba_samples"
DATASET_ALERTS = "legacy_alerts_comparison"
DATASET_SEAL = "intraday_seal_facts"
CONTRACT_VERSION = "3.0.0"                       # DataService stamps every manifest with it
CONTRACTS = {
    "pulse.bf_live.quote_snapshot": "One recorded quote snapshot of one instrument (price columns decimal, lots float, times UTC).",
    "pulse.bf_live.ext_quote_snapshot": "One recorded quote of an extended-hours instrument (ETF, index future, A50, CNH).",
    "pulse.bf_live.legacy_table": "A legacy recorder table passed through unchanged (guba samples, alert log lines).",
    "pulse.bf_live.seal_facts": "Per-instrument seal facts of one session, estimated from 60 s snapshots.",
}
SEAL_ROLE = "quote_snapshots"
STREAM_ENDPOINT = {"quotes": LR.EP_QUOTES, "hot": LR.EP_HOT, "ext": LR.EP_EXT, "guba": LR.EP_GUBA, "alerts": LR.EP_ALERTS}


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dec(v) -> Decimal | None:
    return None if v is None else (v if isinstance(v, Decimal) else Decimal(str(v)))


class LiveService:
    """Imports + seal facts. One instance per command; the clock is injected."""

    def __init__(self, conn: psycopg.Connection, lake: LakeStore, recordings: LegacyRecordings | None = None, *,
                 clock: Callable[[], datetime] | None = None):
        if not conn.autocommit:
            raise ValueError("LiveService needs an autocommit connection; it opens explicit short transactions")
        self.conn, self.lake = conn, lake
        self.rec = recordings or LegacyRecordings()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.data = DataService(conn, lake, clock=self.clock)

    # ---- bootstrap ---------------------------------------------------------------------------------------------
    def bootstrap(self) -> None:
        self.data.bootstrap()
        now = self.clock()
        with self.conn.transaction():
            for cid, doc in CONTRACTS.items():
                schema = {"description": doc, "type": "object"}
                self.conn.execute(
                    "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)"
                    " VALUES (%s,%s,%s,%s,'additive-optional-v1',%s) ON CONFLICT (contract_id, schema_version) DO NOTHING",
                    (cid, CONTRACT_VERSION, Jsonb(schema), sha256_of(schema), now))

    # ---- manifests ---------------------------------------------------------------------------------------------
    def latest_ready(self, dataset_id: str, session: date | None = None, cutoff: datetime | None = None) -> dict | None:
        """Latest READY version of ``dataset_id`` for one session (or the newest session when ``session`` is None).

        With ``cutoff`` (a release's decision_as_of) only versions that were ready by then are visible (point in time)."""
        where, args = "dataset_id=%s AND manifest_state='READY'", [dataset_id]
        if cutoff is not None:
            where += " AND ready_at <= %s"
            args.append(cutoff)
        if session is not None:
            return self.conn.execute(f"SELECT * FROM market.dataset_manifest WHERE {where} AND coverage_document->>'session_date'=%s"
                                     " ORDER BY manifest_version DESC LIMIT 1", (*args, session.isoformat())).fetchone()
        return self.conn.execute(f"SELECT * FROM market.dataset_manifest WHERE {where}"
                                 " ORDER BY coverage_document->>'session_date' DESC, manifest_version DESC LIMIT 1", args).fetchone()

    def _publish_parts(self, *, day: date, dataset_id: str, contract_id: str, streams: tuple[str, ...], reader: Callable[[SourcePart], pl.DataFrame],
                       s: Summary, extra: dict | None = None, replace_all_when: Callable[[list[SourcePart]], bool] = lambda parts: False) -> dict | None:
        """Cumulative, idempotent import of the new / changed parts of ``streams`` into one dataset version."""
        parts = [p for st in streams for p in self.rec.parts(st, day)]
        s.counts["source_parts_seen"] = len(parts)
        if not parts:
            s.notes.append(f"no {'/'.join(streams)} files for {day}")
            return None
        prev = self.latest_ready(dataset_id, day)
        prev_parts: dict = dict((prev["coverage_document"].get("parts") or {}) if prev else {})
        todo = [p for p in parts if prev_parts.get(p.short, {}).get("source_sha256") != p.sha256]
        if not todo:
            s.add("parts_unchanged", len(parts))
            return None
        now = self.clock()
        loaded: list[tuple[SourcePart, pl.DataFrame]] = []
        for p in todo:
            try:
                loaded.append((p, reader(p)))
            except PartUnreadable as exc:
                s.add("parts_unreadable")
                s.notes.append(f"{exc}; retried by the next run")
            except SchemaMismatch as exc:
                s.add("parts_schema_mismatch")
                s.notes.append(f"{exc}; nothing imported from this file")
        if not loaded:
            return None
        first_stream = loaded[0][0].stream
        spec = registry.spec(LR.SOURCE_ID, STREAM_ENDPOINT[first_stream])
        batch_id = ingest_repo.create_batch(self.conn, spec, f"{dataset_id}:{day}", now)
        owner = f"job:{batch_id}"
        try:
            new_cov: dict[str, dict] = {}
            members: dict[str, ObjectRef] = {}
            for p, df in loaded:
                data = p.read_bytes()
                raw = self.lake.put_bytes(data, owner=owner, kind="raw", suffix=p.path.suffix)
                pspec = registry.spec(LR.SOURCE_ID, STREAM_ENDPOINT[p.stream])
                with self.conn.transaction():
                    rid, _ = ingest_repo.insert_raw_object(
                        self.conn, source_id=pspec.source_id, endpoint_id=pspec.endpoint_id, source_schema_version=pspec.source_schema_version,
                        source_record_key=f"{day}/{p.short}", content_hash=raw.sha256, storage_uri=raw.rel_path, mime_type="application/octet-stream",
                        published_at=None, source_time_precision="unknown", first_seen_at=now, ingested_at=now)
                    ingest_repo.add_batch_member(self.conn, batch_id, rid, now)
                ref = self.lake.put_parquet(df, owner=owner)
                members[p.short] = ref
                new_cov[p.short] = {"stream": p.stream, "source_sha256": raw.sha256, "object_sha256": ref.sha256, "rows": df.height, "source_size": len(data)}
                s.add("parts_imported")
                s.add("rows_imported", df.height)
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            # keep only earlier parts that the source still lists: when the recorder compacts loose parts into one file the
            # listing returns the compacted file only, and the loose parts must leave the dataset (else every row counts twice)
            listed = {p.short for p in parts}
            keep: dict[str, dict] = {} if replace_all_when(parts) else {k: v for k, v in prev_parts.items() if k not in new_cov and k in listed}
            old_members = {}
            if prev and keep:
                old = self.lake.read_manifest(prev["content_hash"])
                old_members = {m["sha256"]: ObjectRef(m["sha256"], m["path"], m["size"], m.get("kind", "data")) for m in old.members}
            for k, v in keep.items():
                members[k] = old_members[v["object_sha256"]]
            all_cov = {**keep, **new_cov}
            row_count = sum(v["rows"] for v in all_cov.values())
            coverage = {"dataset": dataset_id, "session_date": day.isoformat(), "parts": dict(sorted(all_cov.items())), "imported_at": _z(now),
                        "source_complete": self.rec.done_marker(day) is not None, "available_at_policy": "import time (not the recording time)",
                        "times": "poll_at / quote_at are UTC converted from naive Asia/Shanghai source times",
                        "capability": LR.ENDPOINT_POLICIES[STREAM_ENDPOINT[first_stream]].get("capability_class"), **(extra or {})}
            manifest = self.lake.commit_manifest(dataset_id, [members[k] for k in sorted(members)], owner=owner,
                                                 meta={"row_count": row_count, "kind": "table", "session_date": day.isoformat(), "parts": sorted(members)})
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            pub = self.data._stage_and_publish(dataset_id, uuid.uuid4(), batch_id, manifest, contract_id, coverage, owner, row_count, now, (), ())
        except Exception:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="import_error")
            self.lake.release(owner)
            raise
        newest = None
        if "quote_at" in loaded[0][1].columns:
            newest = max((df["quote_at"].max() for _, df in loaded if df.height), default=None)
        self.data.record_attempt(spec, str(day), "success_data", new_records=sum(df.height for _, df in loaded),
                                 newest_content_at=newest if newest is not None and newest <= now else None, content_expected_now=False, canonical_ready=True)
        s.counts["manifest_version"] = pub["manifest_version"]
        s.counts["session_rows_total"] = row_count
        return pub

    # ---- intraday quotes -------------------------------------------------------------------------------------------
    def import_intraday(self, day: date) -> Summary:
        s = Summary(f"legacy-intraday {day}")
        self._publish_parts(day=day, dataset_id=DATASET_QUOTES, contract_id="pulse.bf_live.quote_snapshot", streams=("quotes", "hot"),
                            reader=lambda p: self.rec.read_quotes(p, day), s=s,
                            extra={"capability": "sampled_snapshots_estimated", "sampled_interval_seconds_nominal": 60, "not_tick_data": True,
                                   "px_zero_is_missing": True, "recorder": "legacy_recordings"})
        return s

    def import_ext(self, day: date) -> Summary:
        s = Summary(f"legacy-ext {day}")
        self._publish_parts(day=day, dataset_id=DATASET_EXT, contract_id="pulse.bf_live.ext_quote_snapshot", streams=("ext",),
                            reader=lambda p: self.rec.read_ext(p, day), s=s, extra={"recorder": "legacy_recordings"})
        return s

    def import_guba(self, day: date) -> Summary:
        s = Summary(f"legacy-guba {day}")
        self._publish_parts(day=day, dataset_id=DATASET_GUBA, contract_id="pulse.bf_live.legacy_table", streams=("guba",),
                            reader=lambda p: self.rec.read_table(p).with_columns(pl.lit(p.short).alias("source_part")), s=s,
                            extra={"note": "passed through unchanged; naive time columns keep the recorder's zone"})
        return s

    def import_alerts(self, day: date) -> Summary:
        s = Summary(f"legacy-alerts {day}")
        self._publish_parts(day=day, dataset_id=DATASET_ALERTS, contract_id="pulse.bf_live.legacy_table", streams=("alerts",),
                            reader=lambda p: self.rec.read_alert_lines(p).with_columns(pl.lit(p.short).alias("source_part")), s=s,
                            extra={"purpose": "comparison with pulse3's own alerts (M5 reconciliation); never copied into pulse3 alerts"})
        return s

    # ---- seal facts ------------------------------------------------------------------------------------------------
    def publish_seal_facts(self, day: date) -> Summary:
        s = Summary(f"seal-facts {day}")
        src = self.latest_ready(DATASET_QUOTES, day)
        if src is None:
            s.notes.append(f"no READY {DATASET_QUOTES} for {day}; run legacy-intraday first")
            return s
        cur = self.latest_ready(DATASET_SEAL, day)
        if (cur is not None and cur["coverage_document"].get("input_snapshot_id") == str(src["snapshot_id"])
                and cur["coverage_document"].get("policy_version") == S.POLICY_VERSION):     # a new policy recomputes the same input
            s.add("facts_unchanged")
            return s
        frame = self.lake.read_snapshot_frames(src["content_hash"])
        now = self.clock()
        facts, info = compute_facts(frame, day)
        rows = [{"instrument_id": str(f["id"]), **f["fact"], "available_at": now} for f in facts]
        df = pl.DataFrame(rows, schema=SEAL_SCHEMA) if rows else pl.DataFrame(schema=SEAL_SCHEMA)
        spec = registry.spec(LR.SOURCE_ID, LR.EP_QUOTES)
        batch_id = ingest_repo.create_batch(self.conn, spec, f"seal:{day}", now)
        owner = f"job:{batch_id}"
        try:
            ref = self.lake.put_parquet(df, owner=owner)
            manifest = self.lake.commit_manifest(DATASET_SEAL, [ref], owner=owner, meta={"row_count": df.height, "kind": "table", "session_date": day.isoformat()})
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            coverage = {"dataset": DATASET_SEAL, "session_date": day.isoformat(), "estimation": S.ESTIMATION, "policy_version": S.POLICY_VERSION,
                        "input_snapshot_id": str(src["snapshot_id"]), "input_content_hash": src["content_hash"], "computed_at": _z(now),
                        "windows_cst": "09:25-11:30, 13:00-14:57", **info}
            pub = self.data._stage_and_publish(DATASET_SEAL, uuid.uuid4(), batch_id, manifest, "pulse.bf_live.seal_facts", coverage, owner, df.height, now,
                                               [(src["snapshot_id"], SEAL_ROLE)], (SEAL_ROLE,))
        except Exception:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="seal_error")
            self.lake.release(owner)
            raise
        s.counts.update({k: v for k, v in info.items() if isinstance(v, int)})
        s.counts["manifest_version"] = pub["manifest_version"]
        return s

    # ---- reads ---------------------------------------------------------------------------------------------------------
    def _facts_frame(self, session: date | None, cutoff: datetime | None = None) -> tuple[dict | None, pl.DataFrame | None]:
        man = self.latest_ready(DATASET_SEAL, session, cutoff)
        if man is None:
            return None, None
        return man, self.lake.read_snapshot_frames(man["content_hash"])

    @staticmethod
    def _source(man: dict) -> dict:
        cov = man["coverage_document"]
        return {"snapshot_id": man["snapshot_id"], "content_hash": man["content_hash"], "input_snapshot_id": cov.get("input_snapshot_id"),
                "available_at": man["available_watermark"] or man["ready_at"]}

    def seal_clock(self, session: date | None, bucket_minutes: int = 5, cutoff: datetime | None = None) -> dict | None:
        """Seal clock of the session (latest session when None); None when no seal facts were published for it."""
        man, df = self._facts_frame(session, cutoff)
        if man is None or df is None:
            return None
        facts = [_fact_from_row(r) for r in df.iter_rows(named=True)]
        agg = S.seal_clock(facts, bucket_minutes)
        return {"session_date": date.fromisoformat(man["coverage_document"]["session_date"]), "estimation": S.ESTIMATION,
                "policy_version": man["coverage_document"].get("policy_version") or S.POLICY_VERSION, "clock_basis": "Asia/Shanghai", **agg, "source": self._source(man),
                "notes": ["Each figure is estimated from about one snapshot per minute: a first-seal time may be up to one sampling gap late "
                          "and a break followed by a re-seal inside one gap is not counted.",
                          "Only the continuous sessions 09:25-11:30 and 13:00-14:57 are used; the closing call auction 14:57-15:00 is not observed."]}

    def seal_fact(self, instrument_id: uuid.UUID, session: date | None, cutoff: datetime | None = None) -> dict:
        man, df = self._facts_frame(session, cutoff)
        if man is None or df is None:
            return {"instrument_id": instrument_id, "session_date": session, "status": "no_dataset", "fact": None, "source": None,
                    "notes": ["no seal facts were published for this session (the intraday recording was not imported)"]}
        sd = date.fromisoformat(man["coverage_document"]["session_date"])
        hit = df.filter(pl.col("instrument_id") == str(instrument_id))
        if hit.height == 0:
            return {"instrument_id": instrument_id, "session_date": sd, "status": "not_observed", "fact": None, "source": self._source(man),
                    "notes": ["the recording has no snapshot of this instrument in the session"]}
        r = hit.row(0, named=True)
        return {"instrument_id": instrument_id, "session_date": sd, "status": "ok", "source": self._source(man),
                "fact": {k: r[k] for k in FACT_WIRE_FIELDS}, "notes": []}


# ---- seal fact computation (frame -> facts) -----------------------------------------------------------------------------------
SEAL_SCHEMA = {"instrument_id": pl.String, "code": pl.String, "name": pl.String, "session_date": pl.Date, "limit_up": pl.Decimal(18, 4),
               "snapshots_in_window": pl.Int64, "touched_limit": pl.Boolean, "first_seal_at": pl.Datetime("ms", "UTC"),
               "last_seal_at": pl.Datetime("ms", "UTC"), "broken_count": pl.Int64, "sealed_at_close": pl.Boolean,
               "close_seal_order_amount_cny": pl.Decimal(24, 2), "first_seal_uncertainty_seconds": pl.Int64, "max_gap_seconds": pl.Int64,
               "estimation": pl.String, "policy_version": pl.String, "available_at": pl.Datetime("ms", "UTC")}
FACT_WIRE_FIELDS = ("instrument_id", "code", "name", "session_date", "limit_up", "snapshots_in_window", "touched_limit", "first_seal_at", "last_seal_at",
                    "broken_count", "sealed_at_close", "close_seal_order_amount_cny", "first_seal_uncertainty_seconds", "max_gap_seconds", "estimation",
                    "policy_version")


def _fact_from_row(r: dict) -> S.SealFact:
    return S.SealFact(r["code"], r["name"], r["session_date"], r["limit_up"], r["snapshots_in_window"], r["touched_limit"], r["first_seal_at"],
                      r["last_seal_at"], r["broken_count"], r["sealed_at_close"], r["close_seal_order_amount_cny"], r["first_seal_uncertainty_seconds"],
                      r["max_gap_seconds"])


def _fact_dict(f: S.SealFact) -> dict:
    return {"code": f.code, "name": f.name, "session_date": f.session_date, "limit_up": f.limit_up, "snapshots_in_window": f.snapshots_in_window,
            "touched_limit": f.touched_limit, "first_seal_at": f.first_seal_at, "last_seal_at": f.last_seal_at, "broken_count": f.broken_count,
            "sealed_at_close": f.sealed_at_close, "close_seal_order_amount_cny": f.close_seal_order_amount_cny,
            "first_seal_uncertainty_seconds": f.first_seal_uncertainty_seconds, "max_gap_seconds": f.max_gap_seconds, "estimation": f.estimation,
            "policy_version": f.policy_version}


def compute_facts(frame: pl.DataFrame, day: date) -> tuple[list[dict], dict]:
    """Facts for every instrument of a session's snapshot frame; only touched instruments go through the row-by-row derivation."""
    f = frame.filter(pl.col("code").is_not_null() & pl.col("quote_at").is_not_null())
    cst = pl.col("quote_at").dt.convert_time_zone("Asia/Shanghai").dt.time()
    win = ((cst >= time(9, 25)) & (cst <= time(11, 30))) | ((cst >= time(13, 0)) & (cst <= time(15, 0)))
    f = f.filter(win)
    zero = pl.lit(0).cast(pl.Decimal(18, 4))
    touched = (f.filter(pl.col("limit_up").is_not_null() & (pl.col("limit_up") > zero) & (pl.col("price") == pl.col("limit_up")))
               .select("code").unique()["code"].to_list())
    base = f.group_by("code").agg(pl.len().alias("n"), pl.col("name").drop_nulls().last().alias("name"),
                                  pl.col("limit_up").drop_nulls().last().alias("limit_up"))
    out: list[dict] = []
    unsupported = 0
    tset = set(touched)
    derived: dict[str, S.SealFact] = {}
    if tset:
        sub = f.filter(pl.col("code").is_in(touched)).sort(["code", "quote_at"])
        cols = ["code", "name", "quote_at", "price", "limit_up", "bid1_px", "bid1_lots", *[f"ask{i}_lots" for i in range(1, 6)]]
        for code, g in sub.select(cols).group_by("code", maintain_order=True):
            code = code[0]
            snaps = [S.SealSnapshot(r["quote_at"], r["price"], r["limit_up"], r["bid1_px"], _dec(r["bid1_lots"]),
                                    tuple(_dec(r[f"ask{i}_lots"]) for i in range(1, 6))) for r in g.iter_rows(named=True)]
            name = next((n for n in reversed(g["name"].to_list()) if n), None)
            derived[code] = S.derive(code, name, day, snaps)
    for r in base.iter_rows(named=True):
        code = r["code"]
        try:
            iid = instrument_uuid(venue_for_code(code), code)
        except ValueError:
            unsupported += 1
            continue
        fact = derived.get(code) or S.SealFact(code, r["name"], day, r["limit_up"], r["n"], False, None, None, 0, None, None, None, None)
        out.append({"id": iid, "fact": _fact_dict(fact)})
    info = {"instruments_observed": len(out), "instruments_unsupported_code": unsupported, "touched_limit_count": len(tset),
            "sealed_ever_count": sum(1 for d in derived.values() if d.first_seal_at), "snapshots_in_window": int(f.height)}
    return out, info


# ---- news import through the intelligence path ----------------------------------------------------------------------------------
class LegacyNewsImporter(IntelligenceService):
    """Recorded 7x24 messages -> document revisions -> claims, reusing the intelligence path of ``IntelligenceService``.

    Each distinct content state of a message becomes one document revision (``source_id`` = legacy_news_recorder, ``source_record_id`` =
    ``<origin source>:<item id>``); first_seen_at = the recorder's ``received_at``, available_at = import time. Re-import adds nothing:
    revisions are appended only beyond the number already stored."""

    def bootstrap(self) -> None:
        self.data.bootstrap()                              # registers the legacy_news_recorder source / endpoint
        super().bootstrap()

    def import_news(self, rec: LegacyRecordings, day: date) -> Summary:
        s = Summary(f"legacy-news {day}")
        msgs: list[dict] = []
        parts = rec.parts("news", day)
        s.counts["source_parts_seen"] = len(parts)
        if not parts:
            s.notes.append(f"no news files for {day}")
            return s
        for p in parts:
            try:
                msgs += rec.read_news(p)
            except PartUnreadable as exc:
                s.add("parts_unreadable")
                s.notes.append(f"{exc}; retried by the next run")
            except SchemaMismatch as exc:
                s.add("parts_schema_mismatch")
                s.notes.append(f"{exc}; nothing imported from this file")
        msgs.sort(key=lambda m: (m["received_at"], m["revision_id"] or ""))
        items: dict[str, list[tuple[dict, NewsRecord]]] = {}
        for m in msgs:
            nr = LR.parse_recorded_news(LR.recorded_news_bytes(m)).records[0]
            states = items.setdefault(nr.source_record_id, [])
            if not states or self._record_hash(states[-1][1]) != self._record_hash(nr):
                states.append((m, nr))                      # consecutive identical content is one state
        s.counts["messages_read"] = len(msgs)
        s.counts["items"] = len(items)
        have = {r["source_record_id"]: r for r in self.conn.execute(
            "SELECT DISTINCT ON (source_record_id) source_record_id, revision, field_quality FROM intelligence.document_revision"
            " WHERE source_id=%s AND source_record_id = ANY(%s) ORDER BY source_record_id, revision DESC", (LR.NEWS_SOURCE_ID, list(items))).fetchall()}
        layers: list[list[tuple[dict, NewsRecord]]] = []
        for key, states in items.items():
            n = have[key]["revision"] if key in have else 0
            if n and n <= len(states):
                stored = (have[key]["field_quality"].get("record_content_hash") or {}).get("raw_value")
                if stored != self._record_hash(states[n - 1][1]):
                    s.add("revision_conflicts")
                    s.notes.append(f"{key}: stored revision {n} differs from the recording; kept as stored")
            for j, st in enumerate(states[n:]):
                while len(layers) <= j:
                    layers.append([])
                layers[j].append((st[0], st[1]))
        if not layers:
            s.add("documents_unchanged", len(items))
            return s
        spec = registry.spec(LR.NEWS_SOURCE_ID, LR.EP_NEWS)
        partition = f"legacy:{day}"
        batch_id, owner = self._batch(spec, partition)
        try:
            raw_layers = []
            for layer in layers:
                row = []
                for m, nr in layer:
                    key = f"{nr.source_record_id}:{m['revision_id'] or m['content_hash'][:12]}"
                    cap = Capture(LR.NEWS_SOURCE_ID, LR.EP_NEWS, key, f"file://news/{day}/{m['part']}", "GET", None, 200, LR.recorded_news_bytes(m),
                                  "application/json", m["received_at"])
                    rid = self._store(spec, batch_id, cap, owner, published_at=nr.published_at, precision=nr.published_time_precision)
                    row.append((nr, rid, m["received_at"]))
                raw_layers.append(row)
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            for row in raw_layers:
                self._normalize_news(spec, partition, batch_id, row, s, False)
        finally:
            self.lake.release(owner)
        return s

    def process_new_claims(self, *, limit: int = 500, max_rounds: int = 40) -> Summary:
        """Claims for every document that has none yet (the alert scan reads claims, never documents)."""
        total = Summary("claims")
        for _ in range(max_rounds):
            part = self.process_claims(limit=limit)
            for k, v in part.counts.items():
                total.add(k, v)
            total.notes += part.notes[:5]
            if part.counts.get("documents_to_process", 0) < limit:
                break
        return total


# ---- native recorder (written and tested, NOT scheduled before the M6 cutover) -------------------------------------------------------
POLL_WINDOWS = ((time(9, 15), time(11, 31)), (time(13, 0), time(15, 6)))
_LIMITS = PriceLimitRegistry()
_EX = {"XSHG": Exchange.SSE, "XSHE": Exchange.SZSE, "XBSE": Exchange.BSE}


def _limit_up(code: str, name: str, prev_close: Decimal | None, day: date) -> Decimal | None:
    """Limit-up price from the versioned rules; None (never a guess) when the rule or the previous close is missing."""
    if prev_close is None or prev_close <= 0:
        return None
    try:
        venue = venue_for_code(code)
        if code[:3] not in ("000", "001", "002", "003", "600", "601", "603", "605"):
            return None
        _, st = parse_display_name(name)
        spec = InstrumentSpec(code, _EX[venue], Board.MAIN, st)
        return _LIMITS.band(spec, day, prev_close).limit_up
    except (ValueError, RuleNotVerified):
        return None


def _px(v) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001 - vendor text such as "" or "-"
        return None
    return None if d <= 0 else d


def native_rows(source: str, records: list[dict], poll_at: datetime, day: date) -> list[dict]:
    """Vendor quote records (Sina shares / Tencent lots) -> rows of the snapshot dataset shape; unavailable levels stay None."""
    out = []
    for r in records:
        code = r["symbol"][2:]
        if source == "sina":
            qt = datetime.strptime(r["quote_time"], "%Y-%m-%d %H:%M:%S")
            vol = _dec(r["volume"]) / 100 if r["volume"] not in (None, "") else None
            bid_l = _dec(r["bid1_volume"]) / 100 if r["bid1_volume"] not in (None, "") else None
            ask_l = _dec(r["ask1_volume"]) / 100 if r["ask1_volume"] not in (None, "") else None
            amount = _dec(r["amount"]) if r["amount"] not in (None, "") else None
        else:
            qt = datetime.strptime(r["quote_time"], "%Y%m%d%H%M%S")
            vol = _dec(r["volume_lots"]) if r["volume_lots"] not in (None, "") else None
            bid_l = _dec(r["bid1_lots"]) if r["bid1_lots"] not in (None, "") else None
            ask_l = _dec(r["ask1_lots"]) if r["ask1_lots"] not in (None, "") else None
            amount = _dec(r["amount_yuan"]) if r["amount_yuan"] not in (None, "") else None
        prev = _px(r["prev_close"])
        out.append({"code": code, "name": r["name"], "kind": "native", "poll_at": poll_at, "quote_at": qt.replace(tzinfo=CST).astimezone(UTC),
                    "price": _px(r["last"]), "prev_close": prev, "open": _px(r["open"]), "high": _px(r["high"]), "low": _px(r["low"]),
                    "bid1_px": _px(r["bid1_price"]), "ask1_px": _px(r["ask1_price"]), "bid1_lots": None if bid_l is None else float(bid_l),
                    "ask1_lots": None if ask_l is None else float(ask_l), "volume_lots": None if vol is None else float(vol), "amount_yuan": amount,
                    "limit_up": _limit_up(code, r["name"], prev, day)})
    return out


def _native_frame(rows: list[dict], day: date, stream: str) -> pl.DataFrame:
    """Same columns as the imported snapshots; levels the Sina/Tencent single-quote endpoint does not carry are NULL."""
    schema = LR_SNAPSHOT_SCHEMA
    cols = {c: [r.get(c) for r in rows] for c in schema}
    cols["source_stream"] = [stream] * len(rows)
    cols["source_part"] = ["native"] * len(rows)
    cols["session_date"] = [day] * len(rows)
    return pl.DataFrame(cols, schema=schema)


def _snapshot_schema() -> dict:
    sch: dict = {"source_stream": pl.String, "source_part": pl.String, "session_date": pl.Date, "code": pl.String, "name": pl.String, "kind": pl.String,
                 "poll_at": pl.Datetime("ms", "UTC"), "quote_at": pl.Datetime("ms", "UTC")}
    for c in (*LR._QUOTE_PX,):
        sch[c] = pl.Decimal(18, 4)
    for c, p in LR._QUOTE_DEC.items():
        sch[c] = pl.Decimal(*p)
    for c in LR._QUOTE_FLOAT:
        sch[c] = pl.Float64
    return sch


LR_SNAPSHOT_SCHEMA = _snapshot_schema()


class IntradayRecorder:
    """Polls Sina/Tencent quotes of explicit codes and publishes the same snapshot dataset shape (NOT scheduled).

    One request per chunk of 50 codes through a ``RateLimitedClient`` (<= 1 request/s overall, hence per host); the first block
    (403/429/456/501) stops the run at once after flushing what was captured (circuit OPEN, exit code 2 in the CLI)."""

    def __init__(self, svc: LiveService, client: RateLimitedClient, *, source: str = "sina", chunk: int = 50,
                 sleep: Callable[[float], None] | None = None):
        if source not in ("sina", "tencent"):
            raise ValueError("source must be sina or tencent")
        self.svc, self.client, self.source, self.chunk = svc, client, source, chunk
        import time as _t

        self.sleep = sleep or _t.sleep
        self.adapter = sina.SinaAdapter(client) if source == "sina" else tencent.TencentAdapter(client)
        self.spec = registry.spec(source, "hq_quote" if source == "sina" else "qt_quote")
        self._caps: list[Capture] = []
        self._rows: list[dict] = []

    @staticmethod
    def in_poll_window(t: time) -> bool:
        return any(a <= t < b for a, b in POLL_WINDOWS)

    def poll_once(self, codes: list[str], day: date) -> int:
        n = 0
        for i in range(0, len(codes), self.chunk):
            part = codes[i:i + self.chunk]
            pfx = {"6": "sh", "0": "sz", "3": "sz"}
            symbols = [pfx[c[0]] + c for c in part if c[:1] in pfx]
            cap = self.svc.data._fetch(self.spec, f"record:{day}", lambda sy=symbols: self.adapter.quotes(sy))
            records, _empty = (sina.parse_hq if self.source == "sina" else tencent.parse_qt)(cap.body)
            self._caps.append(cap)
            self._rows += native_rows(self.source, records, cap.fetched_at, day)
            n += len(records)
        return n

    def flush(self, day: date, s: Summary) -> None:
        if not self._rows:
            return
        svc, now = self.svc, self.svc.clock()
        df = _native_frame(self._rows, day, "native")
        stamp = now.strftime("%H%M%S")
        batch_id = ingest_repo.create_batch(svc.conn, self.spec, f"record:{day}:{stamp}", now)
        owner = f"job:{batch_id}"
        try:
            for cap in self._caps:
                svc.data.store_capture(batch_id, cap, owner)
            with svc.conn.transaction():
                ingest_repo.commit_raw(svc.conn, batch_id, svc.clock())
            prev = svc.latest_ready(DATASET_QUOTES, day)
            parts = dict((prev["coverage_document"].get("parts") or {}) if prev else {})
            old = {}
            if prev:
                old = {m["sha256"]: ObjectRef(m["sha256"], m["path"], m["size"], m.get("kind", "data")) for m in svc.lake.read_manifest(prev["content_hash"]).members}
            ref = svc.lake.put_parquet(df, owner=owner)
            name = f"native/part-{stamp}.parquet"
            members = {k: old[v["object_sha256"]] for k, v in parts.items()}
            members[name] = ref
            parts[name] = {"stream": "native", "source_sha256": ref.sha256, "object_sha256": ref.sha256, "rows": df.height, "source_size": ref.size}
            total = sum(v["rows"] for v in parts.values())
            coverage = {"dataset": DATASET_QUOTES, "session_date": day.isoformat(), "parts": dict(sorted(parts.items())), "imported_at": _z(now),
                        "recorder": f"pulse_native_{self.source}", "capability": "sampled_snapshots_estimated", "not_tick_data": True,
                        "book_levels": "level 1 only (single-quote endpoint); levels 2-5 are NULL", "px_zero_is_missing": True,
                        "limit_up": "from the versioned price-limit rules and the previous close; NULL when unverified"}
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
        s.add("rows_published", df.height)
        s.add("flushes")
        self._rows, self._caps = [], []

    def run(self, codes: list[str], day: date, until: time, *, cycle_seconds: float = 60.0, flush_every: int = 10) -> Summary:
        s = Summary(f"intraday-record {day} until {until}")
        cycles = 0
        try:
            while True:
                cst_now = self.svc.clock().astimezone(CST)
                if cst_now.time() >= until:
                    break
                if not self.in_poll_window(cst_now.time()):
                    self.sleep(30.0)
                    s.add("idle_waits")
                    continue
                t0 = self.svc.clock()
                try:
                    s.add("rows_polled", self.poll_once(codes, day))
                    s.add("cycles")
                    cycles += 1
                except TransportFailure as exc:
                    s.add("transport_errors")
                    s.notes.append(str(exc)[:120])
                if cycles and cycles % flush_every == 0 and self._rows:
                    self.flush(day, s)
                self.sleep(max(0.0, cycle_seconds - (self.svc.clock() - t0).total_seconds()))
        finally:
            self.flush(day, s)               # also after a block: what was captured before it is kept
        return s
