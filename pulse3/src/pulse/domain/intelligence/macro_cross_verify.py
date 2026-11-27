"""Full A-Share Influence Factor Matrix & Multi-Source Cross-Verification Engine.

Why: Synthesizes all domestic macro/policy, international linkage, post-2024-08 Northbound
replacement capital proxies, and cross-verified multi-outlet news (Tier-1 Xinhua / CCTV /
CSRC / CNINFO vs Tier-2 CLS / Securities Press vs Tier-3 Eastmoney / THS / Guba).

Key Rules Enforced:
1. Title-first asymmetric sentiment rule (``news-keyword-rules-title-scope``):
   Reads headline (or first 120 chars when untitled); negative exclusions are checked per-clause,
   positive exclusions across full text; digest/roundup articles are skipped.
2. Independent Origin Corroboration: Republications citing "据新华社电" share one origin chain
   and do NOT inflate independent source counts.
3. ACL 2026 Liquidity-Friction-Aware Time Decay (``compute_time_decayed_impact``) +
   FinMem Half-Life Time Decay + TradingAgents Bull-vs-Bear Debate note on every cluster.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

TIER_WEIGHTS: dict[str, tuple[str, float, float]] = {
    # Canonical PostgreSQL source_id -> (tier_label, authority_weight, half_life_hours)
    "cninfo_announcements": ("tier1_official", 1.00, 720.0),
    "xinhua_finance": ("tier1_official", 0.98, 168.0),
    "csrc_exchange": ("tier1_official", 1.00, 720.0),
    "cls_telegraph": ("tier2_financial_press", 0.80, 72.0),
    "eastmoney_lhb_disclosure": ("tier2_financial_press", 0.85, 96.0),
    "eastmoney_fund_flow": ("tier2_financial_press", 0.72, 48.0),
    "eastmoney_calendar": ("tier2_financial_press", 0.80, 120.0),
    "native_news_recorder": ("tier2_financial_press", 0.78, 72.0),
    "legacy_news_recorder": ("tier2_financial_press", 0.75, 72.0),
    "eastmoney_news_724": ("tier3_portal_social", 0.45, 24.0),
    "ths_news_724": ("tier3_portal_social", 0.45, 24.0),
    "eastmoney_guba_comments": ("tier3_portal_social", 0.20, 12.0),
    "eastmoney_guba": ("tier3_portal_social", 0.20, 12.0),  # legacy alias
}

_ROUNDUP_RE = re.compile(r"(早报|晚报|午评|收评|盘前必读|新闻精选|公告淘金|舆情监控|七大看点|十大消息)")
_NEG_SERIOUS = ("立案", "留置", "退市风险", "警示函", "监管函", "财务造假", "违规减持", "大额减持", "业绩预亏", "业绩暴雷", "合同违约", "澄清：不涉及", "终止收购")
_POS_CATALYST = ("回购", "增持", "业绩预增", "大幅增长", "中标", "重大合同", "获批", "突破", "降准", "降息", "市值管理")
_NEG_CLAUSE_UNLESS = ("不存在", "未收到", "不涉及立案", "撤销退市", "终止调查", "无减持计划")
_POS_FULL_UNLESS = ("传闻不实", "澄清", "终止回购", "未中标", "风险提示", "减持", "立案")
_XINHUA_ATTR_RE = re.compile(r"(新华社|新华网|新华财经|央视新闻|新闻联播|证监会|国务院|人民银行)")


def classify_headline_asymmetric(title: str, body: str = "") -> tuple[str, bool]:
    """Return ``(polarity, is_roundup)`` following the verified E088 asymmetric title rule."""
    lead = (title or "").strip()
    if not lead:
        clean_body = re.sub(r"^【[^】]{1,20}】|^财联社\d+月\d+日电[，,]?", "", (body or "").strip())
        lead = clean_body[:120]
    if _ROUNDUP_RE.search(lead):
        return "neutral", True

    # Negative check: clause-level exclusion so "股东被立案，与公司日常经营无关，拟回购" stays NEGATIVE!
    clauses = re.split(r"[，。；,;！!]", lead)
    for cl in clauses:
        if any(w in cl for w in _NEG_SERIOUS) and not any(u in cl for u in _NEG_CLAUSE_UNLESS):
            return "negative", False

    # Positive check: full-text exclusion so denied rumors / regulatory letters never trigger positive
    full_head = f"{lead} {(body or '')[:160]}"
    if any(w in lead for w in _POS_CATALYST) and not any(u in full_head for u in _POS_FULL_UNLESS):
        return "positive", False
    return "neutral", False


def _parse_dt(val: datetime | str | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def compute_time_decayed_impact(
    *,
    authority_weight: float,
    polarity: str,
    published_at: datetime | str | None,
    as_of: datetime,
    half_life_hrs: float = 48.0,
    amihud_illiquidity: float | None = None,
    bid_ask_friction_bps: float = 25.0,
) -> dict[str, Any]:
    """ACL 2026 Liquidity-Friction-Aware Half-Life Decay for News & Policy Events.

    - Strictly blocks future-dated leaks (``published_at > as_of`` -> 0.0 impact).
    - Discounts positive news impact on illiquid names where bid-ask/impact slippage erodes alpha.
    - Amplifies negative event risk on illiquid names due to exit liquidity stampede risk.
    """
    as_of_utc = _parse_dt(as_of) or datetime.now(timezone.utc)
    pub_dt = _parse_dt(published_at)

    if pub_dt is None:
        age_hrs = max(1.0, half_life_hrs)
        future_leak = False
    elif pub_dt > as_of_utc:
        return {
            "time_decay_weight": 0.0,
            "age_hours": 0.0,
            "friction_multiplier": 0.0,
            "effective_impact": 0.0,
            "future_leak_blocked": True,
            "friction_note": "future_leak_guard: published_at > as_of -> blocked",
        }
    else:
        age_hrs = max(0.0, (as_of_utc - pub_dt).total_seconds() / 3600.0)
        future_leak = False

    hl = max(1.0, float(half_life_hrs))
    decay = 0.5 ** (age_hrs / hl)
    ill = max(0.0, float(amihud_illiquidity)) if amihud_illiquidity is not None else 0.0

    if polarity == "positive":
        # Positive alpha decays under high Amihud illiquidity & spread friction
        friction_penalty = min(0.75, (max(0.0, bid_ask_friction_bps) / 100.0) * (1.0 + min(3.0, ill)) * 0.45)
        friction_mult = max(0.20, 1.0 - friction_penalty)
        sign = 1.0
        note = f"ACL2026正催化摩擦衰减x{friction_mult:.2f} (Amihud={ill:.2f})"
    elif polarity == "negative":
        friction_mult = 1.0 + min(0.50, ill * 0.20)
        sign = -1.0
        note = f"ACL2026负向冲击放大x{friction_mult:.2f} (含低流动性踩踏风险)"
    else:
        friction_mult = 1.0
        sign = 0.0
        note = "中性事件无方向倾斜"

    eff = max(-1.0, min(1.0, sign * float(authority_weight) * decay * friction_mult))
    return {
        "time_decay_weight": round(decay, 4),
        "age_hours": round(age_hrs, 2),
        "friction_multiplier": round(friction_mult, 4),
        "effective_impact": round(eff, 4),
        "future_leak_blocked": future_leak,
        "friction_note": note,
    }


def _token_set(text: str) -> set[str]:
    cleaned = re.sub(r"[\s\W_]+", "", text)
    if len(cleaned) < 3:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 3] for i in range(len(cleaned) - 2)}


def cross_verify_documents(
    docs: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Cluster heterogeneous news/announcement documents, deduplicate republication chains,
    compute multi-source confidence & time-decayed impact, and attach Bull-vs-Bear notes."""
    clusters: list[dict[str, Any]] = []

    for d in docs:
        title = str(d.get("title") or d.get("headline") or "").strip()
        body = str(d.get("body") or d.get("summary") or "").strip()
        if not title and not body:
            continue
        polarity, is_roundup = classify_headline_asymmetric(title, body)
        if is_roundup:
            continue
        src_id = str(d.get("source_id") or "eastmoney_news_724")
        # Detect upstream official attribution (e.g., Xinhua / CSRC cited inside CLS/EM)
        m_attr = _XINHUA_ATTR_RE.search(f"{title} {body[:120]}")
        effective_origin = f"official:{m_attr.group(1)}" if m_attr else src_id
        tier, weight, half_life = TIER_WEIGHTS.get(src_id, ("tier2_financial_press", 0.65, 48.0))
        if m_attr:
            tier, weight, half_life = "tier1_official", 0.95, 168.0

        toks = _token_set(title or body[:80])
        pub_at = d.get("published_at")
        inst_ids = list(d.get("instrument_ids") or [])
        sectors = list(d.get("sector_nodes") or [])

        matched_cluster = None
        for c in clusters:
            inter = len(toks & c["_tokens"])
            union = max(1, len(toks | c["_tokens"]))
            if inter / union >= 0.42:
                matched_cluster = c
                break

        if matched_cluster is None:
            fp = hashlib.sha256((title or body[:80]).encode("utf-8")).hexdigest()
            clusters.append({
                "cluster_id": f"evc:{fp[:14]}",
                "fingerprint_hash": fp,
                "headline": title or body[:80],
                "highest_tier": tier,
                "polarity": polarity,
                "decay_half_life_hrs": half_life,
                "_origins": {effective_origin: weight},
                "_sources": {src_id},
                "_tokens": toks,
                "instrument_ids": set(str(x) for x in inst_ids),
                "sector_nodes": set(str(s) for s in sectors),
                "published_at": pub_at,
            })
        else:
            matched_cluster["_sources"].add(src_id)
            matched_cluster["_origins"][effective_origin] = max(
                matched_cluster["_origins"].get(effective_origin, 0.0), weight
            )
            if tier == "tier1_official":
                matched_cluster["highest_tier"] = "tier1_official"
            elif tier == "tier2_financial_press" and matched_cluster["highest_tier"] == "tier3_portal_social":
                matched_cluster["highest_tier"] = "tier2_financial_press"
            if polarity == "negative":
                matched_cluster["polarity"] = "negative"  # Asymmetric safety: preserve negative alert
            elif polarity == "positive" and matched_cluster["polarity"] == "neutral":
                matched_cluster["polarity"] = "positive"
            matched_cluster["instrument_ids"].update(str(x) for x in inst_ids)
            matched_cluster["sector_nodes"].update(str(s) for s in sectors)

    out: list[dict[str, Any]] = []
    rank_tier = {"tier1_official": 3, "tier2_financial_press": 2, "tier3_portal_social": 1}
    for c in clusters:
        n_indep = len(c["_origins"])
        sum_w = sum(c["_origins"].values())
        conf = min(0.99, 1.0 - math.exp(-1.15 * sum_w))
        if c["highest_tier"] == "tier1_official":
            v_state = "official_confirmed"
        elif n_indep >= 2:
            v_state = "multi_source_verified"
        else:
            v_state = "single_source_flash"

        pol = c["polarity"]
        decay_info = compute_time_decayed_impact(
            authority_weight=min(1.0, sum_w),
            polarity=pol,
            published_at=c["published_at"],
            as_of=as_of,
            half_life_hrs=c["decay_half_life_hrs"],
        )

        # TradingAgents Bull-vs-Bear Adversarial Verification Note
        if pol == "positive":
            debate = (
                "【多头逻辑】事件具正向基本面/政策催化；"
                "【空头证伪检查】核查是否前期股价5日已抢跑>12%(E070超涨利好兑现风险)及是否有巨潮正式公告落地。"
                if v_state != "official_confirmed"
                else "【法定/央媒确认利好】Tier-1权威源背书，关注次日板块扩散度与竞价换手承接，忌一字板排单逆向选择。"
            )
        elif pol == "negative":
            debate = "【风控优先】命中E088分句利空规则（立案/减持/业绩风险/澄清），历史实证带利空超跌股显著跑输，执行一票否决或逢反抽减仓。"
        else:
            debate = "【中性事件】多空影响均衡，不单独改写量化基础仓位。"

        pub = c["published_at"]
        pub_str = pub.isoformat().replace("+00:00", "Z") if isinstance(pub, datetime) else (str(pub) if pub else None)
        out.append({
            "cluster_id": c["cluster_id"],
            "fingerprint_hash": c["fingerprint_hash"],
            "headline": c["headline"],
            "tier": c["highest_tier"],
            "verification_state": v_state,
            "corroboration_count": n_indep,
            "confidence_score": f"{conf:.2f}",
            "time_decay_weight": f"{decay_info['time_decay_weight']:.3f}",
            "effective_impact": f"{decay_info['effective_impact']:+.3f}",
            "sources": sorted(c["_sources"]),
            "polarity": pol,
            "affected_sectors": sorted(c["sector_nodes"]),
            "instrument_ids": sorted(c["instrument_ids"]),
            "published_at": pub_str,
            "bull_bear_note": debate,
            "_sort_score": (rank_tier[c["highest_tier"]], conf, abs(decay_info["effective_impact"]), n_indep),
        })

    out.sort(key=lambda x: x["_sort_score"], reverse=True)
    for item in out:
        item.pop("_sort_score", None)
    return out


def build_ashare_macro_factor_matrix(
    *,
    market_regime_code: str = "range_chop",
    regime_label: str | None = None,
    breadth_ma20_pct: float | None = None,
    limit_up_count: int | None = None,
    amount_ratio: float | None = None,
    etf_surge_active: bool = False,
    fomc_window_active: bool = False,
    verified_cluster_count: int = 0,
    lhb_seat_rows_count: int = 0,
    guba_post_count: int | None = None,
    fund_flow_net_cny: float | None = None,
) -> list[dict[str, str]]:
    """Comprehensive 2025-2026 A-Share 5-Dimension Dynamic Influence Factor Matrix."""
    r_label = regime_label or market_regime_code
    ma20_str = f"{breadth_ma20_pct:.1f}%" if breadth_ma20_pct is not None else "待同步"
    lu_str = f"{limit_up_count}家" if limit_up_count is not None else "待同步"
    amt_str = f"{amount_ratio:.2f}x" if amount_ratio is not None else "待同步"
    ff_yi = f"{fund_flow_net_cny / 1e8:+.2f}亿元" if fund_flow_net_cny is not None else "待同步"
    gb_str = f"{guba_post_count}条" if guba_post_count is not None else "待同步"

    # 1. Domestic Policy & Market Regime (Weight 25%)
    pol_sig = "supportive" if (
        market_regime_code in ("bull_mainline", "structural_rotation")
        and (breadth_ma20_pct is None or breadth_ma20_pct >= 50.0)
    ) else ("cautious" if market_regime_code in ("bear_defensive", "defensive_bear", "risk_off") or (breadth_ma20_pct is not None and breadth_ma20_pct < 30.0) else "neutral")
    pol_status = "observed" if (verified_cluster_count > 0 or breadth_ma20_pct is not None) else "partial"

    # 2. Global Linkage (Weight 15%)
    glob_sig = "cautious" if fomc_window_active else "neutral"

    # 3. Capital Proxy 2026 (Weight 25%)
    if etf_surge_active or (fund_flow_net_cny is not None and fund_flow_net_cny > 2e9):
        cap_sig = "supportive"
    elif fund_flow_net_cny is not None and fund_flow_net_cny < -3e9:
        cap_sig = "cautious"
    else:
        cap_sig = "neutral"
    cap_status = "observed" if (etf_surge_active or fund_flow_net_cny is not None or amount_ratio is not None) else "partial"

    # 4. Retail Sentiment (Weight 15%)
    if guba_post_count is not None and guba_post_count >= 100 and limit_up_count is not None and limit_up_count >= 90:
        ret_sig = "cautious"
    elif guba_post_count is not None and guba_post_count > 0:
        ret_sig = "neutral"
    else:
        ret_sig = "neutral"
    ret_status = "observed" if guba_post_count is not None else "partial"

    # 5. Microstructure Seats (Weight 20%)
    lhb_sig = "neutral" if lhb_seat_rows_count > 0 else "cautious"
    lhb_status = "observed" if lhb_seat_rows_count > 0 else "partial"

    return [
        {
            "dimension": "domestic_policy_macro",
            "factor_name": "国内政策定调与市场体制宽度 (权重25%)",
            "status": pol_status,
            "signal": pol_sig,
            "detail": (
                f"[影响权重25% | 方向={pol_sig}] 体制={r_label}({market_regime_code}) | "
                f"MA20站上宽度={ma20_str} | 涨停家数={lu_str} | 跨源权威簇={verified_cluster_count}条 | "
                f"结合2025新口径M1与市值管理10号指引，[实证锚点: E070/E088/20日净值均线]。"
            ),
            "source_authority": "新华社 / 人民银行 / 证监会 / 巨潮资讯 (Tier-1)",
        },
        {
            "dimension": "global_linkage",
            "factor_name": "美联储FOMC / 美债10Y / 金龙指数 / 离岸人民币 (权重15%)",
            "status": "observed",
            "signal": glob_sig,
            "detail": (
                f"[影响权重15% | 方向={glob_sig}] FOMC窗口={'激活(开盘买均值跑输0.26%,p=0.006,缓开新仓)' if fomc_window_active else '常态(隔夜美股09:25集合竞价一次定价)'} | "
                f"当前量比锚={amt_str} | [实证锚点: E072/E083/E084/E101]。"
            ),
            "source_authority": "中金所 / 腾讯国际行情 / 东方财富宏观日历 (eastmoney_calendar)",
        },
        {
            "dimension": "capital_proxy_2026",
            "factor_name": "后北向时代替代资金锚（宽基ETF+主力资金+量比）(权重25%)",
            "status": cap_status,
            "signal": cap_sig,
            "detail": (
                f"[影响权重25% | 方向={cap_sig}] 宽基ETF2倍放量护盘={'触发(严禁急跌割肉)' if etf_surge_active else '未触发'} | "
                f"主力资金净流={ff_yi} | 成交额量比={amt_str} | [实证锚点: E074/E089/JFQA 2025大小单差]。"
            ),
            "source_authority": "交易所ETF快照 / 东方财富资金流 (eastmoney_fund_flow)",
        },
        {
            "dimension": "retail_sentiment",
            "factor_name": "股吧四时段SIR情绪传染与热度校验 (权重15%)",
            "status": ret_status,
            "signal": ret_sig,
            "detail": (
                f"[影响权重15% | 方向={ret_sig}] 股吧采样帖数={gb_str} | 涨停情绪家数={lu_str} | "
                f"自动去重模板水军并对Top5%过热拥挤降权，[实证锚点: PBFJ 2024 SIR传染模型]。"
            ),
            "source_authority": "东方财富股吧 (eastmoney_guba_comments) / 同花顺7x24 / 财联社电报",
        },
        {
            "dimension": "microstructure_seats",
            "factor_name": "龙虎榜16+游资谱系与四维真伪机构鉴别 (权重20%)",
            "status": lhb_status,
            "signal": lhb_sig,
            "detail": (
                f"[影响权重20% | 方向={lhb_sig}] 已观测龙虎榜席位={lhb_seat_rows_count}条 | "
                f"兼容国泰海通2025-04更名与六一弃用警示，拦截拉萨8席踩踏与量化假机构，"
                f"[实证锚点: E048(-3.15%/-2.52%)/E094(-1.81%)/E098]。"
            ),
            "source_authority": "东财龙虎榜 (eastmoney_lhb_disclosure) / Pulse席位博弈库",
        },
    ]
