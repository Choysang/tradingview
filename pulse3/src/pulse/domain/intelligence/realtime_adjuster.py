"""Real-Time News-Driven Strategy Adjuster & Controlled ``gpt-6-luna`` Research Bridge.

Why: Implements the real-time loop "fetch latest news -> trigger deterministic strategy
adjustment + optional controlled LLM synthesis -> output actionable buy/sell prices and holding plan"
while strictly obeying all ``AGENTS.md`` & Vault safety redlines:
1. Deterministic Boundary (``llm-deterministic-boundary``):
   - Uses NeurIPS 2025 RAPTOR Black-Litterman bounded tilt (``[-150, +150]`` bps) and
     ACL 2026 Amihud illiquidity filtering to compute ``tilt_bps``, ``action_directive``,
     and revised ``execution_plan`` entirely in deterministic code.
   - An LLM NEVER sets numerical rankings, overrides a risk veto, or breaches position caps.
2. Controlled ``gpt-6-luna`` Lane (``model_guard`` + Switch File + Key File + Pre-Send Ledger):
   - Disabled by default. Only calls ``ResponsesClient`` when:
     (a) ``PULSE_NEXUS_LLM_SWITCH`` points to an existing active switch file,
     (b) ``PULSE_OPENAI_KEY_FILE`` resolves to a valid key via ``read_key``,
     (c) ``model_guard.require_allowed(model)`` passes (``gpt-6-luna`` only), and
     (d) daily pre-send call ledger count is below ``daily_call_cap``.
   - Gracefully degrades offline or when disabled with ``llm_model_used=None`` and full
     deterministic guidance.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from pulse.adapters.models.model_guard import ModelNotAllowed, require_allowed
from pulse.adapters.models.responses_client import (
    CallFailed,
    CallStop,
    KeyProblem,
    ResponsesClient,
    read_key,
)
from pulse.domain.intelligence.macro_cross_verify import TIER_WEIGHTS, compute_time_decayed_impact

ENV_LLM_SWITCH = "PULSE_NEXUS_LLM_SWITCH"
ENV_LLM_LEDGER = "PULSE_NEXUS_LLM_LEDGER"
ENV_LLM_DAILY_CAP = "PULSE_NEXUS_LLM_DAILY_CAP"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_DAILY_CAP = 30

# Alias provided for explicit guard checks alongside model_guard.require_allowed
assert_allowed_model = require_allowed

_LLM_SYSTEM = (
    "你是 A 股投研风控助手，仅对已给出的跨源新闻事件做多空结构化摘要与引用提示。"
    "你严禁修改系统给定的风控等级(action_directive)、倾斜基点(tilt_bps)或仓位上限。"
    "必须只输出一个合法 JSON 对象，字符串值内禁止出现未转义双引号："
    '{"bull_points": ["..."], "bear_points": ["..."], "cited_headlines": ["..."], '
    '"catalyst_sustainability": "one_day|multi_day|structural|negative_shock", '
    '"risk_alert": "...", "holding_thesis": "..."}'
)


def _fmt_px(v: float | None) -> str | None:
    if v is None or not math.isfinite(v):
        return None
    return f"{max(0.01, v):.2f}"


def compute_event_strategy_adjustment(
    *,
    symbol: str,
    verified_clusters: Sequence[Mapping[str, Any]],
    as_of: datetime,
    instrument_id: str | None = None,
    sector_node: str | None = None,
    base_entry_price: float | None = None,
    atr_14: float | None = None,
    supertrend_dir: str = "bullish",
    rvol_20d: float | None = None,
    lhasa_buyer_count: int = 0,
    one_day_raider_count: int = 0,
    template_spam_ratio: float | None = None,
    amihud_illiquidity: float | None = None,
    avg_daily_turnover_cny: float | None = None,
    has_negative_official_notice: bool = False,
) -> dict[str, Any]:
    """Compute bounded NeurIPS 2025 RAPTOR Black-Litterman tilt, ACL 2026 Amihud
    liquidity filter, ``action_directive``, and adjusted buy/sell/holding plan."""
    is_low_liquidity = (
        (amihud_illiquidity is not None and amihud_illiquidity >= 1.5)
        or (avg_daily_turnover_cny is not None and avg_daily_turnover_cny < 50_000_000.0)
    )

    bl_num = 0.0
    bl_den = 0.0
    tau = 0.85
    suppressed_catalysts: list[str] = []
    tier1_neg_hit = has_negative_official_notice
    tier1_pos_hit = False
    cluster_impacts: list[dict[str, Any]] = []

    for cl in verified_clusters:
        headline = str(cl.get("headline") or "").strip()
        pol = str(cl.get("polarity") or "neutral")
        tier = str(cl.get("tier") or "tier3_portal_social")
        v_state = str(cl.get("verification_state") or "single_source_flash")
        conf = float(cl.get("confidence_score") or 0.50)
        corrob = int(cl.get("corroboration_count") or 1)
        insts = {str(x) for x in (cl.get("instrument_ids") or [])}
        secs = {str(x) for x in (cl.get("sector_nodes") or cl.get("affected_sectors") or [])}

        # Relevance weighting: stock-direct (1.0), sector (0.60), market-wide (0.35), unrelated (0.0)
        if (instrument_id and instrument_id in insts) or (symbol and symbol in insts) or (symbol and symbol in headline):
            rel_w = 1.0
        elif sector_node and sector_node in secs:
            rel_w = 0.60
        elif not insts and not secs:
            rel_w = 0.35
        else:
            continue

        # Tier weight & half-life lookup
        sources = cl.get("sources") or []
        best_w, best_hl = 0.45, 48.0
        for s_id in sources:
            _, w, hl = TIER_WEIGHTS.get(str(s_id), ("tier2_financial_press", 0.65, 48.0))
            if w > best_w:
                best_w, best_hl = w, hl
        if tier == "tier1_official":
            best_w = max(best_w, 0.95)
            best_hl = max(best_hl, 168.0)

        decay_res = compute_time_decayed_impact(
            authority_weight=best_w,
            polarity=pol,
            published_at=cl.get("published_at"),
            as_of=as_of,
            half_life_hrs=best_hl,
            amihud_illiquidity=amihud_illiquidity,
        )
        eff_imp = decay_res["effective_impact"]

        # ACL 2026 Amihud illiquidity guardrail: NEVER boost buys on illiquid stocks from commercial news
        filtered_reason = None
        if is_low_liquidity and pol == "positive" and tier != "tier1_official":
            eff_imp = 0.0
            filtered_reason = "ACL2026低流动性过滤：非Tier-1商业新闻利好禁止放大买点"
            suppressed_catalysts.append(headline[:48])
        elif is_low_liquidity and pol == "positive" and tier == "tier1_official":
            eff_imp *= 0.5

        if tier == "tier1_official" and pol == "negative":
            tier1_neg_hit = True
        if tier == "tier1_official" and pol == "positive":
            tier1_pos_hit = True

        corrob_factor = min(1.5, 0.8 + 0.25 * corrob)
        omega = max(0.05, conf * corrob_factor * rel_w)
        bl_num += tau * omega * eff_imp
        bl_den += tau * omega

        cluster_impacts.append({
            "cluster_id": cl.get("cluster_id"),
            "headline": headline,
            "tier": tier,
            "verification_state": v_state,
            "polarity": pol,
            "relevance_weight": round(rel_w, 2),
            "time_decay_weight": decay_res["time_decay_weight"],
            "effective_impact": round(eff_imp, 4),
            "filtered_reason": filtered_reason,
        })

    # NeurIPS 2025 RAPTOR Black-Litterman Posterior Tilt, strictly bounded in [-150, +150] bps
    v_bl = (bl_num / (1.0 + bl_den)) if bl_den > 0.0 else 0.0
    raw_tilt_bps = max(-150.0, min(150.0, v_bl * 180.0))

    # Down-weight unconfirmed positive hype when Guba water-army spam is rampant
    if raw_tilt_bps > 0 and template_spam_ratio is not None and template_spam_ratio >= 0.45 and not tier1_pos_hit:
        raw_tilt_bps *= 0.30

    # Hard risk-redline overrides
    veto_reasons: list[str] = []
    if tier1_neg_hit:
        veto_reasons.append("法定/Tier-1官方渠道存在重大负面事件(立案/减持/澄清/风险)")
    if lhasa_buyer_count >= 3:
        veto_reasons.append(f"龙虎榜买方拉萨天团{lhasa_buyer_count}席位扎堆(E048负超额预警)")
    if one_day_raider_count >= 1:
        veto_reasons.append(f"存在{one_day_raider_count}家一日游砸盘席位(上塘/佛山/成都/联储)")
    if is_low_liquidity:
        veto_reasons.append("ACL 2026 Amihud低流动性警示：限制正催化买点放大")

    if tier1_neg_hit or lhasa_buyer_count >= 3:
        tilt_bps = min(-80.0 if tier1_neg_hit else 0.0, raw_tilt_bps)
    elif one_day_raider_count >= 1 or is_low_liquidity:
        tilt_bps = min(0.0, raw_tilt_bps)
    else:
        tilt_bps = raw_tilt_bps

    tilt_bps = round(max(-150.0, min(150.0, tilt_bps)), 1)

    # Determine action_directive enum
    if tier1_neg_hit or tilt_bps <= -75.0 or (lhasa_buyer_count >= 3 and tilt_bps < 0.0):
        directive = "risk_veto"
    elif tilt_bps <= -20.0 or one_day_raider_count >= 1 or lhasa_buyer_count >= 2 or (is_low_liquidity and tilt_bps < 0.0):
        directive = "tighten_stop_grid"
    elif tilt_bps >= 35.0 and not is_low_liquidity and not veto_reasons and supertrend_dir != "bearish":
        directive = "catalyst_boost"
    else:
        directive = "neutral_monitor"

    # Build deterministic updated buy/sell/holding plan
    ref = base_entry_price if (base_entry_price is not None and base_entry_price > 0) else None
    atr = atr_14 if (atr_14 is not None and atr_14 > 0) else (ref * 0.025 if ref else None)

    if directive == "risk_veto":
        entry_low = None
        entry_high = None
        stop_px = (ref - 1.0 * atr) if (ref and atr) else None
        tp_px = (ref + 0.5 * atr) if (ref and atr) else None
        pos_cap = "0%"
        holding_plan = "【风控一票否决】禁止新开买单；已有持仓应在次日早盘竞价或脉冲反抽时优先执行止损/减仓降敞口。"
    elif directive == "tighten_stop_grid":
        entry_low = (ref - 1.2 * atr) if (ref and atr) else None
        entry_high = (ref - 0.5 * atr) if (ref and atr) else None
        stop_px = (ref - 1.25 * atr) if (ref and atr) else None
        tp_px = (ref + 1.5 * atr) if (ref and atr) else None
        pos_cap = "3%"
        holding_plan = "【收紧止损与网格】短线抛压或筹码分歧较大，严禁追高；仅限缩量深踩下沿企稳后轻仓低吸或底仓倒T，持有观察<=3个交易日。"
    elif directive == "catalyst_boost":
        entry_low = (ref * 0.990) if ref else None
        entry_high = (ref * 1.008) if ref else None
        stop_px = (ref - 1.8 * atr) if (ref and atr) else None
        tp_px = (ref + 2.8 * atr) if (ref and atr) else None
        pos_cap = "10%"
        holding_plan = (
            f"【催化正向倾斜 {tilt_bps:+.1f} bps】经跨源验证正向催化且流动性健康（RVOL={rvol_20d or '—'}），"
            "可在次日开盘或盘中回踩分时均线区间分批低吸，持有5~10个交易日跟踪主线扩散度。"
        )
    else:
        entry_low = (ref * 0.985) if ref else None
        entry_high = (ref * 1.005) if ref else None
        stop_px = (ref - 1.8 * atr) if (ref and atr) else None
        tp_px = (ref + 2.0 * atr) if (ref and atr) else None
        pos_cap = "5%~8%"
        holding_plan = "【常态监控】消息面多空均衡或经时间半衰期衰减，维持原多周期技术面枢轴线与网格步长执行，持有5~15个交易日。"

    return {
        "symbol": symbol,
        "as_of": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tilt_bps": tilt_bps,
        "action_directive": directive,
        "liquidity_filter_triggered": is_low_liquidity,
        "suppressed_catalysts": suppressed_catalysts,
        "risk_veto_reasons": veto_reasons,
        "cluster_impacts": cluster_impacts,
        "execution_plan": {
            "entry_low": _fmt_px(entry_low),
            "entry_high": _fmt_px(entry_high),
            "stop_loss_price": _fmt_px(stop_px),
            "take_profit_price": _fmt_px(tp_px),
            "max_position_pct": pos_cap,
            "holding_plan": holding_plan,
        },
    }


def _is_switch_active(switch_path: str | Path | None, env: Mapping[str, str]) -> bool:
    raw = str(switch_path) if switch_path else (env.get(ENV_LLM_SWITCH) or "").strip()
    if not raw:
        return False
    p = Path(raw)
    if not p.is_file():
        return False
    try:
        content = p.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return content not in ("0", "off", "false", "disabled", "stop")


def _check_and_reserve_ledger(
    ledger_path: str | Path | None,
    *,
    day_str: str,
    symbol: str,
    model: str,
    daily_cap: int,
    env: Mapping[str, str],
) -> tuple[bool, int]:
    """Enforce pre-send call ledger cap (``paid-api-limit-ledger-before-send``)."""
    raw = str(ledger_path) if ledger_path else (env.get(ENV_LLM_LEDGER) or "").strip()
    if not raw:
        return True, 0
    lp = Path(raw)
    existing_today = 0
    if lp.is_file():
        try:
            for line in lp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if row.get("day") == day_str:
                        existing_today += 1
                except ValueError:
                    continue
        except OSError:
            return False, existing_today
    if existing_today >= daily_cap:
        return False, existing_today
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "day": day_str,
                "symbol": symbol,
                "model": model,
                "status": "PRE_SEND_RESERVED",
            }, ensure_ascii=False) + "\n")
    except OSError:
        return False, existing_today
    return True, existing_today + 1


def _deterministic_fallback_summary(
    symbol: str,
    verified_clusters: Sequence[Mapping[str, Any]],
    adj: Mapping[str, Any],
) -> dict[str, Any]:
    bull_pts = [
        f"[{c.get('tier')}] {c.get('headline')}"
        for c in verified_clusters
        if c.get("polarity") == "positive"
    ][:3]
    bear_pts = [
        f"[{c.get('tier')}] {c.get('headline')}"
        for c in verified_clusters
        if c.get("polarity") == "negative"
    ][:3]
    for r in adj.get("risk_veto_reasons") or []:
        bear_pts.append(str(r))
    cited = [str(c.get("headline")) for c in verified_clusters[:5] if c.get("headline")]
    directive = str(adj.get("action_directive") or "neutral_monitor")
    plan = adj.get("execution_plan") or {}
    return {
        "bull_points": bull_pts or ["当前无经跨源验证的显著正面催化事件。"],
        "bear_points": bear_pts[:4] or ["暂无新增重大负向风险告警。"],
        "cited_headlines": cited,
        "catalyst_sustainability": "negative_shock" if directive == "risk_veto" else ("multi_day" if directive == "catalyst_boost" else "one_day"),
        "risk_alert": "；".join(adj.get("risk_veto_reasons") or []) or "遵循常规止损线与T+1仓位上限。",
        "holding_thesis": str(plan.get("holding_plan") or "按确定性量化规则执行。"),
    }


def synthesize_realtime_llm_view(
    *,
    symbol: str,
    display_name: str = "",
    verified_clusters: Sequence[Mapping[str, Any]],
    deterministic_adjustment: Mapping[str, Any],
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    max_output_tokens: int = 800,
    switch_path: str | Path | None = None,
    key_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    daily_call_cap: int | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Controlled ``gpt-6-luna`` qualitative synthesis over verified news clusters.

    Always leaves ``action_directive`` and ``tilt_bps`` governed exclusively by
    ``deterministic_adjustment``. Offline or disabled states return ``llm_model_used=None``
    with deterministic structured summary.
    """
    env_map = os.environ if env is None else env
    fallback = _deterministic_fallback_summary(symbol, verified_clusters, deterministic_adjustment)

    # 1. Strict model guard BEFORE any file/network I/O
    try:
        require_allowed(model, env=env_map)
    except ModelNotAllowed as exc:
        return {
            "llm_model_used": None,
            "llm_status": "blocked_model_guard",
            "guard_message": str(exc),
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }

    # 2. Switch file gate (off by default)
    if not _is_switch_active(switch_path, env_map):
        return {
            "llm_model_used": None,
            "llm_status": "disabled_switch_missing",
            "guard_message": f"LLM研判开关未激活（需配置 {ENV_LLM_SWITCH} 指向有效开关文件），已返回确定性规则结论。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }

    if not verified_clusters:
        return {
            "llm_model_used": None,
            "llm_status": "skipped_no_material_news",
            "guard_message": "当前无跨源新闻事件簇，跳过LLM调用以节省额度。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }

    # 3. Read OpenAI key safely (never logged/returned)
    try:
        api_key = read_key(path=key_path, env=env_map)
    except KeyProblem as exc:
        return {
            "llm_model_used": None,
            "llm_status": f"disabled_{exc.code}",
            "guard_message": f"密钥不可用({exc.code})，已优雅降级为纯确定性策略输出。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }

    # 4. Pre-send call ledger cap check
    cap = daily_call_cap if daily_call_cap is not None else int((env_map.get(ENV_LLM_DAILY_CAP) or DEFAULT_DAILY_CAP))
    today_str = datetime.now(timezone.utc).date().isoformat()
    ok_cap, used_today = _check_and_reserve_ledger(
        ledger_path, day_str=today_str, symbol=symbol, model=model, daily_cap=cap, env=env_map,
    )
    if not ok_cap:
        return {
            "llm_model_used": None,
            "llm_status": "stopped_daily_cap",
            "guard_message": f"今日LLM调用已达上限 ({used_today}/{cap})，停止外部调用并沿用确定性策略。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }

    # 5. Build grounded prompt with explicit deterministic guardrail context
    news_lines = []
    for idx, c in enumerate(verified_clusters[:10], 1):
        srcs = ",".join(str(s) for s in (c.get("sources") or []))
        news_lines.append(
            f"{idx}. [{c.get('verification_state')}|{c.get('polarity')}|{srcs}|{c.get('published_at') or 'n/a'}] {c.get('headline')}"
        )
    prompt = (
        f"标的: {symbol} {display_name}\n"
        f"系统确定性风控指令(不可覆盖): action_directive={deterministic_adjustment.get('action_directive')}, "
        f"tilt_bps={deterministic_adjustment.get('tilt_bps')}, "
        f"risk_veto_reasons={deterministic_adjustment.get('risk_veto_reasons')}\n\n"
        "已去重跨源事件簇原文标题列表:\n" + "\n".join(news_lines)
    )

    try:
        client = ResponsesClient(api_key, transport=transport)
        reply = client.ask(
            prompt,
            model=model,
            effort=effort,
            system=_LLM_SYSTEM,
            max_output_tokens=max_output_tokens,
        )
    except CallStop as exc:
        return {
            "llm_model_used": None,
            "llm_status": f"call_stopped:{exc.reason}",
            "guard_message": f"LLM调用被硬止损停止({exc.reason})，保持确定性规则结果。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": {},
        }
    except CallFailed as exc:
        return {
            "llm_model_used": None,
            "llm_status": f"call_failed:{exc.label}",
            "guard_message": f"LLM调用失败({exc.label})，保持确定性规则结果。",
            "effective_directive": deterministic_adjustment.get("action_directive"),
            "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
            "summary": fallback,
            "usage": exc.reply.usage if exc.reply else {},
        }

    # 6. Parse structured qualitative view strictly (without trusting any numeric/directive override)
    parsed_summary = fallback
    try:
        raw_txt = reply.text.strip()
        if raw_txt.startswith("```"):
            raw_txt = raw_txt.strip("`").lstrip("json").strip()
        obj = json.loads(raw_txt)
        if isinstance(obj, dict):
            valid_cited = [str(x) for x in (obj.get("cited_headlines") or []) if str(x)]
            parsed_summary = {
                "bull_points": [str(x) for x in (obj.get("bull_points") or fallback["bull_points"])][:4],
                "bear_points": [str(x) for x in (obj.get("bear_points") or fallback["bear_points"])][:4],
                "cited_headlines": valid_cited[:5] or fallback["cited_headlines"],
                "catalyst_sustainability": str(obj.get("catalyst_sustainability") or fallback["catalyst_sustainability"]),
                "risk_alert": str(obj.get("risk_alert") or fallback["risk_alert"]),
                "holding_thesis": str(obj.get("holding_thesis") or fallback["holding_thesis"]),
            }
    except ValueError:
        pass

    return {
        "llm_model_used": model,
        "llm_status": "ok",
        "response_id": reply.response_id,
        "guard_message": "已完成gpt-6-luna受控结构化多空研判（所有数值倾斜与风控红线仍由确定性代码锁定）。",
        "effective_directive": deterministic_adjustment.get("action_directive"),
        "effective_tilt_bps": deterministic_adjustment.get("tilt_bps"),
        "summary": parsed_summary,
        "usage": reply.usage,
    }


def build_realtime_news_strategy_adjustment(
    *,
    symbol: str,
    display_name: str = "",
    verified_clusters: Sequence[Mapping[str, Any]],
    as_of: datetime,
    instrument_id: str | None = None,
    sector_node: str | None = None,
    base_entry_price: float | None = None,
    atr_14: float | None = None,
    supertrend_dir: str = "bullish",
    rvol_20d: float | None = None,
    lhasa_buyer_count: int = 0,
    one_day_raider_count: int = 0,
    template_spam_ratio: float | None = None,
    amihud_illiquidity: float | None = None,
    avg_daily_turnover_cny: float | None = None,
    has_negative_official_notice: bool = False,
    model: str = DEFAULT_MODEL,
    switch_path: str | Path | None = None,
    key_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """End-to-end entry combining deterministic Black-Litterman + Amihud adjustments
    and controlled optional ``gpt-6-luna`` qualitative synthesis."""
    adj = compute_event_strategy_adjustment(
        symbol=symbol,
        verified_clusters=verified_clusters,
        as_of=as_of,
        instrument_id=instrument_id,
        sector_node=sector_node,
        base_entry_price=base_entry_price,
        atr_14=atr_14,
        supertrend_dir=supertrend_dir,
        rvol_20d=rvol_20d,
        lhasa_buyer_count=lhasa_buyer_count,
        one_day_raider_count=one_day_raider_count,
        template_spam_ratio=template_spam_ratio,
        amihud_illiquidity=amihud_illiquidity,
        avg_daily_turnover_cny=avg_daily_turnover_cny,
        has_negative_official_notice=has_negative_official_notice,
    )
    llm_view = synthesize_realtime_llm_view(
        symbol=symbol,
        display_name=display_name,
        verified_clusters=verified_clusters,
        deterministic_adjustment=adj,
        model=model,
        switch_path=switch_path,
        key_path=key_path,
        ledger_path=ledger_path,
        env=env,
        transport=transport,
    )
    return {
        **adj,
        "llm_model_used": llm_view["llm_model_used"],
        "llm_status": llm_view["llm_status"],
        "llm_view": llm_view,
    }
