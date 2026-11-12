"""News alert tiers P0/P1/P2 on top of the I3 claim pipeline (old site E075 semantics, new evidence model).

Why: the old alert process judged one message against holdings and the watch list. In pulse3 the unit of verification
is the CLAIM, so a tier is decided per claim revision from what the pipeline already stores (category, polarity hint,
named subjects of the impact mapping, sector hints, verification status); no second judgement of truth is made here.
  P0  negative claim that names an instrument the user HOLDS
  P1  negative policy/macro/industry claim whose industries contain a holding (and which names no holding)
  P2  any claim naming a WATCH-LIST instrument, or a positive claim naming a main-board instrument not held
Invariants: pure and deterministic; a tier needs a NAMED subject (P0/P2) or a keyword->industry mapping from the
versioned table ``alert-industry-map-v1`` (P1) - a sector keyword alone never names a company; the verification status
is carried into the evidence (an ``unverified`` claim still alerts, but says so); the dedup key contains the content
version so a status change is a new, superseding intent while a re-scan of the same content is a no-op.
Not covered: LLM fallback judgement (the optional model port of I3 stays upstream), delivery.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

POLICY_VERSION = "news-tier-v1"
INDUSTRY_MAP_VERSION = "alert-industry-map-v1"
NEGATIVE, POSITIVE = "-", "+"
MACRO_PREFIXES = ("policy.", "macro.", "industry.", "global.trade_supply_restriction", "global.geopolitics")
MAX_PUBLICATION_LAG_SECONDS = 2 * 3600       # an item published long before we learned of it is a backfill, not breaking news
ALERT_LIFETIME_SECONDS = 24 * 3600

# keyword in headline/sector hint -> display names of the legacy_boards_industry taxonomy (the only industry map we hold)
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "房地产": ("房地产",), "楼市": ("房地产",), "地产": ("房地产",), "房贷": ("房地产", "金融行业"), "限购": ("房地产",),
    "半导体": ("电子器件",), "芯片": ("电子器件",), "集成电路": ("电子器件",), "光刻": ("电子器件",),
    "银行": ("金融行业",), "券商": ("金融行业",), "证券": ("金融行业",), "保险": ("金融行业",), "降息": ("金融行业", "房地产"),
    "降准": ("金融行业", "房地产"), "印花税": ("金融行业",),
    "汽车": ("汽车制造",), "新能源车": ("汽车制造",), "新能源汽车": ("汽车制造",),
    "光伏": ("电力行业", "发电设备"), "储能": ("电力行业", "发电设备"), "电力": ("电力行业",),
    "煤炭": ("煤炭行业",), "原油": ("石油行业",), "油价": ("石油行业",), "石油": ("石油行业",),
    "钢铁": ("钢铁行业",), "有色": ("有色金属",), "黄金": ("有色金属",), "稀土": ("有色金属",),
    "医药": ("生物制药",), "集采": ("生物制药", "医疗器械"), "带量采购": ("生物制药", "医疗器械"), "医疗器械": ("医疗器械",),
    "白酒": ("酿酒行业",), "食品": ("食品行业",), "猪肉": ("农林牧渔",), "生猪": ("农林牧渔",), "农业": ("农林牧渔",),
    "家电": ("家电行业",), "关税": ("物资外贸", "交通运输", "纺织行业", "服装鞋类"), "航运": ("交通运输",), "航空": ("交通运输", "飞机制造"),
    "旅游": ("酒店旅游",), "传媒": ("传媒娱乐",), "游戏": ("传媒娱乐",), "AI": ("电子信息",), "算力": ("电子信息",), "软件": ("电子信息",),
    "化工": ("化工行业",), "水泥": ("水泥行业",), "建材": ("建筑建材",), "基建": ("建筑建材", "公路桥梁"), "造船": ("船舶制造",),
}


@dataclass(frozen=True)
class ClaimView:
    claim_id: str
    claim_revision_id: str
    revision: int
    headline: str
    category: str | None
    polarity: str                       # '+', '-', 'mixed', 'none'
    claim_status: str
    assertion_type: str
    available_at: str                   # ISO instant (when WE learned it)
    event_time: str | None
    published_lag_seconds: float | None     # available_at - publication time of the earliest evidence; None = unknown
    instrument_ids: tuple[str, ...] = ()    # named subjects of the impact mapping
    sector_hints: tuple[str, ...] = ()
    cluster_seed: str | None = None


@dataclass(frozen=True)
class Context:
    holdings: frozenset[str]
    watch: frozenset[str]
    main_board: frozenset[str]                          # instruments on the SH/SZ main board
    holding_industries: dict[str, frozenset[str]]       # board display name -> held instrument ids in it


@dataclass(frozen=True)
class TierHit:
    tier: str
    reason: str
    instrument_ids: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    held_in_industry: tuple[str, ...] = ()
    trigger: dict = field(default_factory=dict)


def industries_for(headline: str, sector_hints: Iterable[str] = ()) -> tuple[str, ...]:
    """Board names an item may concern, from the versioned keyword table. Order stable, duplicates dropped."""
    text = headline + " " + " ".join(sector_hints)
    out: list[str] = []
    for key in sorted(INDUSTRY_KEYWORDS, key=lambda k: (-len(k), k)):
        if key in text:
            out.extend(INDUSTRY_KEYWORDS[key])
    return tuple(dict.fromkeys(out))


def is_backfill(claim: ClaimView) -> bool:
    """History re-collected late must not raise 'just happened' alerts (PEX13 acceptance)."""
    lag = claim.published_lag_seconds
    return lag is not None and lag > MAX_PUBLICATION_LAG_SECONDS


def decide(claim: ClaimView, ctx: Context) -> list[TierHit]:
    """Tiers of one claim for one user. A claim can hit several tiers (P0 on a holding and P2 on a watched peer)."""
    if is_backfill(claim):
        return []
    hits: list[TierHit] = []
    named = set(claim.instrument_ids)
    held = named & ctx.holdings
    if claim.polarity == NEGATIVE and held:
        hits.append(TierHit("P0", "negative claim names a holding", tuple(sorted(held)), trigger={"category": claim.category}))
    macro = claim.category is not None and claim.category.startswith(MACRO_PREFIXES)
    if claim.polarity == NEGATIVE and macro and not held:
        inds = industries_for(claim.headline, claim.sector_hints)
        touched = {i: sorted(ctx.holding_industries[i]) for i in inds if ctx.holding_industries.get(i)}
        if touched:
            hits.append(TierHit("P1", "negative policy/macro claim in an industry that contains a holding", (), tuple(touched),
                                tuple(sorted({h for hs in touched.values() for h in hs})),
                                {"category": claim.category, "industry_map": INDUSTRY_MAP_VERSION}))
    watched = (named & ctx.watch) - held
    if watched:
        hits.append(TierHit("P2", "claim names a watch-list instrument", tuple(sorted(watched)), trigger={"watch": True}))
    elif claim.polarity == POSITIVE and (named - ctx.holdings) & ctx.main_board:
        hits.append(TierHit("P2", "positive claim names a main-board instrument that is not held",
                            tuple(sorted((named - ctx.holdings) & ctx.main_board)), trigger={"positive": True}))
    return hits


def content_version(claim: ClaimView) -> str:
    """What a reader would notice changing: status, polarity, category, headline. Revisions that only add documents keep it."""
    raw = "|".join((claim.claim_status, claim.polarity, claim.category or "", claim.headline))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dedup_key(tier: str, claim: ClaimView, target: str) -> str:
    return f"{POLICY_VERSION}:{tier}:{claim.claim_id}:{target}:{content_version(claim)}"


def evidence_body(hit: TierHit, claim: ClaimView, *, rule_id: str, names: dict[str, dict] | None = None) -> dict:
    """Immutable body of the alert's evidence artifact (contract ``pulse.ext_c.alert_evidence@1``)."""
    names = names or {}
    return {
        "tier": hit.tier, "headline": claim.headline, "category": claim.category,
        "direction": "negative" if claim.polarity == NEGATIVE else ("positive" if claim.polarity == POSITIVE else "unclear"),
        "reason": hit.reason, "policy_version": POLICY_VERSION, "rule_id": rule_id,
        "instruments": [{"instrument_id": i, **names.get(i, {"code": None, "name": None})} for i in hit.instrument_ids],
        "industries": list(hit.industries), "held_in_industry": list(hit.held_in_industry),
        "claim_status": claim.claim_status, "assertion_type": claim.assertion_type,
        "verification_note": ("unverified: no official disclosure and no two independent origins yet"
                              if claim.claim_status == "unverified" else claim.claim_status),
        "source_claim_revision_ids": [claim.claim_revision_id], "claim_id": claim.claim_id,
        "available_at": claim.available_at, "event_time": claim.event_time, "trigger": hit.trigger,
        "already_reacted": None,          # price reaction since publication is not computed (no intraday source): stays null
    }
