"""Impact mapping: fact -> business variable -> exposed instruments -> horizon -> falsifier (spec FINAL §5, sources_sectors §5.1).

Why: "只有关键词命中'半导体'不足以推定所有半导体公司受益". Every impact relation says WHAT fact changes WHICH
business/valuation variable, WHICH named instruments are exposed, over WHICH horizon, and WHAT observation
would falsify it. The '+/-' is only an uncalibrated prior taken from keyword polarity; it carries no
probability and does not decide any ranking.

Invariants: ``exposed_instruments`` contains only instruments NAMED by the source record (vendor stock refs
resolved to existing instruments); a sector keyword produces a ``sector_hints`` entry with
``exposure_status = sector_hypothesis_unmapped`` and NO instruments; horizons and falsifiers come from the
versioned rule table ``impact-rules-v1`` (engineering defaults, not calibrated); competing explanations are
always listed (a single-cause story is never emitted). Not covered: supply-chain graph exposure.
"""
from __future__ import annotations

POLICY_VERSION = "impact-rules-v1"
SCHEMA_VERSION = "impact-mapping-v1"

# category -> (business variables, horizon (unit, from, to), falsifier observable, competing explanations)
_TABLE: dict[str, tuple[list[str], tuple[str, int, int], str, list[str]]] = {
    "company.earnings_guidance": (["reported_earnings", "earnings_expectation_gap"], ("quarters", 0, 2), "后续正式定期报告披露的净利润与该口径不一致", ["预期已计入价格", "一次性损益"]),
    "company.contract_order": (["revenue_backlog", "customer_concentration"], ("quarters", 1, 4), "订单未在后续定期报告或公告中确认履行/金额低于披露", ["订单金额相对营收占比很小", "框架协议不等于确定收入"]),
    "company.capital_action": (["share_supply_demand", "cash_return", "non_recurring_income"], ("days", 1, 60), "回购/增持/减持计划未按公告实施或实施规模明显偏离", ["计划区间上限而非实际实施量", "市场整体风格变化"]),
    "company.ma_governance": (["control_structure", "legal_liability", "asset_composition"], ("months", 1, 12), "交易未获批准/终止或诉讼金额与披露不符", ["并购整合风险", "估值支付方式"]),
    "policy.monetary": (["funding_cost", "liquidity"], ("weeks", 1, 8), "后续公开市场操作/利率走势与该政策方向不一致", ["预期已充分定价", "海外流动性变化"]),
    "policy.fiscal": (["demand_support", "sector_subsidy"], ("months", 1, 12), "财政资金下达规模与表述不符", ["地方配套不足"]),
    "policy.industry": (["industry_demand", "regulatory_environment"], ("months", 1, 12), "细则/资金/时间表未落地", ["政策表述与前期一致无新增量"]),
    "policy.market_regulation": (["trading_cost", "market_liquidity"], ("weeks", 1, 12), "规则正式文本或生效日期与报道不一致", ["规则影响范围有限"]),
    "macro.release": (["demand_growth", "inflation_path"], ("weeks", 1, 8), "数据修正或后续月份数据反向", ["基数效应", "季节性"]),
    "macro.rates_fx": (["discount_rate", "export_competitiveness"], ("weeks", 1, 12), "利率/汇率路径与该事件方向相反", ["风险偏好变化"]),
    "global.geopolitics": (["commodity_supply", "risk_appetite"], ("weeks", 1, 26), "冲突范围/制裁实际措施未发生", ["事件已被价格反映", "短期情绪脉冲"]),
    "global.trade_supply_restriction": (["export_access", "input_cost"], ("months", 1, 12), "措施未实施或被豁免", ["替代供应", "转口贸易"]),
    "global.cross_asset": (["risk_appetite", "commodity_price"], ("days", 1, 20), "海外资产价格反向修复", ["隔夜情绪未传导至A股"]),
    "industry.supply_demand": (["volume", "inventory_cycle"], ("quarters", 1, 4), "后续行业产销/库存数据与该判断相反", ["季节性", "统计口径差异"]),
    "industry.price_cost": (["product_price", "input_cost"], ("quarters", 1, 3), "价格回落或成本传导受阻", ["上下游传导方向相反(上游受益下游受损)", "套保安排"]),
    "industry.technology_capacity": (["capacity", "technology_lead"], ("quarters", 2, 8), "量产良率/产能爬坡低于披露", ["实验室成果不等于商业化"]),
    "market.index_passive_flow": (["passive_demand"], ("days", 1, 20), "指数基金实际调仓规模低于估计", ["提前交易已兑现"]),
    "market.microstructure": (["liquidity", "short_term_attention"], ("days", 1, 5), "成交与封单结构次日反向", ["一日游情绪"]),
    "event.disruption": (["supply_chain_continuity", "operating_interruption"], ("weeks", 1, 12), "停产/中断时长短于初报", ["保险赔付", "库存缓冲"]),
    "social.attention_sentiment": (["attention"], ("days", 1, 5), "关注度回落且未伴随成交变化", ["关注度不等于资金流入"]),
}
_PRIOR = {"+": "+", "-": "-", "mixed": "±", "none": "unknown"}


def map_impact(*, claim_id: str, claim_revision_id: str, category: str, headline: str, polarity_hint: str, subject_code: str | None,
               subject_instrument_id: str | None, sector_hints: list[str], event_time: str | None) -> dict:
    variables, (unit, lo, hi), falsifier, competing = _TABLE[category]
    named = subject_code is not None and subject_instrument_id is not None
    exposed = [{"instrument_id": subject_instrument_id, "code": subject_code, "exposure_basis": "named_subject_in_source_record"}] if named else []
    if named:
        status = "named_subject"
    elif sector_hints:
        status = "sector_hypothesis_unmapped"
    else:
        status = "none"
    return {
        "policy_version": POLICY_VERSION,
        "fact": {"claim_id": claim_id, "claim_revision_id": claim_revision_id, "category": category, "headline": headline, "event_time": event_time},
        "business_variables": [{"variable": v, "direction_prior": _PRIOR[polarity_hint], "direction_status": "uncalibrated_prior"} for v in variables],
        "exposure_status": status,
        "exposed_instruments": exposed,
        "sector_hints": [{"hint": h, "status": "keyword_hit_not_a_mapping"} for h in sector_hints],
        "horizon": {"unit": unit, "from": lo, "to": hi, "basis": "rule_table_default_uncalibrated"},
        "falsifier": {"observable": falsifier, "check_against": "official periodic report or later disclosure"},
        "competing_explanations": competing,
        "calibration": {"status": "uncalibrated", "probability": None},
    }
