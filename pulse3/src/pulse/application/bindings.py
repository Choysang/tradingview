"""Port bindings: domain Protocols -> real adapters (I1, integration package).

Why: the decision packages (ranking, plans, portfolio) only define small Protocols
(``FeatureMatrixPort``, ``ReleaseComponentWriter``, ``AccountSnapshotPort``) and pure domain logic. This module
binds them to the real lake, ``market_repo``, ``PgAnalysisRelease`` and the unified ledger, and holds the
point-in-time loaders the release build needs (research bars, raw bars, instrument facts, sector membership).

Invariants:
  * every read is point-in-time: a bar/fact is visible only when ``available_at <= decision_as_of`` (and its
    manifest ``ready_at <= decision_as_of``); the latest visible revision wins, a later revision never leaks back;
  * research features run on the legacy *forward-adjusted* snapshot (research only); raw prices (universe price
    cap, plan ranges, quotes) come from ``market.bar_projection`` raw bars - the two are never mixed in one series;
  * shared features go through ``domain.features`` kernels and are registered in ``market.feature_result`` under
    the full 19-dimension key, reused when a READY result exists (idempotent re-run);
  * missing stays ``None``: a feature that cannot be computed is a missing matrix cell, never 0;
  * derived datasets (membership, caps, explanations) are published through the normal manifest protocol so GC
    sees them and a release component can pin a snapshot.
Not covered: the release orchestration itself (``release_service``), live quote ingestion (``account_service``),
a supplier of continuing adjusted history after the legacy cut (see docs/integration/I1_evidence.md).
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import ingest_repo, market_repo
from pulse.adapters.postgres._canon import sha256_of
from pulse.adapters.postgres.ledger_repo import LedgerRepo
from pulse.adapters.postgres.release import PgAnalysisRelease
from pulse.adapters.sources.registry import EndpointSpec
from pulse.application.data_service import DataService, ingest_repo_create_batch
from pulse.domain import features as F
from pulse.domain.features.definitions import Series
from pulse.domain.portfolio.coordinator import AccountSnapshot, PositionView
from pulse.domain.ranking.baseline import FeatureMatrix
from pulse.domain.ranking.universe import InstrumentFacts
from pulse.domain.sectors.ranking import SectorNode, SectorSnapshot

SLOT_KEY = "main"
AVAILABILITY_POLICY = "observed-actual-available-at-v1"
RECONSTRUCTED_POLICY = "reconstructed-session-close-plus-1h-v1"      # policy_usable_at = 15:00 CST close + 1 hour, per bar
RECONSTRUCTED_DELAY = timedelta(hours=1)
SESSION_POLICY = "regular_continuous_v1"
RESEARCH_DATASET = "bars_legacy_fwd_adjusted_1d"
INDEX_DATASET = "index_bars_legacy_1d"
RAW_DATASETS = ("bars_raw_1d", "bars_raw_1d_tencent")
BENCHMARK_SYMBOL = "000300"                       # CSI 300 as the relative-strength benchmark (index points)
DERIVED_SOURCE = "pulse_analysis"
DERIVED_ENDPOINT = "release_artifacts"
DERIVED_SCHEMA = "1.0"
CHECK_CONTRACTS = {
    "pulse.sector_membership": "Sector membership facts: instrument_id, node_id, effective_from, available_at, evidence_ref.",
    "pulse.instrument_cap_fact": "Circulating/total market capitalisation facts in CNY with their as-of time and unit assumption.",
    "pulse.analysis_artifact": "Content-addressed release artifacts (explanation bodies, component summaries).",
}
NS = uuid.UUID("b3f6c1a0-52d7-4f4e-8a39-7c1e0d9a5b21")
CST = timezone(timedelta(hours=8))


def aid(kind: str, *parts: object) -> uuid.UUID:
    """Deterministic id for integration-owned objects."""
    return uuid.uuid5(NS, kind + "|" + "|".join(str(p) for p in parts))


def dec(value: Any, places: int = 12) -> Decimal | None:
    """float/str -> Decimal at wire precision; None, NaN and inf stay missing."""
    if value is None:
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


# --------------------------------------------------------------------------------------------------------
# research (forward-adjusted) bars and benchmark
# --------------------------------------------------------------------------------------------------------
@dataclass
class ResearchInputs:
    manifest: dict
    bars: pl.DataFrame                    # instrument_id str, trade_date, open/high/low/close/volume Float64, available_at
    sessions: list[date]
    session_date: date
    benchmark: dict[date, float]
    benchmark_manifest: dict | None
    decision_as_of: datetime
    notes: list[str] = field(default_factory=list)
    availability_policy: str = AVAILABILITY_POLICY


def reconstructed(policy: str) -> bool:
    return policy == RECONSTRUCTED_POLICY


def policy_usable_at(day: date) -> datetime:
    """History rebuild: a bar is usable one declared delay after the session close; ``available_at`` is never rewritten."""
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=CST).astimezone(timezone.utc) + RECONSTRUCTED_DELAY


def _ready_manifest(conn: psycopg.Connection, dataset_id: str, as_of: datetime | None) -> dict | None:
    sql = "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'"
    args: list[Any] = [dataset_id]
    if as_of is not None:
        sql += " AND ready_at <= %s"
        args.append(as_of)
    return conn.execute(sql + " ORDER BY manifest_version DESC LIMIT 1", args).fetchone()


def ready_manifests(conn: psycopg.Connection, dataset_ids: Sequence[str], as_of: datetime | None) -> list[dict]:
    sql = "SELECT * FROM market.dataset_manifest WHERE dataset_id = ANY(%s) AND manifest_state='READY'"
    args: list[Any] = [list(dataset_ids)]
    if as_of is not None:
        sql += " AND ready_at <= %s"
        args.append(as_of)
    return conn.execute(sql + " ORDER BY dataset_id, manifest_version", args).fetchall()


def load_research_inputs(conn: psycopg.Connection, lake: LakeStore, decision_as_of: datetime | None,
                         availability_policy: str = AVAILABILITY_POLICY) -> ResearchInputs:
    """Visible adjusted bars, latest revision per key, plus the benchmark index series.

    Observed policy: ``available_at <= decision_as_of``. Reconstructed policy (history rebuild): the FIRST version of a bar is
    usable once ``policy_usable_at(trade_date) <= decision_as_of`` (its stored ``available_at`` is the back-fill ingest time and is
    left as it is); a later revision exists for the rebuild only if it was recorded by then (``available_at <= decision_as_of``).
    """
    recon = reconstructed(availability_policy)
    if recon and decision_as_of is None:
        raise ValueError("a reconstructed availability policy needs an explicit decision_as_of")
    man = _ready_manifest(conn, RESEARCH_DATASET, None if recon else decision_as_of)
    if man is None:
        raise LookupError(f"no READY {RESEARCH_DATASET} snapshot visible at {decision_as_of}")
    df = lake.read_snapshot_frames(man["content_hash"])
    if df is None or df.height == 0:
        raise LookupError("research snapshot is empty")
    cutoff = decision_as_of or datetime.now(timezone.utc)
    if recon:
        usable = pl.col("trade_date").map_elements(lambda d: policy_usable_at(d) <= cutoff, return_dtype=pl.Boolean)
        first = pl.col("revision") == 1       # the lake keeps the latest revision per key: a later one not yet recorded leaves the bar missing
        df = df.filter(usable & (first | (pl.col("available_at") <= cutoff)))
    else:
        df = df.filter(pl.col("available_at") <= cutoff)                   # point-in-time visibility
    df = df.sort("revision").unique(["instrument_id", "trade_date"], keep="last")   # latest visible revision
    df = df.select(
        "instrument_id", "trade_date", "available_at",
        *[pl.col(c).cast(pl.Float64).alias(c.split("_")[0]) for c in ("open_price", "high_price", "low_price", "close_price")],
        pl.col("volume_shares").cast(pl.Float64).alias("volume"))
    per_day = df.group_by("trade_date").agg(pl.len().alias("n")).sort("trade_date")
    if per_day.height == 0:
        raise LookupError("no research bars visible at the decision cutoff")
    median = per_day["n"].median()
    full_days = per_day.filter(pl.col("n") >= 0.5 * median)["trade_date"].to_list()   # a thin last day is not a session yet
    session_date = max(full_days)
    df = df.filter(pl.col("trade_date") <= session_date).sort(["instrument_id", "trade_date"])
    sessions = sorted(full_days)
    notes: list[str] = []
    bench: dict[date, float] = {}
    bman = _ready_manifest(conn, INDEX_DATASET, None if recon else decision_as_of)
    if bman is not None:
        idx = lake.read_snapshot_frames(bman["content_hash"])
        if idx is not None:
            vis = (pl.col("date").map_elements(lambda d: policy_usable_at(d) <= cutoff, return_dtype=pl.Boolean)
                   & ((pl.col("ingested_at") == pl.col("ingested_at").min().over(["symbol", "date"])) | (pl.col("ingested_at") <= cutoff))
                   if recon else pl.col("ingested_at") <= cutoff)
            idx = idx.filter((pl.col("symbol") == BENCHMARK_SYMBOL) & vis & (pl.col("date") <= session_date)).sort("ingested_at").unique("date", keep="last")
            bench = {d: float(c) for d, c in zip(idx["date"].to_list(), idx["close"].to_list()) if c is not None}
    if not bench:
        notes.append(f"benchmark {BENCHMARK_SYMBOL} missing: relative_strength features stay missing")
    if recon:
        notes.append(f"availability reconstructed: {RECONSTRUCTED_POLICY}; stored available_at (ingest time) is not rewritten")
    return ResearchInputs(man, df, sessions, session_date, bench, bman, cutoff, notes, availability_policy)


# --------------------------------------------------------------------------------------------------------
# raw bars (execution-grade prices)
# --------------------------------------------------------------------------------------------------------
_USABLE_SQL = "((trade_date::timestamp + time '15:00' + interval '1 hour') AT TIME ZONE 'Asia/Shanghai') <= %s"
#: Reconstructed policy over raw bar versions: the first version is usable by policy time, a later one only once recorded.
_FIRST_VERSION = ("available_at = min(available_at) OVER (PARTITION BY instrument_id, trade_date, timeframe) AS is_first")


def load_raw_close(conn: psycopg.Connection, session_date: date, as_of: datetime,
                   availability_policy: str = AVAILABILITY_POLICY) -> dict[uuid.UUID, Decimal]:
    """Raw (unadjusted) close of the session for every instrument that has a visible raw daily bar."""
    if reconstructed(availability_policy):
        rows = conn.execute(
            "SELECT DISTINCT ON (instrument_id) instrument_id, close_price_raw FROM (SELECT *, " + _FIRST_VERSION
            + " FROM market.bar_projection WHERE trade_date=%s AND timeframe='1d' AND " + _USABLE_SQL + ") x"
            " WHERE (is_first OR available_at <= %s) AND close_price_raw IS NOT NULL ORDER BY instrument_id, available_at DESC, snapshot_id",
            (session_date, as_of, as_of)).fetchall()
        return {r["instrument_id"]: r["close_price_raw"] for r in rows}
    rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, close_price_raw FROM market.bar_projection"
        " WHERE trade_date=%s AND timeframe='1d' AND available_at <= %s"
        " AND close_price_raw IS NOT NULL ORDER BY instrument_id, available_at DESC, snapshot_id", (session_date, as_of)).fetchall()
    return {r["instrument_id"]: r["close_price_raw"] for r in rows}


def load_raw_series(conn: psycopg.Connection, instrument_ids: Sequence[uuid.UUID], session_date: date,
                    as_of: datetime, lookback_days: int = 400, availability_policy: str = AVAILABILITY_POLICY) -> dict[uuid.UUID, dict[str, list]]:
    """Raw daily OHLCV per instrument up to ``session_date`` (latest visible version per day).

    ``o/h/l/c/v`` are float64 for the shared feature kernels; ``dc`` carries the exact Decimal raw closes for price levels."""
    args = (list(instrument_ids), session_date - timedelta(days=lookback_days), session_date, as_of)
    cols = "instrument_id, trade_date, open_price_raw o, high_price_raw h, low_price_raw l, close_price_raw c, volume_shares v"
    if reconstructed(availability_policy):
        rows = conn.execute(
            "SELECT DISTINCT ON (instrument_id, trade_date) " + cols + " FROM (SELECT *, " + _FIRST_VERSION + " FROM market.bar_projection"
            " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND " + _USABLE_SQL + ") x"
            " WHERE is_first OR available_at <= %s ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id",
            (*args, as_of)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT ON (instrument_id, trade_date) " + cols + " FROM market.bar_projection"
            " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
            " ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id", args).fetchall()
    out: dict[uuid.UUID, dict[str, list]] = {}
    for r in rows:
        s = out.setdefault(r["instrument_id"], {k: [] for k in ("d", "o", "h", "l", "c", "v", "dc")})
        s["d"].append(r["trade_date"])
        s["dc"].append(r["c"])
        for k in ("o", "h", "l", "c"):
            s[k].append(None if r[k] is None else float(r[k]))
        s["v"].append(None if r["v"] is None else float(r["v"]))
    return out


def raw_snapshot_refs(conn: psycopg.Connection, as_of: datetime, availability_policy: str = AVAILABILITY_POLICY) -> list[dict]:
    return ready_manifests(conn, RAW_DATASETS, None if reconstructed(availability_policy) else as_of)


# --------------------------------------------------------------------------------------------------------
# instrument facts
# --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class InstrumentMeta:
    instrument_id: uuid.UUID
    code: str | None
    venue_code: str
    instrument_type: str
    currency: str
    exchange_timezone: str
    display_name: str
    state_version: int
    trading_status: str
    is_risk_warning: bool
    board_code: str
    source_symbols: tuple[tuple[str, str], ...]


BOARD_VOCAB = {"main": "mainboard"}        # DB board_code -> UniverseRules vocabulary


def load_instrument_meta(conn: psycopg.Connection, as_of: datetime, availability_policy: str = AVAILABILITY_POLICY) -> tuple[dict[uuid.UUID, InstrumentMeta], dict[uuid.UUID, date | None]]:
    """Latest instrument state visible at ``as_of``: effective_from and (observed policy) available_at bounded.

    Reconstructed policy: states/aliases first observed at import are used by ``effective_from`` only; their stored
    ``available_at`` (import time) is not rewritten and is not used as the visibility rule for history.
    """
    avail = "TRUE" if reconstructed(availability_policy) else "{t}.available_at <= %(as_of)s"
    rows = conn.execute(
        "SELECT i.instrument_id, i.instrument_type, i.venue_code, i.currency, i.exchange_timezone, i.listed_on,"
        " s.board_code, s.display_name, s.is_risk_warning, s.trading_status, s.state_version"
        " FROM core.instrument i JOIN LATERAL (SELECT * FROM core.instrument_state st WHERE st.instrument_id=i.instrument_id"
        " AND st.effective_from <= %(as_of)s AND " + avail.format(t="st") + " ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1) s ON true"
        " WHERE i.instrument_type='equity'", {"as_of": as_of}).fetchall()
    aliases: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for a in conn.execute(
            "SELECT instrument_id, source_id, source_symbol FROM core.instrument_alias WHERE effective_from <= %(as_of)s"
            " AND (effective_to IS NULL OR effective_to > %(as_of)s) AND " + avail.format(t="core.instrument_alias") + " ORDER BY source_id",
            {"as_of": as_of}):
        aliases.setdefault(a["instrument_id"], []).append((a["source_id"], a["source_symbol"]))
    metas, listed = {}, {}
    for r in rows:
        syms = tuple(aliases.get(r["instrument_id"], ()))
        code = next((s for src, s in syms if src == "legacy_warehouse"), None)
        metas[r["instrument_id"]] = InstrumentMeta(
            r["instrument_id"], code, r["venue_code"], r["instrument_type"], r["currency"], r["exchange_timezone"],
            r["display_name"], r["state_version"], r["trading_status"], r["is_risk_warning"], r["board_code"], syms)
        listed[r["instrument_id"]] = r["listed_on"]
    return metas, listed


def listed_sessions(listed_on: date | None, sessions: Sequence[date]) -> int | None:
    """Sessions since listing as a lower bound inside the observed calendar; unknown listing date stays None."""
    if listed_on is None or not sessions:
        return None
    return sum(1 for d in sessions if d >= listed_on)


def build_facts(metas: Mapping[uuid.UUID, InstrumentMeta], listed: Mapping[uuid.UUID, date | None],
                sessions: Sequence[date], raw_close: Mapping[uuid.UUID, Decimal],
                caps_cny: Mapping[uuid.UUID, Decimal]) -> list[InstrumentFacts]:
    out = []
    for iid, m in metas.items():
        out.append(InstrumentFacts(
            instrument_id=iid, venue_code=m.venue_code, board_code=BOARD_VOCAB.get(m.board_code, m.board_code),
            instrument_type=m.instrument_type, is_st=m.is_risk_warning,
            listed_sessions=listed_sessions(listed.get(iid), sessions), price_raw=raw_close.get(iid),
            circulating_market_cap_cny=caps_cny.get(iid), is_suspended=(m.trading_status.lower() in ("suspended", "halted"))))
    return out


# --------------------------------------------------------------------------------------------------------
# derived dataset publishing (membership, caps, explanations)
# --------------------------------------------------------------------------------------------------------
def ensure_internal_registry(conn: psycopg.Connection, now: datetime) -> EndpointSpec:
    """Register the internal ``pulse_analysis`` source/endpoint and the derived-artifact contract versions."""
    spec = EndpointSpec(DERIVED_SOURCE, DERIVED_ENDPOINT, DERIVED_SCHEMA, "1.0.0", "pulse.analysis_artifact", "batch",
                        7 * 86400, None, None, {"mode": "derived datasets of the analysis pipeline", "timezone": "Asia/Shanghai"}, True, None)
    with conn.transaction():
        conn.execute("INSERT INTO core.source(source_id, display_name, provider_uri, created_at) VALUES (%s,%s,%s,%s)"
                     " ON CONFLICT (source_id) DO NOTHING",
                     (DERIVED_SOURCE, "Pulse analysis pipeline (derived datasets)", "internal://pulse_analysis", now))
        for cid, text in CHECK_CONTRACTS.items():          # registered before the endpoint that references them
            doc = {"description": text, "type": "object"}
            conn.execute(
                "INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash,"
                " compatibility_policy, created_at) VALUES (%s,%s,%s,%s,'additive-optional-v1',%s)"
                " ON CONFLICT (contract_id, schema_version) DO NOTHING", (cid, contracts.CONTRACT_VERSION, Jsonb(doc), sha256_of(doc), now))
        ingest_repo.ensure_endpoint(conn, spec, now)
    return spec


def find_derived(conn: psycopg.Connection, dataset_id: str, key: str, value: str) -> dict | None:
    """READY derived manifest whose coverage document carries ``key == value`` (content-addressed reuse)."""
    return conn.execute(
        "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' AND coverage_document->>%s=%s"
        " ORDER BY manifest_version DESC LIMIT 1", (dataset_id, key, value)).fetchone()


def publish_derived(svc: DataService, dataset_id: str, frame: pl.DataFrame, coverage: dict, *, contract_id: str,
                    dependencies: Sequence[tuple[uuid.UUID, str]] = (), watermark: datetime | None = None) -> dict:
    """Publish ``frame`` as an immutable READY dataset snapshot (stage -> validate -> READY + outbox)."""
    now = svc.clock()
    spec = ensure_internal_registry(svc.conn, now)
    snapshot_id = uuid.uuid4()
    batch_id = ingest_repo_create_batch(svc, spec, f"{dataset_id}:{snapshot_id}", now)
    owner = f"derived:{batch_id}"
    with svc.conn.transaction():
        ingest_repo.commit_raw(svc.conn, batch_id, now)
        ingest_repo.set_batch_state(svc.conn, batch_id, "NORMALIZED")
    pub = svc.publish_dataset(dataset_id=dataset_id, snapshot_id=snapshot_id, batch_id=batch_id, frames={"all": frame},
                              contract_id=contract_id, coverage=coverage, owner=owner,
                              available_watermark=watermark or now, dependencies=dependencies)
    return market_repo.get_manifest(svc.conn, pub["snapshot_id"])


# --------------------------------------------------------------------------------------------------------
# circulating market cap facts (legacy securities snapshot, read through the legacy adapter)
# --------------------------------------------------------------------------------------------------------
CAP_DATASET = "instrument_circulating_cap_legacy"
CAP_UNIT_NOTE = ("legacy securities.float_cap interpreted as 100 million CNY (亿元): magnitude check 000001 total_cap=2301.54; "
                 "the unit is not stated by the source and is unverified")


def publish_cap_snapshot(svc: DataService, warehouse_root: str) -> dict | None:
    """Publish the legacy securities caps, one row per (instrument, as_of) version, as a dataset.

    Every dated version is kept so a history rebuild sees the cap that was known for its session; readers pick the
    latest visible version per instrument. Returns the manifest (reused when identical)."""
    from pathlib import Path

    from pulse.adapters.sources.legacy import LegacyWarehouse

    wh = LegacyWarehouse(Path(warehouse_root))
    sec = wh.read_securities().sort("ingested_at").unique(["code", "as_of"], keep="last")
    sec = sec.filter(pl.col("float_cap").is_not_null())
    if sec.height == 0:
        return None
    ids = {r["source_symbol"]: r["instrument_id"] for r in svc.conn.execute(
        "SELECT a.source_symbol, a.instrument_id FROM core.instrument_alias a JOIN core.instrument i USING (instrument_id)"
        " WHERE a.source_id='legacy_warehouse' AND i.instrument_type='equity'")}
    rows = []
    for code, cap, tot, asof, ing in zip(sec["code"], sec["float_cap"], sec["total_cap"], sec["as_of"], sec["ingested_at"]):
        iid = ids.get(code)
        if iid is None:
            continue
        rows.append({"instrument_id": str(iid), "as_of": asof, "float_cap_cny": Decimal(str(cap)) * Decimal("100000000"),
                     "total_cap_cny": None if tot is None else Decimal(str(tot)) * Decimal("100000000"), "available_at": ing})
    frame = pl.DataFrame(rows, schema={"instrument_id": pl.String, "as_of": pl.String, "float_cap_cny": pl.Decimal(38, 2),
                                       "total_cap_cny": pl.Decimal(38, 2), "available_at": pl.Datetime("us", "UTC")}).sort(["instrument_id", "as_of"])
    digest = hashlib.sha256(frame.write_csv().encode()).hexdigest()
    hit = find_derived(svc.conn, CAP_DATASET, "frame_sha256", digest)
    if hit is not None:
        return hit
    cov = {"dataset": CAP_DATASET, "frame_sha256": digest, "rows": frame.height, "unit": CAP_UNIT_NOTE,
           "source": "legacy_warehouse/securities (read-only legacy adapter)", "as_of": max(frame["as_of"].to_list()),
           "missing": "instruments absent from the legacy securities snapshot have no cap (unknown, never 0)"}
    return publish_derived(svc, CAP_DATASET, frame, cov, contract_id="pulse.instrument_cap_fact",
                           watermark=max(frame["available_at"].to_list()))


def load_caps(conn: psycopg.Connection, lake: LakeStore, as_of: datetime,
              availability_policy: str = AVAILABILITY_POLICY) -> tuple[dict[uuid.UUID, Decimal], dict | None]:
    man = _ready_manifest(conn, CAP_DATASET, None if reconstructed(availability_policy) else as_of)
    if man is None:
        return {}, None
    return visible_caps(lake.read_snapshot_frames(man["content_hash"]), as_of, availability_policy), man


def visible_caps(df: pl.DataFrame, as_of: datetime, availability_policy: str) -> dict[uuid.UUID, Decimal]:
    """Latest cap version per instrument visible at ``as_of``: by recorded time (observed) or by its own date (reconstructed)."""
    if not reconstructed(availability_policy):
        df = df.filter(pl.col("available_at") <= as_of)
    else:                                       # a cap fact is usable only for sessions on/after its own as-of date
        df = df.filter(pl.col("as_of") <= as_of.astimezone(CST).date().isoformat())
    df = df.sort(["as_of", "available_at"]).unique("instrument_id", keep="last")
    return {uuid.UUID(i): c for i, c in zip(df["instrument_id"].to_list(), df["float_cap_cny"].to_list()) if c is not None}


# --------------------------------------------------------------------------------------------------------
# shared features (universe level, registered in market.feature_result)
# --------------------------------------------------------------------------------------------------------
def _series_for(g: pl.DataFrame, bench: Mapping[date, float] | None) -> Series:
    dates = g["trade_date"].to_list()
    f = lambda c: tuple(None if v is None else float(v) for v in g[c].to_list())  # noqa: E731
    return Series(tuple(d.isoformat() for d in dates), f("open"), f("high"), f("low"), f("close"), f("volume"),
                  None if bench is None else tuple(bench.get(d) for d in dates))


def feature_key(fid: str, params: dict | None, *, inputs: ResearchInputs, instrument_ids: tuple[str, ...],
                universe_policy_version: str, market_rules_version: str) -> F.FeatureKey:
    man = inputs.manifest
    cov = man["coverage_document"]
    bench = f"+bench:{inputs.benchmark_manifest['content_hash'][:16]}" if inputs.benchmark_manifest else ""
    return F.make_key(
        fid, params, instrument_ids=instrument_ids, timeframe="1d", adjustment_mode=cov.get("adjustment_mode", "forward_adjusted"),
        snapshot_id=str(man["snapshot_id"]), decision_as_of=inputs.decision_as_of, availability_policy_version=inputs.availability_policy,
        session_policy_version=SESSION_POLICY, input_revision=f"manifest:{man['content_hash'][:16]}{bench}",
        input_schema_version=f"{man['contract_id']}-{man['schema_version']}", universe_policy_version=universe_policy_version,
        calendar_version=cov.get("calendar_version", "not-applicable"), adjustment_version=cov.get("adjustment_version", "not-applicable"),
        market_rules_version=market_rules_version)


class SharedFeatures:
    """Compute-or-reuse shared feature results, one registered key per (feature, params, instrument set)."""

    def __init__(self, conn: psycopg.Connection, lake: LakeStore, inputs: ResearchInputs, *, universe_policy_version: str,
                 market_rules_version: str, clock=lambda: datetime.now(timezone.utc)):
        self.conn, self.lake, self.inputs = conn, lake, inputs
        self.universe_policy_version, self.market_rules_version, self.clock = universe_policy_version, market_rules_version, clock
        self.computed = 0
        self.reused = 0
        self._parts: dict[str, pl.DataFrame] | None = None

    def _partitions(self) -> dict[str, pl.DataFrame]:
        if self._parts is None:
            self._parts = {k[0]: v for k, v in self.inputs.bars.partition_by("instrument_id", as_dict=True, maintain_order=True).items()}
        return self._parts

    def result(self, fid: str, params: dict | None, instrument_ids: Sequence[str]) -> tuple[pl.DataFrame, F.FeatureKey, dict]:
        """(frame[instrument_id, trade_date, outputs...], full key, feature_result row)."""
        ids = tuple(sorted(instrument_ids))
        key = feature_key(fid, params, inputs=self.inputs, instrument_ids=ids,
                          universe_policy_version=self.universe_policy_version, market_rules_version=self.market_rules_version)
        contracts.check("FeatureKey", key.to_wire())
        existing = market_repo.get_feature(self.conn, key)
        if existing and existing["result_state"] == "READY":
            self.reused += 1
            return pl.read_parquet(io.BytesIO(self.lake.read_object(existing["result_hash"]))), key, existing
        epoch = (existing["lease_epoch"] + 1) if existing else 1
        if not market_repo.begin_feature(self.conn, key, epoch):
            raise RuntimeError("feature key is owned by a newer lease epoch")
        owner = f"feature:{market_repo.key_hash(key)[:16]}:{epoch}"
        try:
            bench = self.inputs.benchmark if fid == "relative_strength" else None
            frames = []
            parts = self._partitions()
            for iid in ids:
                g = parts.get(iid)
                if g is None:
                    continue
                res = F.compute(fid, _series_for(g, bench), params)
                frames.append(pl.DataFrame({"instrument_id": iid, "trade_date": g["trade_date"], **res},
                                           schema_overrides={k: pl.Float64 for k in res}))
            out = pl.concat(frames) if frames else pl.DataFrame()
            ref = self.lake.put_parquet(out, owner=owner, kind="feature")
            if not market_repo.finish_feature(self.conn, key, epoch, ok=True, result_uri=ref.rel_path, result_hash=ref.sha256,
                                              computed_at=self.clock()):
                raise RuntimeError("lease epoch fenced out; result discarded")
        except Exception:
            market_repo.finish_feature(self.conn, key, epoch, ok=False)
            raise
        finally:
            self.lake.release(owner)
        self.computed += 1
        return out, key, market_repo.get_feature(self.conn, key)


# horizon-specific recipes: (derived feature id) -> (shared feature, params, output, transform)
def _ratio_minus_one(close: float | None, base: float | None) -> float | None:
    return None if close is None or base is None or base == 0 else close / base - 1.0


@dataclass(frozen=True)
class Recipe:
    derived_id: str
    shared_id: str
    params: tuple[tuple[str, Any], ...]
    output: str
    transform: str          # identity | close_over | over_close | minus_one

    def params_dict(self) -> dict | None:
        return dict(self.params) or None


RECIPES: dict[str, Recipe] = {r.derived_id: r for r in (
    Recipe("trend_dev_5", "ma", (("n", 5),), "ma", "close_over"),
    Recipe("trend_dev_20", "ma", (("n", 20),), "ma", "close_over"),
    Recipe("trend_dev_60", "ma", (("n", 60),), "ma", "close_over"),
    Recipe("rel_strength_5", "relative_strength", (("n", 5),), "relative_strength", "minus_one"),
    Recipe("rel_strength_20", "relative_strength", (("n", 20),), "relative_strength", "minus_one"),
    Recipe("rel_strength_60", "relative_strength", (("n", 60),), "relative_strength", "minus_one"),
    Recipe("volume_ratio_5", "volume_ratio", (("n", 5),), "volume_ratio", "identity"),
    Recipe("atr_pct_14", "atr", (("n", 14),), "atr", "over_close"),
    Recipe("macd_hist_pct", "macd", (), "hist", "over_close"),
)}


class PitFeatureMatrix:
    """``FeatureMatrixPort`` over registered shared features, point-in-time at ``inputs.decision_as_of``.

    The value of a derived feature is its value on the session date; an instrument without a bar on that
    date (suspension / gap) has a missing cell, it never inherits an older value.
    """

    def __init__(self, shared: SharedFeatures, instrument_ids: Sequence[uuid.UUID]):
        self.shared = shared
        self.instrument_ids = tuple(instrument_ids)
        self.session_date = shared.inputs.session_date
        self._cache: dict[str, dict[uuid.UUID, Decimal | None]] = {}
        self.keys: dict[str, F.FeatureKey] = {}
        self.rows: dict[str, dict] = {}
        self._close = {uuid.UUID(i): c for i, c in zip(
            *shared.inputs.bars.filter(pl.col("trade_date") == self.session_date).select("instrument_id", "close").to_dict(as_series=False).values())}

    def _derive(self, derived_id: str) -> dict[uuid.UUID, Decimal | None]:
        if derived_id in self._cache:
            return self._cache[derived_id]
        r = RECIPES[derived_id]
        frame, key, row = self.shared.result(r.shared_id, r.params_dict(), [str(i) for i in self.instrument_ids])
        self.keys[derived_id], self.rows[derived_id] = key, row
        day = frame.filter(pl.col("trade_date") == self.session_date)
        out: dict[uuid.UUID, Decimal | None] = {}
        for iid, v in zip(day["instrument_id"].to_list(), day[r.output].to_list()):
            u = uuid.UUID(iid)
            close = self._close.get(u)
            if v is None:
                out[u] = None
            elif r.transform == "identity":
                out[u] = dec(v)
            elif r.transform == "close_over":
                out[u] = dec(_ratio_minus_one(close, v))
            elif r.transform == "minus_one":
                out[u] = dec(v - 1.0)
            elif r.transform == "over_close":
                out[u] = dec(None if not close else v / close)
            else:
                raise ValueError(r.transform)
        self._cache[derived_id] = out
        return out

    def matrix(self, instrument_ids: Sequence[uuid.UUID], feature_ids: Sequence[str], decision_as_of: datetime) -> FeatureMatrix:
        if decision_as_of > self.shared.inputs.decision_as_of:
            raise ValueError("matrix requested for a cutoff later than the loaded inputs: would read the future")
        if decision_as_of < self.shared.inputs.decision_as_of:
            raise ValueError("matrix was computed with inputs visible at a later cutoff: refusing to answer for an earlier one")
        cols = {fid: self._derive(fid) for fid in feature_ids}
        return {iid: {fid: cols[fid].get(iid) for fid in feature_ids} for iid in instrument_ids}


# --------------------------------------------------------------------------------------------------------
# sector membership from the legacy boards evidence
# --------------------------------------------------------------------------------------------------------
TAXONOMY_ID = "legacy_boards_industry"
TAXONOMY_VERSION = 1
MEMBERSHIP_DATASET = "sector_membership_legacy_boards_industry"


@dataclass
class Membership:
    taxonomy_id: str
    taxonomy_version: int
    manifest: dict
    nodes: dict[str, str]                       # node_id -> display name
    members: dict[str, tuple[uuid.UUID, ...]]   # node_id -> instrument ids
    available_at: datetime
    as_of: date
    evidence: str


def _boards_frame(conn: psycopg.Connection, lake: LakeStore, as_of: datetime) -> tuple[pl.DataFrame, str] | None:
    """Newest legacy boards file (by ingest time) that was available at ``as_of``; raw bytes come from the lake."""
    best = None
    for r in conn.execute(
            "SELECT raw_object_id, source_record_key, content_hash, ingested_at FROM ingest.raw_object"
            " WHERE source_id='legacy_warehouse' AND endpoint_id='evidence_files' AND source_record_key LIKE 'boards/%%'"):
        df = pl.read_parquet(io.BytesIO(lake.read_object(r["content_hash"])))
        ing = df["ingested_at"].max()
        if ing is None or ing > as_of:
            continue
        if "industry" not in set(df["kind"].unique().to_list()):
            continue
        if best is None or ing > best[0]:
            best = (ing, df, r["source_record_key"], r["content_hash"])
    if best is None:
        return None
    return best[1].filter(pl.col("kind") == "industry"), f"{best[2]}@{best[3][:16]}"


def ensure_membership(svc: DataService, lake: LakeStore, as_of: datetime) -> Membership | None:
    """Taxonomy + instrument_tag rows + membership dataset snapshot from the legacy boards evidence (idempotent)."""
    conn = svc.conn
    got = _boards_frame(conn, lake, as_of)
    if got is None:
        return None
    df, evidence = got
    ids = {r["source_symbol"]: r["instrument_id"] for r in conn.execute(
        "SELECT a.source_symbol, a.instrument_id FROM core.instrument_alias a JOIN core.instrument i USING (instrument_id)"
        " WHERE a.source_id='legacy_warehouse' AND i.instrument_type='equity'")}
    df = df.filter(pl.col("code").is_in(list(ids)))
    board_asof = df["as_of"].max()
    available_at = df["ingested_at"].max()
    eff_from = datetime(board_asof.year, board_asof.month, board_asof.day, tzinfo=CST)
    nodes = {b: n for b, n in zip(df["board_id"].to_list(), df["board_name"].to_list())}
    with conn.transaction():
        conn.execute("INSERT INTO core.taxonomy(taxonomy_id, taxonomy_version, taxonomy_kind, owner_source_id, definition, available_at)"
                     " VALUES (%s,%s,'industry','legacy_warehouse',%s,%s) ON CONFLICT DO NOTHING",
                     (TAXONOMY_ID, TAXONOMY_VERSION, Jsonb({"origin": "legacy_warehouse/boards kind=industry",
                                                            "evidence": evidence, "taxonomies_not_merged": True,
                                                            "display_names_are_not_identifiers": True}), available_at))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO core.taxonomy_node(taxonomy_id, taxonomy_version, node_id, parent_node_id, display_name, definition)"
                " VALUES (%s,%s,%s,NULL,%s,%s) ON CONFLICT DO NOTHING",
                [(TAXONOMY_ID, TAXONOMY_VERSION, b, n, Jsonb({"board_id": b})) for b, n in sorted(nodes.items())])
            cur.executemany(
                "INSERT INTO core.instrument_tag(assignment_id, instrument_id, taxonomy_id, taxonomy_version, node_id, effective_from,"
                " effective_to, available_at, assertion_kind, evidence_ref, weight) VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,'observed',%s,NULL)"
                " ON CONFLICT DO NOTHING",
                [(aid("tag", ids[c], b), ids[c], TAXONOMY_ID, TAXONOMY_VERSION, b, eff_from, available_at, evidence)
                 for c, b in zip(df["code"].to_list(), df["board_id"].to_list())])
    tags = pl.DataFrame({"instrument_id": [str(ids[c]) for c in df["code"].to_list()],
                         "node_id": df["board_id"].to_list()}).sort(["node_id", "instrument_id"])
    digest = hashlib.sha256(tags.write_csv().encode()).hexdigest()
    man = find_derived(conn, MEMBERSHIP_DATASET, "members_sha256", digest)
    if man is None:
        frame = tags.with_columns(pl.lit(eff_from).alias("effective_from").cast(pl.Datetime("us", "UTC")),
                                  pl.lit(available_at).alias("available_at").cast(pl.Datetime("us", "UTC")))
        cov = {"dataset": MEMBERSHIP_DATASET, "members_sha256": digest, "taxonomy_id": TAXONOMY_ID, "taxonomy_version": TAXONOMY_VERSION,
               "nodes": len(nodes), "rows": frame.height, "evidence": evidence, "membership_as_of": board_asof.isoformat(),
               "note": "membership is a point-in-time observation of the legacy boards file; it is not back-filled to earlier dates"}
        man = publish_derived(svc, MEMBERSHIP_DATASET, frame, cov, contract_id="pulse.sector_membership", watermark=available_at)
    members: dict[str, list[uuid.UUID]] = {}
    for iid, b in zip(tags["instrument_id"].to_list(), tags["node_id"].to_list()):
        members.setdefault(b, []).append(uuid.UUID(iid))
    return Membership(TAXONOMY_ID, TAXONOMY_VERSION, man, nodes, {k: tuple(v) for k, v in members.items()}, available_at, board_asof, evidence)


def sector_snapshots(m: Membership) -> list[SectorSnapshot]:
    return [SectorSnapshot(SectorNode(m.taxonomy_id, m.taxonomy_version, node), m.manifest["snapshot_id"], tuple(ids), m.available_at)
            for node, ids in sorted(m.members.items())]


# --------------------------------------------------------------------------------------------------------
# release component writer and account snapshot ports
# --------------------------------------------------------------------------------------------------------
class PgReleaseComponentWriter:
    """``ReleaseComponentWriter`` over ``PgAnalysisRelease``: registers a READY component for a BUILDING release."""

    def __init__(self, release: PgAnalysisRelease, *, input_version_hash: str, computed_at: datetime,
                 snapshot_id: uuid.UUID | None = None):
        self.release, self.input_version_hash, self.computed_at, self.snapshot_id = release, input_version_hash, computed_at, snapshot_id

    def add_component(self, release_id: uuid.UUID, component_key: str, artifact_ref: str, artifact_hash: str) -> None:
        self.release.mark_ready(release_id, component_key, artifact_ref=artifact_ref, artifact_hash=artifact_hash,
                                input_version_hash=self.input_version_hash, snapshot_id=self.snapshot_id,
                                computed_at=self.computed_at)


class LedgerAccountSnapshotPort:
    """``AccountSnapshotPort`` over the unified ledger (replayed from the stored events)."""

    def __init__(self, conn: psycopg.Connection):
        self.repo = LedgerRepo(conn)

    def snapshot(self, account_id: uuid.UUID) -> AccountSnapshot:
        return self.of_ledger(account_id, self.repo.load(account_id))

    @staticmethod
    def of_ledger(account_id: uuid.UUID, ledger) -> AccountSnapshot:
        positions = {uuid.UUID(sym): PositionView(h.quantity, ledger.sellable(sym), ledger.reserved_sell(sym))
                     for sym, h in ledger.holdings.items() if h.quantity}
        return AccountSnapshot(account_id=account_id, account_version=ledger.account_version,
                               cash_available_cny=ledger.cash_available(), positions=positions)


def canonical_json(value: Any) -> str:
    return json.dumps(contracts.to_wire(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
