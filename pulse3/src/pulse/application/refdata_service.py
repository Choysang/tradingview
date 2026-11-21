"""Native reference-data producers on the data plane: adjusted bars, index bars, board membership, listing dates (Step 2, lane D).

Why: one place wires the adapters (``ref_market``, ``ref_listing``) to the recoverable protocol of ``DataService`` (raw capture
-> batch -> observations -> lake manifest -> READY) and publishes datasets beside the legacy-imported ones:
``bars_native_fwd_adjusted_1d``, ``index_bars_native_1d``, ``sector_membership_native_boards_{industry,concept}``,
``instrument_listing_native``. Frames carry the legacy schemas, so switching a consumer later is a dataset-id change.

Invariants: nothing here writes or replaces a legacy dataset or ``core.instrument``; ``available_at`` of every fact is the
fetch time; adjusted bars keep ALL revisions in the frame (a new ex-right event re-publishes the whole past of a code as
revision n+1, an identical re-fetch publishes nothing); every HTTP call goes through ``RateLimitedClient`` (<= 1 request per
second) and the first block stops the run after the already fetched items were durably published and the cursor moved
only over them; a cursor never moves past a code that failed (failed codes are retried first on the next run); a run
refuses to start while a capture endpoint's circuit is open and its cooldown has not elapsed.
Not covered: scheduling, deleting or rewriting legacy data, the cutover switch of consumers.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence

import polars as pl

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import ingest_repo, market_repo
from pulse.adapters.sources import ref_listing as L
from pulse.adapters.sources import ref_market as M
from pulse.adapters.sources import registry, tencent
from pulse.adapters.sources.http import Capture, SourceBlocked, TransportFailure
from pulse.adapters.sources.registry import EndpointSpec
from pulse.application import refdata_core as C
from pulse.application.data_service import DataService, Summary, ingest_repo_create_batch
from pulse.domain.evidence.announcements import NotData, SchemaChanged
from pulse.domain.instruments import identity as ident
from pulse.domain.market import builders
from pulse.domain.market.calendar import session_close_utc, session_open_utc
from pulse.domain.market.capability import REGISTRY
from pulse.domain.market.units import MappingDrift, normalize, sha256_json

MAX_CONSECUTIVE_FAILURES = 10
MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


class RefDataError(RuntimeError):
    """The run cannot continue for a reason that is not a block (shape drift, too many failures)."""


def instrument_id_of(code: str) -> str:
    return str(ident.instrument_uuid(ident.venue_for_code(code), code))


def _resolve(symbol: str, trade_date: str, market: str | None) -> str:
    return instrument_id_of(symbol)               # ValueError for codes outside the A-share ranges -> an invalid-row issue


def _carry(exc: Exception, summary: Summary) -> Exception:
    """Attach the partial summary so the CLI can print what was done before the run stopped."""
    exc.summary = summary                                    # type: ignore[attr-defined]
    return exc


def adj_partition(code: str) -> str:
    return f"p{code[:3]}"


@dataclass
class _Pending:
    """One code fetched and adjusted, waiting for its chunk to be normalized and published."""
    code: str
    rows: list[dict]                                   # ratio-adjusted rows ([] in a raw-only fetch)
    raw_object_id: uuid.UUID
    available_at: datetime
    source: str = "sina"
    raw_rows: list[dict] | None = None                 # the same rows BEFORE adjustment (Sina kline strings in ``text``)
    factor_rows: list[tuple[date, str]] | None = None  # Sina's qfq.js rows as served (None = no factor file fetched)


@dataclass
class _ChunkResult:
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stop: Exception | None = None
    batch_id: uuid.UUID | None = None


class RefDataService:
    def __init__(self, svc: DataService):
        self.svc = svc
        self.conn = svc.conn
        self.lake: LakeStore = svc.lake

    # ---- generic helpers ------------------------------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.svc.clock()

    def _spec(self, source: str, endpoint: str) -> EndpointSpec:
        return registry.spec(source, endpoint)

    def gate(self, specs: Sequence[EndpointSpec], partition: str) -> None:
        """Refuse to start while a capture endpoint's circuit is open and its cooldown has not elapsed."""
        now = self._now()
        for spec in specs:
            st = ingest_repo.load_health(self.conn, spec, partition)
            if st.circuit == "open" and st.next_attempt_at and now < st.next_attempt_at:
                raise SourceBlocked(f"circuit_open:{spec.source_id}/{spec.endpoint_id} until {st.next_attempt_at.isoformat()}")

    def _read_cursor(self, spec: EndpointSpec, partition: str) -> tuple[dict | None, int]:
        row = ingest_repo.read_cursor(self.conn, spec, partition)
        return (row["committed_cursor"], row["cursor_version"]) if row else (None, 0)

    def _advance(self, spec: EndpointSpec, partition: str, batch_id: uuid.UUID, doc: dict, watermark: datetime | None = None) -> None:
        _, version = self._read_cursor(spec, partition)
        with self.conn.transaction():
            ok = ingest_repo.advance_cursor(self.conn, spec, partition, batch_id=batch_id, new_cursor=doc, watermark=watermark,
                                            expected_version=version, now=self._now())
        if not ok:
            raise RefDataError(f"cursor {partition} was advanced by another run (compare-and-set lost)")

    def _begin(self, spec: EndpointSpec, partition: str) -> tuple[uuid.UUID, str]:
        batch_id = ingest_repo_create_batch(self.svc, spec, partition, self._now())
        return batch_id, f"job:{batch_id}"

    def _end_raw(self, batch_id: uuid.UUID, owner: str, *, release: bool) -> None:
        with self.conn.transaction():
            ingest_repo.commit_raw(self.conn, batch_id, self._now())
            ingest_repo.set_batch_state(self.conn, batch_id, "NORMALIZED")
        if release:
            self.lake.release(owner)

    def _fail_batch(self, batch_id: uuid.UUID, owner: str, code: str = "job_error") -> None:
        with self.conn.transaction():
            ingest_repo.set_batch_state(self.conn, batch_id, "FAILED", failure_code=code)
        self.lake.release(owner)

    def _call(self, spec: EndpointSpec, partition: str, fn: Callable[[], Capture]) -> Capture:
        return self.svc._fetch(spec, partition, fn)          # records blocked / transport health, then re-raises

    def read_dataset(self, dataset_id: str) -> tuple[dict | None, dict[str, pl.DataFrame]]:
        """Latest READY manifest and its partition frames ({} when the dataset does not exist yet)."""
        man = market_repo.latest_ready(self.conn, dataset_id)
        if man is None:
            return None, {}
        m = self.lake.read_manifest(man["content_hash"])
        names = m.meta.get("partitions") or ["all"]
        frames = {n: pl.read_parquet(io.BytesIO(self.lake.read_object(x))) for n, x in zip(names, m.members)}
        return man, frames

    def read_frame(self, dataset_id: str) -> pl.DataFrame | None:
        man, frames = self.read_dataset(dataset_id)
        return pl.concat(list(frames.values())) if frames else None

    def _publish(self, dataset_id: str, frames: dict[str, pl.DataFrame], *, contract: str, coverage: dict, batch_id: uuid.UUID,
                 owner: str, watermark: datetime | None) -> dict:
        return self.svc.publish_dataset(dataset_id=dataset_id, snapshot_id=uuid.uuid4(), batch_id=batch_id, frames=frames,
                                        contract_id=contract, coverage=coverage, owner=owner, available_watermark=watermark)

    def _success(self, spec: EndpointSpec, partition: str, *, new_records: int, newest: datetime | None, ready: bool) -> None:
        now = self._now()
        self.svc.record_attempt(spec, partition, "success_data", new_records=new_records,
                                newest_content_at=newest if newest is not None and newest <= now else None,
                                content_expected_now=False, canonical_ready=ready)

    # ---- adjusted bars --------------------------------------------------------------------------------------------
    def _normalize_pending(self, spec: EndpointSpec, pending: Sequence[_Pending], s: Summary
                           ) -> tuple[pl.DataFrame, dict[tuple[str, date], tuple[dict, builders.Bar, uuid.UUID]]]:
        """Pending codes -> fresh frame (revision 1 placeholder) + the normalized record behind every row."""
        recorded: dict[tuple[str, date], tuple[dict, builders.Bar, uuid.UUID]] = {}
        rows: list[dict] = []
        for p in pending:
            t = p.available_at.isoformat().replace("+00:00", "Z")
            for r in p.rows:
                raw = {"code": p.code, "date": r["date"].isoformat(), "open": r["open"], "high": r["high"], "low": r["low"],
                       "close": r["close"], "volume": r["volume"], "amount": None}
                ctx = {"source_id": spec.source_id, "endpoint_id": spec.endpoint_id, "source_schema_version": spec.source_schema_version,
                       "mapping_version": spec.mapping_version, "adjustment_mode": "forward_adjusted", "first_seen_at": t,
                       "ingested_at": t, "available_at": t, "raw_object_id": str(p.raw_object_id)}
                try:
                    n = normalize(spec.profile, raw, ctx, _resolve)
                except MappingDrift as exc:
                    raise RefDataError(f"adjusted bars profile drift: {exc}") from exc
                if n.get("instrument_id") is None or n["quality_status"] == "invalid":
                    s.add("rows_rejected")
                    with self.conn.transaction():
                        ingest_repo.record_issue(self.conn, issue_code="invalid_canonical_record", severity="warning", now=self._now(),
                                                 raw_object_id=p.raw_object_id, payload={"code": p.code, "date": raw["date"], "issues": n["issues"]})
                    continue
                bar = builders.daily_bar(n, adjusted=True, adjustment_version=M.ADJUSTMENT_VERSION)
                recorded[(bar.instrument_id, bar.trade_date)] = (n, bar, p.raw_object_id)
                rows.append({"instrument_id": bar.instrument_id, "trade_date": bar.trade_date, "timeframe": "1d",
                             "bar_open_time": bar.bar_open_time, "bar_close_time": bar.bar_close_time, "open_price": bar.open,
                             "high_price": bar.high, "low_price": bar.low, "close_price": bar.close, "volume_shares": bar.volume_shares,
                             "turnover_amount_cny": bar.turnover_cny, "adjustment_mode": "forward_adjusted",
                             "adjustment_version": M.ADJUSTMENT_VERSION, "is_final": bar.is_final, "available_at": bar.available_at,
                             "quality_status": bar.quality_status, "revision": 1})
        return pl.DataFrame(rows, schema=C.ADJ_SCHEMA), recorded

    def _finalize_adjusted(self, spec: EndpointSpec, partition: str, batch_id: uuid.UUID, owner: str, pending: Sequence[_Pending],
                           s: Summary, codes_of: Mapping[str, str], *, raw_only: bool = False) -> int:
        """Commit the chunk's raw captures, then publish what the SAME fetches carry: the adjusted bars (unless ``raw_only``), the RAW
        bars and Sina's factor rows, each as its own cumulative dataset with its own revisions. Returns the new adjusted rows."""
        self._end_raw(batch_id, owner, release=False)
        n = 0 if raw_only else self._publish_adjusted(spec, batch_id, owner, pending, s, codes_of)
        self._publish_raw(batch_id, owner, pending, s, codes_of)
        if not raw_only:
            self._publish_factors(batch_id, owner, pending, s, codes_of)
        self.lake.release(owner)
        return n

    def _publish_adjusted(self, spec: EndpointSpec, batch_id: uuid.UUID, owner: str, pending: Sequence[_Pending],
                          s: Summary, codes_of: Mapping[str, str]) -> int:
        """Normalize, diff against the stored latest revisions, write observations, publish the cumulative snapshot.

        ``codes_of``: instrument_id -> code of the codes in this chunk. Returns the number of new revision rows."""
        fresh, recorded = self._normalize_pending(spec, [p for p in pending if p.rows], s)
        man, frames = self.read_dataset(C.ADJ_DATASET)
        touched = {adj_partition(p.code) for p in pending}
        existing = pl.concat([frames[k] for k in touched if k in frames]) if any(k in frames for k in touched) else None
        new = C.next_revisions(existing, fresh)
        s.add("bars_fetched", fresh.height)
        s.add("bars_unchanged", fresh.height - new.height)
        if new.is_empty():
            return 0
        revised = new.filter(pl.col("revision") > 1)
        s.add("bars_new_keys", new.height - revised.height)
        s.add("bars_new_revisions", revised.height)
        s.add("codes_with_revised_history", len({codes_of[i] for i in revised["instrument_id"].unique().to_list() if i in codes_of}))
        recorded_at = self._now()
        obs: list[dict] = []
        for r in new.iter_rows(named=True):
            n, bar, raw_id = recorded[(r["instrument_id"], r["trade_date"])]
            nat = f"{bar.instrument_id}|1d|{bar.bar_close_time.isoformat()}|fwd_adj"
            payload = builders.observation_payload(bar)
            obs.append({"observation_id": ingest_repo.observation_id(raw_id, nat, r["revision"]), "raw_object_id": raw_id,
                        "contract_id": spec.contract_id, "schema_version": "3.0.0", "mapping_version": spec.mapping_version,
                        "entity_type": "instrument", "entity_key": bar.instrument_id, "natural_key": nat,
                        "observation_revision": r["revision"], "event_time": bar.bar_close_time, "available_at": bar.available_at,
                        "recorded_at": recorded_at, "quality_status": n["quality_status"], "canonical_payload": payload,
                        "field_quality": builders.compact_field_quality(n, spec.profile["profile_id"]), "content_hash": sha256_json(payload)})
        for i in range(0, len(obs), 20000):
            with self.conn.transaction():
                s.add("observations_created", ingest_repo.insert_observations(self.conn, obs[i:i + 20000]))
        part_of = {i: adj_partition(c) for i, c in codes_of.items()}
        by_part = {k[0]: g.drop("_p") for k, g in new.with_columns(pl.col("instrument_id").replace_strict(part_of).alias("_p")).group_by("_p")}
        merged = dict(frames)
        for part, g in by_part.items():
            merged[part] = (pl.concat([frames[part], g]) if part in frames else g).sort(["instrument_id", "trade_date", "revision"])
        allrows = pl.concat(list(merged.values()))
        latest = C.latest_bars(allrows)
        coverage = {
            "dataset": C.ADJ_DATASET, "price_basis": "forward_adjusted", "adjustment_mode": "forward_adjusted",
            "adjustment_version": M.ADJUSTMENT_VERSION,
            "adjustment_note": "Sina raw kline divided by Sina's qfq factor file (ratio): every daily return equals the raw one. A new ex-right "
                               "event changes past values; the whole affected series of the code is stored as revision n+1 (all revisions kept).",
            "units": "volume shares (float64 integral); turnover_amount_cny NULL (not provided by the source)",
            "instruments": latest["instrument_id"].n_unique(), "rows": allrows.height, "latest_rows": latest.height,
            "max_revision": int(allrows["revision"].max()), "first_date": latest["trade_date"].min().isoformat(),
            "last_date": latest["trade_date"].max().isoformat(), "partitions": sorted(merged),
            "finality_policy": builders.DAILY_FINAL_POLICY, "schema": "identical to bars_legacy_fwd_adjusted_1d",
            "capability": REGISTRY[C.ADJ_DATASET].to_doc(),                      # the bars API refuses a snapshot without it
            "provenance": "bars appended from the Tencent closing quote (refresh mode) reference that quote capture; all others the Sina kline",
            "gap_note": "missing sessions are not classified (no suspension feed registered)",
            "window_note": "every fetch re-publishes the requested window (default 3 years) of a code; rows older than the window keep their last "
                           "published revision and are not re-adjusted (the old system had the same one-request limit of 1023 bars)"}
        pub = self._publish(C.ADJ_DATASET, merged, contract="AdjustedBar", coverage=coverage, batch_id=batch_id, owner=owner,
                            watermark=allrows["available_at"].max())
        s.counts["manifest_version_adjusted"] = pub["manifest_version"]
        s.counts["lake_rows_adjusted"] = pub["row_count"]
        return new.height

    # ---- raw bars and factor rows from the same fetches -------------------------------------------------------------------------
    def _publish_cumulative(self, dataset_id: str, fresh: pl.DataFrame, *, schema: dict, key: list[str], values: list[str], promote: str | None,
                            codes_of: Mapping[str, str], contract: str, batch_id: uuid.UUID, owner: str, coverage: dict, label: str,
                            s: Summary) -> int:
        """Diff ``fresh`` against the latest stored revisions, append the new rows to their code-prefix partitions, publish a READY snapshot."""
        man, frames = self.read_dataset(dataset_id)
        part_of = {i: adj_partition(c) for i, c in codes_of.items()}
        touched = {part_of[i] for i in fresh["instrument_id"].unique().to_list() if i in part_of}
        existing = pl.concat([frames[k] for k in touched if k in frames]) if any(k in frames for k in touched) else None
        new = C.next_revisions(existing, fresh, schema=schema, key=key, values=values, promote=promote)
        s.add(f"{label}_rows_fetched", fresh.height)
        s.add(f"{label}_rows_new", new.height)
        s.add(f"{label}_rows_new_revisions", new.filter(pl.col("revision") > 1).height)
        if new.is_empty():
            return 0
        merged = dict(frames)
        grouped = new.with_columns(pl.col("instrument_id").replace_strict(part_of).alias("_p")).group_by("_p")
        sort_cols = key + ["revision"]
        for (part,), g in grouped:
            g = g.drop("_p")
            merged[part] = (pl.concat([frames[part], g]) if part in frames else g).sort(sort_cols)
        allrows = pl.concat(list(merged.values()))
        cov = {"dataset": dataset_id, "rows": allrows.height, "max_revision": int(allrows["revision"].max()), "partitions": sorted(merged),
               "instruments": allrows["instrument_id"].n_unique(), **coverage}
        pub = self._publish(dataset_id, merged, contract=contract, coverage=cov, batch_id=batch_id, owner=owner,
                            watermark=allrows["available_at"].max())
        s.counts[f"manifest_version_{label}"] = pub["manifest_version"]
        return new.height

    def _raw_frame(self, pending: Sequence[_Pending], s: Summary) -> pl.DataFrame:
        """Raw rows as the kline gives them: exact decimals from the vendor strings, shares, no turnover (NULL, never 0)."""
        rows: list[dict] = []
        for p in pending:
            if not p.raw_rows:
                continue
            iid = instrument_id_of(p.code)
            for r in p.raw_rows:
                text = r.get("text") or {k: repr(r[k]) for k in ("open", "high", "low", "close", "volume")}
                try:
                    o, h, lo, c = (Decimal(text[k]) for k in ("open", "high", "low", "close"))
                    vol = Decimal(text["volume"])
                except (InvalidOperation, KeyError):
                    s.add("raw_rows_rejected")
                    continue
                if not (o > 0 and h > 0 and lo > 0 and c > 0) or vol < 0 or vol != vol.to_integral_value():
                    s.add("raw_rows_rejected")
                    continue
                d = r["date"]
                rows.append({"instrument_id": iid, "trade_date": d, "timeframe": "1d", "bar_open_time": session_open_utc(d),
                             "bar_close_time": session_close_utc(d), "open_price_raw": o, "high_price_raw": h, "low_price_raw": lo,
                             "close_price_raw": c, "volume_shares": int(vol), "turnover_amount_cny": None,
                             "is_final": builders.daily_is_final(d, p.available_at), "available_at": p.available_at,
                             "quality_status": "partial", "revision": 1})
        return pl.DataFrame(rows, schema=C.RAW_SCHEMA)

    def _publish_raw(self, batch_id: uuid.UUID, owner: str, pending: Sequence[_Pending], s: Summary, codes_of: Mapping[str, str]) -> int:
        fresh = self._raw_frame(pending, s)
        if fresh.is_empty():
            return 0
        cov = {"price_basis": "raw", "adjustment_mode": "raw", "source": "Sina raw daily kline (the request the adjusted bars are built from); "
                                                                       "bars from the Tencent closing quote in refresh mode",
               "units": "prices CNY exactly as the vendor strings (decimal 38,12); volume shares; turnover_amount_cny NULL (the kline has no amount)",
               "revisions": "a changed raw value of an existing (instrument, date) is appended as revision n+1; a bar becoming final too; nothing is overwritten",
               "finality_policy": builders.DAILY_FINAL_POLICY, "schema": "frame of bars_raw_1d plus revision",
               "purpose": "exact limit-up / limit-down flags (``refdata_core.limit_flags_raw``) over about 3 years",
               "first_date": fresh["trade_date"].min().isoformat(), "last_date_fetched": fresh["trade_date"].max().isoformat(),
               "exchange_reference_price": "not provided by the kline; derived from adj_factor_native on ex-right days",
               "window_note": "rows older than the fetch window keep their last published revision"}
        return self._publish_cumulative(C.RAW_DATASET, fresh, schema=C.RAW_SCHEMA, key=C.BAR_KEY, values=C.RAW_VALUES, promote="is_final",
                                        codes_of=codes_of, contract="CanonicalBar", batch_id=batch_id, owner=owner, coverage=cov, label="raw", s=s)

    def _publish_factors(self, batch_id: uuid.UUID, owner: str, pending: Sequence[_Pending], s: Summary, codes_of: Mapping[str, str]) -> int:
        rows = [{"instrument_id": instrument_id_of(p.code), "effective_from": d, "factor": Decimal(f), "available_at": p.available_at, "revision": 1}
                for p in pending if p.factor_rows for d, f in p.factor_rows]
        if not rows:
            return 0
        fresh = pl.DataFrame(rows, schema=C.FACTOR_SCHEMA)
        cov = {"source": "Sina qfq.js, every row exactly as served (factor string with 16 decimals)",
               "semantics": "from effective_from on (until the next row) adjusted price = raw price / factor; the latest factor is 1.0; "
                            "a new ex-right event changes every older factor of the code, which is stored as revision n+1 of each row",
               "ex_right_reference": "ref(t) = raw close(t-1) * f(t) / f(t-1), rounded half up to the tick (see refdata_core.limit_flags_raw)",
               "adjustment_version": M.ADJUSTMENT_VERSION, "codes_without_factor_file": "have no rows here (an unadjusted series)"}
        return self._publish_cumulative(C.FACTOR_DATASET, fresh, schema=C.FACTOR_SCHEMA, key=C.FACTOR_KEY, values=["factor"], promote=None,
                                        codes_of=codes_of, contract="EvidenceDocument", batch_id=batch_id, owner=owner, coverage=cov, label="factor", s=s)

    def _adjusted_chunk(self, adapter: M.SinaRefAdapter, codes: Sequence[str], *, start: date, today: date, cut: date | None,
                        partition: str, s: Summary, failures: int, raw_only: bool = False) -> _ChunkResult:
        out_spec = self._spec(M.SINA_SOURCE, M.OUT_RAW if raw_only else M.OUT_ADJUSTED)
        k_spec, f_spec = self._spec(M.SINA_SOURCE, M.EP_KLINE), self._spec(M.SINA_SOURCE, M.EP_FACTOR)
        batch_id, owner = self._begin(out_spec, partition)
        res = _ChunkResult(batch_id=batch_id)
        pending: list[_Pending] = []
        codes_of = {instrument_id_of(c): c for c in codes}
        consecutive = failures
        try:
            for code in codes:
                symbol = f"{M.market_prefix(code)}{code}"
                try:
                    kc = self._call(k_spec, partition, lambda: adapter.kline(symbol, M.bars_needed(start, today)))
                    rows = C.complete_rows(M.parse_kline(kc.body), cut, start=start, end=today)
                    if not rows:                                     # not listed in the window: stored evidence, nothing to publish
                        self.svc.store_capture(batch_id, kc, owner)
                        s.add("codes_without_bars")
                        res.done.append(code)
                        consecutive = 0
                        continue
                    fc = factors = factor_rows = None
                    if not raw_only:
                        fc = self._call(f_spec, partition, lambda: adapter.factors(symbol))
                        factor_rows = M.parse_factor_rows(fc.body)
                        factors = [(d, float(f)) for d, f in factor_rows]
                except (NotData, SchemaChanged) as exc:
                    s.add("codes_failed")
                    if len(s.notes) < 20:
                        s.notes.append(f"{code}: {type(exc).__name__}:{exc}")
                    res.failed.append(code)
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        res.stop = RefDataError(f"{consecutive} consecutive failures (last {code}: {exc})")
                        break
                    continue
                except TransportFailure as exc:
                    s.add("codes_failed")
                    res.failed.append(code)
                    consecutive += 1
                    if consecutive >= MAX_CONSECUTIVE_FAILURES:
                        res.stop = exc
                        break
                    continue
                except SourceBlocked as exc:
                    res.stop = exc
                    break
                consecutive = 0
                raw_k, _ = self.svc.store_capture(batch_id, kc, owner)
                if fc is not None:
                    self.svc.store_capture(batch_id, fc, owner)
                    if not factors:
                        s.add("codes_without_factor_file")
                pending.append(_Pending(code, [] if raw_only else M.adjust_rows(rows, factors), raw_k,
                                        max(self._now(), fc.fetched_at if fc is not None else kc.fetched_at), raw_rows=rows, factor_rows=factor_rows))
                res.done.append(code)
                s.add("codes_fetched")
            if pending:
                self._finalize_adjusted(out_spec, partition, batch_id, owner, pending, s, codes_of, raw_only=raw_only)
            else:
                self._end_raw(batch_id, owner, release=True)
            if res.done and res.stop is None:
                for spec in ((k_spec,) if raw_only else (k_spec, f_spec)):
                    self._success(spec, partition, new_records=len(pending), newest=None, ready=bool(pending))
        except Exception:
            self._fail_batch(batch_id, owner)
            raise
        return res

    def adjusted_bars(self, adapter: M.SinaRefAdapter, codes: Sequence[str], *, start: date, today: date | None = None, chunk: int = 300,
                      scope: str | None = "universe", restart: bool = False, max_codes: int | None = None, raw_only: bool = False) -> Summary:
        """Fetch ratio-adjusted bars for ``codes`` in chunks of ``chunk`` codes (2 requests per code), and publish the RAW bars and Sina's
        factor rows from the same two responses. ``raw_only``: 1 request per code (the kline, no factor file), only the raw dataset is
        published, with its own cursor (partition ``raw:<scope>``) so a raw backfill never disturbs the adjusted one.

        With a ``scope`` the run has a resumable cursor in PG: ``last_code`` (every sorted code up to it was fetched or
        failed) and the ``failed`` codes to retry first. ``max_codes`` splits one universe over several runs. The first
        block stops the run: the finished codes of the current chunk are published, the cursor moves over them only, and
        ``SourceBlocked`` propagates (exit code 2 in the CLI). Without a scope (ad hoc codes) no cursor is kept."""
        today = today or self._now().astimezone(C.CST).date()
        s = Summary(f"refdata adjusted-bars {start}..{today}" + (" (raw only)" if raw_only else ""))
        out_spec = self._spec(M.SINA_SOURCE, M.OUT_RAW if raw_only else M.OUT_ADJUSTED)
        kind = "raw" if raw_only else "adjusted"
        partition = f"{kind}:{scope}" if scope else f"{kind}:adhoc"
        self.gate([self._spec(M.SINA_SOURCE, M.EP_KLINE)] + ([] if raw_only else [self._spec(M.SINA_SOURCE, M.EP_FACTOR)]), partition)
        want = sorted(set(codes))
        cursor, _ = self._read_cursor(out_spec, partition) if scope else (None, 0)
        resumed = bool(cursor and cursor.get("state") == "running" and not restart)
        last = cursor.get("last_code", "") if resumed else ""
        failed_before = sorted(c for c in (cursor.get("failed", []) if resumed else []) if c in set(want))
        todo_all = sorted(set(failed_before) | {c for c in want if c > last})
        todo = todo_all if max_codes is None else todo_all[:max_codes]
        s.counts["codes_requested"] = len(want)
        s.counts["codes_this_run"] = len(todo)
        if resumed:
            s.notes.append(f"resumed after {last or '-'} with {len(failed_before)} failed codes to retry first")
        cut = C.session_cut(self._now())
        started = (cursor or {}).get("started_at") if resumed else None
        started = started or self._now().isoformat()
        failed_all = list(failed_before)
        consecutive = 0
        stop: Exception | None = None
        for part in C.chunked(todo, chunk):
            res = self._adjusted_chunk(adapter, part, start=start, today=today, cut=cut, partition=partition, s=s, failures=consecutive, raw_only=raw_only)
            consecutive = 0 if res.done else consecutive + len(res.failed)
            failed_all = sorted((set(failed_all) | set(res.failed)) - set(res.done))
            processed = sorted(set(res.done) | set(res.failed))
            if processed:
                last = max(last, processed[-1])
            if scope:
                self._advance(out_spec, partition, res.batch_id, {"state": "running", "last_code": last, "failed": failed_all, "started_at": started})
            if res.stop is not None:
                stop = res.stop
                break
        if stop is None and scope and len(todo) == len(todo_all):
            batch_id, owner = self._begin(out_spec, partition)
            self._end_raw(batch_id, owner, release=True)
            self._advance(out_spec, partition, batch_id, {"state": "complete", "last_code": last, "failed": failed_all,
                                                          "started_at": started, "finished_at": self._now().isoformat()})
        s.counts["codes_failed_total"] = len(failed_all)
        if failed_all:
            s.notes.append("failed codes (retried first on the next run): " + ",".join(failed_all[:30]) + (" ..." if len(failed_all) > 30 else ""))
        if stop is not None:
            raise _carry(stop, s)
        return s

    # ---- refresh (cheap daily path) ------------------------------------------------------------------------------
    def adjusted_refresh(self, sina: M.SinaRefAdapter, tadapter, codes: Sequence[str], *, start: date, today: date | None = None,
                         chunk: int = 300, max_refetch: int | None = None) -> Summary:
        """Daily refresh from the closing quote: append today's bar for unchanged codes, refetch ex-right / gap / new codes whole.

        About ``len(codes) / 50`` quote requests plus 2 per refetched code; an unfinished session appends nothing."""
        today = today or self._now().astimezone(C.CST).date()
        s = Summary(f"refdata adjusted-bars refresh {today}")
        q_spec = self._spec(tencent.SOURCE_ID, "qt_quote")
        out_spec = self._spec(M.SINA_SOURCE, M.OUT_ADJUSTED)
        partition = "adjusted:refresh"
        self.gate([q_spec], partition)
        man, frames = self.read_dataset(C.ADJ_DATASET)
        if man is None:
            raise RefDataError("no native adjusted dataset yet: run `adjusted-bars --mode full` first")
        latest = C.latest_bars(pl.concat(list(frames.values())))
        ids = {instrument_id_of(c): c for c in codes}
        mine = latest.filter(pl.col("instrument_id").is_in(list(ids)))
        known = sorted(ids[i] for i in mine["instrument_id"].unique().to_list())
        tail: dict[str, dict[date, float]] = {c: {} for c in known}
        for iid, d, close in mine.filter(pl.col("trade_date") >= today - timedelta(days=C.RECENT_DAYS)).select(
                "instrument_id", "trade_date", "close_price").iter_rows():
            tail[ids[iid]][d] = float(close)
        batch_id, owner = self._begin(out_spec, partition)
        quotes: list[dict] = []
        qraw: dict[str, uuid.UUID] = {}
        stop: Exception | None = None
        try:
            for part in C.chunked([f"{M.market_prefix(c)}{c}" for c in known], 50):
                try:
                    cap = self._call(q_spec, partition, lambda: tadapter.quotes(list(part)))
                except SourceBlocked as exc:
                    stop = exc
                    break
                recs, _empty = tencent.parse_qt(cap.body)
                rid, _ = self.svc.store_capture(batch_id, cap, owner)
                s.add("quote_requests")
                for q in recs:
                    code = q["symbol"][2:]
                    try:
                        quotes.append({"code": code, "date": datetime.strptime(q["quote_time"], "%Y%m%d%H%M%S").date(),
                                       "volume": float(q["volume_lots"]) * 100.0, "prev_close": float(q["prev_close"]), "open": float(q["open"]),
                                       "high": float(q["high"]), "low": float(q["low"]), "close": float(q["last"])})
                        qraw[code] = rid
                    except (ValueError, TypeError):
                        s.add("quotes_unreadable")
            if stop is not None:
                self._end_raw(batch_id, owner, release=True)
                raise _carry(stop, s)
            plan = C.plan_refresh(quotes, tail, sorted(ids.values()), C.session_cut(self._now()), known=known)
            s.counts.update({"codes_requested": len(ids), "codes_stored": len(known), "to_append": len(plan.append),
                             "to_refetch": len(plan.refetch), "suspended": plan.skipped_suspended, "already_current": plan.up_to_date})
            for reason in ("gap", "readjusted", "new"):
                s.counts[f"refetch_{reason}"] = sum(1 for v in plan.refetch.values() if v == reason)
            quote_rows = {c: [{k: q[k] for k in ("date", "open", "high", "low", "close", "volume")}] for c, q in plan.append.items()}
            appended = [_Pending(c, quote_rows[c], qraw[c], self._now(), source="tencent_quote", raw_rows=quote_rows[c]) for c in sorted(plan.append)]
            if appended:
                self._finalize_adjusted(out_spec, partition, batch_id, owner, appended, s, {instrument_id_of(c): c for c in plan.append})
            else:
                self._end_raw(batch_id, owner, release=True)
            if known:
                self._success(q_spec, partition, new_records=len(appended), newest=None, ready=bool(appended))
        except SourceBlocked:
            raise
        except Exception:
            self._fail_batch(batch_id, owner)
            raise
        refetch = sorted(plan.refetch)[:max_refetch] if max_refetch is not None else sorted(plan.refetch)
        if refetch:
            sub = self.adjusted_bars(sina, refetch, start=start, today=today, chunk=chunk, scope=None)
            for k, v in sub.counts.items():
                s.counts[f"refetch_{k}"] = v
            s.notes.extend(sub.notes)
        return s

    # ---- index bars -----------------------------------------------------------------------------------------------
    def index_bars(self, sina: M.SinaRefAdapter, tadapter: M.TencentRefAdapter, *, start: date, today: date | None = None,
                   symbols: Sequence[str] | None = None) -> Summary:
        """One request per index (6 in all); new versions are appended with ``ingested_at`` = fetch time."""
        today = today or self._now().astimezone(C.CST).date()
        s = Summary(f"refdata index-bars {start}..{today}")
        out_spec = self._spec(M.SINA_SOURCE, M.OUT_INDEX)
        partition = "index:all"
        self.gate([self._spec(M.SINA_SOURCE, M.EP_INDEX), self._spec(M.TENCENT_SOURCE, M.EP_INDEX)], partition)
        cut = C.session_cut(self._now())
        batch_id, owner = self._begin(out_spec, partition)
        rows: list[dict] = []
        stop: Exception | None = None
        try:
            for symbol in symbols or sorted(M.INDEX_TICKERS):
                ticker, source = M.INDEX_TICKERS[symbol]
                spec = self._spec(source, M.EP_INDEX)
                try:
                    if source == M.TENCENT_SOURCE:
                        cap = self._call(spec, partition, lambda: tadapter.index_kline(ticker, 640))
                        parsed = M.parse_tencent_index(cap.body, ticker)
                    else:
                        cap = self._call(spec, partition, lambda: sina.index_kline(ticker, M.bars_needed(start, today)))
                        parsed = M.parse_kline(cap.body)
                except (NotData, SchemaChanged, TransportFailure) as exc:
                    s.add("indices_failed")
                    s.notes.append(f"{symbol}: {type(exc).__name__}:{exc}")
                    continue
                except SourceBlocked as exc:
                    stop = exc
                    break
                self.svc.store_capture(batch_id, cap, owner)
                t = max(self._now(), cap.fetched_at)
                iid = str(ident.index_uuid(ident.XSHE if symbol.startswith("399") else ident.XSHG, symbol))
                for r in C.complete_rows(parsed, cut, start=start, end=today):
                    rows.append({"instrument_id": iid, "symbol": symbol, "date": r["date"], "timeframe": "1d", "open": r["open"],
                                 "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"],
                                 "price_basis": "index_points_unadjusted", "ingested_at": t})
                s.add("indices_fetched")
            self._end_raw(batch_id, owner, release=False)
            fresh = pl.DataFrame(rows, schema=C.INDEX_SCHEMA)
            man, frames = self.read_dataset(C.INDEX_DATASET)
            existing = frames.get("all")
            new = C.next_index_versions(existing, fresh)
            s.counts["index_rows_fetched"] = fresh.height
            s.counts["index_new_versions"] = new.height
            if new.is_empty():
                self.lake.release(owner)
            else:
                merged = (pl.concat([existing, new]) if existing is not None else new).sort(["symbol", "date", "ingested_at"])
                cov = {"kind": "index daily bars", "symbols": sorted(merged["symbol"].unique().to_list()), "rows": merged.height,
                       "units": "index points (not CNY); volume unit NOT verified against a live source (as the legacy rows)",
                       "versions": "a new row is appended when a value of (symbol, date) changes; ingested_at is the fetch time",
                       "sources": {"000985": "tencent fqkline (Sina's series stops in 2016)", "others": "sina kline"},
                       "schema": "identical to index_bars_legacy_1d"}
                pub = self._publish(C.INDEX_DATASET, {"all": merged}, contract="AdjustedBar", coverage=cov, batch_id=batch_id, owner=owner,
                                    watermark=merged["ingested_at"].max())
                s.counts["manifest_version_index"] = pub["manifest_version"]
            if s.counts.get("indices_fetched") and stop is None:
                for source in sorted({M.INDEX_TICKERS[x][1] for x in (symbols or M.INDEX_TICKERS)}):
                    spec = self._spec(source, M.EP_INDEX)
                    self._success(spec, partition, new_records=new.height, newest=None, ready=not new.is_empty())
        except Exception:
            self._fail_batch(batch_id, owner)
            raise
        if stop is not None:
            raise _carry(stop, s)
        return s

    # ---- boards ---------------------------------------------------------------------------------------------------
    def boards(self, adapter: M.SinaRefAdapter, *, chunk_boards: int = 25, limit: int | None = None) -> Summary:
        """Current industry + concept membership. Published only when EVERY board was fetched (``limit`` is a smoke test and never publishes).

        Progress (fetched boards with their members) is kept in the PG cursor of the day, so a block or a crash resumes at the first board
        not yet fetched; a cursor of another day starts over (a snapshot never mixes days)."""
        s = Summary("refdata boards")
        out_spec = self._spec(M.SINA_SOURCE, M.OUT_BOARDS)
        n_spec, m_spec = self._spec(M.SINA_SOURCE, M.EP_NODES), self._spec(M.SINA_SOURCE, M.EP_MEMBERS)
        partition = "boards:snapshot"
        self.gate([n_spec, m_spec], partition)
        day = self._now().astimezone(C.CST).date()
        cur, _ = self._read_cursor(out_spec, partition)
        if cur and cur.get("state") == "running" and cur.get("as_of") == day.isoformat():
            boards = [M.Board(*b) for b in cur["nodes"]]
            done: dict[str, list[str]] = dict(cur["done"])
            s.notes.append(f"resumed: {len(done)}/{len(boards)} boards already fetched today")
        else:
            boards, done = self._fetch_nodes(adapter, n_spec, out_spec, partition, day), {}
        s.counts["boards_total"] = len(boards)
        todo = [b for b in boards if b.board_id not in done]
        if limit is not None:
            todo = todo[:limit]
        failures = 0
        stop: Exception | None = None
        failed: list[str] = []
        for part in C.chunked(todo, chunk_boards):
            batch_id, owner = self._begin(out_spec, partition)
            try:
                for b in part:
                    try:
                        done[b.board_id] = self._fetch_members(adapter, m_spec, partition, b, batch_id, owner, s)
                        failures = 0
                        s.add("boards_fetched")
                    except (NotData, SchemaChanged, TransportFailure) as exc:
                        failed.append(b.board_id)
                        failures += 1
                        s.add("boards_failed")
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            stop = RefDataError(f"{failures} consecutive board failures (last {b.board_id}: {exc})")
                            break
                    except SourceBlocked as exc:
                        stop = exc
                        break
                self._end_raw(batch_id, owner, release=True)
                self._advance(out_spec, partition, batch_id, {"state": "running", "as_of": day.isoformat(), "nodes": [[b.kind, b.board_id, b.name] for b in boards],
                                                              "done": done, "failed": failed})
                if done and stop is None:
                    self._success(m_spec, partition, new_records=len(done), newest=None, ready=False)
            except Exception:
                self._fail_batch(batch_id, owner)
                raise
            if stop is not None:
                break
        s.counts["boards_done"] = len(done)
        if stop is not None:
            raise _carry(stop, s)
        missing = [b for b in boards if b.board_id not in done]
        if missing or limit is not None:
            s.notes.append(f"incomplete: {len(missing)} boards not fetched" + (" (--limit smoke run)" if limit is not None else "") + "; nothing published")
            s.counts["published"] = 0
            return s
        self._publish_boards(out_spec, partition, boards, done, day, s)
        return s

    def _fetch_nodes(self, adapter: M.SinaRefAdapter, n_spec: EndpointSpec, out_spec: EndpointSpec, partition: str, day: date) -> list[M.Board]:
        batch_id, owner = self._begin(out_spec, partition)
        try:
            cap = self._call(n_spec, partition, adapter.board_nodes)
            boards = M.parse_nodes(cap.body)
            self.svc.store_capture(batch_id, cap, owner)
            self._end_raw(batch_id, owner, release=True)
            self._advance(out_spec, partition, batch_id, {"state": "running", "as_of": day.isoformat(), "nodes": [[b.kind, b.board_id, b.name] for b in boards],
                                                          "done": {}, "failed": []})
            return boards
        except Exception:
            self._fail_batch(batch_id, owner)
            raise

    def _fetch_members(self, adapter: M.SinaRefAdapter, spec: EndpointSpec, partition: str, board: M.Board, batch_id: uuid.UUID, owner: str,
                       s: Summary) -> list[str]:
        members: list[str] = []
        for page in range(1, M.MAX_PAGES + 1):
            cap = self._call(spec, partition, lambda: adapter.board_members(board.board_id, page))
            codes, n_rows = M.parse_members(cap.body)
            self.svc.store_capture(batch_id, cap, owner)
            s.add("member_pages")
            fresh = [c for c in codes if c not in members]
            members.extend(fresh)
            if n_rows < M.PAGE_SIZE or not fresh:                         # a short page ends the board; nothing new means paging is ignored
                return members
        raise SchemaChanged(f"{board.board_id}: more than {M.MAX_PAGES} pages")

    def _publish_boards(self, out_spec: EndpointSpec, partition: str, boards: Sequence[M.Board], done: Mapping[str, Sequence[str]], day: date,
                        s: Summary) -> None:
        from pulse.application import bindings as B

        now = self._now()
        B.ensure_internal_registry(self.conn, now)                         # registers the pulse.sector_membership contract
        eff = datetime(day.year, day.month, day.day, tzinfo=C.CST)
        batch_id, owner = self._begin(out_spec, partition)
        self._end_raw(batch_id, owner, release=False)
        published = 0
        try:
            for kind, dataset in C.MEMBERSHIP_DATASETS.items():
                rows = []
                for b in boards:
                    if b.kind != kind:
                        continue
                    for code in done.get(b.board_id, ()):
                        try:
                            rows.append({"instrument_id": instrument_id_of(code), "node_id": b.board_id})
                        except ValueError:
                            s.add("members_unsupported_code")
                frame = (pl.DataFrame(rows, schema={"instrument_id": pl.String, "node_id": pl.String}).unique().sort(["node_id", "instrument_id"])
                         .with_columns(pl.lit(eff).cast(pl.Datetime("us", "UTC")).alias("effective_from"),
                                       pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("available_at")).select(list(C.MEMBERSHIP_SCHEMA)))
                digest = hashlib.sha256(frame.select("instrument_id", "node_id").write_csv().encode()).hexdigest()
                s.counts[f"{kind}_boards"] = sum(1 for b in boards if b.kind == kind)
                s.counts[f"{kind}_pairs"] = frame.height
                prev = market_repo.latest_ready(self.conn, dataset)
                if prev is not None and (prev["coverage_document"] or {}).get("members_sha256") == digest:
                    s.add("snapshots_unchanged")
                    continue
                cov = {"dataset": dataset, "members_sha256": digest, "kind": kind, "taxonomy": f"sina_{kind}", "nodes": {b.board_id: b.name for b in boards if b.kind == kind},
                       "rows": frame.height, "membership_as_of": day.isoformat(),
                       "note": "CURRENT membership observed at fetch time; not back-filled to earlier dates (look-ahead for past sessions); history = the sequence of READY manifests",
                       "schema": "identical to sector_membership_legacy_boards_industry (node names are in this document)"}
                pub = self._publish(dataset, {"all": frame}, contract="pulse.sector_membership", coverage=cov, batch_id=batch_id, owner=owner, watermark=now)
                s.counts[f"manifest_version_{kind}"] = pub["manifest_version"]
                published += 1
            s.counts["published"] = published
            self.lake.release(owner)
            self._advance(out_spec, partition, batch_id, {"state": "complete", "as_of": day.isoformat(), "boards": len(boards)})
        except Exception:
            self._fail_batch(batch_id, owner)
            raise

    # ---- listing dates --------------------------------------------------------------------------------------------
    def _eastmoney_rows(self, adapter: L.ListingAdapter, spec: EndpointSpec, partition: str, batch_id: uuid.UUID, owner: str, s: Summary) -> list[dict]:
        """The whole Eastmoney list (pages of 100, 1 request per second); an incomplete list (fewer rows than the declared total) is an error."""
        rows: dict[str, dict] = {}
        total: int | None = None
        for page in range(1, L.EM_MAX_PAGES + 1):
            cap = self._call(spec, partition, lambda p=page: adapter.eastmoney(p))
            got, declared = L.parse_eastmoney(cap.body)
            self.svc.store_capture(batch_id, cap, owner)
            s.add("eastmoney_requests")
            total = declared if declared is not None else total
            fresh = [r for r in got if r["code"] not in rows]
            rows.update({r["code"]: r for r in got})
            if not got or len(got) < L.EM_PAGE_SIZE or not fresh or (total is not None and len(rows) >= total):
                break
        if total is not None and len(rows) < total:
            raise RefDataError(f"the Eastmoney list is incomplete: {len(rows)} of {total} rows; not published")
        s.counts["eastmoney_rows"] = len(rows)
        return list(rows.values())

    def listings(self, adapter: L.ListingAdapter, *, min_rows: int = L.MIN_ROWS) -> Summary:
        """Two requests: SSE main-board A list and SZSE A list. A shorter list than ``min_rows`` is rejected (the old guard).

        An exchange list that is UNREACHABLE (connection reset / timeout, ``TransportFailure``) is replaced for that exchange by the Eastmoney
        list (about 50 requests); a block (``SourceBlocked``) on any source still stops the run and never triggers the fallback. The column
        ``source`` of every row says which list gave its date (sse | szse | eastmoney)."""
        s = Summary("refdata listings")
        out_spec = self._spec(L.SOURCE_ID, L.OUT_LISTINGS)
        sse_spec, szse_spec = self._spec(L.SOURCE_ID, L.EP_SSE), self._spec(L.SOURCE_ID, L.EP_SZSE)
        em_spec = self._spec(L.EM_SOURCE_ID, L.EP_EASTMONEY)
        partition = "listings:all"
        self.gate([sse_spec, szse_spec], partition)
        batch_id, owner = self._begin(out_spec, partition)
        try:
            lists: dict[str, tuple[str, list[dict]]] = {}
            for exch, spec, fetch, parse, tag in (("XSHG", sse_spec, adapter.sse, L.parse_sse, "sse"), ("XSHE", szse_spec, adapter.szse, L.parse_szse, "szse")):
                try:
                    cap = self._call(spec, partition, fetch)
                except TransportFailure as exc:
                    s.add("exchange_lists_unreachable")
                    s.notes.append(f"{tag} list unreachable ({exc}); the Eastmoney list is used for {exch}")
                    continue
                lists[exch] = (tag, parse(cap.body))
                self.svc.store_capture(batch_id, cap, owner)
            if len(lists) < 2:
                self.gate([em_spec], partition)
                em_rows = self._eastmoney_rows(adapter, em_spec, partition, batch_id, owner, s)
                for exch in ("XSHG", "XSHE"):
                    if exch not in lists:
                        lists[exch] = ("eastmoney", [r for r in em_rows if ident.venue_for_code(r["code"]) == exch])
            self._end_raw(batch_id, owner, release=False)
            rows = []
            for exch, (tag, items) in lists.items():
                s.counts[f"{tag}_rows"] = len(items)
                for r in items:
                    try:
                        venue = ident.venue_for_code(r["code"])
                    except ValueError:
                        s.add("codes_unsupported")
                        continue
                    rows.append({"instrument_id": instrument_id_of(r["code"]), "code": r["code"], "listed_on": r["list_date"], "exchange": venue, "source": tag, "name": r.get("name")})
            if len(rows) < min_rows:
                raise RefDataError(f"only {len(rows)} listing rows fetched (< {min_rows}); not published")
            now = self._now()
            frame = (pl.DataFrame(rows, schema={"instrument_id": pl.String, "code": pl.String, "listed_on": pl.Date, "exchange": pl.String, "source": pl.String, "name": pl.String})
                     .unique("code", keep="last").sort("code").with_columns(pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("available_at"))
                     .select(list(C.LISTING_SCHEMA)))
            s.counts["listings"] = frame.height
            s.counts["listing_date_null"] = frame["listed_on"].null_count()
            digest = hashlib.sha256(frame.select("code", "listed_on", "source", "name").write_csv().encode()).hexdigest()
            prev = market_repo.latest_ready(self.conn, C.LISTING_DATASET)
            if prev is not None and (prev["coverage_document"] or {}).get("listings_sha256") == digest:
                s.add("snapshots_unchanged")
                self.lake.release(owner)
            else:
                cov = {"dataset": C.LISTING_DATASET, "listings_sha256": digest, "rows": frame.height, "sources": {"sse": "SSE main board A list (XSHG)", "szse": "SZSE A-share list (XSHE)",
                                                         "eastmoney": "Eastmoney stock list f26, used per exchange only when that exchange list was unreachable"},
                       "rows_by_source": dict(frame["source"].value_counts().iter_rows()),
                       "scope": "Shanghai main board + all Shenzhen A shares; STAR and BSE are not in the legacy table either",
                       "note": "core.instrument.listed_on is NOT written by this producer (a cutover decision)"}
                pub = self._publish(C.LISTING_DATASET, {"all": frame}, contract="EvidenceDocument", coverage=cov, batch_id=batch_id, owner=owner, watermark=now)
                s.counts["manifest_version_listings"] = pub["manifest_version"]
            used = {"sse": sse_spec, "szse": szse_spec, "eastmoney": em_spec}
            for tag in sorted({t for t, _ in lists.values()}):
                self._success(used[tag], partition, new_records=frame.height, newest=None, ready=True)
        except Exception:
            self._fail_batch(batch_id, owner)
            raise
        return s

    # ---- listed_on fill (cutover helper) ------------------------------------------------------------------------------
    def fill_listed_on(self, *, dry: bool = False, examples: int = 10) -> Summary:
        """Fill ``core.instrument.listed_on`` from the newest ``instrument_listing_native`` snapshot, ONLY where it is NULL.

        A value that is already set is never overwritten: a different native date is counted and listed as a conflict. Codes of the
        listing without a registered instrument and listing rows without a date are counted and ignored. ``dry`` changes nothing."""
        s = Summary("refdata fill-listed-on" + (" (dry)" if dry else ""))
        frame = self.read_frame(C.LISTING_DATASET)
        if frame is None:
            raise RefDataError("no native listing dataset: run the `listings` command first")
        have = {str(r["instrument_id"]): r["listed_on"] for r in self.conn.execute(
            "SELECT instrument_id, listed_on FROM core.instrument WHERE instrument_type='equity' AND instrument_id = ANY(%s::uuid[])",
            (frame["instrument_id"].to_list(),))}
        fill: list[tuple[date, str]] = []
        conflicts: list[dict] = []
        for r in frame.iter_rows(named=True):
            iid = r["instrument_id"]
            if iid not in have:
                s.add("native_codes_without_instrument")
            elif r["listed_on"] is None:
                s.add("native_rows_without_date")
            elif have[iid] is None:
                fill.append((r["listed_on"], iid))
            elif have[iid] != r["listed_on"]:
                conflicts.append({"code": r["code"], "native": r["listed_on"].isoformat(), "stored": have[iid].isoformat()})
            else:
                s.add("already_equal")
        s.counts["native_rows"] = frame.height
        s.counts["to_fill"] = len(fill)
        s.counts["conflicts_not_overwritten"] = len(conflicts)
        s.counts["instruments_still_null_after"] = sum(1 for v in have.values() if v is None) - (0 if dry else len(fill))
        if not dry and fill:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.executemany("UPDATE core.instrument SET listed_on=%s WHERE instrument_id=%s AND listed_on IS NULL", fill)
            s.counts["filled"] = len(fill)
        for c in conflicts[:examples]:
            s.notes.append(f"conflict {c['code']}: stored {c['stored']} kept, native says {c['native']}")
        return s

    # ---- reconcile (read only) --------------------------------------------------------------------------------------
    def legacy_boards(self, kind: str) -> pl.DataFrame | None:
        """Newest legacy boards evidence file (raw parquet kept by ``legacy-import``) reduced to the membership schema for ``kind``."""
        best = None
        for r in self.conn.execute("SELECT source_record_key, content_hash FROM ingest.raw_object WHERE source_id='legacy_warehouse'"
                                   " AND endpoint_id='evidence_files' AND source_record_key LIKE 'boards/%%'"):
            df = pl.read_parquet(io.BytesIO(self.lake.read_object(r["content_hash"])))
            if kind not in set(df["kind"].unique().to_list()):
                continue
            ing = df["ingested_at"].max()
            if best is None or ing > best[0]:
                best = (ing, df)
        if best is None:
            return None
        df = best[1].filter(pl.col("kind") == kind)
        rows = []
        for code, node in zip(df["code"].to_list(), df["board_id"].to_list()):
            try:
                rows.append({"instrument_id": instrument_id_of(code), "node_id": node})
            except ValueError:
                continue
        return pl.DataFrame(rows, schema={"instrument_id": pl.String, "node_id": pl.String})

    def legacy_listings(self) -> dict[str, date | None]:
        rows = self.conn.execute("SELECT a.source_symbol AS code, i.listed_on FROM core.instrument i JOIN core.instrument_alias a USING (instrument_id)"
                                 " WHERE a.source_id='legacy_warehouse' AND i.instrument_type='equity'").fetchall()
        return {r["code"]: r["listed_on"] for r in rows}

    def reconcile(self, which: Sequence[str] = ("adjusted", "index", "boards", "listings"), *, rel_tol: float = C.REL_TOL) -> str:
        """Compare each native dataset with the legacy-imported one; reads only, prints nothing, writes nothing."""
        sections: dict[str, dict] = {}
        if "adjusted" in which:
            n, g = self.read_frame(C.ADJ_DATASET), self.read_frame(C.LEGACY_ADJ_DATASET)
            sections["adjusted bars"] = {"note": "native dataset missing"} if n is None else (
                {"note": "legacy dataset missing"} if g is None else C.compare_bars(n, g, rel_tol=rel_tol))
        if "index" in which:
            n, g = self.read_frame(C.INDEX_DATASET), self.read_frame(C.LEGACY_INDEX_DATASET)
            sections["index bars"] = {"note": "native dataset missing"} if n is None else (
                {"note": "legacy dataset missing"} if g is None else C.compare_index(n, g, rel_tol=rel_tol))
        if "boards" in which:
            n = self.read_frame(C.MEMBERSHIP_DATASETS["industry"])
            g = self.read_frame(C.LEGACY_INDUSTRY_DATASET)
            sections["boards industry"] = {"note": "native dataset missing"} if n is None else (
                {"note": "legacy dataset missing"} if g is None else C.compare_membership(n, g))
            nc, gc = self.read_frame(C.MEMBERSHIP_DATASETS["concept"]), self.legacy_boards("concept")
            sections["boards concept"] = {"note": "native dataset missing"} if nc is None else (
                {"note": "legacy concept evidence missing (the legacy import keeps it as raw files only)"} if gc is None else C.compare_membership(nc, gc))
        if "listings" in which:
            n = self.read_frame(C.LISTING_DATASET)
            sections["listings"] = {"note": "native dataset missing"} if n is None else C.compare_listings(n, self.legacy_listings())
        return C.render_report(sections)

