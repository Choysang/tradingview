"""Field check of the auction quote fields (pre-registration section 4): what bid1 / ask1 / bid2 lots mean between 09:15 and 09:25, decided from the recorded days themselves.

Why: ``unmatched_buy`` and the reading of the matched quantity depend on it; the check is done once on the recorded sessions BEFORE the forward start (sessions before 2026-11-19) and its
conclusion is stored as an immutable dataset version (``batch6_fieldcheck``); the forward step reads that conclusion and never changes it.
Invariants: only imported READY recordings of sessions before ``FIRST_EVALUATED`` are used; the rule that turns the counters into a conclusion is ``domain.batch6.auction.conclude``;
a dependent feature whose criterion fails is "not computable" (no other definition is substituted).
Not covered: sessions without an imported recording (the old recorder keeps 11-02..11-11 raw files; they are not imported here).
"""
from __future__ import annotations

import io
import json
from datetime import date
from hashlib import sha256

import polars as pl
import psycopg

from pulse.application import bindings as B
from pulse.application.bf_batch6_forward import CONTRACT, FIRST_EVALUATED, SEC, _f
from pulse.application.bf_live_service import DATASET_QUOTES, LiveService
from pulse.domain.batch6 import auction as AU

DATASET = "batch6_fieldcheck"
FIELDS = ("code", "kind", "poll_at", "price", "bid1_px", "bid1_lots", "bid2_lots", "ask1_px", "ask1_lots", "ask2_lots", "volume_lots", "amount_yuan")


def session_counts(live: LiveService, man) -> AU.FieldCounts:
    c = AU.FieldCounts()
    lake = live.lake
    m = lake.read_manifest(man["content_hash"])
    frames = []
    for x in m.members:
        if not x["path"].endswith(".parquet"):
            continue
        f = pl.read_parquet(io.BytesIO(lake.read_object(x)), columns=list(FIELDS))
        f = f.filter(pl.col("code").str.slice(0, 3).is_in(["600", "601", "603", "605", "000", "001", "002", "003"]) & pl.col("kind").is_in(["auction", "auction_hot", "auction_final"]))
        frames.append(f)
    if not frames:
        return c
    f = pl.concat(frames, how="diagonal_relaxed").with_columns(pl.col("poll_at").dt.convert_time_zone("Asia/Shanghai").alias("t"))
    f = f.with_columns((pl.col("t").dt.hour() * 3600 + pl.col("t").dt.minute() * 60 + pl.col("t").dt.second()).alias("sec"))
    pre = f.filter((pl.col("kind") != "auction_final") & (pl.col("sec") < SEC(9, 25)))
    for r in pre.iter_rows(named=True):
        AU.count_auction_row(c, price=_f(r["price"]), bid1_px=_f(r["bid1_px"]), ask1_px=_f(r["ask1_px"]), bid1_lots=_f(r["bid1_lots"]), ask1_lots=_f(r["ask1_lots"]),
                             bid2_lots=_f(r["bid2_lots"]), ask2_lots=_f(r["ask2_lots"]))
    last = {r["code"]: r for r in pre.sort(["code", "poll_at"]).group_by("code", maintain_order=True).last().iter_rows(named=True)}
    for r in f.filter(pl.col("kind") == "auction_final").iter_rows(named=True):
        prev = last.get(r["code"])
        AU.count_final_row(c, price=_f(r["price"]), volume_lots=_f(r["volume_lots"]), amount=_f(r["amount_yuan"]), bid1_px=_f(r["bid1_px"]), ask1_px=_f(r["ask1_px"]),
                           last_auction_price=_f(prev["price"]) if prev else None, last_auction_matched=_f(prev["bid1_lots"]) if prev else None)
    return c


def recorded_sessions(conn: psycopg.Connection) -> list[date]:
    rows = conn.execute("SELECT DISTINCT coverage_document->>'session_date' d FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY'", (DATASET_QUOTES,)).fetchall()
    return sorted(date.fromisoformat(r["d"]) for r in rows if r["d"] and date.fromisoformat(r["d"]) < FIRST_EVALUATED)


def run(live: LiveService, *, as_of) -> dict:
    total = AU.FieldCounts()
    per_day = {}
    for d in recorded_sessions(live.conn):
        man = live.latest_ready(DATASET_QUOTES, d, as_of)
        if man is None:
            continue
        c = session_counts(live, man)
        per_day[d.isoformat()] = AU.conclude(c) | {"complete": bool(man["coverage_document"].get("source_complete"))}
        total.merge(c)
    return {"sessions": sorted(per_day), "per_day": per_day, "overall": AU.conclude(total)}


def stored(conn: psycopg.Connection, lake) -> dict | None:
    man = conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version DESC LIMIT 1", (DATASET,)).fetchone()
    if man is None:
        return None
    df = lake.read_snapshot_frames(man["content_hash"])
    return json.loads(df["body"][0])


def publish_once(svc, live: LiveService, *, as_of) -> tuple[dict, bool]:
    """Store the conclusion if none exists; returns (conclusion, newly_written). Once a forward session is decided, or the stored version was made by the current rule, it is never replaced.

    A stored conclusion made by an OLDER rule version (a bug fix before the forward start) is replaced by a new dataset version; the old version stays in the lake."""
    from pulse.application import bf_batch6_forward as FW
    have = stored(svc.conn, svc.lake)
    if have is not None:
        current = have.get("overall", {}).get("rule_version") == AU.FIELD_RULE_VERSION
        if current or FW.decided_sessions(svc.conn):
            return have, False
    out = run(live, as_of=as_of)
    if not out["sessions"] or out["overall"]["auction_rows"] == 0:
        return out, False                                  # nothing recorded before the forward start yet: the conclusion is stored only when evidence exists
    body = json.dumps(out, sort_keys=True, ensure_ascii=False, default=str)
    frame = pl.DataFrame({"key": ["fieldcheck"], "body": [body]})
    cov = {"dataset": DATASET, "sessions": out["sessions"], "frame_sha256": sha256(frame.write_csv().encode("utf-8")).hexdigest(), "decided_at": as_of.isoformat()}
    B.publish_derived(svc, DATASET, frame, cov, contract_id=CONTRACT, watermark=as_of)
    return out, True
