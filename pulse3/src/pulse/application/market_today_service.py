"""Market state service (extension group A): market today, sentiment, ranking history / outcomes, sector details, tape.

Why: PEX01 / PEX02 / PEX03 / PEX09 / PEX16 and the old-site overview (docs/LEGACY_FEATURE_INVENTORY.md §1/§3/§5)
need market-wide facts that were never part of a release. This module loads point-in-time raw daily bars, runs
the pure ``domain.market_state`` definitions, persists the results as versioned artifacts and serves them.

Persistence (no new fact table; migration 0020 only registers contract ids and three read bindings):
  * D01 ``pulse.market_today``      dataset ``market_today``: one row per panel; each panel is also a registered
    ``market.feature_result`` (FeatureKey with the scope uuid as its single "instrument", the raw-bar snapshot as input).
  * D11 ``pulse.market_sentiment``  dataset ``market_sentiment``: three features per scope (market + every sector).
  * D02 ``pulse.ranking_outcome``   dataset ``ranking_outcome``: resolved forward outcomes, immutable, pending ones are
    never written (a pending ranking simply has no row).
  * the event tape is the ops.outbox log, aggregate ``market_tape``.
The release wires this in as ONE optional component ``market_state`` (see ``release_service``); a failure leaves the
component MISSING and never touches the required components.
Invariants: every read is pinned to a release; an artifact is addressed by ``feature:<key hash>`` (content hash checked on
read); counts and rates come from the domain, the client never recomputes; unknown stays null; the response of an older
release is never relabelled as current (``freshness_of``).
Not covered: intraday seal times (capability gap), market-wide tape (only the quoted set), SSE transport (api layer).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import decision_repo, market_repo
from pulse.adapters.postgres.outbox import PgOutbox
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.application.ports.delivery import OutboxEvent
from pulse.domain.features.key import FeatureKey, key_hash, parameter_digest
from pulse.domain.market_rules import MARKET_RULES_VERSION, Board, Exchange, PriceLimitRegistry
from pulse.domain.market_rules.costs import CostEngine, user_account_2026
from pulse.domain.market_state import daily, mood, outcomes, panels, regime, tape
from pulse.domain.market_state.daily import Bar, Stock
from pulse.domain.strategy.canonical import canonical_hash

COMPONENT_KEY = "market_state"
TODAY_DATASET, SENTIMENT_DATASET, OUTCOME_DATASET = "market_today", "market_sentiment", "ranking_outcome"
TODAY_CONTRACT, SENTIMENT_CONTRACT, OUTCOME_CONTRACT = "pulse.market_today", "pulse.market_sentiment", "pulse.ranking_outcome"
UNIVERSE_POLICY = "main-board-all-instruments-v1"
MARKET_SCOPE = ("market", "main-board")
STATE_PANELS = ("thermometer", "vitals", "ladder", "breadth", "themes", "growth_strip", "regime")
SENTIMENT_FEATURES = ("mood", "limit_board", "promotion")
NS = uuid.UUID("5e1b1d3a-8c0f-4a43-9a6e-2f7d41c0b9a1")
#: the one evaluation method implemented for ranking outcomes (id derived from the method version)
OUTCOME_CONTEXT_ID = uuid.uuid5(NS, "ranking-outcome-context|" + outcomes.OUTCOME_VERSION)
HORIZON_SESSIONS = {"5td": 5, "20td": 20, "60td": 60}
OUTCOME_NOTIONAL = Decimal("50000")
STALE_AFTER = timedelta(hours=36)
MAX_SESSIONS = 300
TAPE_AGGREGATE = "market_tape"
_VENUE = {"XSHG": Exchange.SSE, "XSHE": Exchange.SZSE, "XBSE": Exchange.BSE}
_BOARD = {"main": Board.MAIN, "chinext": Board.CHINEXT, "star": Board.STAR, "bse": Board.BSE}
IMPLEMENTATION = canonical_hash([daily.MARKET_STATE_DEFINITIONS, mood.MOOD_VERSION, regime.REGIME_VERSION, panels.THEME_STRENGTH_VERSION,
                                 outcomes.OUTCOME_VERSION, tape.TAPE_VERSION])


def aid(kind: str, *parts: object) -> uuid.UUID:
    return uuid.uuid5(NS, kind + "|" + "|".join(str(p) for p in parts))


def jdump(value: Any) -> str:
    return B.canonical_json(value)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------------
# loading (point in time)
# --------------------------------------------------------------------------------------------------------
def load_stocks(conn: psycopg.Connection, as_of: datetime, availability: str) -> dict[str, Stock]:
    metas, _ = B.load_instrument_meta(conn, as_of, availability)
    out: dict[str, Stock] = {}
    for iid, m in metas.items():
        if m.board_code != "main" or m.venue_code not in _VENUE:
            continue
        out[str(iid)] = Stock(str(iid), m.code, m.display_name, _VENUE[m.venue_code], _BOARD[m.board_code], m.is_risk_warning)
    return out


def load_bars(conn: psycopg.Connection, ids: Sequence[str], as_of: datetime, availability: str, *, until: date | None = None,
              since: date | None = None) -> dict[str, list[Bar]]:
    """Latest visible revision per (instrument, day) of the raw daily bars, ascending; dates outside [since, until] dropped."""
    since = since or date(2000, 1, 1)
    until = until or as_of.astimezone(B.CST).date()
    cols = "instrument_id, trade_date, open_price_raw o, high_price_raw h, low_price_raw l, close_price_raw c, volume_shares v"
    uid = [uuid.UUID(i) for i in ids]
    if B.reconstructed(availability):
        rows = conn.execute(
            "SELECT DISTINCT ON (instrument_id, trade_date) " + cols + " FROM (SELECT *, " + B._FIRST_VERSION + " FROM market.bar_projection"
            " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND " + B._USABLE_SQL + ") x"
            " WHERE is_first OR available_at <= %s ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id",
            (uid, since, until, as_of, as_of), prepare=False).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT ON (instrument_id, trade_date) " + cols + " FROM market.bar_projection"
            " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s AND is_final"
            " ORDER BY instrument_id, trade_date, available_at DESC, snapshot_id", (uid, since, until, as_of), prepare=False).fetchall()
    out: dict[str, list[Bar]] = {}
    for r in rows:
        out.setdefault(str(r["instrument_id"]), []).append(Bar(r["trade_date"], r["o"], r["h"], r["l"], r["c"], r["v"]))
    return out


def observed_sessions(series: Mapping[str, Sequence[Bar]]) -> list[date]:
    """Dates with at least half of the median instrument count (a thin day is not a session yet), newest ``MAX_SESSIONS``."""
    count: dict[date, int] = {}
    for bars in series.values():
        for b in bars:
            if b.c is not None:
                count[b.d] = count.get(b.d, 0) + 1
    if not count:
        return []
    med = sorted(count.values())[len(count) // 2]
    full = sorted(d for d, n in count.items() if n >= 0.5 * med)
    return full[-MAX_SESSIONS:]


def load_indices(conn: psycopg.Connection, lake: LakeStore, as_of: datetime, availability: str) -> tuple[dict[str, dict[date, float]], dict | None]:
    recon = B.reconstructed(availability)
    man = B._ready_manifest(conn, B.INDEX_DATASET, None if recon else as_of)
    if man is None:
        return {}, None
    idx = lake.read_snapshot_frames(man["content_hash"])
    if idx is None or idx.height == 0:
        return {}, man
    if recon:
        vis = (pl.col("date").map_elements(lambda d: B.policy_usable_at(d) <= as_of, return_dtype=pl.Boolean)
               & ((pl.col("ingested_at") == pl.col("ingested_at").min().over(["symbol", "date"])) | (pl.col("ingested_at") <= as_of)))
    else:
        vis = pl.col("ingested_at") <= as_of
    idx = idx.filter(vis).sort("ingested_at").unique(["symbol", "date"], keep="last")
    out: dict[str, dict[date, float]] = {}
    for sym, d, c in zip(idx["symbol"].to_list(), idx["date"].to_list(), idx["close"].to_list()):
        if c is not None:
            out.setdefault(sym, {})[d] = float(c)
    return out, man


# --------------------------------------------------------------------------------------------------------
# pack: everything computed for one release
# --------------------------------------------------------------------------------------------------------
@dataclass
class Pack:
    session_date: date
    decision_as_of: datetime
    availability: str
    history: daily.MarketHistory
    stocks: dict[str, Stock]
    panels: dict[str, dict[str, Any]]
    scopes: list[tuple[str, str, str, dict[str, Any]]]       # (scope_kind, scope_key, feature, body)
    gaps: list[dict[str, str]]
    expected: int
    input_manifests: list[dict]
    notes: list[str] = field(default_factory=list)


def compute_pack(conn: psycopg.Connection, lake: LakeStore, *, decision_as_of: datetime, availability: str,
                 members: Mapping[str, Sequence[str]] | None, node_names: Mapping[str, str] | None,
                 taxonomy_id: str = B.TAXONOMY_ID) -> Pack:
    stocks = load_stocks(conn, decision_as_of, availability)
    if not stocks:
        raise LookupError("no main-board instruments visible")
    series = load_bars(conn, list(stocks), decision_as_of, availability,
                       since=decision_as_of.astimezone(B.CST).date() - timedelta(days=int(MAX_SESSIONS * 1.6)))
    sessions = observed_sessions(series)
    if len(sessions) < 2:
        raise LookupError("fewer than two observed sessions")
    hist = daily.compute_history(stocks, series, PriceLimitRegistry(), sessions)
    indices, index_manifest = load_indices(conn, lake, decision_as_of, availability)
    reg = regime.build_regime(hist.sessions, indices)
    expected = len(stocks)
    pn: dict[str, dict[str, Any]] = {
        "thermometer": panels.thermometer(hist), "vitals": panels.vitals(hist, stocks, expected), "ladder": panels.ladder(hist, stocks),
        "breadth": panels.breadth(hist), "growth_strip": panels.growth_strip(hist, stocks), "regime": reg,
        "themes": panels.themes(hist, stocks, node_names or {}, members or {}) if members else
        {"session": hist.last_session, "themes": [], "ranked": 0, "version": panels.THEME_STRENGTH_VERSION, "unavailable": "NO_SECTOR_MEMBERSHIP"}}
    cur = hist.sessions[-1]
    gaps = [{"dataset_id": TODAY_DATASET, "requirement": "seal clock: first-seal time of each limit-up (intraday quotes or minute bars)",
             "reason_code": "NO_INTRADAY_SEAL_TIMES"},
            {"dataset_id": TODAY_DATASET, "requirement": "market turnover amount (daily bars carry shares only; amount is estimated as shares x close)",
             "reason_code": "TURNOVER_AMOUNT_ESTIMATED"}]
    if not members:
        gaps.append({"dataset_id": TODAY_DATASET, "requirement": "sector membership for theme resonance", "reason_code": "NO_SECTOR_MEMBERSHIP"})
    if not reg.get("available"):
        gaps.append({"dataset_id": TODAY_DATASET, "requirement": "index bars for the regime timeline", "reason_code": reg.get("reason") or "NO_INDEX_BARS"})
    elif reg["index"]["through"] < hist.last_session:
        gaps.append({"dataset_id": TODAY_DATASET, "requirement": f"index bars through {hist.last_session} (index data ends {reg['index']['through']}); "
                     "the regime is dated at the last index session", "reason_code": "INDEX_BARS_BEHIND_SESSION"})
    if cur.limit_unverifiable:
        gaps.append({"dataset_id": TODAY_DATASET, "requirement": f"{cur.limit_unverifiable} instruments have no verified price-limit rule and are excluded from limit statistics",
                     "reason_code": "LIMIT_RULE_NOT_VERIFIED"})
    scopes: list[tuple[str, str, str, dict[str, Any]]] = []
    scopes += _scope_bodies(MARKET_SCOPE, cur, hist, expected)
    for node, ids in sorted((members or {}).items()):
        rows = [hist.last_rows[i] for i in ids if i in hist.last_rows]
        if rows:
            scopes += _scope_bodies(("sector", f"{taxonomy_id}/{node}"), panels.subset_stats(hist.last_session, rows), None, len(ids))
    return Pack(hist.last_session, decision_as_of, availability, hist, stocks, pn, scopes, gaps, expected,
                [m for m in (index_manifest,) if m])


def _scope_bodies(scope: tuple[str, str], st: daily.SessionStats, hist: daily.MarketHistory | None, expected: int) -> list[tuple[str, str, str, dict]]:
    kind, key = scope
    rungs = [{"from_streak": k, "frozen_count": c[0], "promoted": c[1], "missing": c[2] if hist is not None else None,
              "rate_pct": daily.promotion_rate(c if hist is not None else [c[0], c[1], 0])} for k, c in sorted(st.promo.items())]
    board = {"session": st.d, "limit_up": st.limit_up if st.limit_verified else None, "broken": st.broken if st.limit_verified else None,
             "limit_down": st.limit_down if st.limit_verified else None, "seal_rate_pct": daily.seal_rate(st.limit_up, st.broken),
             "max_height": st.max_height if st.limit_verified else None, "ladder": {str(k): v for k, v in sorted(st.ladder.items())},
             "up": st.up, "down": st.down, "flat": st.flat, "coverage": panels.coverage_of(st, expected), "definitions": daily.MARKET_STATE_DEFINITIONS}
    promo = {"session": st.d, "rungs": rungs, "premium": {"close_pct": daily.mean_pct(st.prem_close_sum, st.prem_n), "sample_count": st.prem_n},
             "missing_countable": hist is not None,
             "note": "denominator = yesterday's frozen limit-up set at rung k; missing (no bar today) is excluded, never a failure"
             if hist is not None else "sector scope: members without a bar today cannot be counted as missing"}
    return [(kind, key, "mood", {**mood.score(st), "session": st.d}), (kind, key, "limit_board", board), (kind, key, "promotion", promo)]


def quality_of(body: Mapping[str, Any]) -> str:
    if body.get("available") is False or body.get("missing_components"):
        return "partial"
    cur = body.get("current")
    if isinstance(cur, Mapping) and any(v != "ok" for v in (cur.get("basis") or {}).values()):
        return "partial"
    return "valid"


# --------------------------------------------------------------------------------------------------------
# persistence of a pack
# --------------------------------------------------------------------------------------------------------
def scope_uuid(kind: str, key: str) -> uuid.UUID:
    return aid("scope", kind, key)


def make_key(feature_id: str, scope: tuple[str, str], pack: Pack, snapshot_id: uuid.UUID, dependency: str, params: dict) -> FeatureKey:
    return FeatureKey(
        feature_id=feature_id, definition_version=daily.MARKET_STATE_DEFINITIONS, implementation_digest=IMPLEMENTATION,
        parameter_digest=parameter_digest({"scope": list(scope), **params}), instrument_ids=(str(scope_uuid(*scope)),), timeframe="1d",
        adjustment_mode="raw", snapshot_id=str(snapshot_id), decision_as_of=pack.decision_as_of,
        availability_policy_version=pack.availability, session_policy_version=B.SESSION_POLICY,
        input_revision=f"session:{pack.session_date.isoformat()}", input_schema_version="CanonicalBar-3.0.0", dependency_digest=dependency,
        universe_policy_version=UNIVERSE_POLICY, calendar_version="not-applicable", adjustment_version="raw-unadjusted",
        market_rules_version=MARKET_RULES_VERSION, numeric_policy_version="decimal-50-half-even-v1")


def register_feature(conn: psycopg.Connection, lake: LakeStore, key: FeatureKey, body_json: str, computed_at: datetime) -> dict:
    """Store one panel body as an immutable lake object and register it as a READY feature_result (idempotent)."""
    contracts.check("FeatureKey", key.to_wire())
    existing = market_repo.get_feature(conn, key)
    if existing and existing["result_state"] == "READY":
        return existing
    epoch = (existing["lease_epoch"] + 1) if existing else 1
    if not market_repo.begin_feature(conn, key, epoch):
        raise RuntimeError("feature key is owned by a newer lease epoch")
    owner = f"feature:{key_hash(key)[:16]}:{epoch}"
    try:
        ref = lake.put_parquet(pl.DataFrame({"body": [body_json]}), owner=owner, kind="feature")
        if not market_repo.finish_feature(conn, key, epoch, ok=True, result_uri=ref.rel_path, result_hash=ref.sha256, computed_at=computed_at):
            raise RuntimeError("lease epoch fenced out")
    except Exception:
        market_repo.finish_feature(conn, key, epoch, ok=False)
        raise
    finally:
        lake.release(owner)
    return market_repo.get_feature(conn, key)


@dataclass
class Published:
    today: dict
    sentiment: dict
    feature_hashes: dict[str, str]


def publish_pack(svc: DataService, pack: Pack, *, release_id: uuid.UUID | None, input_snapshot: uuid.UUID, input_hash: str) -> Published:
    """Register every panel / scope feature, then publish D11 and D01 (D01 depends on D11)."""
    conn, lake, now = svc.conn, svc.lake, svc.clock()
    dep = canonical_hash({"inputs": [input_hash, *[m["content_hash"] for m in pack.input_manifests]], "session": pack.session_date.isoformat()})
    hashes: dict[str, str] = {}
    sent_rows = []
    for kind, skey, feat, body in pack.scopes:
        text = jdump(body)
        key = make_key(f"market_sentiment.{feat}", (kind, skey), pack, input_snapshot, dep, {"feature": feat})
        row = register_feature(conn, lake, key, text, now)
        hashes[f"{kind}|{skey}|{feat}"] = row["feature_key_hash"]
        sent_rows.append({"scope_kind": kind, "scope_key": skey, "feature": feat, "feature_key_hash": row["feature_key_hash"],
                          "quality_status": quality_of(body), "body": text, "body_sha256": sha(text)})
    sent_frame = pl.DataFrame(sent_rows).sort(["scope_kind", "scope_key", "feature"])
    sent_cov = {"dataset": SENTIMENT_DATASET, "release_id": None if release_id is None else str(release_id), "session_date": pack.session_date.isoformat(),
                "scopes": len({(r["scope_kind"], r["scope_key"]) for r in sent_rows}), "frame_sha256": sha(sent_frame.write_csv()),
                "definitions": daily.MARKET_STATE_DEFINITIONS, "mood_version": mood.MOOD_VERSION, "calibrated": False}
    sent = B.publish_derived(svc, SENTIMENT_DATASET, sent_frame, sent_cov, contract_id=SENTIMENT_CONTRACT,
                             dependencies=[(input_snapshot, "bars")], watermark=pack.decision_as_of)
    rows = []
    for name in STATE_PANELS:
        body = pack.panels[name]
        text = jdump(body)
        key = make_key(f"market_state.{name}", MARKET_SCOPE, pack, input_snapshot, dep, {"panel": name})
        row = register_feature(conn, lake, key, text, now)
        hashes[name] = row["feature_key_hash"]
        rows.append({"panel": name, "feature_key_hash": row["feature_key_hash"], "quality_status": quality_of(body), "body": text,
                     "body_sha256": sha(text)})
    frame = pl.DataFrame(rows)
    cov = {"dataset": TODAY_DATASET, "release_id": None if release_id is None else str(release_id), "session_date": pack.session_date.isoformat(),
           "decision_as_of": pack.decision_as_of.isoformat(), "availability_policy_version": pack.availability, "panels": list(STATE_PANELS),
           "capability_gaps": pack.gaps, "expected_instruments": pack.expected, "window_sessions": pack.history.window_sessions,
           "sentiment_snapshot_id": str(sent["snapshot_id"]), "frame_sha256": sha(frame.write_csv()), "definitions": daily.MARKET_STATE_DEFINITIONS,
           "versions": {"mood": mood.MOOD_VERSION, "regime": regime.REGIME_VERSION, "themes": panels.THEME_STRENGTH_VERSION},
           "note": "raw unadjusted daily bars of the main board; limit statistics exclude instruments without a verified price-limit rule"}
    today = B.publish_derived(svc, TODAY_DATASET, frame, cov, contract_id=TODAY_CONTRACT,
                              dependencies=[(input_snapshot, "bars"), (sent["snapshot_id"], "sentiment")], watermark=pack.decision_as_of)
    return Published(today, sent, hashes)


def build_component(svc: DataService, *, release_id: uuid.UUID, decision_as_of: datetime, availability: str,
                    membership: B.Membership | None) -> tuple[Pack, Published]:
    """Release hook: compute and publish the market state of ``decision_as_of``."""
    members = None if membership is None else {n: tuple(str(i) for i in ids) for n, ids in membership.members.items()}
    names = None if membership is None else dict(membership.nodes)
    pack = compute_pack(svc.conn, svc.lake, decision_as_of=decision_as_of, availability=availability, members=members, node_names=names,
                        taxonomy_id=B.TAXONOMY_ID)
    raw = B.raw_snapshot_refs(svc.conn, decision_as_of, availability)
    if not raw:
        raise LookupError("no raw daily bars visible")
    digest = canonical_hash(sorted(m["content_hash"] for m in raw))
    return pack, publish_pack(svc, pack, release_id=release_id, input_snapshot=raw[-1]["snapshot_id"], input_hash=digest)


# --------------------------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------------------------
_FRAMES: "OrderedDict[str, pl.DataFrame]" = OrderedDict()


def dataset_frame(conn: psycopg.Connection, lake: LakeStore, snapshot_id: uuid.UUID) -> tuple[dict, pl.DataFrame]:
    man = market_repo.get_manifest(conn, snapshot_id)
    if man is None or man["manifest_state"] != "READY":
        raise LookupError(f"snapshot {snapshot_id} is not READY")
    key = man["content_hash"]
    if key not in _FRAMES:
        _FRAMES[key] = lake.read_snapshot_frames(key)
        while len(_FRAMES) > 12:
            _FRAMES.popitem(last=False)
    _FRAMES.move_to_end(key)
    return man, _FRAMES[key]


def release_component(conn: psycopg.Connection, release_id: uuid.UUID, key: str = COMPONENT_KEY) -> dict | None:
    return conn.execute("SELECT * FROM decision.release_component WHERE release_id=%s AND component_key=%s AND component_state='READY'",
                        (release_id, key)).fetchone()


def release_row(conn: psycopg.Connection, release_id: uuid.UUID) -> dict | None:
    return conn.execute("SELECT * FROM decision.release WHERE release_id=%s", (release_id,)).fetchone()


def freshness_of(release_state: str, decision_as_of: datetime, now: datetime, *, expired_components: bool = False) -> str:
    """READY, recent and unexpired = current; anything else is stale (an old package is never labelled current analysis)."""
    if release_state != "READY" or expired_components or now - decision_as_of > STALE_AFTER:
        return "stale"
    return "current"


def feature_wire(conn: psycopg.Connection, key_hash_: str, contract_id: str, quality: str) -> dict:
    row = conn.execute("SELECT * FROM market.feature_result WHERE feature_key_hash=%s", (key_hash_,)).fetchone()
    if row is None or row["result_state"] != "READY":
        raise LookupError(f"feature {key_hash_} is not READY")
    return {"feature_id": row["feature_id"], "definition_version": row["definition_version"], "key_hash": key_hash_,
            "result": {"feature_key_hash": key_hash_, "key": row["full_key"], "result_state": "READY",
                       "artifact": {"ref": f"feature:{key_hash_}", "content_hash": row["result_hash"], "contract_id": contract_id,
                                    "schema_version": "3.0.0"},
                       "computed_at": row["computed_at"], "quality_status": quality}}


def resolve_artifact(conn: psycopg.Connection, lake: LakeStore, ref: str) -> dict[str, Any]:
    """Body of an artifact reference (``feature:<hash>`` or ``outcome:<snapshot>:<ranking>``); content hash is verified."""
    if ref.startswith("feature:"):
        h = ref.split(":", 1)[1]
        row = conn.execute("SELECT * FROM market.feature_result WHERE feature_key_hash=%s AND result_state='READY'", (h,)).fetchone()
        if row is None or not row["feature_id"].startswith(("market_state.", "market_sentiment.")):
            raise LookupError("unknown artifact")
        frame = pl.read_parquet(io.BytesIO(lake.read_object(row["result_hash"])))
        return {"ref": ref, "feature_id": row["feature_id"], "content_hash": row["result_hash"], "body": json.loads(frame["body"][0])}
    if ref.startswith("outcome:"):
        _, snap, ranking = ref.split(":", 2)
        man, df = dataset_frame(conn, lake, uuid.UUID(snap))
        if man["dataset_id"] != OUTCOME_DATASET:
            raise LookupError("unknown artifact")
        hit = df.filter(pl.col("ranking_id") == ranking)
        if hit.height != 1:
            raise LookupError("unknown artifact")
        return {"ref": ref, "feature_id": "ranking_outcome", "content_hash": hit["body_sha256"][0], "body": json.loads(hit["body"][0])}
    raise LookupError("unsupported artifact reference")


def market_today(conn: psycopg.Connection, lake: LakeStore, release_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, Any]:
    """The data of ``queryMarketToday`` without overlays (the api adds them): state features and gaps of the pinned release."""
    now = now or datetime.now(timezone.utc)
    comp = release_component(conn, release_id)
    state: list[dict] = []
    gaps: list[dict] = []
    quality = "valid"
    snapshot = None
    if comp is None:
        gaps.append({"dataset_id": TODAY_DATASET, "requirement": "optional release component market_state", "reason_code": "COMPONENT_NOT_IN_RELEASE"})
        quality = "partial"
    else:
        man, df = dataset_frame(conn, lake, comp["snapshot_id"])
        snapshot = man
        for r in df.sort("panel").iter_rows(named=True):
            state.append(feature_wire(conn, r["feature_key_hash"], TODAY_CONTRACT, r["quality_status"]))
            quality = "partial" if r["quality_status"] != "valid" else quality
        gaps.extend(man["coverage_document"].get("capability_gaps", []))
        order = {n: i for i, n in enumerate(STATE_PANELS)}
        state.sort(key=lambda f: order.get(f["feature_id"].split(".", 1)[1], 99))
    return {"state": state, "capability_gaps": gaps, "quality": quality, "snapshot": snapshot}


def sentiment_for(conn: psycopg.Connection, lake: LakeStore, release_id: uuid.UUID, scope_kind: str, scope_key: str) -> dict[str, Any] | None:
    comp = release_component(conn, release_id)
    if comp is None:
        return None
    today = market_repo.get_manifest(conn, comp["snapshot_id"])
    sid = uuid.UUID(today["coverage_document"]["sentiment_snapshot_id"])
    man, df = dataset_frame(conn, lake, sid)
    sel = df.filter((pl.col("scope_kind") == scope_kind) & (pl.col("scope_key") == scope_key))
    if sel.height == 0:
        return None
    feats = [feature_wire(conn, r["feature_key_hash"], SENTIMENT_CONTRACT, r["quality_status"]) for r in sel.sort("feature").iter_rows(named=True)]
    digest = canonical_hash(sorted(sel["body_sha256"].to_list()))
    body = {r["feature"]: json.loads(r["body"]) for r in sel.iter_rows(named=True)}
    cov = body["limit_board"]["coverage"]
    reasons = []
    if cov["pct_unverifiable"]:
        reasons.append(f"pct_unverifiable:{cov['pct_unverifiable']}")
    if cov["limit_unverifiable"]:
        reasons.append(f"limit_unverifiable:{cov['limit_unverifiable']}")
    expected = cov["expected_instruments"]
    coverage = {"policy_version": daily.MARKET_STATE_DEFINITIONS, "expected_count": expected, "observed_count": cov["observed"],
                "missing_count": None if expected is None else max(expected - cov["observed"], 0), "unverifiable_reasons": reasons}
    return {"features": feats, "coverage": coverage, "snapshot": man,
            "artifact": {"ref": f"dataset:{SENTIMENT_DATASET}:{man['snapshot_id']}#{scope_kind}/{scope_key}", "content_hash": digest,
                         "contract_id": SENTIMENT_CONTRACT, "schema_version": "3.0.0"}, "bodies": body}


def membership_of(conn: psycopg.Connection, lake: LakeStore, release_id: uuid.UUID, taxonomy_id: str, node_id: str) -> tuple[list[str], dict | None]:
    comp = release_component(conn, release_id, "sector_membership")
    if comp is None:
        return [], None
    man, df = dataset_frame(conn, lake, comp["snapshot_id"])
    ids = sorted(df.filter(pl.col("node_id") == node_id)["instrument_id"].to_list())
    return ids, man


def pool_for_node(conn: psycopg.Connection, release: Mapping[str, Any], taxonomy_id: str, version: int, node_id: str) -> list[dict]:
    """Frozen candidate pools at the release time (events recorded for decision times <= the release), filtered to the node."""
    rows = conn.execute(
        "SELECT DISTINCT ON (pool_id, instrument_id) * FROM decision.candidate_pool_event WHERE decision_as_of <= %s AND taxonomy_id=%s"
        " AND taxonomy_version=%s ORDER BY pool_id, instrument_id, event_sequence DESC",
        (release["decision_as_of"], taxonomy_id, version)).fetchall()
    pools: dict[uuid.UUID, list[dict]] = {}
    heads: dict[uuid.UUID, dict] = {}
    for r in rows:
        heads.setdefault(r["pool_id"], r)
        if r["node_id"] == node_id:
            pools.setdefault(r["pool_id"], []).append(r)
    out = []
    for pid, items in sorted(pools.items(), key=lambda kv: str(kv[0])):
        h = heads[pid]
        out.append(contracts.to_wire({
            "pool_id": pid, "release_id": release["release_id"], "policy_id": h["policy_id"], "policy_version": h["policy_version"],
            "items": [{"pool_event_id": r["pool_event_id"], "instrument_id": r["instrument_id"], "event_sequence": r["event_sequence"],
                       "taxonomy_node": {"taxonomy_id": r["taxonomy_id"], "taxonomy_version": r["taxonomy_version"], "node_id": r["node_id"]},
                       "membership_snapshot_id": r["membership_snapshot_id"], "current_state": r["current_state"],
                       "trigger_code": r["trigger_code"],
                       "plan_ref": None if r["plan_id"] is None else {"plan_id": r["plan_id"], "plan_version": r["plan_version"]},
                       "available_at": r["available_at"]} for r in sorted(items, key=lambda r: str(r["instrument_id"]))]}))
    return out


# --------------------------------------------------------------------------------------------------------
# ranking history / outcomes
# --------------------------------------------------------------------------------------------------------
def _enc(doc: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(doc, separators=(",", ":")).encode()).decode().rstrip("=")


def _dec(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


class CursorExpired(Exception):
    pass


def ranking_history(conn: psycopg.Connection, lake: LakeStore, *, scope_kind: str, scope_key: str, start: datetime, end: datetime,
                    horizon_id: str, target: str, limit: int, cursor: str | None, slot: str = B.SLOT_KEY) -> dict[str, Any]:
    """Public rankings of one scope/horizon/target published in [start, end), newest first, pinned by a watermark."""
    qhash = canonical_hash([scope_kind, scope_key, start.isoformat(), end.isoformat(), horizon_id, target, slot])
    if cursor:
        try:
            c = _dec(cursor)
        except Exception as e:
            raise CursorExpired("malformed cursor") from e
        if c.get("q") != qhash:
            raise CursorExpired("cursor belongs to another query")
        watermark, after = datetime.fromisoformat(c["w"]), c["a"]
    else:
        watermark, after = datetime.now(timezone.utc), None
    key = scope_key if scope_kind == "market" else scope_key
    rows = conn.execute(
        "SELECT r.ranking_id, r.release_id, rel.decision_as_of, r.created_at FROM decision.ranking_run r JOIN decision.release rel USING (release_id)"
        " JOIN decision.release_publication pub ON pub.release_id=rel.release_id WHERE pub.slot_key=%s AND r.ranking_scope=%s AND r.scope_key=%s AND r.horizon_id=%s AND r.forecast_target=%s AND rel.release_state IN ('READY','SUPERSEDED')"
        " AND rel.decision_as_of >= %s AND rel.decision_as_of < %s AND rel.published_at <= %s"
        + (" AND (rel.decision_as_of, r.ranking_id::text) < (%s, %s)" if after else "") +
        " ORDER BY rel.decision_as_of DESC, r.ranking_id::text DESC LIMIT %s",
        (slot, scope_kind, f"{key}/{horizon_id}", horizon_id, target, start, end, watermark,
         *( [datetime.fromisoformat(after[0]), after[1]] if after else [] ), limit + 1)).fetchall()
    page, more = rows[:limit], len(rows) > limit
    outcome_refs = latest_outcome_refs(conn, lake, [r["ranking_id"] for r in page])
    entries = [{"ranking_id": r["ranking_id"], "release_id": r["release_id"], "decision_as_of": r["decision_as_of"],
                "outcome_artifact": outcome_refs.get(r["ranking_id"])} for r in page]
    nxt = _enc({"q": qhash, "w": watermark.isoformat(), "a": [page[-1]["decision_as_of"].isoformat(), str(page[-1]["ranking_id"])]}) if more else None
    return {"entries": entries, "page": {"next_cursor": nxt, "snapshot_hash": canonical_hash([qhash, watermark.isoformat()])}}


def outcome_snapshots(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' ORDER BY manifest_version",
                        (OUTCOME_DATASET,)).fetchall()


def latest_outcome_refs(conn: psycopg.Connection, lake: LakeStore, ranking_ids: Sequence[uuid.UUID], *, outcome_as_of: datetime | None = None
                        ) -> dict[uuid.UUID, dict]:
    """Newest stored (resolved) outcome per ranking whose own cut-off is <= ``outcome_as_of`` (no future facts)."""
    want = {str(i): i for i in ranking_ids}
    out: dict[uuid.UUID, dict] = {}
    for man in outcome_snapshots(conn):
        if outcome_as_of is not None and datetime.fromisoformat(man["coverage_document"]["outcome_as_of"]) > outcome_as_of:
            continue
        df = lake.read_snapshot_frames(man["content_hash"])
        for r in df.filter(pl.col("ranking_id").is_in(list(want))).iter_rows(named=True):
            out[want[r["ranking_id"]]] = {"ref": f"outcome:{man['snapshot_id']}:{r['ranking_id']}", "content_hash": r["body_sha256"],
                                          "contract_id": OUTCOME_CONTRACT, "schema_version": "3.0.0"}
    return out



def component_summary(conn: psycopg.Connection, lake: LakeStore, release_id: uuid.UUID, component_key: str) -> dict | None:
    """Body of a ``summary:`` component of the release (stored in the release's explanations dataset); None when absent."""
    comp = conn.execute("SELECT artifact_ref FROM decision.release_component WHERE release_id=%s AND component_key=%s",
                        (release_id, component_key)).fetchone()
    expl = release_component(conn, release_id, "explanations")
    if comp is None or expl is None or not comp["artifact_ref"].startswith("summary:"):
        return None
    _, df = dataset_frame(conn, lake, expl["snapshot_id"])
    hit = df.filter(pl.col("ref") == comp["artifact_ref"])
    return json.loads(hit["body"][0]) if hit.height == 1 else None


def release_session_date(conn: psycopg.Connection, lake: LakeStore, release_id: uuid.UUID) -> date | None:
    """Session the release's features were computed at (the ``universe`` component summary body); None when not recoverable."""
    body = component_summary(conn, lake, release_id, "universe")
    try:
        return None if body is None else date.fromisoformat(body["session_date"])
    except (KeyError, ValueError):
        return None


def round_trip_cost() -> tuple[Decimal, str]:
    prof = user_account_2026()
    fee = lambda side: CostEngine.cumulative_fee(prof, side, OUTCOME_NOTIONAL)[0]      # noqa: E731
    return (fee("buy") + fee("sell")) / OUTCOME_NOTIONAL, prof.version


def evaluate_outcomes(svc: DataService, *, outcome_as_of: datetime, slot: str = "main") -> dict[str, Any]:
    """Resolve every public market ranking whose horizon has been observed; publish one immutable snapshot of new rows."""
    conn, lake = svc.conn, svc.lake
    done = set()
    for man in outcome_snapshots(conn):
        done |= set(lake.read_snapshot_frames(man["content_hash"])["ranking_id"].to_list())
    runs = conn.execute(
        "SELECT r.ranking_id, r.release_id, r.horizon_id, rel.decision_as_of, rel.availability_policy_version FROM decision.ranking_run r"
        " JOIN decision.release rel USING (release_id) WHERE r.ranking_scope='market' AND rel.release_state IN ('READY','SUPERSEDED')"
        " AND rel.decision_as_of <= %s ORDER BY rel.decision_as_of, r.ranking_id", (outcome_as_of,)).fetchall()
    todo = [r for r in runs if str(r["ranking_id"]) not in done and r["horizon_id"] in HORIZON_SESSIONS]
    if not todo:
        return {"published": False, "evaluated": 0, "pending": 0, "resolved": 0}
    cost, cost_version = round_trip_cost()
    stocks = load_stocks(conn, outcome_as_of, B.AVAILABILITY_POLICY)
    ids = [str(i) for i in stocks]
    series = load_bars(conn, ids, outcome_as_of, B.AVAILABILITY_POLICY, since=min(r["decision_as_of"] for r in todo).astimezone(B.CST).date() - timedelta(days=10))
    cal = observed_sessions(series)
    rows, pending = [], 0
    for r in todo:
        sess = release_session_date(conn, lake, r["release_id"]) or max(
            (d for d in cal if d <= r["decision_as_of"].astimezone(B.CST).date()), default=None)
        entries = [(str(e["instrument_id"]), e["alpha_rank"]) for e in conn.execute(
            "SELECT instrument_id, alpha_rank FROM decision.ranking_entry WHERE ranking_id=%s ORDER BY alpha_rank", (r["ranking_id"],))]
        body = outcomes.evaluate(ranking_id=str(r["ranking_id"]), release_id=str(r["release_id"]), horizon_id=r["horizon_id"],
                                 horizon_sessions=HORIZON_SESSIONS[r["horizon_id"]], decision_session=sess or date.min, ranked=entries,
                                 series=series, sessions=cal, cost_ratio=cost, cost_version=cost_version)
        if body["status"] != "resolved":
            pending += 1
            continue
        body["availability_policy_version"] = r["availability_policy_version"]
        body["record_kind"] = "reconstruction" if B.reconstructed(r["availability_policy_version"]) else "live_forward"
        text = jdump(body)
        rows.append({"ranking_id": str(r["ranking_id"]), "evaluation_context_id": str(OUTCOME_CONTEXT_ID),
                     "outcome_as_of": outcome_as_of.isoformat(), "horizon_id": r["horizon_id"], "body": text, "body_sha256": sha(text)})
    if not rows:
        return {"published": False, "evaluated": len(todo), "pending": pending, "resolved": 0}
    frame = pl.DataFrame(rows).sort("ranking_id")
    cov = {"dataset": OUTCOME_DATASET, "evaluation_context_id": str(OUTCOME_CONTEXT_ID), "outcome_as_of": outcome_as_of.isoformat(),
           "method_version": outcomes.OUTCOME_VERSION, "rows": len(rows), "frame_sha256": sha(frame.write_csv()), "slot": slot,
           "note": "resolved outcomes only; pending rankings have no row; the original rankings are never rewritten"}
    man = B.publish_derived(svc, OUTCOME_DATASET, frame, cov, contract_id=OUTCOME_CONTRACT, watermark=outcome_as_of)
    return {"published": True, "evaluated": len(todo), "pending": pending, "resolved": len(rows), "snapshot_id": str(man["snapshot_id"])}


# --------------------------------------------------------------------------------------------------------
# event tape (derived from consecutive quote snapshots, stored in the outbox log)
# --------------------------------------------------------------------------------------------------------
def quote_snapshot_ids(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT quote_snapshot_id, min(first_seen_at) seen, max(trade_date) trade_date, count(*) n FROM market.quote_projection"
        " GROUP BY quote_snapshot_id ORDER BY min(first_seen_at), quote_snapshot_id").fetchall()


def load_quotes(conn: psycopg.Connection, snapshot_id: uuid.UUID) -> dict[str, tape.Quote]:
    return {str(r["instrument_id"]): tape.Quote(str(r["instrument_id"]), r["last_price_raw"], r["ask_price_raw"], r["source_event_time"])
            for r in conn.execute("SELECT instrument_id, last_price_raw, ask_price_raw, source_event_time FROM market.quote_projection WHERE quote_snapshot_id=%s",
                                  (snapshot_id,))}


def tape_state(conn: psycopg.Connection, slot: str, trade_date: date) -> tuple[frozenset[str], dict[tuple[str, str], datetime], set[str]]:
    sealed, fired, done = set(), {}, set()
    for r in conn.execute("SELECT payload FROM ops.outbox WHERE aggregate_type=%s AND aggregate_id=%s AND payload->>'trade_date'=%s",
                          (TAPE_AGGREGATE, slot, trade_date.isoformat())):
        p = r["payload"]
        done.add(p["to_snapshot_id"])
        if p["event_kind"] in ("seal", "reseal"):
            sealed.add(p["instrument_id"])
        if p["event_kind"] in ("surge", "plunge") and p.get("event_time"):
            fired[(p["instrument_id"], p["event_kind"])] = max(fired.get((p["instrument_id"], p["event_kind"]), datetime.min.replace(tzinfo=timezone.utc)),
                                                              datetime.fromisoformat(p["event_time"].replace("Z", "+00:00")))
    return frozenset(sealed), fired, done


def derive_tape(svc: DataService, *, slot: str = "main", release_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Diff every unprocessed pair of consecutive same-day quote snapshots; append events to the tape log (idempotent)."""
    conn = svc.conn
    snaps = quote_snapshot_ids(conn)
    if len(snaps) < 2:
        return {"pairs": 0, "events": 0}
    now = svc.clock()
    registry = PriceLimitRegistry()
    appended, pairs = 0, 0
    refs_by_day: dict[date, tuple[dict[str, tape.TapeRef], int]] = {}
    for a, b in zip(snaps, snaps[1:]):
        if a["trade_date"] != b["trade_date"] or a["trade_date"] is None:
            continue
        day = a["trade_date"]
        sealed, fired, done = tape_state(conn, slot, day)
        if str(b["quote_snapshot_id"]) in done:
            continue
        if day not in refs_by_day:
            refs_by_day[day] = _tape_refs(conn, svc.lake, day, registry, release_id)
        refs, height = refs_by_day[day]
        pa, pb = load_quotes(conn, a["quote_snapshot_id"]), load_quotes(conn, b["quote_snapshot_id"])
        events = tape.diff_snapshots(pa, pb, refs, yesterday_max_height=height, ever_sealed=sealed, last_fired=fired)
        pairs += 1
        version = (conn.execute("SELECT coalesce(max(aggregate_version), 0) v FROM ops.outbox WHERE aggregate_type=%s AND aggregate_id=%s",
                                (TAPE_AGGREGATE, slot)).fetchone()["v"]) + 1
        # a pair without events leaves no row: it is re-diffed (cheaply) on the next run
        for n, ev in enumerate(events):
            payload = contracts.to_wire({
                "event_kind": ev.kind, "instrument_id": ev.iid, "code": ev.code, "name": ev.name, "price": ev.price, "pct_chg": ev.pct_chg,
                "limit_streak": ev.limit_streak, "detail": ev.detail, "themes": list(ev.themes), "trade_date": day,
                "event_time": ev.event_time, "from_snapshot_id": a["quote_snapshot_id"], "to_snapshot_id": b["quote_snapshot_id"],
                "tape_version": tape.TAPE_VERSION})
            validate_tape_payload(payload)
            PgOutbox(conn).append(OutboxEvent(
                event_id=aid("tape", a["quote_snapshot_id"], b["quote_snapshot_id"], ev.iid, ev.kind), event_type="market.tape.event",
                schema_version="3.0.0", aggregate_type=TAPE_AGGREGATE, aggregate_id=slot, aggregate_version=version, event_ordinal=n,
                occurred_at=ev.event_time or now, producer_version="market-tape/1.0", payload=payload))
            appended += 1
    return {"pairs": pairs, "events": appended}


def _tape_refs(conn: psycopg.Connection, lake: LakeStore, day: date, registry: PriceLimitRegistry, release_id: uuid.UUID | None
               ) -> tuple[dict[str, tape.TapeRef], int]:
    """Reference facts for the quoted instruments: yesterday's close / streak / ratio from raw bars, themes from the release."""
    ids = [str(r["instrument_id"]) for r in conn.execute("SELECT DISTINCT instrument_id FROM market.quote_projection WHERE trade_date=%s", (day,))]
    now = datetime.now(timezone.utc)
    stocks = load_stocks(conn, now, B.AVAILABILITY_POLICY)
    ids = [i for i in ids if i in stocks]
    series = load_bars(conn, ids, now, B.AVAILABILITY_POLICY, until=day - timedelta(days=1), since=day - timedelta(days=60))
    cal = observed_sessions(series)
    hist = daily.compute_history(stocks, series, registry, cal[-30:]) if cal else None
    themes: dict[str, list[str]] = {}
    rid = release_id or _current_release(conn)
    if rid is not None:
        comp = release_component(conn, rid, "sector_membership")
        if comp is not None:
            _, df = dataset_frame(conn, lake, comp["snapshot_id"])
            names = {r["node_id"]: r["display_name"] for r in conn.execute("SELECT node_id, display_name FROM core.taxonomy_node WHERE taxonomy_id=%s", (B.TAXONOMY_ID,))}
            for iid, node in zip(df["instrument_id"].to_list(), df["node_id"].to_list()):
                if iid in ids:
                    themes.setdefault(iid, []).append(names.get(node, node))
    height = 0
    if rid is not None:
        try:
            comp = release_component(conn, rid)
            if comp is not None:
                _, df = dataset_frame(conn, lake, comp["snapshot_id"])
                vit = json.loads(df.filter(pl.col("panel") == "vitals")["body"][0])
                height = vit.get("max_height") or 0
        except Exception:                                    # noqa: BLE001 - height only gates the new_high event
            height = 0
    refs: dict[str, tape.TapeRef] = {}
    for iid in ids:
        row = None if hist is None else hist.last_rows.get(iid)
        refs[iid] = tape.TapeRef(iid, stocks[iid].code, stocks[iid].name, None if row is None else row.close,
                                 None if row is None else row.ratio, 0 if row is None else row.streak, tuple(themes.get(iid, ())))
    return refs, int(height)


def _current_release(conn: psycopg.Connection) -> uuid.UUID | None:
    row = conn.execute("SELECT current_release_id FROM decision.release_slot WHERE slot_key=%s", (B.SLOT_KEY,)).fetchone()
    return None if row is None else row["current_release_id"]


TAPE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["event_kind", "instrument_id", "code", "name", "price", "pct_chg", "limit_streak", "detail", "themes", "trade_date",
                 "event_time", "from_snapshot_id", "to_snapshot_id", "tape_version"],
    "properties": {
        "event_kind": {"enum": list(tape.KINDS)}, "instrument_id": {"type": "string"}, "code": {"type": ["string", "null"]},
        "name": {"type": "string"}, "price": {"type": "string"}, "pct_chg": {"type": "string"}, "limit_streak": {"type": ["integer", "null"]},
        "detail": {"type": "string"}, "themes": {"type": "array", "items": {"type": "string"}}, "trade_date": {"type": "string"},
        "event_time": {"type": ["string", "null"]}, "from_snapshot_id": {"type": "string"}, "to_snapshot_id": {"type": "string"},
        "tape_version": {"type": "string"}}}


def validate_tape_payload(payload: Mapping[str, Any]) -> None:
    from jsonschema import Draft202012Validator

    errs = sorted(Draft202012Validator(TAPE_SCHEMA).iter_errors(payload), key=lambda e: list(e.path))
    if errs:
        raise ValueError("tape payload: " + "; ".join(e.message for e in errs[:3]))
