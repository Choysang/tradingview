"""Data-plane orchestration: raw -> observation -> bars -> manifest -> READY (W03/W04, M1).

Why: one place wires adapters, the pure domain and the repositories into the
recoverable protocol of database_design.md section 5:
  1. raw bytes are written to the lake (content addressed, pinned by the job) and
     recorded as ``ingest.raw_object`` + ``ingest.batch_member``; the batch becomes
     RAW_COMMITTED before anything is normalized or any cursor moves;
  2. normalization is profile driven (domain.units); observations are append-only
     revisions, bars are projected only for records that are not invalid;
  3. the lake manifest (immutable member list) is committed, PG records
     COMMITTED_UNPUBLISHED + dependencies, validates, then flips READY together with
     the outbox event; only afterwards is the job's lake pin released.
Every fetch attempt also updates ``ingest.source_health`` through the pure state
machine (blocked -> circuit OPEN and the run stops; login page -> auth_required;
schema drift -> raw kept, canonical paused).

Invariants: services never import HTTP/SDK code (adapters are injected); commits are
explicit short transactions on an autocommit connection; clocks are injected.
Not covered: scheduling/daemons (a caller loops over these methods), PDF bodies.
"""
from __future__ import annotations

import io
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Sequence

import polars as pl
import psycopg

from pulse import contracts
from pulse.adapters.lake import LakeStore, ManifestRef, ObjectRef
from pulse.adapters.postgres import evidence_repo, ingest_repo, instrument_repo, market_repo
from pulse.adapters.sources import cninfo as cninfo_mod
from pulse.adapters.sources import legacy as legacy_mod
from pulse.adapters.sources import registry, sina, tencent
from pulse.adapters.sources.http import Capture, SourceBlocked, TransportFailure
from pulse.adapters.sources.legacy import LegacyWarehouse
from pulse.adapters.sources.registry import EndpointSpec
from pulse.domain.evidence import announcements as ann
from pulse.domain.evidence.health import apply_attempt
from pulse.domain.instruments import identity as ident
from pulse.domain.market import builders, reconcile
from pulse.domain.market.bars import Bar
from pulse.domain.market.calendar import CST, TradingCalendar
from pulse.domain.market.capability import REGISTRY, Requirement, RoutingDecision, route
from pulse.domain.market.units import MappingDrift, canonical_json, detect_unit_change, normalize, sha256_json

UTC = timezone.utc
ALL_ALIAS_SOURCES = ("legacy_warehouse", "sina", "tencent", "cninfo_announcements")
INDEX_NAMES = {"000001": "上证指数", "000300": "沪深300", "000852": "中证1000", "000905": "中证500", "000985": "中证全指",
               "399006": "创业板指"}


class SchemaDrift(RuntimeError):
    """A response no longer matches its registered profile; canonical publication is paused."""


@dataclass
class Summary:
    name: str
    counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def render(self) -> str:
        lines = [f"== {self.name} =="] + [f"  {k}: {v}" for k, v in sorted(self.counts.items())] + [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class DataService:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, *, clock: Callable[[], datetime] | None = None):
        if not conn.autocommit:
            raise ValueError("DataService needs an autocommit connection; it opens explicit short transactions")
        self.conn = conn
        self.lake = lake
        self.clock = clock or (lambda: datetime.now(UTC))

    # ---- bootstrap -----------------------------------------------------------------------------
    def bootstrap(self, partition_from: date = date(2024, 1, 1), partition_through: date | None = None) -> None:
        now = self.clock()
        with self.conn.transaction():
            ingest_repo.bootstrap(self.conn, registry.SPECS.values(), now)
        through = partition_through or (now.date() + timedelta(days=370))
        with self.conn.transaction():
            market_repo.ensure_partitions(self.conn, partition_from, through)

    # ---- instruments ---------------------------------------------------------------------------
    def register_universe(self, securities: pl.DataFrame, listings: pl.DataFrame | None, *, anchor: str) -> dict:
        """Instruments + aliases + dated state versions from legacy securities snapshots (every board)."""
        now = self.clock()
        listed = {} if listings is None else {r["code"]: r["list_date"] for r in listings.iter_rows(named=True)}
        latest = securities.sort(["as_of", "source_file"]).unique("code", keep="last")
        records, skipped = [], 0
        for r in latest.iter_rows(named=True):
            try:
                ld = r.get("list_date")
                rec_listed = listed.get(r["code"]) or (date.fromisoformat(ld) if ld else None)
                records.append(ident.InstrumentRecord.build(r["code"], r["name"], rec_listed))
            except ValueError:
                skipped += 1  # outside the supported A-share ranges (B shares, funds, ...)
        with self.conn.transaction():
            new = instrument_repo.upsert_instruments(self.conn, records, now, ALL_ALIAS_SOURCES, anchor)
        by_code = {r.code: r for r in records}
        states = 0
        history = securities.sort(["as_of", "source_file"]).select(["code", "as_of", "name", "source_file"])
        with self.conn.transaction():
            last_name: dict[str, str] = {}
            for r in history.iter_rows(named=True):
                rec = by_code.get(r["code"])
                if rec is None or last_name.get(r["code"]) == r["name"]:
                    continue
                last_name[r["code"]] = r["name"]
                clean, st = ident.parse_display_name(r["name"])
                snap = ident.InstrumentRecord(rec.instrument_id, rec.venue_code, rec.code, rec.instrument_type, clean,
                                              rec.board_code, st, rec.listed_on)
                if instrument_repo.append_state_if_changed(self.conn, snap, date.fromisoformat(r["as_of"]), now,
                                                           f"legacy_securities:{r['source_file']}:first_observed_snapshot") is not None:
                    states += 1
        return {"instruments_seen": len(records), "instruments_new": new, "unsupported_codes_skipped": skipped, "state_versions_appended": states}

    def register_indexes(self, symbols: Iterable[str]) -> dict[str, uuid.UUID]:
        now = self.clock()
        out, recs = {}, []
        for s in symbols:
            venue = ident.XSHE if s.startswith("399") else ident.XSHG
            rec = ident.InstrumentRecord(ident.index_uuid(venue, s), venue, s, "index", INDEX_NAMES.get(s, s), "index", False)
            recs.append(rec); out[s] = rec.instrument_id
        with self.conn.transaction():
            instrument_repo.upsert_instruments(self.conn, recs, now, ("legacy_warehouse",), "legacy_index_bars")
            for rec in recs:
                instrument_repo.append_state_if_changed(self.conn, rec, date(2023, 10, 31), now, "legacy_index_bars:first_observed")
        return out

    # ---- health ----------------------------------------------------------------------------------
    def record_attempt(self, spec: EndpointSpec, partition: str, outcome: str, *, error_code: str | None = None, **kw):
        now = self.clock()
        with self.conn.transaction():
            st = ingest_repo.load_health(self.conn, spec, partition)
            st = apply_attempt(st, outcome, now, **({"error_code": error_code} if error_code else {}), **kw)
            ingest_repo.save_health(self.conn, spec, partition, st, now)
        return st

    def _fetch(self, spec: EndpointSpec, partition: str, call: Callable[[], Capture]) -> Capture:
        """One HTTP attempt: block -> health OPEN and re-raise; transport failure -> health + re-raise."""
        try:
            return call()
        except SourceBlocked as exc:
            self.record_attempt(spec, partition, "blocked", error_code=str(exc))
            raise
        except TransportFailure as exc:
            self.record_attempt(spec, partition, "transport_error", error_code=str(exc)[:80])
            raise

    # ---- raw capture ------------------------------------------------------------------------------
    def store_capture(self, batch_id: uuid.UUID, cap: Capture, owner: str, *, published_at: datetime | None = None,
                      precision: str = "unknown", origin_group: str | None = None) -> tuple[uuid.UUID, ObjectRef]:
        ext = {"application/json": ".json", "text/plain": ".txt", "text/html": ".html"}.get(cap.mime_type, ".bin")
        ref = self.lake.put_bytes(cap.body, owner=owner, kind="raw", suffix=ext)  # durable before the DB learns about it
        ingested = max(self.clock(), cap.fetched_at)
        spec = registry.SPECS.get((cap.source_id, cap.endpoint_id))
        schema = spec.source_schema_version if spec else "unregistered"
        with self.conn.transaction():
            rid, _ = ingest_repo.insert_raw_object(
                self.conn, source_id=cap.source_id, endpoint_id=cap.endpoint_id, source_schema_version=schema,
                source_record_key=cap.request_key + "@" + _z(cap.fetched_at), content_hash=ref.sha256, storage_uri=ref.rel_path,
                mime_type=cap.mime_type, published_at=published_at, source_time_precision=precision, first_seen_at=cap.fetched_at,
                ingested_at=ingested, origin_group=origin_group)
            ingest_repo.add_batch_member(self.conn, batch_id, rid, cap.fetched_at)
        return rid, ref

    # ---- dataset publish ----------------------------------------------------------------------------
    def publish_dataset(self, *, dataset_id: str, snapshot_id: uuid.UUID, batch_id: uuid.UUID, frames: dict[str, pl.DataFrame],
                        contract_id: str, coverage: dict, owner: str, available_watermark: datetime | None,
                        dependencies: Sequence[tuple[uuid.UUID, str]] = (), required_roles: Sequence[str] = (),
                        projector: Callable[[], None] | None = None) -> dict:
        members = [self.lake.put_parquet(df, owner=owner) for _, df in sorted(frames.items())]
        row_count = sum(df.height for df in frames.values())
        manifest = self.lake.commit_manifest(dataset_id, members, owner=owner,
                                             meta={"row_count": row_count, "partitions": sorted(frames), "snapshot_id": str(snapshot_id)})
        return self._stage_and_publish(dataset_id, snapshot_id, batch_id, manifest, contract_id, coverage, owner, row_count,
                                       available_watermark, dependencies, required_roles, projector)

    def publish_raw_manifest(self, *, dataset_id: str, snapshot_id: uuid.UUID, batch_id: uuid.UUID, members: list[ObjectRef],
                             coverage: dict, owner: str, contract_id: str, available_watermark: datetime | None) -> dict:
        manifest = self.lake.commit_manifest(dataset_id, members, owner=owner, meta={"row_count": len(members), "kind": "raw_files"})
        return self._stage_and_publish(dataset_id, snapshot_id, batch_id, manifest, contract_id, coverage, owner, len(members),
                                       available_watermark, (), ())

    def _stage_and_publish(self, dataset_id, snapshot_id, batch_id, manifest: ManifestRef, contract_id, coverage, owner, row_count,
                           watermark, dependencies, required_roles, projector=None) -> dict:
        now = self.clock()
        with self.conn.transaction():
            version = market_repo.stage_manifest(
                self.conn, snapshot_id=snapshot_id, dataset_id=dataset_id, batch_id=batch_id, lake_manifest_sha=manifest.sha256,
                storage_uri=manifest.rel_path, contract_id=contract_id, schema_version="3.0.0", row_count=row_count,
                available_watermark=watermark, coverage_document=coverage, now=now, dependencies=dependencies)
        try:
            if projector is not None:
                projector()  # hot projection rows reference the staged manifest (FK), so they follow stage and precede READY
            self._validate_lake_rows(manifest, row_count)  # the validation step between COMMITTED_UNPUBLISHED and READY
            row = market_repo.publish_manifest(self.conn, snapshot_id, self.clock(), required_dependency_roles=required_roles,
                                               expected_lake_sha=manifest.sha256, expected_row_count=row_count)
        except Exception:
            with self.conn.transaction():
                market_repo.fail_manifest(self.conn, snapshot_id)
            self.lake.release(owner)
            raise
        self.lake.release(owner)  # the READY manifest is now the reference that protects the objects
        return {"snapshot_id": snapshot_id, "dataset_id": dataset_id, "manifest_version": version, "content_hash": manifest.sha256,
                "row_count": row_count, "state": row["manifest_state"]}

    def _validate_lake_rows(self, manifest: ManifestRef, expected: int) -> None:
        m = self.lake.read_manifest(manifest.sha256)
        if m.meta.get("kind") == "raw_files":
            for x in m.members:
                self.lake.read_object(x)  # re-verifies every member's sha256
            return
        total = 0
        for x in m.members:
            total += pl.read_parquet(io.BytesIO(self.lake.read_object(x))).height
        if total != expected:
            raise ValueError(f"lake rows {total} != expected {expected}")

    def gc(self, *, min_age_seconds: float = 3600.0):
        """Reclaim unreferenced lake objects; PG supplies the manifests that must stay."""
        return self.lake.gc(lambda: market_repo.referenced_manifest_hashes(self.conn), min_age_seconds=min_age_seconds,
                            referenced_objects=lambda: market_repo.referenced_feature_hashes(self.conn))

    # ---- shared features ------------------------------------------------------------------------------------
    def compute_features(self, snapshot_id: uuid.UUID, specs: Sequence[tuple[str, dict | None]], *, decision_as_of: datetime,
                         codes: Sequence[str] | None = None, lease_epoch: int = 1) -> list[dict]:
        """Compute shared features on a READY daily snapshot and register them in market.feature_result.

        Inputs are filtered by ``available_at <= decision_as_of`` (observed-availability mode), so a bar
        revised or backfilled after the decision instant cannot leak. The result is a lake object whose
        sha256 is ``result_hash``; the FeatureKey carries the input snapshot, calendar and adjustment ids.
        An existing READY result for the same full key is returned, not recomputed.
        """
        from pulse.domain import features as F
        from pulse.domain.features.definitions import Series

        man = market_repo.get_manifest(self.conn, snapshot_id)
        if man is None or man["manifest_state"] != "READY":
            raise ValueError("features need a READY input snapshot")
        cov = man["coverage_document"]
        df = self.lake.read_snapshot_frames(man["content_hash"])
        price = ("open_price", "high_price", "low_price", "close_price") if "open_price" in df.columns else (
            "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw")
        df = df.filter(pl.col("available_at") <= decision_as_of)
        if "revision" in df.columns:  # latest visible revision per key
            df = df.sort("revision").unique(["instrument_id", "trade_date"], keep="last")
        if codes:
            wanted = {str(ident.InstrumentRecord.build(c, c).instrument_id) for c in codes}
            df = df.filter(pl.col("instrument_id").is_in(wanted))
        ids = tuple(sorted(df["instrument_id"].unique().to_list()))
        if not ids:
            raise ValueError("no visible input rows at decision_as_of")
        out = []
        for fid, params in specs:
            key = F.make_key(fid, params, instrument_ids=ids, timeframe="1d", adjustment_mode=cov.get("adjustment_mode", "raw"),
                             snapshot_id=str(snapshot_id), decision_as_of=decision_as_of,
                             availability_policy_version="observed-actual-available-at-v1", session_policy_version="regular_continuous_v1",
                             input_revision=f"manifest:{man['content_hash'][:16]}", input_schema_version=f"{man['contract_id']}-{man['schema_version']}",
                             universe_policy_version="explicit-instrument-list", calendar_version=cov.get("calendar_version", "not-applicable"),
                             adjustment_version=cov.get("adjustment_version", "not-applicable"), market_rules_version="not-applicable")
            contracts.check("FeatureKey", key.to_wire())
            existing = market_repo.get_feature(self.conn, key)
            if existing and existing["result_state"] == "READY":
                out.append({"feature_key_hash": existing["feature_key_hash"], "reused": True, "result_hash": existing["result_hash"]})
                continue
            if not market_repo.begin_feature(self.conn, key, lease_epoch):
                raise RuntimeError("feature key is owned by a newer lease epoch")
            try:
                frames = []
                for iid in ids:
                    g = df.filter(pl.col("instrument_id") == iid).sort("trade_date")
                    f = lambda c: tuple(None if v is None else float(v) for v in g[c].to_list())  # noqa: E731
                    series = Series(tuple(d.isoformat() for d in g["trade_date"].to_list()), f(price[0]), f(price[1]), f(price[2]),
                                    f(price[3]), f("volume_shares"))
                    res = F.compute(fid, series, params)
                    frames.append(pl.DataFrame({"instrument_id": iid, "trade_date": g["trade_date"], **res}))
                result = pl.concat(frames)
                ref = self.lake.put_parquet(result, owner=f"feature:{lease_epoch}", kind="feature")
                with self.conn.transaction():
                    ok = market_repo.finish_feature(self.conn, key, lease_epoch, ok=True, result_uri=ref.rel_path, result_hash=ref.sha256,
                                                    computed_at=self.clock())
                if not ok:
                    raise RuntimeError("lease epoch fenced out; result discarded")
            except Exception:
                market_repo.finish_feature(self.conn, key, lease_epoch, ok=False)
                raise
            finally:
                self.lake.release(f"feature:{lease_epoch}")  # after PG knows the artifact (READY) or the attempt failed
            out.append({"feature_key_hash": market_repo.key_hash(key), "reused": False, "result_hash": ref.sha256, "rows": result.height})
        return out

    # ---- observations ------------------------------------------------------------------------------------
    def _existing_hashes(self, entity_key: str, source_id: str, endpoint_id: str, natural_keys: Sequence[str]) -> dict[str, tuple[int, str, uuid.UUID]]:
        rows = self.conn.execute(
            "SELECT o.natural_key, o.observation_revision, o.content_hash, o.observation_id FROM ingest.observation o"
            " JOIN ingest.raw_object r USING (raw_object_id) WHERE o.entity_key=%s AND r.source_id=%s AND r.endpoint_id=%s"
            " AND o.natural_key = ANY(%s) ORDER BY o.observation_revision DESC", (entity_key, source_id, endpoint_id, list(natural_keys))).fetchall()
        out: dict[str, tuple[int, str, uuid.UUID]] = {}
        for r in rows:
            out.setdefault(r["natural_key"], (r["observation_revision"], r["content_hash"], r["observation_id"]))
        return out

    def _bar_observation(self, spec: EndpointSpec, n: dict, bar: Bar, raw_id: uuid.UUID, natural_key: str, revision: int,
                         recorded_at: datetime) -> dict:
        payload = builders.observation_payload(bar)
        return {
            "observation_id": ingest_repo.observation_id(raw_id, natural_key, revision), "raw_object_id": raw_id,
            "contract_id": spec.contract_id, "schema_version": "3.0.0", "mapping_version": spec.mapping_version,
            "entity_type": "instrument", "entity_key": bar.instrument_id, "natural_key": natural_key,
            "observation_revision": revision, "event_time": bar.bar_close_time, "available_at": bar.available_at,
            "recorded_at": recorded_at, "quality_status": n["quality_status"], "canonical_payload": payload,
            "field_quality": builders.compact_field_quality(n, spec.profile["profile_id"]), "content_hash": sha256_json(payload)}

    # ---- live bars (daily + minute) ----------------------------------------------------------------------
    def _ingest_bar_capture(self, spec: EndpointSpec, cap: Capture, rows: list[dict], batch_id: uuid.UUID, raw_id: uuid.UUID,
                            resolver, *, minutes: int | None, project: bool, snapshot_id: uuid.UUID, summary: Summary) -> list[tuple[Bar, dict, uuid.UUID | None]]:
        """Normalize rows -> observations (append-only revisions) -> list of (bar, normalized, observation_id)."""
        now = self.clock()
        t_first = _z(cap.fetched_at)
        out: list[tuple[Bar, dict, uuid.UUID | None]] = []
        obs_rows: list[dict] = []
        pending: list[tuple[Bar, dict, str]] = []
        for row in rows:
            ctx = {"source_id": spec.source_id, "endpoint_id": spec.endpoint_id, "source_schema_version": spec.source_schema_version,
                   "mapping_version": spec.mapping_version, "first_seen_at": t_first, "ingested_at": _z(max(now, cap.fetched_at)),
                   "available_at": _z(max(now, cap.fetched_at)), "raw_object_id": str(raw_id)}
            try:
                n = normalize(spec.profile, row, ctx, resolver)
            except MappingDrift as exc:
                raise SchemaDrift(str(exc)) from exc
            if n.get("instrument_id") is None:
                summary.add("rows_unresolved_identity")
                continue
            try:
                bar = builders.minute_bar(n, minutes) if minutes else builders.daily_bar(n)
            except builders.OutOfSession:
                summary.add("rows_outside_regular_session")
                continue
            if n["quality_status"] == "invalid":
                summary.add("rows_invalid")
                with self.conn.transaction():
                    ingest_repo.record_issue(self.conn, issue_code="invalid_canonical_record", severity="warning", now=now, batch_id=batch_id,
                                             raw_object_id=raw_id, payload={"issues": n["issues"], "endpoint": spec.endpoint_id,
                                                                            "bar": n.get("trade_date")})
                continue
            tf = f"{minutes}m" if minutes else "1d"
            nat = f"{bar.instrument_id}|{tf}|{bar.bar_close_time.isoformat()}|raw"
            pending.append((bar, n, nat))
        by_inst: dict[str, list[tuple[Bar, dict, str]]] = defaultdict(list)
        for item in pending:
            by_inst[item[0].instrument_id].append(item)
        for inst, items in by_inst.items():
            existing = self._existing_hashes(inst, spec.source_id, spec.endpoint_id, [x[2] for x in items])
            for bar, n, nat in items:
                payload_hash = sha256_json(builders.observation_payload(bar))
                prev = existing.get(nat)
                if prev is not None and prev[1] == payload_hash:
                    summary.add("observations_unchanged")
                    out.append((bar, n, prev[2]))
                    continue
                revision = 1 if prev is None else prev[0] + 1
                o = self._bar_observation(spec, n, bar, raw_id, nat, revision, now)
                obs_rows.append(o)
                out.append((bar, n, o["observation_id"]))
                if revision > 1:
                    summary.add("observation_revisions_appended")
        with self.conn.transaction():
            created = ingest_repo.insert_observations(self.conn, obs_rows)
        summary.add("observations_created", created)
        return out

    def _project_and_publish_bars(self, *, spec: EndpointSpec, dataset_id: str, batch_id: uuid.UUID, snapshot_id: uuid.UUID,
                                  bars: list[tuple[Bar, dict, uuid.UUID | None]], owner: str, summary: Summary, calendar_version: str,
                                  extra_coverage: dict | None = None) -> dict | None:
        if not bars:
            summary.notes.append("no canonical bars produced; no snapshot published")
            return None
        # contract proof on every distinct payload shape (full check on all rows is cheap for live volumes)
        rows, frame_rows = [], []
        for bar, n, obs_id in bars:
            fq = builders.compact_field_quality(n, spec.profile["profile_id"])
            contracts.check("CanonicalBar", builders.wire_bar_payload(bar, snapshot_id=str(snapshot_id), field_quality=fq))
            rows.append({"trade_date": bar.trade_date, "instrument_id": uuid.UUID(bar.instrument_id), "timeframe": bar.timeframe,
                         "session_scope": bar.session_scope, "bar_open_time": bar.bar_open_time, "bar_close_time": bar.bar_close_time,
                         "snapshot_id": snapshot_id, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                         "volume_shares": bar.volume_shares, "turnover_cny": bar.turnover_cny,
                         "exchange_reference_price": bar.exchange_reference_price, "is_final": bar.is_final,
                         "available_at": bar.available_at, "quality_status": bar.quality_status, "observation_id": obs_id})
            frame_rows.append({"instrument_id": bar.instrument_id, "trade_date": bar.trade_date, "timeframe": bar.timeframe,
                               "bar_open_time": bar.bar_open_time, "bar_close_time": bar.bar_close_time,
                               "open_price_raw": bar.open, "high_price_raw": bar.high, "low_price_raw": bar.low,
                               "close_price_raw": bar.close, "volume_shares": bar.volume_shares, "turnover_amount_cny": bar.turnover_cny,
                               "is_final": bar.is_final, "available_at": bar.available_at, "quality_status": bar.quality_status})
        def projector() -> None:
            with self.conn.transaction():
                summary.add("bar_projection_rows", market_repo.insert_bars(self.conn, rows))

        df = _bars_frame(frame_rows)
        months = {m: g for m, g in _split_months(df).items()}
        sessions = sorted({b.trade_date for b, _, _ in bars})
        coverage = {"dataset": dataset_id, "price_basis": "raw", "adjustment_mode": "raw", "units": "volume shares (interval), prices CNY",
                    "instruments": len({b.instrument_id for b, _, _ in bars}), "rows": len(bars),
                    "first_date": sessions[0].isoformat(), "last_date": sessions[-1].isoformat(), "calendar_version": calendar_version,
                    "capability": REGISTRY[spec.capability_dataset].to_doc(), "turnover_amount": "missing at source (NULL, never 0)",
                    "finality_policy": builders.DAILY_FINAL_POLICY if bars[0][0].timeframe == "1d" else builders.MINUTE_FINAL_POLICY,
                    "non_final_rows": sum(1 for b, _, _ in bars if not b.is_final), **(extra_coverage or {})}
        watermark = max(b.available_at for b, _, _ in bars)
        return self.publish_dataset(dataset_id=dataset_id, snapshot_id=snapshot_id, batch_id=batch_id, frames=months,
                                    contract_id="CanonicalBar", coverage=coverage, owner=owner, available_watermark=watermark,
                                    projector=projector)

    def _run_bar_job(self, *, spec: EndpointSpec, dataset_id: str, symbols: dict[str, str], fetch: Callable[[str], Capture],
                     parse: Callable[[bytes, str], list[dict]], minutes: int | None, partition: str, summary: Summary,
                     source_for_alias: str, project: bool = True) -> dict | None:
        """Generic per-symbol loop for kline-style endpoints; stops at the first block."""
        now = self.clock()
        batch_id = ingest_repo_create_batch(self, spec, partition, now)
        owner = f"job:{batch_id}"
        snapshot_id = uuid.uuid4()
        resolver = instrument_repo.alias_resolver(self.conn, source_for_alias)
        collected: list[tuple[Bar, dict, uuid.UUID | None]] = []
        captures: list[tuple[Capture, uuid.UUID, list[dict]]] = []
        try:
            for code, symbol in symbols.items():
                cap = self._fetch(spec, partition, lambda s=symbol: fetch(s))
                try:
                    rows = parse(cap.body, symbol)
                except (ann.NotData, ann.SchemaChanged) as exc:
                    raw_id, _ = self.store_capture(batch_id, cap, owner)  # keep the evidence, publish nothing from it
                    outcome = "auth_required" if _looks_like_login(cap.body) else ("schema_changed" if isinstance(exc, ann.SchemaChanged) else "parse_error")
                    self.record_attempt(spec, partition, outcome, error_code=str(exc)[:80])
                    summary.add(f"captures_{outcome}")
                    continue
                raw_id, _ = self.store_capture(batch_id, cap, owner)
                captures.append((cap, raw_id, rows))
                summary.add("captures_stored")
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            ok_captures = 0
            for cap, raw_id, rows in captures:
                try:
                    collected += self._ingest_bar_capture(spec, cap, rows, batch_id, raw_id, resolver, minutes=minutes, project=project,
                                                          snapshot_id=snapshot_id, summary=summary)
                    ok_captures += 1
                except SchemaDrift as exc:
                    self.record_attempt(spec, partition, "schema_changed", error_code=str(exc)[:80])
                    summary.add("captures_schema_changed")
                    summary.notes.append(f"canonical publication paused for {spec.endpoint_id}: {exc}")
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            published = self._project_and_publish_bars(spec=spec, dataset_id=dataset_id, batch_id=batch_id, snapshot_id=snapshot_id,
                                                       bars=collected, owner=owner, summary=summary, calendar_version="not-applicable")
            newest = max((b.bar_close_time for b, _, _ in collected), default=None)
            new_records = summary.counts.get("observations_created", 0)
            if ok_captures:  # a login page / drifted payload already recorded its failure; it must not be overwritten by "healthy"
                self.record_attempt(spec, partition, "success_data", new_records=new_records,
                                    newest_content_at=newest if newest and newest <= self.clock() else None,
                                    content_expected_now=_session_open(self.clock()), canonical_ready=published is not None)
            return published
        except SourceBlocked:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="blocked")
            summary.notes.append("source blocked the client: stopped immediately, health set to unavailable (circuit OPEN)")
            self.lake.release(owner)
            raise
        except Exception:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="job_error")
            self.lake.release(owner)
            raise

    def ingest_daily(self, adapter, source_id: str, codes: Sequence[str], *, count: int = 320) -> Summary:
        """Raw daily bars for ``codes`` from Sina (``kline_daily``) or Tencent (``kline_day``)."""
        s = Summary(f"live-daily {source_id}")
        recs = {c: ident.InstrumentRecord.build(c, c) for c in codes}
        if source_id == "sina":
            spec, fetch, parse = registry.spec("sina", "kline_daily"), (lambda sym: adapter.kline(sym, 240, count)), sina.parse_kline
            dataset = "bars_raw_1d"
        elif source_id == "tencent":
            spec, fetch, parse = (registry.spec("tencent", "kline_day"), (lambda sym: adapter.daily(sym, count)),
                                  tencent.parse_kline_day)
            dataset = "bars_raw_1d_tencent"
        else:
            raise ValueError("source must be sina or tencent")
        symbols = {c: r.symbols[source_id] for c, r in recs.items()}
        pub = self._run_bar_job(spec=spec, dataset_id=dataset, symbols=symbols, fetch=fetch, parse=parse, minutes=None,
                                partition=f"daily:{','.join(sorted(codes))[:200]}", summary=s, source_for_alias=source_id)
        if pub:
            s.counts["manifest_version"] = pub["manifest_version"]
        return s

    def ingest_minute(self, adapter, source_id: str, codes: Sequence[str], *, period_minutes: int = 1, count: int = 240) -> Summary:
        s = Summary(f"minute {source_id} {period_minutes}m")
        recs = {c: ident.InstrumentRecord.build(c, c) for c in codes}
        if source_id == "tencent":
            if period_minutes not in (1, 5, 15, 30, 60):
                raise ValueError("tencent minute periods: 1/5/15/30/60")
            period = f"m{period_minutes}"
            spec, fetch = registry.spec("tencent", "kline_minute"), (lambda sym: adapter.minute(sym, period, count))
            parse = lambda body, sym: tencent.parse_mkline(body, sym, period)  # noqa: E731
            dataset = "bars_raw_1m" if period_minutes == 1 else f"bars_raw_{period_minutes}m_tencent"
        elif source_id == "sina":
            if period_minutes not in (5, 15, 30, 60):
                raise ValueError("sina minute scales: 5/15/30/60")
            spec, fetch = registry.spec("sina", "kline_minute"), (lambda sym: adapter.kline(sym, period_minutes, count))
            parse = sina.parse_kline
            dataset = f"bars_raw_{period_minutes}m"
        else:
            raise ValueError("source must be tencent or sina")
        symbols = {c: r.symbols[source_id] for c, r in recs.items()}
        pub = self._run_bar_job(spec=spec, dataset_id=dataset, symbols=symbols, fetch=fetch, parse=parse, minutes=period_minutes,
                                partition=f"minute{period_minutes}:{','.join(sorted(codes))[:180]}", summary=s, source_for_alias=source_id)
        if pub:
            s.counts["manifest_version"] = pub["manifest_version"]
        return s

    # ---- quotes (observations only; quote_projection belongs to the quote snapshot job) -------------------------
    def ingest_quotes(self, adapter, source_id: str, codes: Sequence[str]) -> Summary:
        s = Summary(f"quotes {source_id}")
        recs = {c: ident.InstrumentRecord.build(c, c) for c in codes}
        spec = registry.spec(source_id, "hq_quote" if source_id == "sina" else "qt_quote")
        symbols = [r.symbols[source_id] for r in recs.values()]
        partition = f"quote:{','.join(sorted(codes))[:200]}"
        now = self.clock()
        batch_id = ingest_repo_create_batch(self, spec, partition, now)
        owner = f"job:{batch_id}"
        try:
            cap = self._fetch(spec, partition, lambda: adapter.quotes(symbols))
            raw_id, _ = self.store_capture(batch_id, cap, owner)
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            parsed, empty = (sina.parse_hq if source_id == "sina" else tencent.parse_qt)(cap.body)
            resolver = instrument_repo.alias_resolver(self.conn, source_id)
            rows, newest = [], None
            for rec in parsed:
                t = _z(max(self.clock(), cap.fetched_at))
                ctx = {"source_id": spec.source_id, "endpoint_id": spec.endpoint_id, "source_schema_version": spec.source_schema_version,
                       "mapping_version": spec.mapping_version, "first_seen_at": _z(cap.fetched_at), "ingested_at": t, "available_at": t,
                       "raw_object_id": str(raw_id)}
                n = normalize(spec.profile, rec, ctx, resolver)
                if n.get("instrument_id") is None:
                    s.add("rows_unresolved_identity")
                    continue
                unit_flag = None
                if n.get("cumulative_volume_shares") and n.get("cumulative_turnover_cny") and n.get("last_price_raw"):
                    unit_flag = detect_unit_change(n["cumulative_volume_shares"], n["cumulative_turnover_cny"], n["last_price_raw"])
                if unit_flag:
                    n["quality_status"] = "disputed"
                    with self.conn.transaction():
                        ingest_repo.record_issue(self.conn, issue_code="unit_mismatch", severity="error", now=self.clock(), batch_id=batch_id,
                                                 raw_object_id=raw_id, field_path="cumulative_volume_shares",
                                                 payload={"detail": unit_flag, "endpoint": spec.endpoint_id, "symbol": rec["symbol"]})
                    s.add("quotes_unit_mismatch")
                payload = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in n.items()
                           if k in ("trade_date", "event_time", "last_price_raw", "open_price_raw", "high_price_raw", "low_price_raw",
                                    "prev_close_price_raw", "bid_price_raw", "ask_price_raw", "bid_volume_shares", "ask_volume_shares",
                                    "cumulative_volume_shares", "cumulative_turnover_cny")}
                nat = f"{n['instrument_id']}|quote|{n['event_time']}"
                ev = datetime.fromisoformat(n["event_time"].replace("Z", "+00:00")) if n["event_time"] else None
                newest = max(newest, ev) if (newest and ev) else (ev or newest)
                existing = self._existing_hashes(n["instrument_id"], spec.source_id, spec.endpoint_id, [nat])
                h = sha256_json(payload)
                if nat in existing and existing[nat][1] == h:
                    s.add("observations_unchanged")
                    continue
                rev = 1 if nat not in existing else existing[nat][0] + 1
                rows.append({
                    "observation_id": ingest_repo.observation_id(raw_id, nat, rev), "raw_object_id": raw_id, "contract_id": spec.contract_id,
                    "schema_version": "3.0.0", "mapping_version": spec.mapping_version, "entity_type": "instrument",
                    "entity_key": n["instrument_id"], "natural_key": nat, "observation_revision": rev, "event_time": ev,
                    "available_at": datetime.fromisoformat(n["available_at"].replace("Z", "+00:00")), "recorded_at": self.clock(),
                    "quality_status": n["quality_status"], "canonical_payload": payload,
                    "field_quality": builders.compact_field_quality(n, spec.profile["profile_id"]), "content_hash": h})
            with self.conn.transaction():
                s.add("observations_created", ingest_repo.insert_observations(self.conn, rows))
                ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            for c in empty:
                s.notes.append(f"symbol {c} unknown to {source_id}")
            self.record_attempt(spec, partition, "success_data", new_records=len(rows), newest_content_at=newest,
                                content_expected_now=_session_open(self.clock()), canonical_ready=False)
        except SourceBlocked:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="blocked")
            raise
        finally:
            self.lake.release(owner)
        return s

    # ---- announcements -----------------------------------------------------------------------------------------
    def ingest_announcements(self, adapter, *, days: int = 3, page_size: int = 30, page_cap: int = 40) -> Summary:
        s = Summary("announcements cninfo")
        spec = registry.spec("cninfo_announcements", cninfo_mod.ENDPOINT_ID)
        now = self.clock()
        for d0, d1 in ann.date_windows(now - timedelta(days=days - 1), now):
            partition = f"se:{d0}~{d1}"
            batch_id = ingest_repo_create_batch(self, spec, partition, self.clock())
            owner = f"job:{batch_id}"
            records: list[tuple[dict, uuid.UUID]] = []
            fully_paged = False
            try:
                page = 1
                while page <= page_cap:
                    cap = self._fetch(spec, partition, lambda p=page: adapter.page(p, f"{d0}~{d1}", page_size))
                    raw_id, _ = self.store_capture(batch_id, cap, owner)
                    s.add("pages_fetched")
                    try:
                        parsed = ann.parse_response(cap.body)
                    except (ann.NotData, ann.SchemaChanged) as exc:
                        outcome = "auth_required" if _looks_like_login(cap.body) else ("schema_changed" if isinstance(exc, ann.SchemaChanged) else "parse_error")
                        self.record_attempt(spec, partition, outcome, error_code=str(exc)[:80])
                        s.add(f"pages_{outcome}")
                        s.notes.append(f"{partition} page {page}: {exc}; canonical paused, raw kept")
                        break
                    records += [(r, raw_id) for r in parsed.records]
                    if not parsed.has_more:
                        fully_paged = True
                        break
                    page += 1
                else:
                    s.notes.append(f"{partition}: page cap {page_cap} reached; window not fully paged (no withdrawal inference)")
                with self.conn.transaction():
                    ingest_repo.commit_raw(self.conn, batch_id, self.clock())
                if not records and not fully_paged:
                    with self.conn.transaction():
                        ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="no_parsable_page")
                    continue
                self._normalize_announcements(spec, partition, batch_id, records, fully_paged, d0, d1, s)
            except SourceBlocked:
                with self.conn.transaction():
                    ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="blocked")
                s.notes.append("cninfo blocked the client: stopped immediately (circuit OPEN)")
                self.lake.release(owner)
                raise
            finally:
                self.lake.release(owner)
        return s

    def _normalize_announcements(self, spec, partition, batch_id, records, fully_paged, d0, d1, s: Summary) -> None:
        now = self.clock()
        seen_ids, newest, created = set(), None, 0
        start = datetime.fromisoformat(d0).replace(tzinfo=CST).astimezone(UTC)
        end = datetime.fromisoformat(d1).replace(tzinfo=CST).astimezone(UTC) + timedelta(days=1)
        last_raw = records[-1][1] if records else None
        with self.conn.transaction():
            for rec, raw_id in records:
                doc = ann.normalize_record(rec)
                seen_ids.add(doc.source_record_id)
                prev = evidence_repo.latest_revision(self.conn, doc.source_record_id)
                prev_hash = (prev["field_quality"].get("record_content_hash") or {}).get("raw_value") if prev else None
                if prev is None:
                    revision, status = 1, "published"
                elif prev_hash == doc.content_hash and prev["document_status"] != "deleted_at_source":
                    s.add("announcements_unchanged")
                    continue
                else:
                    revision, status = prev["revision"] + 1, "corrected"
                doc.field_quality["record_content_hash"] = {"status": "observed", "reason": None, "raw_object_id": None,
                                                              "value_origin": "primary_source", "raw_value": doc.content_hash}
                first_seen = now
                rid = evidence_repo.insert_document(self.conn, doc, raw_object_id=raw_id, revision=revision, status=status,
                                                    first_seen_at=first_seen, available_at=max(self.clock(), first_seen), now=self.clock())
                crid = evidence_repo.insert_publication_claim(self.conn, doc, rid, revision, max(self.clock(), first_seen), self.clock())
                evidence_repo.insert_relation_claims(self.conn, doc, rid, revision, max(self.clock(), first_seen), self.clock())
                if evidence_repo.insert_event(self.conn, doc, crid, revision, max(self.clock(), first_seen), self.clock()):
                    s.add("events_created")
                s.add("documents_published" if revision == 1 else "documents_corrected")
                created += 1
                if doc.published_at:
                    newest = max(newest, doc.published_at) if newest else doc.published_at
            if fully_paged and last_raw is not None:
                gone = ann.detect_deleted(evidence_repo.window_ids(self.conn, start, end), seen_ids, window_fully_paged=True)
                for sid in sorted(gone):
                    prev = evidence_repo.latest_revision(self.conn, sid)
                    if prev is None:
                        continue
                    evidence_repo.append_deleted_revision(self.conn, prev, raw_object_id=last_raw, now=self.clock())
                    s.add("documents_deleted_at_source")
            ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            cur = ingest_repo.read_cursor(self.conn, spec, partition)
            ingest_repo.advance_cursor(self.conn, spec, partition, batch_id=batch_id,
                                       new_cursor={"complete": fully_paged, "records": len(records)}, watermark=newest,
                                       expected_version=cur["cursor_version"] if cur else 0, now=self.clock())
        self.record_attempt(spec, partition, "success_data" if records else "success_empty", new_records=created,
                            newest_content_at=newest if newest and newest <= self.clock() else None, content_expected_now=False,
                            canonical_ready=created > 0 or bool(records))

    # ---- legacy warehouse import (path-1 daily history) -----------------------------------------------------------
    def import_legacy(self, wh: LegacyWarehouse, since: date, *, codes: Sequence[str] | None = None, boards: str = "main") -> Summary:
        """Replayable historical import. See adapters/sources/legacy.py for the inspected units and adjustment facts."""
        s = Summary(f"legacy-import since {since}")
        spec = registry.spec("legacy_warehouse", "bars")
        s.counts.update(self.register_universe(wh.read_securities(), wh.read_listings(), anchor="legacy_securities"))
        versions = wh.read_bars_versions(since, codes=list(codes) if codes else None, main_board_only=(boards == "main"))
        distinct = LegacyWarehouse.distinct_versions(versions)
        s.counts["legacy_rows_all_versions"] = versions.height
        s.counts["legacy_distinct_value_versions"] = distinct.height
        s.counts["legacy_revised_keys"] = distinct.filter(pl.col("revision_count") > 1).select(["code", "date"]).unique().height
        if distinct.is_empty():
            s.notes.append("no legacy bars in the requested window")
            return s
        partition = f"bars:{since}:{boards}" + (f":{','.join(sorted(codes))[:100]}" if codes else "")
        batch_id = ingest_repo_create_batch(self, spec, partition, self.clock())
        owner = f"job:{batch_id}"
        snapshot_id, raw_snapshot_id = uuid.uuid4(), uuid.uuid4()
        try:
            raw_ids, members = self._store_legacy_files(wh, batch_id, owner, distinct, versions, s)
            with self.conn.transaction():
                ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            self.publish_raw_manifest(
                dataset_id="legacy_bars_raw_files", snapshot_id=raw_snapshot_id, batch_id=batch_id, members=members, owner=owner,
                contract_id="AdjustedBar", available_watermark=self.clock(),
                coverage={"kind": "legacy parquet files kept byte-exact", "files": len(members), "since": since.isoformat(),
                          "note": "inputs of bars_legacy_fwd_adjusted_1d; the legacy warehouse itself is never modified"})
            frames = self._legacy_normalize(spec, distinct, raw_ids, s)
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
            all_dates = sorted(distinct["date"].unique().to_list())
            cal = TradingCalendar(tuple(all_dates), all_dates[-1], source="observed-bars")
            coverage = self._legacy_coverage(distinct, all_dates, cal, since, s)
            pub = self.publish_dataset(
                dataset_id="bars_legacy_fwd_adjusted_1d", snapshot_id=snapshot_id, batch_id=batch_id, frames=frames,
                contract_id="AdjustedBar", coverage=coverage, owner=owner,
                available_watermark=distinct["ingested_at"].max(), dependencies=[(raw_snapshot_id, "source_files")],
                required_roles=["source_files"])
            s.counts["manifest_version_bars"] = pub["manifest_version"]
            s.counts["lake_rows_bars"] = pub["row_count"]
            with self.conn.transaction():
                cur = ingest_repo.read_cursor(self.conn, spec, partition)
                ingest_repo.advance_cursor(self.conn, spec, partition, batch_id=batch_id,
                                           new_cursor={"since": since.isoformat(), "max_date": all_dates[-1].isoformat(), "files": len(members)},
                                           watermark=datetime.combine(all_dates[-1], time(7, 0), tzinfo=UTC),
                                           expected_version=cur["cursor_version"] if cur else 0, now=self.clock())
            self.record_attempt(spec, partition, "success_data", new_records=s.counts.get("observations_created", 0),
                                newest_content_at=datetime.combine(all_dates[-1], time(7, 0), tzinfo=UTC),
                                content_expected_now=False, canonical_ready=True)
            self._legacy_indexes(wh, since, s)
            self._legacy_evidence_files(wh, s)
        except Exception:
            with self.conn.transaction():
                ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code="job_error")
            self.lake.release(owner)
            raise
        return s

    def _store_legacy_files(self, wh, batch_id, owner, distinct: pl.DataFrame, versions: pl.DataFrame, s: Summary):
        first_seen = {r["source_file"]: r["t"] for r in
                      versions.group_by("source_file").agg(pl.col("ingested_at").min().alias("t")).to_dicts()}
        raw_ids: dict[str, uuid.UUID] = {}
        members: list[ObjectRef] = []
        for fname in sorted(distinct["source_file"].unique().to_list()):
            data = (wh.root / "bars" / fname).read_bytes()
            ref = self.lake.put_bytes(data, owner=owner, kind="raw", suffix=".parquet")
            t0 = first_seen[fname]
            with self.conn.transaction():
                rid, _ = ingest_repo.insert_raw_object(
                    self.conn, source_id="legacy_warehouse", endpoint_id="bars", source_schema_version=legacy_mod.SCHEMA_VERSION,
                    source_record_key=fname, content_hash=ref.sha256, storage_uri=ref.rel_path, mime_type="application/vnd.apache.parquet",
                    published_at=None, source_time_precision="unknown", first_seen_at=t0, ingested_at=max(self.clock(), t0))
                ingest_repo.add_batch_member(self.conn, batch_id, rid, t0)
            raw_ids[fname] = rid
            members.append(ref)
            s.add("legacy_files_kept_raw")
            s.add("legacy_raw_bytes", len(data))
        return raw_ids, members

    def _legacy_normalize(self, spec: EndpointSpec, distinct: pl.DataFrame, raw_ids: dict[str, uuid.UUID], s: Summary) -> dict[str, pl.DataFrame]:
        resolver = instrument_repo.alias_resolver(self.conn, "legacy_warehouse")
        recorded_at = self.clock()
        profile_id = spec.profile["profile_id"]
        frames: dict[str, list[dict]] = defaultdict(list)
        month_rows = distinct.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("_m")).sort(["_m", "code", "date", "revision"])
        for (month,), chunk in month_rows.group_by("_m", maintain_order=True):
            obs_rows: list[dict] = []
            for r in chunk.iter_rows(named=True):
                t = _z(r["ingested_at"])
                raw = {"code": r["code"], "date": r["date"].isoformat(), "open": r["open"], "high": r["high"], "low": r["low"],
                       "close": r["close"], "volume": r["volume"], "amount": r["amount"]}
                ctx = {"source_id": spec.source_id, "endpoint_id": spec.endpoint_id, "source_schema_version": spec.source_schema_version,
                       "mapping_version": spec.mapping_version, "adjustment_mode": "forward_adjusted", "first_seen_at": t,
                       "ingested_at": t, "available_at": t, "raw_object_id": str(raw_ids[r["source_file"]])}
                try:
                    n = normalize(spec.profile, raw, ctx, resolver)
                except MappingDrift as exc:
                    raise SchemaDrift(str(exc)) from exc
                if n.get("instrument_id") is None or n["quality_status"] == "invalid":
                    s.add("legacy_rows_rejected")
                    with self.conn.transaction():
                        ingest_repo.record_issue(self.conn, issue_code="invalid_canonical_record", severity="warning", now=self.clock(),
                                                 raw_object_id=raw_ids[r["source_file"]],
                                                 payload={"code": r["code"], "date": raw["date"], "issues": n["issues"]})
                    continue
                bar = builders.daily_bar(n, adjusted=True, adjustment_version=legacy_mod.ADJUSTMENT_VERSION)
                nat = f"{n['instrument_id']}|1d|{bar.bar_close_time.isoformat()}|fwd_adj"
                payload = builders.observation_payload(bar)
                content_hash = sha256_json(payload)
                payload["revision_count"] = r["revision_count"]
                obs_rows.append({
                    "observation_id": ingest_repo.observation_id(raw_ids[r["source_file"]], nat, r["revision"]),
                    "raw_object_id": raw_ids[r["source_file"]], "contract_id": spec.contract_id, "schema_version": "3.0.0",
                    "mapping_version": spec.mapping_version, "entity_type": "instrument", "entity_key": n["instrument_id"],
                    "natural_key": nat, "observation_revision": r["revision"], "event_time": bar.bar_close_time,
                    "available_at": bar.available_at, "recorded_at": recorded_at, "quality_status": n["quality_status"],
                    "canonical_payload": payload, "field_quality": builders.compact_field_quality(n, profile_id),
                    "content_hash": content_hash})
                if r["revision"] == r["revision_count"]:
                    frames[month].append({
                        "instrument_id": n["instrument_id"], "trade_date": bar.trade_date, "timeframe": "1d",
                        "bar_open_time": bar.bar_open_time, "bar_close_time": bar.bar_close_time, "open_price": bar.open,
                        "high_price": bar.high, "low_price": bar.low, "close_price": bar.close, "volume_shares": bar.volume_shares,
                        "turnover_amount_cny": bar.turnover_cny, "adjustment_mode": "forward_adjusted",
                        "adjustment_version": legacy_mod.ADJUSTMENT_VERSION, "is_final": bar.is_final, "available_at": bar.available_at,
                        "quality_status": bar.quality_status, "revision": r["revision"]})
            with self.conn.transaction():
                s.add("observations_created", ingest_repo.insert_observations(self.conn, obs_rows))
            s.add("observation_rows_built", len(obs_rows))
        dec = pl.Decimal(38, 12)
        schema = {"instrument_id": pl.String, "trade_date": pl.Date, "timeframe": pl.String, "bar_open_time": pl.Datetime("us", "UTC"),
                  "bar_close_time": pl.Datetime("us", "UTC"), "open_price": dec, "high_price": dec, "low_price": dec, "close_price": dec,
                  "volume_shares": pl.Int64, "turnover_amount_cny": dec, "adjustment_mode": pl.String, "adjustment_version": pl.String,
                  "is_final": pl.Boolean, "available_at": pl.Datetime("us", "UTC"), "quality_status": pl.String, "revision": pl.Int32}
        return {m: pl.DataFrame(rows, schema=schema).sort(["instrument_id", "trade_date"]) for m, rows in frames.items()}

    def _legacy_coverage(self, distinct: pl.DataFrame, all_dates: list[date], cal: TradingCalendar, since: date, s: Summary) -> dict:
        latest = distinct.filter(pl.col("revision") == pl.col("revision_count"))
        per = latest.group_by("code").agg(pl.len().alias("n"), pl.col("date").min().alias("d0"))
        expected = per.with_columns(pl.col("d0").map_elements(lambda d: len(cal.between(d, all_dates[-1])), return_dtype=pl.Int64).alias("exp"))
        gaps = expected.filter(pl.col("n") < pl.col("exp"))
        missing = int((gaps["exp"] - gaps["n"]).sum() or 0)
        s.counts["legacy_instruments"] = per.height
        s.counts["legacy_sessions"] = len(all_dates)
        s.counts["legacy_instruments_with_unexplained_gaps"] = gaps.height
        s.counts["legacy_unexplained_missing_session_rows"] = missing
        return {
            "dataset": "bars_legacy_fwd_adjusted_1d", "price_basis": "forward_adjusted", "adjustment_mode": "forward_adjusted",
            "adjustment_version": legacy_mod.ADJUSTMENT_VERSION,
            "adjustment_note": "legacy warehouse prices are forward adjusted with an unknown, drifting factor series; not convertible to raw",
            "units": "volume shares (float64 integral); turnover_amount_cny NULL for every row (absent in source)",
            "instruments": per.height, "rows": latest.height, "first_date": all_dates[0].isoformat(), "last_date": all_dates[-1].isoformat(),
            "since": since.isoformat(), "calendar_version": cal.version, "sessions": len(all_dates),
            "revised_keys": s.counts["legacy_revised_keys"], "distinct_value_versions": distinct.height,
            "unexplained_missing_session_rows": missing, "instruments_with_gaps": gaps.height,
            "gap_note": "missing sessions are unexplained: no suspension feed is registered, so suspension vs source gap is NOT distinguished",
            "capability": REGISTRY["bars_legacy_fwd_adjusted_1d"].to_doc(), "finality_policy": builders.DAILY_FINAL_POLICY,
            "non_final_rows": int(latest.filter(
                pl.col("ingested_at") < (pl.col("date").cast(pl.Datetime("us")) + pl.duration(hours=16)).dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC")).height)}

    def _legacy_indexes(self, wh: LegacyWarehouse, since: date, s: Summary) -> None:
        ib = wh.read_index_bars(since)
        if ib.is_empty():
            return
        ids = self.register_indexes(sorted(ib["symbol"].unique().to_list()))
        spec = registry.spec("legacy_warehouse", "index_bars")
        batch_id = ingest_repo_create_batch(self, spec, f"index_bars:{since}", self.clock())
        owner = f"job:{batch_id}"
        used = {p.replace("\\", "/").rsplit("/", 1)[-1] for p in ib["source_file"].to_list()}
        for f in wh.files("index_bars"):
            if f.name not in used:
                continue
            data = f.read_bytes()
            ref = self.lake.put_bytes(data, owner=owner, kind="raw", suffix=".parquet")
            t0 = self.clock()
            with self.conn.transaction():
                rid, _ = ingest_repo.insert_raw_object(
                    self.conn, source_id="legacy_warehouse", endpoint_id="index_bars", source_schema_version=legacy_mod.SCHEMA_VERSION,
                    source_record_key=f.name, content_hash=ref.sha256, storage_uri=ref.rel_path, mime_type="application/vnd.apache.parquet",
                    published_at=None, source_time_precision="unknown", first_seen_at=t0, ingested_at=t0)
                ingest_repo.add_batch_member(self.conn, batch_id, rid, t0)
        with self.conn.transaction():
            ingest_repo.commit_raw(self.conn, batch_id, self.clock())
        df = ib.with_columns(pl.col("symbol").map_elements(lambda x: str(ids[x]), return_dtype=pl.String).alias("instrument_id"),
                             pl.lit("1d").alias("timeframe"), pl.lit("index_points_unadjusted").alias("price_basis")
                             ).select(["instrument_id", "symbol", "date", "timeframe", "open", "high", "low", "close",
                                       "volume", "price_basis", "ingested_at"])
        pub = self.publish_dataset(
            dataset_id="index_bars_legacy_1d", snapshot_id=uuid.uuid4(), batch_id=batch_id, frames={"all": df}, contract_id="AdjustedBar",
            owner=owner, available_watermark=ib["ingested_at"].max(),
            coverage={"kind": "index daily bars", "symbols": sorted(ib["symbol"].unique().to_list()), "rows": df.height,
                      "units": "index points (not CNY); volume unit NOT verified against a live source",
                      "contract_note": "AdjustedBar requires CNY; an IndexBar contract is proposed (see M1_evidence.md)"})
        s.counts["index_bars_rows"] = pub["row_count"]
        with self.conn.transaction():
            ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")

    def _legacy_evidence_files(self, wh: LegacyWarehouse, s: Summary) -> None:
        """boards / lhb_* belong to the sector and seat packages: keep the bytes + hash, normalize nothing here."""
        spec = registry.spec("legacy_warehouse", "evidence_files")
        batch_id = ingest_repo_create_batch(self, spec, "evidence_files", self.clock())
        owner = f"job:{batch_id}"
        n = 0
        for dataset in ("boards", "lhb_seats", "lhb_stocks"):
            for f in wh.files(dataset):
                data = f.read_bytes()
                ref = self.lake.put_bytes(data, owner=owner, kind="raw", suffix=".parquet")
                t0 = self.clock()
                with self.conn.transaction():
                    rid, _ = ingest_repo.insert_raw_object(
                        self.conn, source_id="legacy_warehouse", endpoint_id="evidence_files", source_schema_version=legacy_mod.SCHEMA_VERSION,
                        source_record_key=f"{dataset}/{f.name}", content_hash=ref.sha256, storage_uri=ref.rel_path,
                        mime_type="application/vnd.apache.parquet", published_at=None, source_time_precision="unknown", first_seen_at=t0, ingested_at=t0)
                    ingest_repo.add_batch_member(self.conn, batch_id, rid, t0)
                n += 1
        with self.conn.transaction():
            ingest_repo.commit_raw(self.conn, batch_id, self.clock())
            ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
        self.lake.release(owner)
        s.counts["legacy_evidence_files_kept_raw"] = n

    # ---- reconciliation ---------------------------------------------------------------------------------------
    def reconcile_daily(self, codes: Sequence[str], *, last_n_dates: int = 5) -> Summary:
        """Compare close and volume across legacy / sina / tencent for dates present in at least two sources."""
        s = Summary("reconcile-daily")
        sources = [("legacy_warehouse", "bars"), ("sina", "kline_daily"), ("tencent", "kline_day")]
        for code in codes:
            iid = ident.InstrumentRecord.build(code, code).instrument_id
            per: dict[str, dict[str, tuple[uuid.UUID, Bar]]] = {}
            for src, ep in sources:
                rows = self.conn.execute(
                    "SELECT DISTINCT ON (o.natural_key) o.observation_id, o.canonical_payload, o.available_at, o.quality_status, o.event_time"
                    " FROM ingest.observation o JOIN ingest.raw_object r USING (raw_object_id)"
                    " WHERE o.entity_key=%s AND r.source_id=%s AND r.endpoint_id=%s AND o.canonical_payload->>'timeframe'='1d'"
                    " ORDER BY o.natural_key, o.observation_revision DESC", (str(iid), src, ep)).fetchall()
                per[src] = {r["canonical_payload"]["trade_date"]: (r["observation_id"], builders.bar_from_payload(str(iid), r["canonical_payload"], r["available_at"], r["quality_status"]))
                            for r in rows}
            dates = sorted({d for m in per.values() for d in m})[-400:]
            common = [d for d in dates if sum(d in m for m in per.values()) >= 2][-last_n_dates:]
            pairs = [("legacy_warehouse", "sina"), ("legacy_warehouse", "tencent"), ("sina", "tencent")]
            for d in common:
                for a, b in pairs:
                    if d not in per[a] or d not in per[b]:
                        continue
                    (oa, ba), (ob, bb) = per[a][d], per[b][d]
                    close = reconcile.compare_close(
                        reconcile.FieldObs(a, ba.close, ba.adjustment_mode, ba.is_final, ba.session_scope),
                        reconcile.FieldObs(b, bb.close, bb.adjustment_mode, bb.is_final, bb.session_scope), field_path=f"{code}:{d}:close_price")
                    vol = reconcile.compare_volume(
                        reconcile.FieldObs(a, ba.volume_shares, ba.adjustment_mode, ba.is_final, ba.session_scope),
                        reconcile.FieldObs(b, bb.volume_shares, bb.adjustment_mode, bb.is_final, bb.session_scope), field_path=f"{code}:{d}:volume_shares")
                    for res in (close, vol):
                        with self.conn.transaction():
                            market_repo.save_reconciliation(self.conn, res, members=[(oa, "primary"), (ob, "secondary")], decided_at=self.clock(),
                                                            extra_trace={"pair": [a, b], "date": d})
                        s.add(f"{res.field_path.split(':')[-1]}_{res.status}")
        return s


# ---- module helpers -------------------------------------------------------------------------------------------------
def ingest_repo_create_batch(svc: DataService, spec: EndpointSpec, partition: str, now: datetime) -> uuid.UUID:
    with svc.conn.transaction():
        cur = ingest_repo.read_cursor(svc.conn, spec, partition)
        return ingest_repo.create_batch(svc.conn, spec, partition, now, cursor_before=cur["committed_cursor"] if cur else None)


def _looks_like_login(body: bytes) -> bool:
    head = body[:2000].lower()
    return any(t in head for t in (b"login", b"captcha", b"verify", "登录".encode(), "验证".encode()))


def _session_open(now: datetime) -> bool:
    local = now.astimezone(CST)
    t = local.time()
    return local.weekday() < 5 and (time(9, 30) <= t <= time(11, 30) or time(13, 0) <= t <= time(15, 5))


def _bars_frame(rows: list[dict]) -> pl.DataFrame:
    dec = pl.Decimal(38, 12)
    return pl.DataFrame(rows, schema={
        "instrument_id": pl.String, "trade_date": pl.Date, "timeframe": pl.String,
        "bar_open_time": pl.Datetime("us", "UTC"), "bar_close_time": pl.Datetime("us", "UTC"),
        "open_price_raw": dec, "high_price_raw": dec, "low_price_raw": dec, "close_price_raw": dec,
        "volume_shares": pl.Int64, "turnover_amount_cny": dec, "is_final": pl.Boolean,
        "available_at": pl.Datetime("us", "UTC"), "quality_status": pl.String})


def _split_months(df: pl.DataFrame) -> dict[str, pl.DataFrame]:
    out: dict[str, pl.DataFrame] = {}
    keyed = df.with_columns(pl.col("trade_date").dt.strftime("%Y-%m").alias("_m"))
    for (m,), g in keyed.group_by("_m", maintain_order=True):
        out[f"{m}"] = g.drop("_m").sort(["instrument_id", "timeframe", "bar_close_time"])
    return out
