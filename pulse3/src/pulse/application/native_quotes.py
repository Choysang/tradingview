"""Quote snapshots from the native intraday recording: no request, the whole board, native lineage (tape / actionability input).

Why: ``analysis actionability`` and ``analysis tape`` used to fetch ~100 Sina quotes themselves (release top N + holdings) and Sina answered 403 to
the server. The native recorder (``intraday_recorder``) already holds the whole main board every minute in the dataset ``quote_snapshots_60s``, so the
snapshot of ``quotes_l1`` is now cut from that recording: every board name, the watchlists included, and no HTTP request at all.
Mechanics: the parts of the NEWEST flush of today's READY ``quote_snapshots_60s`` are read (the recorder flushes every ~2 minutes), the latest row per
instrument not older than ``MAX_AGE`` is kept, and one ``ingest.observation`` (contract CanonicalQuote) is written per quote, because
``market.quote_projection`` references an observation. The observation's raw object is the recording part itself (``pulse_intraday_recorder`` /
``intraday_quotes``, record key ``<day>/<part>``, the lake object of the part); ``event_time`` is the exchange time of the quote, ``available_at`` the
row's ``poll_at`` (when pulse3 had the quote, not when this job ran) and the raw object's ``first_seen_at`` the part's earliest ``poll_at``, so the
projection's availability stays the poll time. Using only the newest flush keeps ``min(first_seen_at)`` of consecutive snapshots strictly increasing,
which is how ``market_today_service.quote_snapshot_ids`` orders them for the tape. The rest is ``account_service.publish_quote_rows``, the code the
fetching path also ends in, so the snapshot, its dataset and its projection rows are the same kind.
Invariants:
* no request, ever: when the recording has no row newer than ``MAX_AGE`` (180 s) the call raises :class:`NoFreshRecording` and publishes nothing;
* idempotent observations: the id is derived from (part, instrument, quote time, revision 1), so a second run over the same recording writes nothing new
  (a quote that did not change still gets an observation per part: it was polled again, ``available_at`` is that poll);
* units as the fetching path: prices yuan, ``bid_volume_shares`` / ``ask_volume_shares`` are the recorder's lots x 100, turnover yuan;
* a NULL price (suspended, no trade yet) is kept as a ``partial`` quote with no last price, never a zero.
Not covered: instruments without a registered code alias (they are skipped and counted), quotes older than the window (reported as missing, never
carried forward), a quote from a vendor day other than today (its own ``trade_date`` is kept).
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import polars as pl

from pulse.adapters.postgres import ingest_repo
from pulse.adapters.postgres.instrument_repo import instrument_ids_of_codes
from pulse.adapters.sources import native_intraday as N
from pulse.application import account_service as acct
from pulse.application.bf_live_service import CST, DATASET_QUOTES, _z
from pulse.application.data_service import DataService
from pulse.domain.market.builders import FQ_SUMMARY_KEY
from pulse.domain.market.units import sha256_json

MAX_AGE = timedelta(seconds=180)
NEWEST_FLUSHES = 1                      # only the parts of the newest flush are read (see the module docstring), not the whole day
_PART = re.compile(r"/native-[a-z]+-(\d{6})-(\d+)\.parquet$")
CONTRACT = "CanonicalQuote"
MIME = "application/vnd.apache.parquet"


class NoFreshRecording(LookupError):
    """The native recording has no quote newer than MAX_AGE (the recorder is not running, or its last flush is too old): nothing is published."""


def _price(v: Decimal | None) -> str | None:
    return None if v is None or v <= 0 else format(v.normalize(), "f")


def _payload(r: dict) -> dict:
    q = r["quote_at"]
    p: dict = {"trade_date": q.astimezone(CST).date().isoformat(), "event_time": _z(q)}
    for key, col in (("last_price_raw", "price"), ("open_price_raw", "open"), ("high_price_raw", "high"), ("low_price_raw", "low"),
                     ("prev_close_price_raw", "prev_close"), ("bid_price_raw", "bid1_px"), ("ask_price_raw", "ask1_px")):
        v = _price(r.get(col))
        if v is not None:
            p[key] = v
    for key, col in (("bid_volume_shares", "bid1_lots"), ("ask_volume_shares", "ask1_lots"), ("cumulative_volume_shares", "volume_lots")):
        if r.get(col) is not None:
            p[key] = int(round(r[col] * 100))
    if r.get("amount_yuan") is not None:
        p["cumulative_turnover_cny"] = format(r["amount_yuan"].normalize(), "f")
    return p


def _latest_manifest(svc: DataService, day: date) -> dict | None:
    return svc.conn.execute(
        "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' AND coverage_document->>'session_date'=%s"
        " ORDER BY manifest_version DESC LIMIT 1", (DATASET_QUOTES, day.isoformat())).fetchone()


def _newest_parts(parts: Mapping[str, dict]) -> list[str]:
    """Names of the native parts of the newest NEWEST_FLUSHES flushes (the part name carries the flush time HHMMSS and a sequence number)."""
    keyed = []
    for name in parts:
        m = _PART.search(name)
        if m:
            keyed.append(((m.group(1), int(m.group(2))), name))
    stamps = sorted({k[0] for k, _ in keyed}, reverse=True)[:NEWEST_FLUSHES]
    return [n for k, n in sorted(keyed, reverse=True) if k[0] in stamps]


def read_fresh_board(svc: DataService, now: datetime, *, max_age: timedelta = MAX_AGE) -> tuple[pl.DataFrame, dict, dict[str, dict]]:
    """(latest fresh row per code, the manifest row, {part name: {sha256, path, first_poll}}) or :class:`NoFreshRecording`."""
    day = now.astimezone(CST).date()
    man = _latest_manifest(svc, day)
    if man is None:
        raise NoFreshRecording(f"no recording of {day} is published (the native recorder has not flushed yet)")
    parts = man["coverage_document"].get("parts") or {}
    names = _newest_parts(parts)
    if not names:
        raise NoFreshRecording(f"the {day} recording has no native part")
    members = {m["sha256"]: m for m in svc.lake.read_manifest(man["content_hash"]).members}
    frames, info = [], {}
    for name in names:
        member = members[parts[name]["object_sha256"]]
        df = pl.read_parquet(io.BytesIO(svc.lake.read_object(member)))
        frames.append(df)
        info[name] = {"sha256": member["sha256"], "path": member["path"], "first_poll": df["poll_at"].min()}
    df = pl.concat(frames)
    newest = df["poll_at"].max()
    fresh = df.filter((pl.col("poll_at") >= now - max_age) & (pl.col("poll_at") <= now) & pl.col("quote_at").is_not_null())
    if fresh.is_empty():
        raise NoFreshRecording(f"no fresh recording: the newest native quote was polled at {newest:%H:%M:%S}Z, older than {int(max_age.total_seconds())} s")
    board = fresh.sort("code", "poll_at").group_by("code", maintain_order=True).last()
    return board, man, info


def _raw_object_ids(svc: DataService, day: date, info: dict[str, dict], now: datetime) -> dict[str, uuid.UUID]:
    out = {}
    for name, part in info.items():
        with svc.conn.transaction():
            rid, _ = ingest_repo.insert_raw_object(
                svc.conn, source_id=N.SOURCE_ID, endpoint_id=N.EP_QUOTES, source_schema_version=N.SCHEMA_VERSION, source_record_key=f"{day}/{name}",
                content_hash=part["sha256"], storage_uri=part["path"], mime_type=MIME, published_at=None, source_time_precision="unknown",
                first_seen_at=part["first_poll"], ingested_at=max(now, part["first_poll"]))
        out[name] = rid
    return out


def _observations(svc: DataService, board: pl.DataFrame, ids: Mapping[str, uuid.UUID], raws: Mapping[str, uuid.UUID], now: datetime) -> list[uuid.UUID]:
    """One observation per board quote, in the raw object of its part; the insert is idempotent (a rerun over the same recording adds nothing)."""
    new, result = [], []
    for r in board.iter_rows(named=True):
        iid = ids.get(r["code"])
        if iid is None:
            continue
        payload = _payload(r)
        nat = f"{iid}|quote|{payload['event_time']}"
        raw_id = raws[r["source_part"]]
        oid = ingest_repo.observation_id(raw_id, nat, 1)
        new.append({
            "observation_id": oid, "raw_object_id": raw_id, "contract_id": CONTRACT, "schema_version": "3.0.0", "mapping_version": N.MAPPING_VERSION,
            "entity_type": "instrument", "entity_key": str(iid), "natural_key": nat, "observation_revision": 1, "event_time": r["quote_at"],
            "available_at": r["poll_at"], "recorded_at": now, "quality_status": "valid" if "last_price_raw" in payload else "partial",
            "canonical_payload": payload, "content_hash": sha256_json(payload),
            "field_quality": {FQ_SUMMARY_KEY: {"status": "observed", "reason": None, "raw_object_id": str(raw_id), "source_id": N.SOURCE_ID, "endpoint_id": N.EP_QUOTES,
                                               "mapping_version": N.MAPPING_VERSION, "conversion": "native-recording:lots x 100 = shares",
                                               "value_origin": "primary_source"}}})
        result.append(oid)
    if new:
        with svc.conn.transaction():
            ingest_repo.insert_observations(svc.conn, new)
    return result


def publish_native_quote_snapshot(svc: DataService, *, wanted: Mapping[uuid.UUID, str] | None = None, only_wanted: bool = False,
                                  max_age: timedelta = MAX_AGE) -> acct.QuoteSnapshot:
    """Publish a ``quotes_l1`` snapshot from the fresh native recording (no request). ``wanted`` instruments without a fresh quote are reported as
    missing; ``only_wanted`` restricts the snapshot to them (default: the whole board plus ``wanted``)."""
    now = svc.clock()
    board, man, info = read_fresh_board(svc, now, max_age=max_age)
    code_to_id = instrument_ids_of_codes(svc.conn, board["code"].to_list())
    if only_wanted:
        keep = {c for iid, c in (wanted or {}).items()}
        board = board.filter(pl.col("code").is_in(list(keep)))
    skipped = board.height - sum(1 for c in board["code"].to_list() if c in code_to_id)
    day = now.astimezone(CST).date()
    raws = _raw_object_ids(svc, day, {n: info[n] for n in set(board["source_part"].to_list()) if n in info}, now)
    oids = _observations(svc, board, code_to_id, raws, now)
    if not oids:
        raise NoFreshRecording("the fresh recording holds no instrument with a registered code")
    rows = svc.conn.execute(
        "SELECT o.observation_id, o.entity_key, o.canonical_payload p, o.event_time, o.available_at, o.quality_status, o.field_quality, o.mapping_version,"
        " r.source_id, r.endpoint_id, r.source_schema_version, r.first_seen_at FROM ingest.observation o JOIN ingest.raw_object r USING (raw_object_id)"
        " WHERE o.observation_id = ANY(%s)", (oids,)).fetchall()
    requested = dict(wanted or {})
    for code, iid in code_to_id.items():
        requested.setdefault(iid, code)
    newest = board["poll_at"].max()
    extra = {"recording": {"dataset": DATASET_QUOTES, "snapshot_id": str(man["snapshot_id"]), "manifest_version": man["manifest_version"],
                           "parts": sorted(info), "newest_poll_at": _z(newest), "max_age_seconds": int(max_age.total_seconds()),
                           "instruments_without_alias_skipped": skipped, "http_requests": 0}}
    return acct.publish_quote_rows(svc, rows, requested, N.SOURCE_ID, coverage_extra=extra)
