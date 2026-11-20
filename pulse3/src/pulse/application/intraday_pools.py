"""Optional limit-pool step of the native intraday recorder, and the read-only comparison against the seal facts (NOT scheduled).

Why: the old recorder fetched the vendor's own limit-up, broken-board and limit-down pools once after the close (15:25-15:50). The seal facts
pulse3 derives from 60 s snapshots are ESTIMATES; the vendor pools are an independent count to reconcile them against (2026-11-13: 36 sealed
at the close against 37 in the pool, 117 breaks against 124). The step is OFF unless ``intraday-record --limit-pools`` is given.
Output: the dataset ``limit_pools_eastmoney`` (one row per name and pool, ``fetched_at`` = the response time of that request, one cumulative
version per session), produced under the native source ``pulse_intraday_recorder`` with the raw bodies stored under ``eastmoney_limit_pools``;
it is never mixed into ``quote_snapshots_60s``.
Invariants:
* its own ``RateLimitedClient`` (1 request/s, the cross-process pacer applies); the first block (403/429/456/501, or a body that is not the
  expected JSON) stops the host for the session, never retried; the quote recording is not affected;
* a pool whose ``qdate`` is not the session (the vendor repeats the last session on a holiday) is noted and not stored;
* one retry after a transport failure (as the old recorder did on a timeout), never after a block;
* the comparison reads datasets only: it writes nothing and fails soft (a missing dataset is reported, not guessed).
Not covered: scheduling, pools of other days, per-name reconciliation of the limit-down pool (the seal facts have no limit-down side).
"""
from __future__ import annotations

import json
import time as _time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any, Callable

import polars as pl

from pulse.adapters.lake import ObjectRef
from pulse.adapters.postgres import ingest_repo
from pulse.adapters.sources import eastmoney_pools as EM
from pulse.adapters.sources import native_intraday as N
from pulse.adapters.sources import registry
from pulse.adapters.sources.http import Capture, RateLimitedClient, SourceBlocked, TransportFailure
from pulse.application.bf_live_service import CST, DATASET_POOLS, DATASET_SEAL, LiveService, _z
from pulse.application.data_service import Summary
from pulse.domain.evidence.announcements import NotData, SchemaChanged

UTC = timezone.utc
RETRY_AFTER_TRANSPORT_FAILURE = 2.0
POOL_SCHEMA: dict[str, Any] = {
    "session_date": pl.Date, "pool": pl.String, "fetched_at": pl.Datetime("ms", "UTC"), "code": pl.String, "name": pl.String, "market": pl.Int64,
    "price": pl.Decimal(18, 3), "limit_up_price": pl.Decimal(18, 3), "zdp": pl.Float64, "amount": pl.Int64, "ltsz": pl.Float64, "tshare": pl.Float64,
    "hs": pl.Float64, "lbc": pl.Int64, "fbt": pl.Int64, "lbt": pl.Int64, "fund": pl.Int64, "zbc": pl.Int64, "zf": pl.Float64, "zs": pl.Float64,
    "pe": pl.Float64, "fba": pl.Int64, "days": pl.Int64, "oc": pl.Int64, "hybk": pl.String, "zttj": pl.String}


def _f(v: Any) -> float | None:
    try:
        return None if v is None or v == "-" else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


def _milli(v: Any) -> Decimal | None:
    i = _i(v)
    return None if i is None else Decimal(i).scaleb(-3)


def pool_frame(rows: list[dict], *, key: str, day: date, fetched_at: datetime) -> pl.DataFrame:
    """Vendor pool rows -> ``POOL_SCHEMA`` (``p`` / ``ztp`` thousandths of a yuan become prices; other vendor names are kept)."""
    cols: dict[str, list] = {c: [] for c in POOL_SCHEMA}
    for r in rows:
        cols["session_date"].append(day)
        cols["pool"].append(key)
        cols["fetched_at"].append(fetched_at)
        cols["code"].append(str(r.get("c") or ""))
        cols["name"].append(r.get("n"))
        cols["market"].append(_i(r.get("m")))
        cols["price"].append(_milli(r.get("p")))
        cols["limit_up_price"].append(_milli(r.get("ztp")))
        for c in ("zdp", "ltsz", "tshare", "hs", "zf", "zs", "pe"):
            cols[c].append(_f(r.get(c)))
        for c in ("amount", "lbc", "fbt", "lbt", "fund", "zbc", "fba", "days", "oc"):
            cols[c].append(_i(r.get(c)))
        cols["hybk"].append(r.get("hybk"))
        cols["zttj"].append(None if r.get("zttj") is None else json.dumps(r["zttj"], ensure_ascii=False, sort_keys=True))
    return pl.DataFrame(cols, schema=POOL_SCHEMA)


class LimitPoolRecorder:
    """The once-a-session pool fetch. ``down`` holds the reason once the host was stopped for the rest of the session."""

    def __init__(self, svc: LiveService, client: RateLimitedClient, *, sleep: Callable[[float], None] | None = None):
        self.svc, self.client = svc, client
        self.adapter = EM.EastmoneyPoolAdapter(client)
        self.spec = registry.spec(EM.SOURCE_ID, EM.ENDPOINT_ID)
        self.out_spec = registry.spec(N.SOURCE_ID, N.EP_POOLS)
        self.sleep = sleep or _time.sleep
        self.down: str | None = None

    def _get(self, key: str, day: date, s: Summary) -> Capture | None:
        partition = f"pools:{day}"
        for attempt in (1, 2):
            try:
                return self.svc.data._fetch(self.spec, partition, lambda: self.adapter.pool(key, day))
            except SourceBlocked as exc:
                self.down = f"blocked ({exc})"
                s.notes.append(f"limit pools: {EM.HOST} stopped for the session: {self.down}")
                return None
            except TransportFailure as exc:
                s.add("pool_transport_errors")
                if attempt == 2:
                    s.notes.append(f"limit pool {key}: transport failure twice, not stored: {str(exc)[:80]}")
                    return None
                self.sleep(RETRY_AFTER_TRANSPORT_FAILURE)
        return None

    def fetch_and_publish(self, day: date, s: Summary) -> None:
        got: list[tuple[str, Capture, int, pl.DataFrame]] = []
        for key in EM.POOLS:
            if self.down:
                break
            cap = self._get(key, day, s)
            if cap is None:
                continue
            try:
                qdate, total, rows = EM.parse_pool(cap.body)
            except NotData as exc:                          # an HTML page instead of JSON is a refusal
                self.svc.data.record_attempt(self.spec, f"pools:{day}", "blocked", error_code=str(exc)[:80])
                self.down = f"not JSON ({exc})"
                s.notes.append(f"limit pools: {EM.HOST} stopped for the session: {self.down}")
                break
            except SchemaChanged as exc:
                s.add("pool_parse_errors")
                s.notes.append(f"limit pool {key}: {exc}")
                continue
            if qdate != f"{day:%Y%m%d}":
                s.notes.append(f"limit pool {key} is for {qdate}, not {day}: not stored")
                continue
            if total > len(rows):
                s.notes.append(f"limit pool {key} truncated: {len(rows)} of {total}")
            got.append((key, cap, total, pool_frame(rows, key=key, day=day, fetched_at=cap.fetched_at)))
            s.add(f"pool_{key}_rows", len(rows))
        if got:
            self._publish(day, got, s)

    def _publish(self, day: date, got: list[tuple[str, Capture, int, pl.DataFrame]], s: Summary) -> None:
        svc, now = self.svc, self.svc.clock()
        stamp = f"{now.astimezone(CST):%H%M%S}"
        batch_id = ingest_repo.create_batch(svc.conn, self.out_spec, f"pools:{day}:{stamp}", now)
        owner = f"job:{batch_id}"
        try:
            for _, cap, _, _ in got:
                svc.data.store_capture(batch_id, cap, owner)
            with svc.conn.transaction():
                ingest_repo.commit_raw(svc.conn, batch_id, svc.clock())
            prev = svc.latest_ready(DATASET_POOLS, day)
            parts: dict[str, dict] = dict((prev["coverage_document"].get("parts") or {}) if prev else {})
            old: dict[str, ObjectRef] = {}
            if prev:
                old = {m["sha256"]: ObjectRef(m["sha256"], m["path"], m["size"], m.get("kind", "data"))
                       for m in svc.lake.read_manifest(prev["content_hash"]).members}
            members = {k: old[v["object_sha256"]] for k, v in parts.items()}
            for key, cap, total, df in got:
                name = f"pools/{key}-{cap.fetched_at.astimezone(CST):%H%M%S}.parquet"
                ref = svc.lake.put_parquet(df, owner=owner)
                members[name] = ref
                parts[name] = {"stream": "pool", "pool": key, "vendor": "eastmoney", "source_sha256": ref.sha256, "object_sha256": ref.sha256,
                               "rows": df.height, "tc": total, "fetched_at": _z(cap.fetched_at), "source_size": ref.size}
            rows = sum(v["rows"] for v in parts.values())
            coverage = {"dataset": DATASET_POOLS, "session_date": day.isoformat(), "parts": dict(sorted(parts.items())), "imported_at": _z(now),
                        "recorder": N.SOURCE_ID, "vendor": EM.SOURCE_ID, "comparison_only": True,
                        "point_in_time": "fetched_at of every row is the response time of its request; the dataset is available from the publish time",
                        "units": "price / limit_up_price yuan (vendor thousandths / 1000); fbt / lbt HHMMSS Asia/Shanghai; amount, fund, ltsz, tshare yuan; "
                                 "zdp, hs, zf percent; other vendor field names kept",
                        "not_stored": "pools whose qdate is not the session"}
            manifest = svc.lake.commit_manifest(DATASET_POOLS, [members[k] for k in sorted(members)], owner=owner,
                                                meta={"row_count": rows, "kind": "table", "session_date": day.isoformat(), "parts": sorted(members)})
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "NORMALIZED")
            svc.data._stage_and_publish(DATASET_POOLS, uuid.uuid4(), batch_id, manifest, "pulse.bf_live.limit_pool", coverage, owner, rows, now, (), ())
        except Exception:
            with svc.conn.transaction():
                ingest_repo.set_batch_state(svc.conn, batch_id, "FAILED", failure_code="pools_error")
            svc.lake.release(owner)
            raise
        if self.down is None:                              # a host stopped in this step keeps the blocked health state it was given
            svc.data.record_attempt(self.spec, str(day), "success_data", new_records=sum(df.height for *_, df in got), content_expected_now=False,
                                    canonical_ready=True)
        s.add("pool_parts_published", len(got))


# ---- read-only comparison against the seal facts ----------------------------------------------------------------------------------------

def _latest(pools: pl.DataFrame, key: str) -> pl.DataFrame:
    sub = pools.filter(pl.col("pool") == key)
    return sub.filter(pl.col("fetched_at") == sub["fetched_at"].max()) if sub.height else sub


def _cst_seconds(ts: datetime) -> int:
    t = ts.astimezone(CST)
    return t.hour * 3600 + t.minute * 60 + t.second


def _hhmmss_seconds(v: int) -> int:
    return (v // 10000) * 3600 + (v // 100 % 100) * 60 + v % 100


def compare_frames(pools: pl.DataFrame, facts: pl.DataFrame) -> dict[str, Any]:
    """Vendor pools (latest fetch of each pool) against the estimated seal facts of the same session. Nothing here is a pass / fail: it
    reports counts, the sets that differ and the first-seal-time gap, so a reader can judge the estimate."""
    zt, zb, dt = (_latest(pools, k) for k in ("zt", "zb", "dt"))
    sealed = facts.filter(pl.col("sealed_at_close").fill_null(False))
    zt_codes, sealed_codes = set(zt["code"].to_list()), set(sealed["code"].to_list())
    both = zt_codes & sealed_codes
    out: dict[str, Any] = {
        "fetched_at": {k: (None if f.is_empty() else f["fetched_at"].max()) for k, f in (("zt", zt), ("zb", zb), ("dt", dt))},
        "sealed_at_close": {"vendor_pool": len(zt_codes), "seal_facts": len(sealed_codes), "both": len(both),
                            "only_vendor": sorted(zt_codes - sealed_codes), "only_seal_facts": sorted(sealed_codes - zt_codes)}}
    joined = zt.filter(pl.col("code").is_in(list(both))).select("code", "fbt").join(
        sealed.select("code", "first_seal_at"), on="code") if both else None
    gaps = [] if joined is None else [_cst_seconds(r["first_seal_at"]) - _hhmmss_seconds(r["fbt"]) for r in joined.iter_rows(named=True)
                                      if r["first_seal_at"] is not None and r["fbt"] is not None]
    out["first_seal_gap_seconds"] = ({"n": len(gaps), "median": median(gaps), "max": max(gaps), "min": min(gaps),
                                      "within_60s": sum(1 for g in gaps if 0 <= g <= 60)} if gaps else {"n": 0})
    ours_broken = facts.filter(pl.col("broken_count") > 0)
    zt_zbc, zb_zbc = int(zt["zbc"].fill_null(0).sum()), int(zb["zbc"].fill_null(0).sum()) if zb.height else 0
    same = facts.filter(pl.col("code").is_in(list(both)))
    out["breaks"] = {"seal_facts_total": int(facts["broken_count"].fill_null(0).sum()), "seal_facts_names": ours_broken.height,
                     "vendor_zt_pool_zbc": zt_zbc, "vendor_zb_pool_zbc": zb_zbc, "vendor_total_zbc": zt_zbc + zb_zbc,
                     "vendor_zb_pool_names": zb.height, "both_sealed_names": {"seal_facts": int(same["broken_count"].fill_null(0).sum()), "vendor_zbc": int(
                         zt.filter(pl.col("code").is_in(list(both)))["zbc"].fill_null(0).sum())}}
    never_sealed = set(facts.filter(pl.col("touched_limit").fill_null(False) & ~pl.col("sealed_at_close").fill_null(False))["code"].to_list())
    zb_codes = set(zb["code"].to_list())
    out["broken_at_close"] = {"vendor_zb_pool": len(zb_codes), "seal_facts_touched_not_sealed": len(never_sealed), "both": len(zb_codes & never_sealed),
                              "only_vendor": sorted(zb_codes - never_sealed), "only_seal_facts": sorted(never_sealed - zb_codes)}
    out["limit_down_pool_names"] = dt.height
    return out


def compare_pools(svc: LiveService, day: date) -> dict[str, Any]:
    """Read-only: the latest READY pool dataset and seal-facts dataset of ``day``, compared by :func:`compare_frames`."""
    pm, sm = svc.latest_ready(DATASET_POOLS, day), svc.latest_ready(DATASET_SEAL, day)
    out: dict[str, Any] = {"session_date": day.isoformat(), "notes": []}
    if pm is None:
        out["notes"].append(f"no READY {DATASET_POOLS} for {day}: the recorder ran without --limit-pools, or the pools were not fetched")
    if sm is None:
        out["notes"].append(f"no READY {DATASET_SEAL} for {day}: run seal-facts first")
    if pm is None or sm is None:
        return out
    out.update(compare_frames(svc.lake.read_snapshot_frames(pm["content_hash"]), svc.lake.read_snapshot_frames(sm["content_hash"])))
    out["pools_manifest_version"], out["seal_facts_manifest_version"] = pm["manifest_version"], sm["manifest_version"]
    out["seal_facts_basis"] = sm["coverage_document"].get("estimation")
    return out


def render(report: dict[str, Any]) -> str:
    lines = [f"== pool-compare {report['session_date']} =="]
    lines += [f"  note: {n}" for n in report.get("notes", [])]
    if "sealed_at_close" in report:
        a, g, b, bc = report["sealed_at_close"], report["first_seal_gap_seconds"], report["breaks"], report["broken_at_close"]
        lines += [f"  sealed at close: vendor pool {a['vendor_pool']} | seal facts {a['seal_facts']} | both {a['both']}",
                  f"    only vendor ({len(a['only_vendor'])}): {', '.join(a['only_vendor'][:25])}",
                  f"    only seal facts ({len(a['only_seal_facts'])}): {', '.join(a['only_seal_facts'][:25])}",
                  f"  first seal, seal facts minus vendor fbt (s): {g}",
                  f"  breaks: seal facts {b['seal_facts_total']} ({b['seal_facts_names']} names) | vendor zbc zt {b['vendor_zt_pool_zbc']} + zb "
                  f"{b['vendor_zb_pool_zbc']} = {b['vendor_total_zbc']} | among names sealed in both: {b['both_sealed_names']}",
                  f"  broken at close: vendor zb pool {bc['vendor_zb_pool']} | seal facts touched-not-sealed {bc['seal_facts_touched_not_sealed']} | both {bc['both']}",
                  f"  limit-down pool names: {report['limit_down_pool_names']}"]
    return "\n".join(lines)
