"""Nexus CLI Worker: multi-source intel polling, RAPTOR adjustments, empirical Arena & AlphaPROBE discovery.

Usage:
    uv run python -m pulse.workers.nexus seed
    uv run python -m pulse.workers.nexus poll-intel [--offline]
    uv run python -m pulse.workers.nexus realtime-adjust
    uv run python -m pulse.workers.nexus run-arena [--rounds 5] [--sessions 120] [--max-stocks 150]
    uv run python -m pulse.workers.nexus discover-strategies [--sessions 120] [--max-stocks 120]
    uv run python -m pulse.workers.nexus audit-integrity

Invariants enforced (AGENTS.md & Vault redlines):
* Creates an isolated ``RateLimitedClient(min_interval_seconds=1.0)`` per target domain
  (Xinhua, CLS, Eastmoney, THS), closed in ``try/finally``, preventing cross-host breaker contagion.
* Stops immediately on first block (403/429/456/501 or HTML block page), never retry-storms,
  and persists exact ``ingest.source_health`` state + structured ``errors`` diagnostics.
* Empirical Arena & Strategy Discovery compute strictly point-in-time historical ``PanelBar`` /
  ``DailyStockBarSlice`` evaluations on ``UnifiedBacktestEngine`` (zero lookahead).
* LLM lane stays behind ``PULSE_LLM_ALLOWED_MODELS`` (``gpt-6-luna`` only), explicit switch file,
  and pre-send call ledger; deterministic rules govern all numerical scores and directives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from pulse import config
from pulse.adapters.postgres import db, ingest_repo
from pulse.adapters.sources import cls_news, eastmoney_news, registry, ths_news, xinhua_news
from pulse.adapters.sources.http import RateLimitedClient, SourceBlocked, TransportFailure
from pulse.adapters.sources.news_items import BadAnswer, BlockedPage
from pulse.application import nexus_service as N
from pulse.domain.arena import (
    DailyStockBarSlice,
    build_full_taxonomy_catalog,
    compute_amihud_illiquidity,
    discover_alphaprobe_candidates,
    run_swiss_glicko_season,
    score_aaai2024_sector_hypergraph,
    score_ijcai2026_prism_vq_moe,
    score_jf2026_margin_lottery_shield,
    score_jfqa2025_micro_tug_of_war,
    score_kdd2026_ebm_interpretable,
    score_ms2025_lot_momentum,
)
from pulse.domain.benchmark import DEFAULT_STANDARD, PanelBar
from pulse.domain.evidence.health import HealthPolicy, apply_attempt, half_open
from pulse.domain.intelligence.guba_four_session import analyze_four_session_resonance
from pulse.domain.intelligence.macro_cross_verify import cross_verify_documents
from pulse.domain.intelligence.realtime_adjuster import build_realtime_news_strategy_adjustment
from pulse.domain.intelligence.seat_personas import analyze_lhb_battle
from pulse.domain.technical.tv_pro import compute_pro_signals_and_markers

_CODE_RE = re.compile(r"(?<!\d)((?:60[0135]\d{3}|00[0123]\d{3}|30[01]\d{3}|68[89]\d{3}|8[2378]\d{3}|43\d{4}|920\d{3}))(?!\d)")


def _dec_or_none(val: Any) -> Decimal | None:
    if val is None or val == "":
        return None
    try:
        d = Decimal(str(val))
        return d if d.is_finite() else None
    except Exception:
        return None


def _entity_lookups(conn) -> tuple[dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]:
    """Load symbol->uuid, stock display_name->uuid, and sector term->node_id from DB."""
    code_to_iid: dict[str, str] = {}
    for r in conn.execute(
        "SELECT DISTINCT ON (source_symbol) source_symbol, instrument_id FROM core.instrument_alias"
        " ORDER BY source_symbol, effective_from DESC"
    ).fetchall():
        code_to_iid[str(r["source_symbol"])] = str(r["instrument_id"])

    stock_names: list[tuple[str, str]] = []
    for r in conn.execute(
        "SELECT DISTINCT ON (instrument_id) instrument_id, display_name FROM core.instrument_state"
        " ORDER BY instrument_id, effective_from DESC, state_version DESC"
    ).fetchall():
        nm = str(r["display_name"] or "").strip()
        clean = nm.replace("*", "").replace("ST", "").strip()
        if len(clean) >= 2:
            stock_names.append((clean, str(r["instrument_id"])))

    sector_terms: list[tuple[str, str]] = []
    seen_sec: set[tuple[str, str]] = set()
    for r in conn.execute("SELECT DISTINCT node_id, display_name FROM core.taxonomy_node").fetchall():
        nid = str(r["node_id"])
        for term in (nid, str(r["display_name"] or "").strip()):
            if len(term) >= 2 and (term, nid) not in seen_sec:
                seen_sec.add((term, nid))
                sector_terms.append((term, nid))

    return code_to_iid, stock_names, sector_terms


def _match_entities(
    title: str,
    body: str,
    vendor_codes: list[str],
    code_to_iid: dict[str, str],
    stock_names: list[tuple[str, str]],
    sector_terms: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    text = f"{title} {body[:400]}"
    insts: set[str] = set()
    for c in vendor_codes:
        if c in code_to_iid:
            insts.add(code_to_iid[c])
    for c in _CODE_RE.findall(text):
        if c in code_to_iid:
            insts.add(code_to_iid[c])
    for clean_name, iid_s in stock_names:
        if clean_name in title or clean_name in text[:160]:
            insts.add(iid_s)
    sectors = sorted({nid for term, nid in sector_terms if term in text})
    return sorted(insts), sectors


def cmd_seed(conn) -> dict[str, Any]:
    N.seed_nexus_tables(conn)
    return {"ok": True, "action": "seed", "season_id": N.DEFAULT_SEASON_ID}


def cmd_poll_intel(conn, *, offline: bool = False, transport=None) -> dict[str, Any]:
    """Poll Xinhua (Tier-1), CLS (Tier-2), Eastmoney & THS (Tier-3) with isolated per-host
    ``RateLimitedClient(min_interval_seconds=1.0)``, record real ``ingest.source_health``,
    link entities/sectors, cross-verify, and store into ``intelligence.cross_source_cluster``."""
    N.seed_nexus_tables(conn)
    now = datetime.now(timezone.utc)
    code_to_iid, stock_names, sector_terms = _entity_lookups(conn)

    collected_docs: list[dict[str, Any]] = N._load_recent_docs(conn, now, limit=120)
    for d in collected_docs:
        if not d["instrument_ids"] or not d["sector_nodes"]:
            m_insts, m_secs = _match_entities(
                d.get("title", ""), d.get("body", ""), [], code_to_iid, stock_names, sector_terms
            )
            if m_insts:
                d["instrument_ids"] = sorted(set(d["instrument_ids"]) | set(m_insts))
            if m_secs:
                d["sector_nodes"] = sorted(set(d["sector_nodes"]) | set(m_secs))

    fetched_counts: dict[str, int] = {}
    blocked_sources: list[str] = []
    errors: dict[str, str] = {}

    if not offline:
        xinhua_spec = registry.spec(xinhua_news.SOURCE_ID, xinhua_news.ENDPOINT_ID)
        ths_spec = registry.spec(ths_news.SOURCE_ID, ths_news.ENDPOINT_ID)
        feeds = [
            (
                xinhua_spec,
                lambda c: xinhua_news.XinhuaNewsAdapter(c).page(),
                lambda b: xinhua_news.recorder_items(b)[0],
            ),
            (
                cls_news.SPEC,
                lambda c: cls_news.ClsNewsAdapter(c).page(page_size=20),
                lambda b: cls_news.recorder_items(b),
            ),
            (
                eastmoney_news.SPEC,
                lambda c: eastmoney_news.EastmoneyNewsAdapter(c).page(page_size=20),
                lambda b: eastmoney_news.recorder_items(b),
            ),
            (
                ths_spec,
                lambda c: ths_news.ThsNewsAdapter(c).page(),
                lambda b: ths_news.recorder_items(b),
            ),
        ]

        with conn.transaction():
            ingest_repo.ensure_contracts(conn, now, ("EvidenceDocument",))
            ingest_repo.ensure_sources(conn, now)
            for sp, _, _ in feeds:
                ingest_repo.ensure_endpoint(conn, sp, now)

        for sp, fetch_fn, parse_fn in feeds:
            policy = HealthPolicy(stale_after_seconds=sp.stale_after_seconds)
            prev_h = ingest_repo.load_health(conn, sp, "latest")
            if prev_h.circuit == "open":
                if prev_h.next_attempt_at and now < prev_h.next_attempt_at:
                    blocked_sources.append(sp.source_id)
                    errors[sp.source_id] = f"circuit_open_until:{prev_h.next_attempt_at.isoformat()}"
                    continue
                prev_h = half_open(prev_h, now)

            # Isolated per-host RateLimitedClient so one site's 403/429 NEVER trips another host!
            host_client = RateLimitedClient(min_interval_seconds=1.0, transport=transport)
            try:
                cap = fetch_fn(host_client)
                items = parse_fn(cap.body)
                fetched_counts[sp.source_id] = len(items)
                newest_pub: datetime | None = None
                for it in items:
                    pub = it.get("source_published_at")
                    if isinstance(pub, datetime) and pub <= now:
                        newest_pub = max(newest_pub, pub) if newest_pub else pub
                    m_insts, m_secs = _match_entities(
                        str(it.get("title") or ""),
                        str(it.get("body") or ""),
                        list(it.get("codes") or []),
                        code_to_iid,
                        stock_names,
                        sector_terms,
                    )
                    collected_docs.append({
                        "source_id": sp.source_id,
                        "title": it.get("title") or "",
                        "body": it.get("body") or "",
                        "published_at": pub or now,
                        "instrument_ids": m_insts,
                        "sector_nodes": m_secs,
                    })

                outcome = "success_data" if items else "success_empty"
                next_h = apply_attempt(
                    prev_h,
                    outcome,
                    now,
                    policy,
                    new_records=len(items),
                    newest_content_at=newest_pub,
                    content_expected_now=True,
                    canonical_ready=bool(items),
                )
                with conn.transaction():
                    ingest_repo.save_health(conn, sp, "latest", next_h, now)
            except (SourceBlocked, BlockedPage) as exc:
                blocked_sources.append(sp.source_id)
                errors[sp.source_id] = f"{type(exc).__name__}: {exc}"
                err_code = f"http_{exc.status}" if isinstance(exc, SourceBlocked) and exc.status else "blocked_page"
                next_h = apply_attempt(prev_h, "blocked", now, policy, error_code=err_code[:64])
                with conn.transaction():
                    ingest_repo.save_health(conn, sp, "latest", next_h, now)
            except (TransportFailure, BadAnswer) as exc:
                fetched_counts[sp.source_id] = 0
                errors[sp.source_id] = f"{type(exc).__name__}: {exc}"
                outcome = "parse_error" if isinstance(exc, BadAnswer) else "transport_error"
                next_h = apply_attempt(prev_h, outcome, now, policy, error_code=str(exc)[:64])
                with conn.transaction():
                    ingest_repo.save_health(conn, sp, "latest", next_h, now)
            except Exception as exc:
                fetched_counts[sp.source_id] = 0
                errors[sp.source_id] = f"{type(exc).__name__}: {exc}"
                next_h = apply_attempt(prev_h, "parse_error", now, policy, error_code=type(exc).__name__[:64])
                with conn.transaction():
                    ingest_repo.save_health(conn, sp, "latest", next_h, now)
            finally:
                host_client.close()

    clusters = cross_verify_documents(collected_docs, as_of=now)
    stored = 0
    with conn.transaction():
        for cl in clusters[:80]:
            pub = cl["published_at"]
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")) if isinstance(pub, str) else now
            conn.execute(
                "INSERT INTO intelligence.cross_source_cluster(cluster_id, fingerprint_hash, headline, highest_tier,"
                " verification_state, corroboration_count, confidence_score, polarity, decay_half_life_hrs,"
                " sources_json, instrument_ids_json, sector_nodes_json, bull_bear_debate, first_published_at, available_at, recorded_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (cluster_id) DO UPDATE SET verification_state=EXCLUDED.verification_state,"
                " corroboration_count=EXCLUDED.corroboration_count, confidence_score=EXCLUDED.confidence_score,"
                " sources_json=EXCLUDED.sources_json, instrument_ids_json=EXCLUDED.instrument_ids_json,"
                " sector_nodes_json=EXCLUDED.sector_nodes_json, bull_bear_debate=EXCLUDED.bull_bear_debate,"
                " recorded_at=EXCLUDED.recorded_at",
                (
                    cl["cluster_id"], cl["fingerprint_hash"], cl["headline"], cl["tier"],
                    cl["verification_state"], cl["corroboration_count"], Decimal(str(cl["confidence_score"])),
                    cl["polarity"], Decimal("72.0"), Jsonb(cl["sources"]), Jsonb(cl["instrument_ids"]),
                    Jsonb(cl["affected_sectors"]), Jsonb({"note": cl["bull_bear_note"]}),
                    pub_dt, now, now,
                ),
            )
            stored += 1

    return {
        "ok": True,
        "offline": offline,
        "fetched_counts": fetched_counts,
        "blocked_sources": blocked_sources,
        "errors": errors,
        "clusters_verified_and_stored": stored,
    }


def cmd_realtime_adjust(conn, *, max_clusters: int = 25) -> dict[str, Any]:
    """Compute RAPTOR + Amihud illiquidity real-time adjustments from verified clusters
    and persist into ``intelligence.realtime_signal_adjustment``."""
    N.seed_nexus_tables(conn)
    now = datetime.now(timezone.utc)

    raw_cls = conn.execute(
        "SELECT cluster_id, headline, highest_tier, verification_state, corroboration_count,"
        " confidence_score, polarity, sources_json, instrument_ids_json, sector_nodes_json,"
        " first_published_at, available_at"
        " FROM intelligence.cross_source_cluster"
        " WHERE available_at <= %s ORDER BY available_at DESC, confidence_score DESC LIMIT %s",
        (now, max_clusters),
    ).fetchall()

    if not raw_cls:
        return {"ok": True, "clusters_scanned": 0, "adjustments_written": 0, "directives": {}}

    cluster_dicts: list[dict[str, Any]] = []
    target_iids: dict[UUID, str] = {}
    sector_hits: dict[str, str] = {}
    market_clusters: list[dict[str, Any]] = []

    for r in raw_cls:
        insts = [str(x) for x in (r["instrument_ids_json"] or [])]
        secs = [str(x) for x in (r["sector_nodes_json"] or [])]
        pub = r["first_published_at"] or r["available_at"]
        cd = {
            "cluster_id": r["cluster_id"],
            "headline": r["headline"],
            "tier": r["highest_tier"],
            "verification_state": r["verification_state"],
            "corroboration_count": int(r["corroboration_count"]),
            "confidence_score": float(r["confidence_score"]),
            "sources": list(r["sources_json"] or []),
            "polarity": r["polarity"],
            "affected_sectors": secs,
            "sector_nodes": secs,
            "instrument_ids": insts,
            "published_at": pub,
        }
        cluster_dicts.append(cd)
        for raw_id in insts:
            try:
                u = UUID(raw_id)
                target_iids.setdefault(u, r["cluster_id"])
            except ValueError:
                pass
        for snode in secs:
            sector_hits.setdefault(snode, r["cluster_id"])
        if not insts and not secs:
            market_clusters.append(cd)

    # Expand mentioned sectors into top tagged instruments when direct stock IDs are sparse
    for snode, cid in sector_hits.items():
        s_rows = conn.execute(
            "SELECT DISTINCT instrument_id FROM core.instrument_tag"
            " WHERE node_id=%s AND available_at <= %s AND (effective_to IS NULL OR effective_to > %s) LIMIT 3",
            (snode, now, now),
        ).fetchall()
        for sr in s_rows:
            target_iids.setdefault(sr["instrument_id"], cid)

    written = 0
    directive_counts: Counter[str] = Counter()
    eff_until = now + timedelta(hours=24)

    with conn.transaction():
        for iid_u, anchor_cid in list(target_iids.items())[:25]:
            info = N._load_instrument_info(conn, iid_u)
            if not info:
                continue
            bars = N._load_daily_bars_pit(conn, iid_u, now, limit=90)
            sl = DailyStockBarSlice(
                instrument_id=str(iid_u),
                symbol=info["symbol"],
                sector_node=info.get("sector_node"),
                opens=tuple(b.o for b in bars),
                highs=tuple(b.h for b in bars),
                lows=tuple(b.l for b in bars),
                closes=tuple(b.c for b in bars),
                volumes=tuple(b.v for b in bars),
            )
            amihud = compute_amihud_illiquidity(sl)
            pro_sig, _ = compute_pro_signals_and_markers(bars)
            atr_14 = float(pro_sig["atr_14"]) if pro_sig.get("atr_14") else None
            rvol = float(pro_sig["rvol_20d"]) if pro_sig.get("rvol_20d") else None
            turns = [b.c * b.v for b in bars[-20:] if b.c > 0 and b.v > 0]
            avg_turn = (sum(turns) / len(turns)) if turns else None

            lhb_d, lhb_rows = N._load_recent_lhb_for_stock(conn, iid_u, now)
            seat = analyze_lhb_battle(lhb_rows, report_date=lhb_d, turnover_cny=avg_turn)
            guba_posts, _ = N._load_recent_guba_for_stock(conn, iid_u, now)
            guba_res = analyze_four_session_resonance(guba_posts, rvol_20d=rvol, lhasa_buyer_count=seat["lhasa_buyer_count"])
            spam_r = float(guba_res["template_spam_ratio"]) if guba_res.get("template_spam_ratio") is not None else None
            has_neg = any(
                c["polarity"] == "negative" and c["tier"] == "tier1_official"
                and (str(iid_u) in c["instrument_ids"] or info["symbol"] in c["headline"])
                for c in cluster_dicts
            )

            adj = build_realtime_news_strategy_adjustment(
                symbol=info["symbol"],
                display_name=info["display_name"],
                verified_clusters=cluster_dicts,
                as_of=now,
                instrument_id=str(iid_u),
                sector_node=info.get("sector_node"),
                base_entry_price=bars[-1].c if bars else None,
                atr_14=atr_14,
                supertrend_dir=pro_sig["supertrend_dir"] if pro_sig["supertrend_dir"] != "unavailable" else "neutral",
                rvol_20d=rvol,
                lhasa_buyer_count=seat["lhasa_buyer_count"],
                one_day_raider_count=seat["one_day_raider_count"],
                template_spam_ratio=spam_r,
                amihud_illiquidity=amihud,
                avg_daily_turnover_cny=avg_turn,
                has_negative_official_notice=has_neg,
            )

            adj_id = "rta:" + hashlib.sha256(f"instrument|{iid_u}|{anchor_cid}".encode()).hexdigest()[:16]
            llm_sum = str((adj["llm_view"]["summary"] or {}).get("holding_thesis") or adj["execution_plan"]["holding_plan"])
            conn.execute(
                "INSERT INTO intelligence.realtime_signal_adjustment("
                " adjustment_id, cluster_id, target_scope, target_key, tilt_bps, action_directive,"
                " liquidity_passed, amihud_ratio, llm_model_used, llm_summary, evidence_json, effective_until, available_at, recorded_at)"
                " VALUES (%s, %s, 'instrument', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (adjustment_id) DO UPDATE SET tilt_bps=EXCLUDED.tilt_bps,"
                " action_directive=EXCLUDED.action_directive, liquidity_passed=EXCLUDED.liquidity_passed,"
                " amihud_ratio=EXCLUDED.amihud_ratio, llm_model_used=EXCLUDED.llm_model_used,"
                " llm_summary=EXCLUDED.llm_summary, evidence_json=EXCLUDED.evidence_json,"
                " effective_until=EXCLUDED.effective_until, available_at=EXCLUDED.available_at, recorded_at=EXCLUDED.recorded_at",
                (
                    adj_id, anchor_cid, str(iid_u),
                    Decimal(str(adj["tilt_bps"])), adj["action_directive"],
                    not bool(adj["liquidity_filter_triggered"]),
                    Decimal(f"{amihud:.6f}") if amihud is not None else None,
                    adj["llm_model_used"], llm_sum,
                    Jsonb({
                        "symbol": info["symbol"],
                        "veto_reasons": adj["risk_veto_reasons"],
                        "cluster_impacts": adj["cluster_impacts"][:5],
                        "execution_plan": adj["execution_plan"],
                    }),
                    eff_until, now, now,
                ),
            )
            written += 1
            directive_counts[adj["action_directive"]] += 1

        # Record macro-wide adjustment if high-confidence market clusters exist without stock-specific tags
        if market_clusters:
            top_m = max(market_clusters, key=lambda c: float(c.get("confidence_score") or 0.0))
            adj_m = build_realtime_news_strategy_adjustment(
                symbol="ALL",
                display_name="A股全市场宏观",
                verified_clusters=[top_m],
                as_of=now,
            )
            adj_id = "rta:" + hashlib.sha256(f"market|ALL|{top_m['cluster_id']}".encode()).hexdigest()[:16]
            llm_sum = str((adj_m["llm_view"]["summary"] or {}).get("holding_thesis") or adj_m["execution_plan"]["holding_plan"])
            conn.execute(
                "INSERT INTO intelligence.realtime_signal_adjustment("
                " adjustment_id, cluster_id, target_scope, target_key, tilt_bps, action_directive,"
                " liquidity_passed, amihud_ratio, llm_model_used, llm_summary, evidence_json, effective_until, available_at, recorded_at)"
                " VALUES (%s, %s, 'market', 'ALL', %s, %s, true, NULL, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (adjustment_id) DO UPDATE SET tilt_bps=EXCLUDED.tilt_bps,"
                " action_directive=EXCLUDED.action_directive, llm_summary=EXCLUDED.llm_summary,"
                " effective_until=EXCLUDED.effective_until, available_at=EXCLUDED.available_at, recorded_at=EXCLUDED.recorded_at",
                (
                    adj_id, top_m["cluster_id"],
                    Decimal(str(adj_m["tilt_bps"])), adj_m["action_directive"],
                    adj_m["llm_model_used"], llm_sum,
                    Jsonb({"headline": top_m["headline"], "cluster_impacts": adj_m["cluster_impacts"]}),
                    eff_until, now, now,
                ),
            )
            written += 1
            directive_counts[adj_m["action_directive"]] += 1

    return {
        "ok": True,
        "clusters_scanned": len(cluster_dicts),
        "adjustments_written": written,
        "directives": dict(directive_counts),
    }


def _pct_map(raw: dict[str, float]) -> dict[str, float]:
    if not raw:
        return {}
    ordered = sorted(raw.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(ordered)
    return {k: (i + 1) / n for i, (k, _) in enumerate(ordered)}


def _load_historical_panel_and_signals(
    conn,
    *,
    sessions: int = 120,
    max_stocks: int = 150,
) -> tuple[dict[str, list[PanelBar]], dict[date, list[DailyStockBarSlice]], dict[str, dict[date, dict[str, float]]]]:
    """Build strictly zero-lookahead historical ``PanelBar`` panel, incremental ``slices_by_day``,
    and rolling causal strategy ``signal_history`` from PostgreSQL."""
    now = datetime.now(timezone.utc)
    date_rows = conn.execute(
        "SELECT DISTINCT trade_date FROM market.bar_projection"
        " WHERE timeframe='1d' AND available_at <= %s"
        " ORDER BY trade_date DESC LIMIT %s",
        (now, max(10, sessions)),
    ).fetchall()
    if not date_rows:
        return {}, {}, {}
    d_max = date_rows[0]["trade_date"]
    d_min = date_rows[-1]["trade_date"]

    # Pick top liquid instruments with valid close >= 2.0
    inst_rows = conn.execute(
        "WITH liq AS ("
        "   SELECT instrument_id, AVG(close_price_raw::float8 * COALESCE(volume_shares, 0)::float8) AS avg_turn"
        "   FROM market.bar_projection"
        "   WHERE timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
        "     AND close_price_raw >= 2.0"
        "   GROUP BY instrument_id"
        "   HAVING COUNT(DISTINCT trade_date) >= %s"
        "   ORDER BY avg_turn DESC NULLS LAST"
        "   LIMIT %s"
        ")"
        " SELECT l.instrument_id, COALESCE(s.board_code, 'main') AS board_code,"
        "        COALESCE(s.is_risk_warning, false) AS is_risk_warning,"
        "        (SELECT a.source_symbol FROM core.instrument_alias a WHERE a.instrument_id=l.instrument_id"
        "         ORDER BY a.effective_from DESC LIMIT 1) AS sym,"
        "        (SELECT t.node_id FROM core.instrument_tag t WHERE t.instrument_id=l.instrument_id"
        "         ORDER BY t.available_at DESC, t.effective_from DESC LIMIT 1) AS sec"
        " FROM liq l"
        " LEFT JOIN LATERAL ("
        "   SELECT board_code, is_risk_warning FROM core.instrument_state st"
        "   WHERE st.instrument_id=l.instrument_id ORDER BY st.effective_from DESC, st.state_version DESC LIMIT 1"
        " ) s ON true",
        (d_min, d_max, now, min(25, len(date_rows)), max_stocks),
    ).fetchall()
    if not inst_rows:
        return {}, {}, {}

    meta_map = {str(r["instrument_id"]): r for r in inst_rows}
    uids = [r["instrument_id"] for r in inst_rows]

    bar_rows = conn.execute(
        "SELECT DISTINCT ON (instrument_id, trade_date)"
        "       instrument_id, trade_date, open_price_raw o, high_price_raw h,"
        "       low_price_raw l, close_price_raw c, volume_shares v"
        " FROM market.bar_projection"
        " WHERE instrument_id = ANY(%s) AND timeframe='1d' AND trade_date BETWEEN %s AND %s AND available_at <= %s"
        "   AND open_price_raw IS NOT NULL AND close_price_raw IS NOT NULL AND close_price_raw > 0"
        " ORDER BY instrument_id, trade_date ASC, available_at DESC",
        (uids, d_min, d_max, now),
    ).fetchall()

    panel: dict[str, list[PanelBar]] = {}
    by_day_bars: dict[date, list[tuple[str, Any]]] = {}
    for r in bar_rows:
        iid_s = str(r["instrument_id"])
        m = meta_map.get(iid_s)
        if not m:
            continue
        pb = PanelBar(
            date=r["trade_date"],
            open=float(r["o"]),
            high=float(r["h"] or r["c"]),
            low=float(r["l"] or r["c"]),
            close=float(r["c"]),
            volume=int(max(0, round(float(r["v"] or 0)))),
            board_code=str(m["board_code"] or "main"),
            is_risk_warning=bool(m["is_risk_warning"]),
            sector_node=m["sec"],
        )
        panel.setdefault(iid_s, []).append(pb)
        by_day_bars.setdefault(r["trade_date"], []).append((iid_s, pb))

    # Build incremental zero-lookahead DailyStockBarSlice prefix per day
    hist_o: dict[str, list[float]] = {k: [] for k in panel}
    hist_h: dict[str, list[float]] = {k: [] for k in panel}
    hist_l: dict[str, list[float]] = {k: [] for k in panel}
    hist_c: dict[str, list[float]] = {k: [] for k in panel}
    hist_v: dict[str, list[float]] = {k: [] for k in panel}

    slices_by_day: dict[date, list[DailyStockBarSlice]] = {}
    signal_history: dict[str, dict[date, dict[str, float]]] = {
        "fusion_static40_safe": {},
        "paper_ms2025_lot_momentum": {},
        "paper_jfqa2025_micro_tug_of_war": {},
        "paper_aaai2024_sector_hypergraph": {},
        "paper_icml2025_alphaqcm_tail": {},
        "paper_kdd2026_alphaeval_rre": {},
        "paper_ijcai2026_prism_vq_moe": {},
        "pb:low_risk_combo": {},
        "pb:low_volatility": {},
        "pb:first_board_relay": {},
    }

    all_days = sorted(by_day_bars.keys())
    for d in all_days:
        day_slices: list[DailyStockBarSlice] = []
        for iid_s, pb in by_day_bars[d]:
            hist_o[iid_s].append(float(pb.open))
            hist_h[iid_s].append(float(pb.high))
            hist_l[iid_s].append(float(pb.low))
            hist_c[iid_s].append(float(pb.close))
            hist_v[iid_s].append(float(pb.volume))
            m = meta_map[iid_s]
            day_slices.append(DailyStockBarSlice(
                instrument_id=iid_s,
                symbol=str(m["sym"] or iid_s[:8]),
                sector_node=m["sec"],
                opens=tuple(hist_o[iid_s]),
                highs=tuple(hist_h[iid_s]),
                lows=tuple(hist_l[iid_s]),
                closes=tuple(hist_c[iid_s]),
                volumes=tuple(hist_v[iid_s]),
            ))
        slices_by_day[d] = day_slices

        if len(day_slices) == 0:
            continue

        # Top-venue 2026 operators (all strictly PIT on day d)
        for key, fn in (
            ("paper_ms2025_lot_momentum", score_ms2025_lot_momentum),
            ("paper_jfqa2025_micro_tug_of_war", score_jfqa2025_micro_tug_of_war),
            ("paper_aaai2024_sector_hypergraph", score_aaai2024_sector_hypergraph),
            ("paper_icml2025_alphaqcm_tail", score_jf2026_margin_lottery_shield),
            ("paper_kdd2026_alphaeval_rre", score_kdd2026_ebm_interpretable),
        ):
            sc = fn(day_slices)
            if sc:
                signal_history[key][d] = sc

        # Infer day's causal market regime for PRISM-VQ codebook
        eligible_ma = [s for s in day_slices if len(s.closes) >= 20]
        if eligible_ma:
            above = sum(1 for s in eligible_ma if s.closes[-1] >= sum(s.closes[-20:]) / 20.0)
            b_pct = 100.0 * above / len(eligible_ma)
            reg_d = "bull_mainline" if b_pct >= 62.0 else ("structural_rotation" if b_pct >= 45.0 else ("defensive_bear" if b_pct < 32.0 else "range_chop"))
        else:
            reg_d = "range_chop"
        sc_vq = score_ijcai2026_prism_vq_moe(day_slices, market_regime=reg_d)
        if sc_vq:
            signal_history["paper_ijcai2026_prism_vq_moe"][d] = sc_vq

        # Causal Fusion proxy & representative classic playbooks
        raw_fusion: dict[str, float] = {}
        raw_low_risk: dict[str, float] = {}
        raw_low_vol: dict[str, float] = {}
        raw_relay: dict[str, float] = {}
        for s in day_slices:
            if len(s.closes) < 25 or s.closes[-1] < 2.0 or s.closes[-21] <= 0:
                continue
            rets = [s.closes[i] / s.closes[i - 1] - 1.0 for i in range(-20, 0) if s.closes[i - 1] > 0]
            if len(rets) < 15:
                continue
            mu_r = sum(rets) / len(rets)
            vol20 = math.sqrt(sum((r - mu_r) ** 2 for r in rets) / (len(rets) - 1))
            mom20 = s.closes[-1] / s.closes[-21] - 1.0
            ret5 = s.closes[-1] / s.closes[-6] - 1.0 if s.closes[-6] > 0 else 0.0
            mean_v20 = sum(s.volumes[-20:]) / 20.0
            rvol_d = (s.volumes[-1] / mean_v20) if mean_v20 > 0 else 1.0

            raw_fusion[s.instrument_id] = 0.45 * (mom20 / max(vol20, 0.01)) - 0.35 * max(0.0, ret5 - 0.12) - 10.0 * vol20 + 0.1 * min(2.0, rvol_d)
            raw_low_vol[s.instrument_id] = -vol20
            raw_low_risk[s.instrument_id] = -vol20 - 0.5 * abs(s.closes[-1] / (sum(s.closes[-20:]) / 20.0) - 1.0)
            if (s.closes[-1] / s.closes[-2] - 1.0) >= 0.095 and 1.5 <= rvol_d <= 6.0 and s.highs[-1] > s.lows[-1]:
                raw_relay[s.instrument_id] = rvol_d

        for k_name, raw_d in (
            ("fusion_static40_safe", raw_fusion),
            ("pb:low_risk_combo", raw_low_risk),
            ("pb:low_volatility", raw_low_vol),
            ("pb:first_board_relay", raw_relay),
        ):
            ranked_d = _pct_map(raw_d)
            if ranked_d:
                signal_history[k_name][d] = ranked_d

    clean_sigs = {k: v for k, v in signal_history.items() if v}
    return panel, slices_by_day, clean_sigs


def cmd_run_arena(
    conn,
    *,
    rounds: int = 5,
    sessions: int = 120,
    max_stocks: int = 150,
    season_id: str = N.DEFAULT_SEASON_ID,
) -> dict[str, Any]:
    """Re-evaluate the 62-strategy catalog through real historical ``UnifiedBacktestEngine``
    (``GIPS-AShare-2026-v1``) + AlphaSAGE clustering + Swiss-System + Glicko-2 + RAM mixer."""
    N.seed_nexus_tables(conn)
    now = datetime.now(timezone.utc)
    cards = build_full_taxonomy_catalog()

    panel, _, signal_history = _load_historical_panel_and_signals(
        conn, sessions=sessions, max_stocks=max_stocks
    )
    n_sessions = len({b.date for bars in panel.values() for b in bars}) if panel else 0
    if panel and signal_history and n_sessions >= 25:
        res = run_swiss_glicko_season(
            cards,
            rounds=rounds,
            season_id=season_id,
            signal_history=signal_history,
            panel=panel,
        )
    else:
        res = run_swiss_glicko_season(cards, rounds=rounds, season_id=season_id)

    with conn.transaction():
        conn.execute(
            "INSERT INTO research.swiss_arena_season(season_id, benchmark_standard, rounds_completed,"
            " evaluation_basis, sessions_evaluated, pairings_json, correlation_json, evaluated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (season_id) DO UPDATE SET benchmark_standard=EXCLUDED.benchmark_standard,"
            " rounds_completed=EXCLUDED.rounds_completed, evaluation_basis=EXCLUDED.evaluation_basis,"
            " sessions_evaluated=EXCLUDED.sessions_evaluated, pairings_json=EXCLUDED.pairings_json,"
            " correlation_json=EXCLUDED.correlation_json, evaluated_at=EXCLUDED.evaluated_at",
            (
                season_id,
                DEFAULT_STANDARD.standard_id,
                int(res["rounds_completed"]),
                str(res["evaluation_basis"]),
                int(res["sessions_evaluated"]),
                Jsonb(res["recent_pairings"]),
                Jsonb(res.get("pairwise_corr") or {}),
                now,
            ),
        )
        for st in res["strategies"]:
            conn.execute(
                "INSERT INTO research.swiss_arena_rating(season_id, strategy_code, glicko_mu, glicko_phi, glicko_sigma,"
                " conservative_score, swiss_wins, swiss_draws, swiss_losses, net_return_pct, excess_return_pct,"
                " sharpe_ratio, max_drawdown_pct, nw_t_stat, worst_cvar_pct, rre_stability, pfs_robustness,"
                " evaluation_source, is_cluster_champion, lifecycle_tier, ensemble_weight, ram_floor_weight, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (season_id, strategy_code) DO UPDATE SET"
                " glicko_mu=EXCLUDED.glicko_mu, glicko_phi=EXCLUDED.glicko_phi, glicko_sigma=EXCLUDED.glicko_sigma,"
                " conservative_score=EXCLUDED.conservative_score, swiss_wins=EXCLUDED.swiss_wins,"
                " swiss_draws=EXCLUDED.swiss_draws, swiss_losses=EXCLUDED.swiss_losses,"
                " net_return_pct=EXCLUDED.net_return_pct, excess_return_pct=EXCLUDED.excess_return_pct,"
                " sharpe_ratio=EXCLUDED.sharpe_ratio, max_drawdown_pct=EXCLUDED.max_drawdown_pct,"
                " nw_t_stat=EXCLUDED.nw_t_stat, worst_cvar_pct=EXCLUDED.worst_cvar_pct,"
                " rre_stability=EXCLUDED.rre_stability, pfs_robustness=EXCLUDED.pfs_robustness,"
                " evaluation_source=EXCLUDED.evaluation_source, is_cluster_champion=EXCLUDED.is_cluster_champion,"
                " lifecycle_tier=EXCLUDED.lifecycle_tier, ensemble_weight=EXCLUDED.ensemble_weight,"
                " ram_floor_weight=EXCLUDED.ram_floor_weight, updated_at=EXCLUDED.updated_at",
                (
                    season_id,
                    st["strategy_code"],
                    Decimal(st["glicko_mu"]),
                    Decimal(st["glicko_phi"]),
                    Decimal(st["glicko_sigma"]),
                    Decimal(st["conservative_score"]),
                    int(st["swiss_wins"]),
                    int(st["swiss_draws"]),
                    int(st["swiss_losses"]),
                    _dec_or_none(st["net_return_pct"]),
                    _dec_or_none(st["excess_return_pct"]),
                    _dec_or_none(st["sharpe_ratio"]),
                    _dec_or_none(st["max_drawdown_pct"]),
                    _dec_or_none(st["nw_t_stat"]),
                    _dec_or_none(st["worst_cvar_pct"]),
                    _dec_or_none(st["rre_stability"]),
                    _dec_or_none(st["pfs_robustness"]),
                    st["evaluation_source"],
                    bool(st["is_cluster_champion"]),
                    st["lifecycle_tier"],
                    Decimal(st["ensemble_weight"]),
                    Decimal(st["ram_floor_weight"]),
                    now,
                ),
            )

    return {
        "ok": True,
        "season_id": season_id,
        "evaluation_basis": res["evaluation_basis"],
        "sessions_evaluated": res["sessions_evaluated"],
        "rounds_completed": res["rounds_completed"],
        "total_strategies": res["total_strategies"],
        "backtested_strategies": sum(1 for x in res["strategies"] if x["evaluation_source"] == "unified_backtest_engine"),
        "active_champions": sum(1 for x in res["strategies"] if float(x["ensemble_weight"]) > 0),
    }


def cmd_discover_strategies(
    conn,
    *,
    sessions: int = 120,
    max_stocks: int = 120,
) -> dict[str, Any]:
    """Run AlphaPROBE DAG depth-decay candidate discovery + 5-Gate screening over historical PIT slices."""
    N.seed_nexus_tables(conn)
    now = datetime.now(timezone.utc)
    _, slices_by_day, existing_signals = _load_historical_panel_and_signals(
        conn, sessions=sessions, max_stocks=max_stocks
    )
    if not slices_by_day:
        return {"ok": True, "candidates_evaluated": 0, "admitted_shadow": 0, "verdicts": {}}

    candidates = discover_alphaprobe_candidates(
        slices_by_day,
        existing_signals,
        hold_sessions=5,
        top_k=10,
        max_candidates=6,
    )

    admitted = 0
    verdict_counts: Counter[str] = Counter()
    with conn.transaction():
        for c in candidates:
            verdict_counts[c["gate_verdict"]] += 1
            conn.execute(
                "INSERT INTO research.strategy_discovery_candidate("
                " candidate_id, parent_code, dag_depth, display_name, formula_spec,"
                " max_corr_existing, rre_stability, pfs_robustness, net_sharpe, nw_t_stat,"
                " gate_verdict, evidence_json, discovered_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (candidate_id) DO UPDATE SET"
                " formula_spec=EXCLUDED.formula_spec, max_corr_existing=EXCLUDED.max_corr_existing,"
                " rre_stability=EXCLUDED.rre_stability, pfs_robustness=EXCLUDED.pfs_robustness,"
                " net_sharpe=EXCLUDED.net_sharpe, nw_t_stat=EXCLUDED.nw_t_stat,"
                " gate_verdict=EXCLUDED.gate_verdict, evidence_json=EXCLUDED.evidence_json,"
                " discovered_at=EXCLUDED.discovered_at",
                (
                    c["candidate_id"],
                    c["parent_code"],
                    int(c["dag_depth"]),
                    c["display_name"],
                    Jsonb(c["formula_spec"]),
                    _dec_or_none(c["max_corr_existing"]),
                    _dec_or_none(c["rre_stability"]),
                    _dec_or_none(c["pfs_robustness"]),
                    _dec_or_none(c["net_sharpe"]),
                    _dec_or_none(c["nw_t_stat"]),
                    c["gate_verdict"],
                    Jsonb(c["evidence_json"]),
                    now,
                ),
            )
            if c["gate_verdict"] == "admitted_shadow":
                admitted += 1
                conn.execute(
                    "INSERT INTO strategy.taxonomy_catalog("
                    " strategy_code, display_name, layer, horizon_bucket, paradigm,"
                    " portfolio_role, cluster_id, regime_fit_json, paper_citation, summary, updated_at)"
                    " VALUES (%s, %s, 'stock_selection', 'short_5d', 'top_paper_2026',"
                    " 'exploratory_shadow', %s, %s, %s, %s, %s)"
                    " ON CONFLICT (strategy_code) DO UPDATE SET display_name=EXCLUDED.display_name, updated_at=EXCLUDED.updated_at",
                    (
                        c["candidate_id"],
                        c["display_name"],
                        f"cluster_ap_{c['parent_code'] or 'root'}",
                        Jsonb(["bull_mainline", "structural_rotation", "range_chop"]),
                        "AlphaPROBE (arXiv 2026) + AlphaSAGE (ICLR 2026)",
                        f"AlphaPROBE DAG depth={c['dag_depth']} 5-Gate admitted shadow candidate (net_sharpe={c['net_sharpe']})",
                        now,
                    ),
                )

    return {
        "ok": True,
        "candidates_evaluated": len(candidates),
        "admitted_shadow": admitted,
        "verdicts": dict(verdict_counts),
    }


def cmd_audit_integrity(conn) -> dict[str, Any]:
    """Run live cross-module accounting parity, source normalizer & database health audit."""
    return N.get_integrity_audit(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 3.5 Nexus Intelligence, RAPTOR, Arena & Discovery Worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")

    p_poll = sub.add_parser("poll-intel")
    p_poll.add_argument("--offline", action="store_true", help="Cross-verify existing DB documents without live HTTP calls")

    sub.add_parser("realtime-adjust")

    p_arena = sub.add_parser("run-arena")
    p_arena.add_argument("--rounds", type=int, default=5)
    p_arena.add_argument("--sessions", type=int, default=120)
    p_arena.add_argument("--max-stocks", type=int, default=150)

    p_disc = sub.add_parser("discover-strategies")
    p_disc.add_argument("--sessions", type=int, default=120)
    p_disc.add_argument("--max-stocks", type=int, default=120)

    sub.add_parser("audit-integrity")

    args = parser.parse_args(argv)
    cfg = config.load()
    with db.connect(cfg.dsn, autocommit=True) as conn:
        if args.cmd == "seed":
            out = cmd_seed(conn)
        elif args.cmd == "poll-intel":
            out = cmd_poll_intel(conn, offline=args.offline)
        elif args.cmd == "realtime-adjust":
            out = cmd_realtime_adjust(conn)
        elif args.cmd == "run-arena":
            out = cmd_run_arena(conn, rounds=args.rounds, sessions=args.sessions, max_stocks=args.max_stocks)
        elif args.cmd == "discover-strategies":
            out = cmd_discover_strategies(conn, sessions=args.sessions, max_stocks=args.max_stocks)
        else:
            out = cmd_audit_integrity(conn)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
