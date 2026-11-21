"""Native instrument register, short-name / risk-warning state versions and float caps, from pulse3's own data (Step 2, lane D).

Why: with ``PULSE_REFDATA_SOURCE=native`` the release must not need the old warehouse for (a) the instrument register (``register_universe``
reads the legacy ``securities`` / ``listings`` files), (b) the circulating-cap snapshot (``publish_cap_snapshot`` reads ``securities``) and
(c) the stock code of an instrument (the ``legacy_warehouse`` alias). Everything here comes from datasets pulse3 publishes itself:
``instrument_listing_native`` (code, listing date, short name from the exchange / Eastmoney list) and the close rows of the latest
``quote_snapshots_60s`` session (short name as quoted, ``float_cap`` / ``total_cap`` in 100 million CNY, the legacy row convention).

Invariants: instrument ids are the deterministic ones (``identity.instrument_uuid``), so an instrument registered by the legacy import and
one registered here are the same object; a NEW instrument gets the aliases ``native`` (its code), ``sina``, ``tencent`` and
``cninfo_announcements``; an instrument that already has a code alias gets no second one (joins on the code aliases stay one row per stock);
state versions are point in time: a changed short name or risk-warning flag appends a version effective from its FIRST OBSERVATION (the
session date of the close rows, else the day the listing snapshot was fetched), never back-filled; a code without any name is not registered
(no placeholder names); a cap is a fact of its session (``as_of`` = session date, ``available_at`` = the poll that carried it), missing is
NULL and never 0; the native cap dataset starts from the legacy cap dataset already in the lake (history continuity for rebuilds) and appends
(instrument, as_of) versions; nothing here reads the old warehouse files or writes a legacy dataset.
Not covered: delisting, code reuse, BSE; caps for a code whose close rows came only from Sina (the Sina quote carries no cap: the Tencent rows
of the recorder provide it, and a stock without one has no cap: unknown, never 0).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import instrument_repo, market_repo
from pulse.application import bindings as B
from pulse.application import refdata_core as C
from pulse.application.data_service import DataService, Summary
from pulse.domain.instruments import identity as ident

DATASET_QUOTES = "quote_snapshots_60s"
NEW_ALIAS_SOURCES = ("native", "sina", "tencent", "cninfo_announcements")
YI = Decimal("100000000")          # the quote rows carry caps in 100 million CNY (document unit of the legacy snapshot as well)
_CAP_SCHEMA = {"instrument_id": pl.String, "as_of": pl.String, "float_cap_cny": pl.Decimal(38, 2), "total_cap_cny": pl.Decimal(38, 2),
               "available_at": pl.Datetime("us", "UTC")}


def instrument_id_of(code: str) -> str:
    return str(ident.instrument_uuid(ident.venue_for_code(code), code))


# ---- sources ------------------------------------------------------------------------------------------------------------------------------
def latest_close_manifest(conn, as_of: datetime | None = None) -> dict | None:
    """Newest session of ``quote_snapshots_60s`` (its latest READY manifest) visible at ``as_of``."""
    sql = "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'"
    args: list[Any] = [DATASET_QUOTES]
    if as_of is not None:
        sql += " AND ready_at <= %s"
        args.append(as_of)
    return conn.execute(sql + " ORDER BY coverage_document->>'session_date' DESC, manifest_version DESC LIMIT 1", args).fetchone()


def close_rows(lake: LakeStore, man: dict) -> pl.DataFrame:
    """One row per code of the session: the LAST quoted short name, the last non-null caps (100 million CNY) and their poll times.

    Only the needed columns are scanned from the lake parts (a whole session is about 0.5 GB)."""
    members = lake.read_manifest(man["content_hash"]).members
    paths = [lake.root / m["path"] for m in members if m["path"].endswith(".parquet")]
    lf = pl.scan_parquet(paths).select("code", "name", "poll_at", "float_cap", "total_cap", "session_date")
    return (lf.filter(pl.col("code").str.contains(r"^\d{6}$")).sort("poll_at").group_by("code")
            .agg(pl.col("name").drop_nulls().last().alias("name"), pl.col("session_date").last().alias("session_date"),
                 pl.col("poll_at").max().alias("poll_at"),
                 pl.col("float_cap").filter(pl.col("float_cap") > 0).last().alias("float_cap"),
                 pl.col("total_cap").filter(pl.col("total_cap") > 0).last().alias("total_cap"),
                 pl.col("poll_at").filter(pl.col("float_cap") > 0).max().alias("cap_at"))
            .collect().sort("code"))


def listing_rows(conn, lake: LakeStore, as_of: datetime | None = None) -> tuple[dict | None, pl.DataFrame | None]:
    sql = "SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'"
    args: list[Any] = [C.LISTING_DATASET]
    if as_of is not None:
        sql += " AND ready_at <= %s"
        args.append(as_of)
    man = conn.execute(sql + " ORDER BY manifest_version DESC LIMIT 1", args).fetchone()
    return man, (None if man is None else lake.read_snapshot_frames(man["content_hash"]))


# ---- register + state versions ----------------------------------------------------------------------------------------------------------------
def register_native(svc: DataService, lake: LakeStore, *, as_of: datetime | None = None) -> Summary:
    """Register new listings and append short-name / risk-warning versions from the native listing and close snapshots (idempotent)."""
    s = Summary("native register")
    conn, now = svc.conn, svc.clock()
    cands: dict[str, dict] = {}
    lman, listing = listing_rows(conn, lake, as_of)
    if listing is not None:
        s.counts["listing_rows"] = listing.height
        seen_on = lman["ready_at"].astimezone(C.CST).date()
        names = listing["name"].to_list() if "name" in listing.columns else [None] * listing.height
        for code, listed_on, name in zip(listing["code"].to_list(), listing["listed_on"].to_list(), names):
            cands[code] = {"name": name, "listed_on": listed_on, "observed_on": seen_on, "evidence": f"native_listing:{lman['content_hash'][:16]}"}
    qman = latest_close_manifest(conn, as_of)
    if qman is not None:
        close = close_rows(lake, qman)
        s.counts["close_rows"] = close.height
        s.notes.append(f"close rows of session {qman['coverage_document'].get('session_date')} (manifest {qman['content_hash'][:16]})")
        for code, name, day in zip(close["code"].to_list(), close["name"].to_list(), close["session_date"].to_list()):
            if name:
                c = cands.setdefault(code, {"listed_on": None})
                c.update(name=name, observed_on=day, evidence=f"native_close:{day}:{qman['content_hash'][:16]}")
    records: dict[str, tuple[ident.InstrumentRecord, date, str]] = {}
    for code, c in sorted(cands.items()):
        if not c.get("name"):
            s.add("skipped_no_name")
            continue
        try:
            records[code] = (ident.InstrumentRecord.build(code, c["name"], c.get("listed_on")), c["observed_on"], c["evidence"])
        except ValueError:
            s.add("skipped_unsupported_code")
    ids = [str(r[0].instrument_id) for r in records.values()]
    have = {str(r["instrument_id"]) for r in conn.execute("SELECT instrument_id FROM core.instrument WHERE instrument_id = ANY(%s::uuid[])", (ids,))}
    prior = {str(r["instrument_id"]): (r["is_risk_warning"], r["display_name"]) for r in conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, is_risk_warning, display_name FROM core.instrument_state"
        " WHERE instrument_id = ANY(%s::uuid[]) ORDER BY instrument_id, state_version DESC", (ids,))}
    new = [r for r, _, _ in records.values() if str(r.instrument_id) not in have]
    with conn.transaction():
        s.counts["instruments_new"] = instrument_repo.upsert_instruments(conn, new, now, NEW_ALIAS_SOURCES, "native_register") if new else 0
        for rec, observed_on, evidence in records.values():
            ver = instrument_repo.append_state_if_changed(conn, rec, observed_on, now, evidence)
            if ver is None:
                continue
            s.add("state_versions_appended")
            before = prior.get(str(rec.instrument_id))
            if before is not None and before[0] != rec.is_risk_warning:
                s.add("risk_warning_changes")
            elif before is not None:
                s.add("name_changes")
    s.counts["instruments_seen"] = len(records)
    return s


# ---- caps ---------------------------------------------------------------------------------------------------------------------------------------
def _frame_of(conn, lake: LakeStore, dataset: str) -> tuple[dict | None, pl.DataFrame | None]:
    man = B._ready_manifest(conn, dataset, None)
    return man, (None if man is None else lake.read_snapshot_frames(man["content_hash"]))


def publish_native_caps(svc: DataService, lake: LakeStore, *, as_of: datetime | None = None) -> dict | None:
    """Publish ``instrument_circulating_cap_native``: the cap versions already known (the native dataset, or the legacy one as the seed of the
    first run) plus the caps of the newest close session, as (instrument, as_of) rows in CNY (cap x 100 million). Returns the manifest or None."""
    conn = svc.conn
    base_man, base = _frame_of(conn, lake, B.NATIVE_CAP_DATASET)
    seeded = False
    if base is None:
        base_man, base = _frame_of(conn, lake, B.LEGACY_CAP_DATASET)
        seeded = base is not None
    qman = latest_close_manifest(conn, as_of)
    rows: list[dict] = []
    if qman is not None:
        close = close_rows(lake, qman).filter(pl.col("float_cap").is_not_null())
        registered = {str(r["instrument_id"]) for r in conn.execute(
            "SELECT instrument_id FROM core.instrument WHERE instrument_type='equity' AND instrument_id = ANY(%s::uuid[])",
            ([instrument_id_of(c) for c in close["code"].to_list()],))}
        for code, day, fcap, tcap, at in zip(close["code"].to_list(), close["session_date"].to_list(), close["float_cap"].to_list(),
                                             close["total_cap"].to_list(), close["cap_at"].to_list()):
            iid = instrument_id_of(code)
            if iid in registered:
                rows.append({"instrument_id": iid, "as_of": day.isoformat(), "float_cap_cny": Decimal(str(fcap)) * YI,
                             "total_cap_cny": None if tcap is None else Decimal(str(tcap)) * YI, "available_at": at})
    fresh = pl.DataFrame(rows, schema=_CAP_SCHEMA)
    if base is not None and not fresh.is_empty():
        fresh = fresh.join(base.select("instrument_id", "as_of"), on=["instrument_id", "as_of"], how="anti")
    if fresh.is_empty():
        if base_man is not None and not seeded:
            return base_man
        if base is None:
            return None
    frame = (pl.concat([base.select(list(_CAP_SCHEMA)), fresh]) if base is not None else fresh).sort(["instrument_id", "as_of"])
    digest = hashlib.sha256(frame.write_csv().encode()).hexdigest()
    hit = B.find_derived(conn, B.NATIVE_CAP_DATASET, "frame_sha256", digest)
    if hit is not None:
        return hit
    cov = {"dataset": B.NATIVE_CAP_DATASET, "frame_sha256": digest, "rows": frame.height, "unit": B.CAP_UNIT_NOTE.replace("legacy securities.float_cap", "quote float_cap / total_cap"),
           "source": "close rows of quote_snapshots_60s (native recorder or the old recorder's import), newest session"
                     + (" + the legacy cap dataset as the history seed" if seeded else ""),
           "newest_session": None if qman is None else qman["coverage_document"].get("session_date"), "as_of": max(frame["as_of"].to_list()),
           "new_rows": fresh.height,
           "missing": "a code without a cap in the close rows has no cap fact (unknown, never 0); the Sina quote carries no cap, the Tencent rows do"}
    return B.publish_derived(svc, B.NATIVE_CAP_DATASET, frame, cov, contract_id="pulse.instrument_cap_fact",
                             watermark=max(frame["available_at"].to_list()))


def refresh(svc: DataService, lake: LakeStore, *, as_of: datetime | None = None) -> tuple[Summary, dict | None]:
    """Register + state versions, then the cap snapshot (what a release build runs first under the native switch)."""
    s = register_native(svc, lake, as_of=as_of)
    man = publish_native_caps(svc, lake, as_of=as_of)
    s.counts["cap_rows_total"] = 0 if man is None else int(man["row_count"])
    s.notes.append("cap snapshot: " + ("none (no close rows and no seed)" if man is None else f"{man['dataset_id']} v{man['manifest_version']}"))
    return s, man
