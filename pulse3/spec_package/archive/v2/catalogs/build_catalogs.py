"""Build the supplied design catalogs; no network calls or live integration.

Input: the research reference list at a caller-supplied path.
Output: JSON catalogs next to this script. All candidates remain unvalidated.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
AS_OF = "2026-11-10"

FACTOR_FAMILIES = [
    ("F01", "国内流动性与财政"), ("F02", "国内宏观"),
    ("F03", "市场制度"), ("F04", "海外金融条件"),
    ("F05", "国际产业映射"), ("F06", "地缘与贸易"),
    ("F07", "产业与技术"), ("F08", "商品与库存"),
    ("F09", "公司经营与股权"), ("F10", "公司治理与监管"),
    ("F11", "资金与筹码"), ("F12", "A股情绪"),
    ("F13", "技术与量价"), ("F14", "订单微观结构"),
    ("F15", "日历与注意力"), ("F16", "叙事与突发"),
]

# slug, name, scope, clock, fields, output unit, definition, source refs, proposed feed
# Definitions are design contracts, not executable expressions or proven alpha.
FACTOR_ROWS = [
    ("policy_rate_surprise", "国内政策利率预期差", "market", "event_available", "policy.instrument,policy.rate,policy.prior_rate,consensus.value,consensus.known_at", "basis_points", "当期首次公布政策利率减公告前冻结的同工具预期；工具期限变化另建版本", [], "人民银行政策公告及事前一致预期数据"),
    ("reserve_requirement_change", "存款准备金率变动", "market,sector", "event_available", "policy.institution_scope,policy.rrr_before,policy.rrr_after,policy.effective_from", "basis_points", "按机构范围计算降准幅度及生效日，不将口头预告视为已经实施", [], "人民银行原文及附件"),
    ("omo_net_injection", "公开市场净投放", "market", "event_available", "omo.operation_id,omo.tenor,omo.amount,omo.maturity_date,omo.rate", "CNY", "按实际结算日汇总投放减到期，保留期限结构和政策工具定义版本", [], "人民银行公开市场操作公告"),
    ("credit_impulse", "信用脉冲", "market,sector", "event_available", "macro.tsf_flow,macro.gdp,macro.release_version", "ratio", "以当时已发布社融和GDP构造滚动信用扩张变化，分母和修订版本必须可回放", [], "人民银行及国家统计局时点数据"),
    ("fiscal_impulse", "财政支出与发债脉冲", "market,sector", "event_available", "fiscal.spend,fiscal.revenue,bond.issuance,bond.maturity,budget.version", "ratio", "分别计算支出增速和国债地方债净发行，不把预算目标与实际支出相加", ["A28"], "财政部财政收支与发行公告"),
    ("tax_subsidy_exposure", "税费补贴政策暴露", "sector,instrument", "event_available", "policy.tax_or_subsidy,policy.scope,issuer.revenue_exposure,policy.effective_from", "score", "将政策适用产品与公司当时可知业务暴露映射，补贴目标与实际补贴分开", ["A28", "A29"], "财政部发改委及公司业务分部"),

    ("gdp_surprise", "GDP首发预期差", "market,sector", "event_available", "macro.gdp_first_release,macro.period,consensus.value,consensus.known_at", "percentage_points", "实际首次公布增速减事前冻结预期；修订形成独立revision而不覆盖历史", ["A12"], "统计局新闻首发和合法一致预期数据"),
    ("inflation_surprise", "CPI与PPI首发预期差", "market,sector", "event_available", "macro.cpi,macro.ppi,macro.period,consensus.value,consensus.known_at", "percentage_points", "分别计算消费端与生产端通胀预期差，保留同比环比口径", ["A12"], "统计局CPI及PPI发布"),
    ("pmi_breadth", "PMI及分项扩散", "market,sector", "event_available", "macro.pmi,macro.new_orders,macro.production,macro.inventory", "index_points", "分开记录PMI水平、变化及订单库存差，避免跨机构口径混合", ["A12"], "统计局PMI首发"),
    ("domestic_demand", "内需与工业增长", "market,sector", "event_available", "macro.retail_sales,macro.industrial_output,macro.fixed_investment,macro.period", "percentage_points", "按首次发布值计算需求与生产的历史标准化变化；累计和单月值独立", ["A12"], "统计局国民经济运行新闻稿"),
    ("property_cycle", "房地产销售投资周期", "sector,instrument", "event_available", "macro.property_sales,macro.property_investment,macro.new_starts,issuer.exposure", "score", "结合当时已知销售投资开工变化映射地产链；城市周频样本与全国月频不直接拼接", ["A12"], "统计局及有覆盖定义的房地产数据"),
    ("trade_growth", "进出口与目的地需求", "sector,instrument", "event_available", "trade.exports,trade.imports,trade.currency,trade.destination,issuer.export_share", "ratio", "按币种和目的地分解贸易增长，再按公司历史业务暴露聚合", [], "海关总署及公司分部披露"),

    ("market_rule_version", "交易制度有效版本", "instrument,account", "session_open", "rules.exchange,rules.board,rules.security_status,rules.effective_from,rules.version", "category", "按交易日和证券状态解析MarketRules；2026-07-06前后规则独立", ["A01", "A02", "A03", "A04", "A05", "A06"], "沪深交易所正式规则及业务参数"),
    ("equity_supply", "IPO与再融资供给", "market,sector", "event_available", "offer.type,offer.amount,offer.status,offer.subscription_date,offer.list_date", "CNY", "按已公开进度构建未来供给日历和实际发行强度，不将所有申请视为已发行", ["A24"], "交易所审核公告及公司融资公告"),
    ("reduction_rule_change", "减持回购制度变更", "market,sector", "event_available", "regulation.rule_id,regulation.changed_clause,regulation.scope,regulation.effective_from", "category", "规则变更形成版本化事件并映射受影响持股结构，不把征求意见当生效", ["A01", "A03"], "证监会及交易所规则"),
    ("margin_eligibility", "两融资格与保证金变化", "instrument", "event_available", "margin.eligible,margin.ratio,margin.effective_from,margin.source_version", "category", "记录证券入选移出和保证金变化；资格可用性与资金净流入是不同特征", ["A04"], "交易所两融标的及业务公告"),
    ("index_membership", "指数成分变更暴露", "instrument", "event_available", "index.announcement,index.effective_date,index.old_members,index.new_members,index.weight", "ratio", "按公布时点识别纳入剔除及权重变化，公告前不得使用未来成分", [], "指数公司调样公告及历史成分"),
    ("calendar_session_status", "交易会话与可交易状态", "instrument,account", "session_open", "session.kind,session.exchange,session.status,instrument.halt_status,rules.version", "category", "解析竞价午休盘后固定价及临时停牌状态，适用时段限制作为行动条件", ["A01", "A04", "A05", "A06"], "交易所日历及实时状态"),

    ("foreign_rate_surprise", "海外央行决议预期差", "market,sector", "event_available", "central_bank.rate,central_bank.guidance,consensus.value,consensus.known_at", "basis_points", "分别编码利率决议与前瞻指引变化，预期必须来自发布前快照", ["A13"], "Fed ECB BOJ BOE官方公告与事前预期"),
    ("us_macro_surprise", "美国通胀就业预期差", "market,sector", "event_available", "macro.cpi,macro.payrolls,macro.unemployment,consensus.value,macro.vintage", "score", "首次公布值相对冻结预期标准化，修订值独立；公告首发与API到达时间分开", ["A14", "A15", "A16", "A17", "A30"], "BLS发布和FRED ALFRED历史版本"),
    ("global_yield_curve", "海外利率与曲线变化", "market,sector", "bar_close", "bond.yield_2y,bond.yield_10y,bond.currency,bond.timestamp", "basis_points", "计算水平和期限利差变化；日频官方统计不冒充盘中可交易报价", ["A13", "A15"], "官方利率统计及授权债券行情"),
    ("fx_exposure", "人民币与美元汇率暴露", "market,sector,instrument", "bar_close", "fx.pair,fx.price,fx.session,issuer.currency_revenue,issuer.currency_cost", "ratio", "分开CNY CNH与美元指数，按净业务暴露推断方向并保留证据", ["A13", "A15"], "授权外汇报价及公司分部数据"),
    ("global_risk_appetite", "全球波动与风险偏好", "market,sector", "bar_close", "global.vol_index,global.equity_returns,global.credit_spread,global.session", "score", "按实际已结束海外会话计算多资产状态，不前移尚未收盘的全球指数", [], "授权全球指数波动率与利差数据"),
    ("offshore_china_signal", "境外中国资产映射", "market,sector", "bar_close", "global.a50_futures,global.hsi,global.hstech,global.china_adr,global.session", "ratio", "对已可得海外中国资产收益按会话对齐，开盘跳空和开盘后收益分别评估", [], "授权A50港股及ADR行情"),

    ("foreign_earnings_surprise", "海外龙头业绩与指引", "sector,instrument", "event_available", "foreign_issuer.earnings,foreign_issuer.guidance,consensus.value,relationship.edge_version", "score", "将已公开海外业绩预期差映射至有证据的A股客户供应商关系", [], "海外公司IR SEC披露及供应链证据"),
    ("foreign_capex_signal", "海外资本开支及订单", "sector,instrument", "event_available", "foreign_issuer.capex,foreign_issuer.orders,relationship.product,relationship.edge_version", "score", "区分资本开支承诺实际支出和订单，结合对应产品暴露与交付期", [], "海外IR和公司产业链披露"),
    ("foreign_product_launch", "海外产品技术发布", "sector,instrument", "event_available", "product.spec,product.release_state,product.volume_plan,relationship.edge_version", "category", "保留正式发布测试传闻量产状态，评估替代与受益方向而非只打利好标签", [], "公司发布会产品页及监管批准"),
    ("supply_chain_exposure", "供应链证据权重", "instrument", "event_available", "relationship.counterparty,relationship.type,relationship.evidence,relationship.effective_from,issuer.revenue_share", "ratio_or_unknown", "只对有可追溯证据关系赋权；无收入占比时输出未知而不猜数", ["A24"], "公司年报公告及海外客户公开资料"),
    ("global_peer_relative_strength", "海外同行相对强度", "sector,instrument", "bar_close", "foreign_peer.returns,benchmark.returns,relationship.peer_set_version", "ratio", "用事前登记同行篮子计算相对收益，剔除尚未结束交易会话", [], "授权海外行情及行业分类"),
    ("supply_substitution", "供给替代与国产化线索", "sector,instrument", "event_available", "supply.restriction,supply.alternative,product.qualification,relationship.evidence", "score", "将限制事件与已证实可供货替代品相连；概念相同不足以确认替代能力", ["A29"], "监管文件产品认证及公司披露"),

    ("tariff_exposure", "关税调整暴露", "sector,instrument", "event_available", "tariff.hs_code,tariff.country,tariff.rate_before,tariff.rate_after,issuer.trade_exposure", "basis_points", "以商品税号与目的地映射税率变化，公布与实施日期分开", ["A28"], "关税税则委员会及海关公开信息"),
    ("export_control_exposure", "出口管制与制裁暴露", "sector,instrument", "event_available", "restriction.entity,restriction.product,restriction.scope,restriction.effective_from,relationship.edge_version", "score", "依据正式实体和产品清单识别受影响订单与替代链，保留豁免细则", [], "商务部及海外主管机关原文"),
    ("conflict_exposure", "冲突与地域经营暴露", "sector,instrument", "event_available", "geopolitics.location,geopolitics.event_type,issuer.facility_location,issuer.sales_region", "score", "把核实事件与经营地点和运输路线关联，信息发现与事实确认分开", ["A20"], "官方消息及国际新闻原文"),
    ("shipping_disruption", "航道与物流中断", "sector,instrument", "event_available", "route.status,route.location,freight.rate,issuer.route_exposure", "score", "按受影响航线和恢复进度估计成本与交付方向，不预设航运和出口企业同向", ["A20"], "航运运营方港口公告及运价数据"),
    ("trade_negotiation_state", "贸易谈判状态变化", "market,sector", "event_available", "negotiation.parties,negotiation.stage,negotiation.signed_text,negotiation.effective_from", "category", "传闻接触框架正式协议实施分别建事件阶段，修订保留历史", ["A28", "A20"], "政府官方发布与协议文本"),
    ("cross_border_policy_surprise", "跨境政策预期兑现", "market,sector", "event_available", "policy.announced_terms,expectation.frozen_terms,market.prior_move,evidence.ids", "score", "对比正式条款与事前冻结预期及已发生价格反应，模型判断保留版本", ["A28", "A13"], "官方政策原文与事前证据快照"),

    ("industrial_policy_strength", "产业政策覆盖与兑现", "sector,instrument", "event_available", "policy.funding,policy.scope,policy.stage,policy.timeline,issuer.exposure", "score", "区分规划目标资金安排申报实施兑现，以量化条款和业务映射构建特征", ["A29", "A28"], "产业主管部门正式政策"),
    ("tender_award_intensity", "招标中标强度", "sector,instrument", "event_available", "tender.budget,tender.winner,tender.contract_amount,issuer.trailing_revenue", "ratio", "已公告合同额相对当时已发布收入；联合体金额按明确份额，无份额则未知", ["A24"], "招标平台及公司合同公告"),
    ("regulatory_product_approval", "产品注册与监管批准", "sector,instrument", "event_available", "approval.product,approval.stage,approval.agency,issuer.economic_rights", "category", "注册受理试验进展批准上市分别标记并关联实际经济权益", [], "药监等主管机构和公司公告"),
    ("standard_change", "行业标准变化", "sector,instrument", "event_available", "standard.version,standard.required_specs,standard.effective_from,issuer.product_specs", "score", "比较新旧标准和公司产品适配证据，草案与正式生效分开", ["A29"], "行业主管部门标准文本"),
    ("technology_commercialization", "技术商业化阶段", "sector,instrument", "event_available", "technology.claim,technology.validation,technology.production,technology.orders,evidence.ids", "category", "论文演示试产量产和收入实现分别编码，不能以技术宣称替代销售", [], "原始论文产品测试与公司披露"),
    ("industry_price_competition", "产业价格战与提价", "sector,instrument", "event_available", "product.price,product.spec,product.volume,issuer.cost_exposure,industry.capacity", "ratio", "比较同规格产品价格变化和成本暴露，价格下降对上下游分别估计", ["A29"], "厂家价目及授权产业报价"),

    ("energy_return", "能源价格变化", "sector,instrument", "bar_close", "commodity.contract,commodity.price,commodity.currency,issuer.energy_exposure", "ratio", "原油天然气煤分别建连续合约及现货序列，换月规则固定且可回放", ["A18"], "EIA及授权能源期现货报价"),
    ("metal_return", "金属与锂价格变化", "sector,instrument", "bar_close", "commodity.product,commodity.price,commodity.contract,issuer.metal_exposure", "ratio", "金铜铝锂等各自标准化，期货现货不能直接拼接为同一价格", [], "交易所及有规格说明的现货报价"),
    ("inventory_surprise", "商品库存预期差", "sector,instrument", "event_available", "inventory.first_release,inventory.period,consensus.value,consensus.known_at", "physical_units", "实际库存变化减事前预期，原始首发与晚到API分别保存available_at", ["A18", "A19"], "EIA库存首发与对应历史数据"),
    ("commodity_curve", "商品期限结构", "sector,instrument", "bar_close", "futures.near_price,futures.far_price,futures.expiry,futures.contract_id", "annualized_ratio", "同商品近远月价差按期限归一，交易日和临近到期换月显式处理", [], "授权商品期货行情"),
    ("production_capacity", "产能开停与利用率", "sector,instrument", "event_available", "industry.capacity,industry.utilization,facility.status,issuer.facility_share", "ratio", "按可确认的投产检修停产与产量变动构造供给变化，名义产能与实际产量分开", ["A12", "A29"], "官方工业能源统计及公司公告"),
    ("input_output_spread", "产业投入产出价差", "sector,instrument", "bar_close", "product.output_price,input.prices,process.fixed_coefficients,issuer.product_mix", "CNY_per_unit", "用预登记工艺系数计算产品原料价差，缺系数时保持行业代理标签", [], "产品原料报价与工艺证据"),

    ("earnings_surprise", "公司盈利预期差", "instrument", "event_available", "financial.first_profit,financial.period,consensus.value,consensus.known_at", "ratio", "首次公布盈利相对冻结预期或已登记历史基线标准化，不用后修订一致预期", ["A24", "T07"], "公司公告及有时点的一致预期"),
    ("earnings_revision", "业绩预告修正", "instrument", "event_available", "forecast.old_low,forecast.old_high,forecast.new_low,forecast.new_high,forecast.revision", "ratio", "保留预告区间上下界变化和修正原因，所有版本按真实发布时点可见", ["A24", "T07"], "业绩预告及修正公告"),
    ("order_revenue_ratio", "订单与收入比", "instrument", "event_available", "contract.verified_value,contract.share,contract.period,financial.trailing_revenue", "ratio", "公司可归属合同额除当时已发布收入，框架协议与确定订单分开", ["A24"], "公司合同公告及财务披露"),
    ("buyback_progress", "回购计划与实施进度", "instrument", "event_available", "buyback.announced_low,buyback.announced_high,buyback.executed,buyback.end_date", "ratio", "计划强度实施进度与实际回购金额分别登记，不将计划上限当已买入", ["A24"], "回购方案和进展公告"),
    ("shareholder_supply", "减持与解禁供给", "instrument", "event_available", "shareholder.reduction_plan,shareholder.executed,unlock.shares,shares.float", "ratio", "分别计算可解禁份额计划减持和实际减持，状态依据已公开文件", ["A24"], "解禁减持及股本公告"),
    ("fundamental_quality_value", "质量价值与分红向量", "instrument", "event_available", "financial.earnings,financial.cashflow,financial.equity,financial.debt,dividend.cash,price.raw,shares.total", "feature_vector", "按可用财报计算估值盈利成长现金流负债和股息率；非正分母输出缺值或独立类别", ["A24", "T07", "T19"], "时点财报公司行动与行情"),

    ("management_change", "管理层变动", "instrument", "event_available", "management.role,management.change_type,management.reason,management.effective_from", "category", "记录职位和变化原因，公告未披露的动机不得由模型补成事实", ["A24"], "公司董事监事高管公告"),
    ("litigation_exposure", "诉讼金额与进展", "instrument", "event_available", "litigation.amount,litigation.stage,litigation.claim_type,financial.net_assets", "ratio", "已披露涉诉金额相对净资产及进展分别记录，判决与预期赔付区分", ["A24"], "公司及司法公开信息"),
    ("audit_opinion", "审计意见与会计修正", "instrument", "event_available", "audit.opinion,audit.period,accounting.restatement,audit.report_revision", "category", "审计意见会计差错和重述形成版本化事件，历史财务版本不可覆盖", ["A24", "T07"], "审计报告及财报更正"),
    ("regulatory_inquiry", "问询调查处罚事件", "instrument", "event_available", "regulator.event_type,regulator.status,regulator.response_due,regulator.document_id", "category", "问询立案处罚结案各自状态，事实和模型解释独立", ["A24"], "交易所问询及监管原文"),
    ("security_status_change", "风险警示停复牌状态", "instrument", "event_available", "instrument.status_before,instrument.status_after,instrument.effective_from,rules.version", "category", "证券状态改变后重新解析交易规则及股票池；公布时间与生效时间分开", ["A01", "A03", "A04", "A24"], "交易所业务状态及公司公告"),
    ("disclosure_correction", "公告澄清与撤回", "instrument,event", "event_available", "document.replaces_id,document.claim_status,document.reason,event.cluster_id", "category", "澄清否认纠错撤回指向原始claim并触发后续影响重评，保留原决策", ["A24"], "公司澄清更正公告"),

    ("etf_share_change", "ETF份额变化", "market,sector", "event_available", "etf.shares,etf.nav,etf.share_date,etf.holdings_version", "ratio", "计算披露份额变化并按历史成分映射，不用成交额代替净申购或推断投资者身份", [], "基金公司和交易所份额及组合披露"),
    ("institutional_holdings", "机构持仓披露变化", "instrument", "event_available", "holder.position,holder.report_period,holder.published_at,shares.float", "ratio", "季报年报持仓只在公开后可用，报告截止日不是系统获知日", ["A24"], "基金和公司持仓披露"),
    ("margin_balance_change", "融资融券余额变化", "instrument,market", "event_available", "margin.balance,margin.purchase,margin.repayment,margin.date", "ratio", "按公开日终口径算融资变化，不能把未发布当日日终数用于盘中", ["A04"], "交易所日度两融数据"),
    ("dragon_tiger_exposure", "龙虎榜席位暴露", "instrument", "event_available", "disclosure.reason,seat.buy_value,seat.sell_value,disclosure.published_at", "CNY", "按公开披露原因和席位计算净额；席位类型不等于已识别最终投资者", ["A04"], "交易所盘后公开交易信息"),
    ("block_trade_discount", "大宗交易折溢价", "instrument", "event_available", "block.price,block.quantity,block.trade_date,reference.price,block.published_at", "ratio", "盘后公布大宗价格对同日参考价的折溢价与数量，发布时间后才可用于决策", ["A04"], "交易所大宗交易披露"),
    ("northbound_disclosed_holdings", "北向公开持仓变化", "instrument,market", "event_available", "northbound.holdings,northbound.report_quarter,northbound.available_at,shares.float", "ratio", "仅对实际公布的季度持仓计算变化；不构造官方实时净流入伪字段", ["A07", "A08"], "HKEX季度北向持仓公开数据"),

    ("limit_up_breadth", "涨跌停与市场广度", "market,sector", "bar_close", "quote.price,quote.limit_up,quote.limit_down,instrument.status,universe.version", "ratio", "以当日真实涨跌停价和时点股票池计算广度，停牌与无价格限制状态独立", ["A01", "A04"], "行情价格限制字段及证券主数据"),
    ("consecutive_limit_ladder", "连板梯队与高度", "market,sector,instrument", "bar_close", "bar.close,bar.limit_up,instrument.status,calendar.previous_session", "count", "依据可得收盘涨停状态计连板并追踪梯队，每次状态按当时可见数据建立", ["A01", "A04"], "日线与市场规则"),
    ("board_promotion_rate", "连板晋级率", "market,sector", "bar_close", "prior.ladder_members,current.limit_up_status,universe.version", "ratio", "昨日各高度股票中今日符合预先定义晋级条件比例，分母按昨日可知集合冻结", ["A01", "A04"], "历史连板梯队与行情"),
    ("break_reseal_rate", "炸板与回封结构", "market,sector,instrument", "quote", "trade.timestamp,quote.limit_up,quote.best_bid,quote.best_ask,orderbook.depth", "ratio", "依次识别触板封板开板回封；快照只能报告可观察下界，不声称捕获全部瞬时事件", ["A09"], "真实逐笔与盘口或明确标注快照"),
    ("leader_episode_state", "龙头生命周期状态", "sector,instrument", "bar_close", "leader.relative_strength,leader.turnover,leader.ladder,theme.diffusion,episode.previous_state", "probability_vector", "按过去可见证据更新首次识别扩散分歧高潮退潮修复，禁止事后指定最终龙头", [], "市场特征和时点题材关系"),
    ("theme_diffusion", "题材扩散与昨日溢价", "sector,instrument", "bar_close", "theme.member_version,member.relative_returns,prior.limit_up_members,member.turnover", "score", "冻结题材成员和昨日集合，计算强度扩散与溢价，板块当天补标签不得回填过去", [], "行情与带known_at的题材分类"),

    ("trend_momentum", "多周期均线与动量", "instrument", "bar_close", "bar.close_adjusted,bar.is_final,bar.timeframe,adjustment.version", "feature_vector", "计算登记参数的SMA EMA及收益动量；最终周期策略仅消费已完成K线", [], "统一K线内核"),
    ("macd_state", "MACD状态", "instrument", "bar_close", "bar.close_adjusted,bar.is_final,indicator.ema_seed,indicator.fast,indicator.slow,indicator.signal", "price_units", "DIF=EMA快线减慢线，DEA为DIF的EMA；柱是否乘2写进定义版本", [], "统一K线与指标内核"),
    ("oscillator_volatility", "RSI ATR与波动状态", "instrument", "bar_close", "bar.open,bar.high,bar.low,bar.close,bar.is_final,indicator.params", "feature_vector", "按固定平滑初始化计算RSI ATR BOLL及波动率，预热不足输出缺值", [], "统一K线与指标内核"),
    ("breakout_structure", "通道箱体与价格结构", "instrument", "bar_close", "bar.high,bar.low,bar.close,bar.is_final,structure.lookback,structure.confirmation_delay", "score", "突破阈值使用此前窗口；分型确认延迟和重绘禁止条件写入定义", [], "统一K线内核"),
    ("volume_turnover", "量能量比换手", "instrument", "bar_close", "bar.volume_shares,bar.turnover_cny,shares.float,bar.session_scope,bar.is_final", "feature_vector", "股数成交额分别计算；同会话量比与历史有效流通股换手，盘后量额最终版本单独", ["A04", "A06"], "行情股本与会话版本"),
    ("vwap_distance", "VWAP偏离与收复", "instrument", "bar_close", "bar.turnover_cny,bar.volume_shares,bar.close,bar.session_scope", "ratio", "会话VWAP=累计成交额除累计股数，单位统一；盘后是否纳入由session_scope决定", ["A04"], "分钟或逐笔价量"),

    ("spread_depth", "买卖价差与深度", "instrument", "quote", "book.best_bid,book.best_ask,book.bid_sizes,book.ask_sizes,book.sequence", "feature_vector", "以有效双边报价计算价差与深度，缺一侧标为单边盘口而不是零价差", ["A09"], "授权L2或真实盘口"),
    ("order_flow_imbalance", "订单流失衡", "instrument", "quote", "order.side,order.action,order.price,order.quantity,order.sequence", "ratio", "按预定义窗口统计新增撤单成交造成的供需变化，缺完整订单流则不可计算同名因子", ["A09"], "订单流与逐笔成交关联"),
    ("queue_ahead_estimate", "排队前量估计", "instrument,order", "quote", "order.submitted_at,order.ack_at,book.level_orders,orderflow.cancel,trade.quantity", "shares", "基于可见队列及自身延迟估计前量区间，不能将估计标成真实队列位置", ["A09", "T15"], "授权盘口订单流及订单日志"),
    ("auction_imbalance", "集合竞价供需", "instrument", "quote", "auction.indicative_price,auction.matched_volume,auction.unmatched_volume,auction.side,exchange_time", "ratio", "按竞价观察时点计算供需；09:20前后的可撤单阶段分别编码", ["A04", "A09"], "真实竞价快照及规则"),
    ("realized_market_impact", "成交冲击与滑点", "instrument,order", "quote", "order.arrival_mid,fill.price,fill.quantity,book.depth,trade.participation", "basis_points", "相对下单时参考中价计算成交偏离，分离费用等待价格变动与冲击", ["T13", "T15"], "订单成交日志及行情"),
    ("intraday_liquidity_shape", "盘中流动性曲线", "instrument", "bar_close", "bar.minute_of_session,bar.volume,bar.spread,calendar.session", "ratio", "按实际会话分钟计算历史同时间成交分布，午休与盘后分别建模", ["A04", "A06"], "分钟行情与交易日历"),

    ("scheduled_event_distance", "会议展会与发布倒计时", "market,sector,instrument", "scheduled", "calendar.event_id,calendar.announced_at,calendar.scheduled_at,calendar.revision,calendar.timezone", "trading_sessions", "使用当时公布日程计算距事件的可交易会话数，延期形成revision", ["A12", "A13", "A30"], "会议主办方和官方发布日历"),
    ("expiry_calendar", "期货期权到期日历", "market", "scheduled", "contract.id,contract.expiry,contract.calendar_version,calendar.holiday_adjustment", "trading_sessions", "按当前合约官方日历计算距到期日；旧规则只能背景参考，不能生成未经核实的2026到期表", ["A27"], "中金所等交易所现行合约日历待接入"),
    ("reporting_season", "财报季与预约披露", "instrument", "scheduled", "report.appointment_date,report.revision,report.published_at,calendar.session", "trading_sessions", "按当时已知预约日期计算事件窗口，实际披露另建事实事件", ["A24"], "交易所预约披露日程"),
    ("holiday_month_end", "假期月末季末位置", "market", "scheduled", "calendar.trading_sessions,calendar.holidays,calendar.month,calendar.quarter", "category", "由交易所日历构造位置变量，不预设周四或假期前必涨跌", ["A04", "A05"], "交易所官方日历"),
    ("attention_growth", "独立注意力增速", "sector,instrument,event", "event_available", "document.event_id,document.author_id,document.source_id,document.first_seen_at,document.repost_parent", "ratio", "按独立作者和来源去重后的新增讨论算增长，转载数另列而非独立事实", ["A10", "A20", "A21", "A22", "A25", "A26"], "已获权限的媒体社区源"),
    ("sentiment_disagreement", "市场叙事分歧", "sector,instrument,event", "event_available", "claim.direction,claim.model_version,claim.author_group,claim.evidence,claim.available_at", "score", "对去重来源的情感与机制分歧度编码，模型标签不是事实且按版本保存", ["A10", "A20", "A21", "A22"], "新闻社区语料及保存的模型输出"),

    ("zodiac_homophone", "生肖与名称谐音叙事", "theme,instrument", "event_available", "theme.first_public_mention,instrument.name_history,calendar.lunar_mapping,theme.member_version", "score", "仅在主题已公开出现后按冻结字词匹配候选，羊洋阳等作为候选族不预设收益", [], "公开主题讨论历史名称和核验农历映射"),
    ("year_end_cluster", "跨年抱团叙事", "theme,instrument", "bar_close", "calendar.trading_sessions,theme.first_public_mention,leader.episode,theme.turnover_concentration", "score", "结合事前跨年话题和当时集中度构建状态，最终跨年龙不能回填先前标签", [], "时点社区记录与市场数据"),
    ("anniversary_narrative", "纪念日题材传播", "theme,instrument", "event_available", "anniversary.calendar_type,anniversary.reference_date,theme.first_public_mention,theme.diffusion", "score", "公历农历纪念日明确区分；预告话题和实际传播达到条件后才形成信号", [], "可追溯日历原始依据及公开讨论"),
    ("weather_disaster", "天气灾害经营暴露", "sector,instrument", "event_available", "weather.location,weather.warning_level,weather.issued_at,issuer.facility_location", "score", "官方预警与已发生灾害分开，按工厂航线零售地域暴露匹配", [], "气象与应急部门及公司公告"),
    ("power_logistics_disruption", "电力物流突发中断", "sector,instrument", "event_available", "disruption.location,disruption.type,disruption.start,disruption.expected_end,issuer.exposure", "score", "基于运营方公告记录中断恢复和持续时间，模型推测持续期单列", [], "电网物流港口运营方原始公告"),
    ("public_health_demand", "公共事件需求变化", "sector,instrument", "event_available", "public_event.type,public_event.scope,public_event.status,issuer.product_exposure", "score", "区分公共事实与行业需求假设，按实际产品和地域暴露映射", [], "卫生主管机构及公司产品披露"),
]

STRATEGY_FAMILIES = [
    ("SF01", "趋势与突破"), ("SF02", "反转"), ("SF03", "情绪接力"),
    ("SF04", "题材传播"), ("SF05", "量价与微观"), ("SF06", "事件"),
    ("SF07", "跨市场"), ("SF08", "基本面"), ("SF09", "因子组合"),
    ("SF10", "日历"), ("SF11", "机器学习"), ("SF12", "模型与RL"),
    ("SF13", "退出与组合"),
]

# family, slug, name, entry, exit, min/max holding sessions, granularity,
# duplicate-group, factor slugs, parameter candidates, optional source refs.
STRATEGY_ROWS = [
    ("SF01", "ma_cross", "双均线趋势", "已完成日线SMA20从下向上穿越SMA60，次交易日首个合格时点入场", "SMA20下穿SMA60或达到最长持有期限", 1, 60, "D1", "trend_price_transform", "trend_momentum", {"fast": [10,20], "slow": [40,60]}, []),
    ("SF01", "macd_cross", "MACD趋势确认", "DIF上穿DEA且DIF大于0，按固定12/26/9定义在日线完成后产生候选", "DIF下穿DEA或预登记ATR退出触发", 1, 30, "D1", "trend_price_transform", "macd_state,oscillator_volatility", {"fast": [12], "slow": [26], "signal": [9]}, []),
    ("SF01", "donchian_breakout", "前高通道突破", "收盘高于此前20或55个已完成交易日最高价，当前K线不进入阈值窗口", "跌破此前10日低点或最长持有期限", 1, 40, "D1", "breakout_high", "breakout_structure,volume_turnover", {"lookback": [20,55], "exit_lookback": [10]}, []),
    ("SF01", "trend_pullback", "趋势中的缩量回踩", "SMA20大于SMA60，价格回踩预登记均线带且缩量，随后完成K线重新收上均线", "跌破回踩低点减ATR缓冲或趋势条件失效", 1, 20, "D1", "trend_pullback", "trend_momentum,volume_turnover,oscillator_volatility", {"band_atr": [0.25,0.5], "volume_ratio_max": [0.7,0.9]}, []),
    ("SF01", "multi_timeframe", "周日多周期共振", "上一根已完成周K趋势为正，日线突破此前窗口；进行中的周K不能充当周收盘确认", "日线趋势失效或已完成周K转弱", 1, 40, "D1", "trend_price_transform", "trend_momentum,breakout_structure", {"weekly_window": [10,20], "daily_window": [10,20]}, []),

    ("SF02", "short_reversal", "短期超跌反转", "过去3至5日行业中性收益位于底部分位且当日不再创新低，次会话入场", "回到行业中位收益带或持有上限", 1, 5, "D1", "short_term_reversal", "trend_momentum,oscillator_volatility", {"lookback": [3,5], "quantile": [0.05,0.1]}, []),
    ("SF02", "rsi_reclaim", "RSI超卖收复", "RSI14曾低于候选阈值，随后已完成日K上穿该阈值且可交易", "RSI达到候选中性阈值或价格触发失效", 1, 10, "D1", "short_term_reversal", "oscillator_volatility", {"entry_rsi": [25,30], "exit_rsi": [45,50]}, []),
    ("SF02", "gap_recovery", "跳空后的分钟修复", "开盘负跳空超过冻结ATR阈值，前15分钟结束后收复开盘区间中点及VWAP", "次日以后跌破修复低点或达到持有上限；当日新买仓不可卖", 1, 3, "M1", "gap_mean_reversion", "oscillator_volatility,vwap_distance,breakout_structure", {"gap_atr": [0.5,1.0], "opening_minutes": [15]}, []),
    ("SF02", "oversold_evidence_filter", "无已知重大利空的超跌", "先满足短期反转，再对已连接消息覆盖内排查已公开盈利下修与监管负面；覆盖不足保持unknown", "反转修复完成或新的可用负面事件改变假设", 1, 10, "D1", "short_term_reversal", "trend_momentum,earnings_revision,regulatory_inquiry,disclosure_correction", {"lookback": [5,10], "coverage_policy": ["required_connected_sources"]}, ["A10", "A24"]),

    ("SF03", "first_board_next_session", "首板次日确认", "前日首次收盘涨停且非持续一字板，次日完成开盘窗口后满足换手和价格确认", "次日后跌破入场结构或题材状态失效", 1, 5, "M1", "limit_up_followthrough", "consecutive_limit_ladder,volume_turnover,auction_imbalance", {"opening_minutes": [5,15], "lookback_no_limit": [10,20]}, []),
    ("SF03", "second_board_reseal", "二板换手回封", "此前一板已知，当日开板形成真实换手后回封；入场订单必须进入队列模型", "下一可卖会话开板失效或最长持有期限", 1, 5, "L2", "limit_up_queue_continuation", "consecutive_limit_ladder,break_reseal_rate,queue_ahead_estimate", {"min_turnover_ratio": [0.05,0.1]}, ["A09", "T15"]),
    ("SF03", "turnover_leader", "换手龙持续确认", "当时LeaderEpisode处于确认或扩散且行业相对强度领先，真实换手满足阈值后确认入场", "生命周期进入退潮或结构低点失效", 1, 10, "M1", "leader_episode_follow", "leader_episode_state,volume_turnover,theme_diffusion", {"rank_quantile": [0.05,0.1]}, []),
    ("SF03", "broken_board_rebound", "断板后反包", "已识别龙头断板后，后续已完成K线收复断板实体高点并有量能确认", "反包结构低点失效或题材退潮", 1, 5, "M1", "leader_recovery", "leader_episode_state,breakout_structure,volume_turnover", {"recovery_sessions": [1,2,3]}, []),
    ("SF03", "replacement_leader", "题材内补位", "原龙头状态转弱但题材广度仍强，事前成员中新的相对强度领先者满足独立入场条件", "题材广度转弱或新领先者结构失效", 1, 8, "M1", "leader_episode_follow", "leader_episode_state,theme_diffusion,trend_momentum", {"candidate_rank": [1,2,3]}, []),
    ("SF03", "emotion_repair", "情绪退潮后的修复", "晋级率和昨日涨停溢价从预登记低位改善，候选股同时收复技术结构", "修复失败或连板广度再次恶化", 1, 5, "D1", "market_emotion_recovery", "board_promotion_rate,limit_up_breadth,theme_diffusion,breakout_structure", {"state_window": [5,10]}, []),

    ("SF04", "sector_diffusion", "题材扩散早期跟随", "已公开事件对应题材的上涨成员占比和成交扩散超过冻结阈值，选择尚可成交的相对强势成员", "扩散率下降或事件假设失效", 1, 10, "M1", "theme_diffusion_follow", "theme_diffusion,attention_growth,industrial_policy_strength", {"diffusion_quantile": [0.7,0.8]}, []),
    ("SF04", "supply_chain_follow", "产业链证据映射", "事件影响路径明确且公司关系证据已公开，技术条件确认后入场；无证据概念关联不算触发", "订单或关系被否认、相对强度失效或期限到达", 1, 20, "D1", "event_supply_chain", "supply_chain_exposure,foreign_capex_signal,order_revenue_ratio,trend_momentum", {"edge_evidence_required": [True]}, []),
    ("SF04", "companion_leader", "伴生龙相对强度", "事前同题材集合内龙头扩散且第二梯队相对收益改善、流动性足够", "龙头进入退潮或候选相对强度回落", 1, 10, "M1", "leader_episode_follow", "leader_episode_state,theme_diffusion,volume_turnover", {"leader_rank_excluded": [1], "candidate_rank_max": [3,5]}, []),
    ("SF04", "homophone_attention", "谐音题材传播候选", "公开讨论首次建立词族后冻结匹配集合，独立关注增速和价格量能共同达到阈值", "关注增长消退、主题否认或持有上限", 1, 8, "M1", "narrative_attention", "zodiac_homophone,attention_growth,volume_turnover", {"minimum_independent_sources": [3,5]}, []),

    ("SF05", "volume_breakout", "放量箱体突破", "收盘突破此前箱体高点且同口径成交量相对历史放大", "跌回箱体或ATR失效位触发", 1, 15, "D1", "breakout_high", "breakout_structure,volume_turnover,oscillator_volatility", {"lookback": [20], "volume_multiple": [1.5,2.0]}, []),
    ("SF05", "vwap_reclaim", "盘中VWAP收复", "完整5分钟K从VWAP下方收回上方，日级趋势及当时事件条件允许开仓", "下一可卖会话VWAP及结构条件转弱或期限到达", 1, 3, "M1", "intraday_reclaim", "vwap_distance,trend_momentum,intraday_liquidity_shape", {"bar_minutes": [5], "confirm_bars": [1,2]}, []),
    ("SF05", "order_flow_breakout", "订单流确认突破", "已完成短窗口订单流失衡为正、价差合格且价格突破事前局部高点", "下次可卖会话结构失效；当日新买不允许反向平仓", 1, 3, "L2", "microstructure_breakout", "order_flow_imbalance,spread_depth,breakout_structure", {"window_seconds": [30,60]}, ["A09", "T15"]),
    ("SF05", "auction_confirmation", "竞价强度确认", "在指定竞价时点用可见未匹配量和指示价格打分，开盘后首个确认窗口再决定买入", "下一可卖会话开盘结构失效或持有上限", 1, 3, "L2", "auction_followthrough", "auction_imbalance,volume_turnover,calendar_session_status", {"observation_local_time": ["09:24:50"], "confirm_minutes": [1,5]}, ["A04", "A09"]),
    ("SF05", "late_session_strength", "尾盘强度延续", "14:45–14:55内已完成分钟K相对指数增强且成交未枯竭，信号完成后在合法时段入场", "后续会话强度转弱或持有上限", 1, 3, "M1", "intraday_momentum", "trend_momentum,intraday_liquidity_shape,volume_turnover", {"start_time": ["14:45"], "last_signal_time": ["14:55"]}, []),

    ("SF06", "earnings_drift", "盈利超预期延续", "盈利首发高于事前冻结预期，消息available_at后技术及流动性确认", "盈利假设修正、趋势失效或持有上限", 1, 40, "D1", "earnings_event_drift", "earnings_surprise,earnings_revision,trend_momentum", {"surprise_quantile": [0.8,0.9]}, ["A24", "T07"]),
    ("SF06", "confirmed_order", "重大订单确认", "确定合同相对收入达到预登记阈值，经济权益明确，消息可用后再买", "合同取消收入假设变化或期限到达", 1, 20, "M1", "event_supply_chain", "order_revenue_ratio,supply_chain_exposure,volume_turnover", {"order_revenue_min": [0.05,0.1]}, ["A24"]),
    ("SF06", "buyback_implementation", "回购实际实施", "新公布实际回购进度加速并满足价格与流动性过滤，计划上限本身不触发", "进度停止、买入假设失效或持有上限", 1, 30, "D1", "corporate_capital_event", "buyback_progress,volume_turnover,trend_momentum", {"progress_window_sessions": [5,10]}, ["A24"]),
    ("SF06", "policy_surprise", "政策预期差主题", "正式政策相比事前预期有可解释增量，业务暴露与题材扩散支持后入场", "政策修订实施落空、市场确认失效或时限到达", 1, 20, "M1", "theme_diffusion_follow", "industrial_policy_strength,tax_subsidy_exposure,theme_diffusion", {"requires_expectation_snapshot": [True]}, ["A28", "A29"]),
    ("SF06", "clarification_reassessment", "澄清后的重新定价", "原负面claim被正式澄清后，模型给出有证据的影响修订并经价格收复确认", "新证据推翻澄清解释或收复结构失效", 1, 10, "M1", "event_reversal", "disclosure_correction,regulatory_inquiry,breakout_structure", {"requires_original_claim_link": [True]}, ["A24"]),

    ("SF07", "foreign_peer_mapping", "海外龙头映射", "海外可得业绩或价格信号对已证实A股暴露为正，A股开盘后单独确认并计入跳空", "映射证据失效、相对强度转弱或期限到达", 1, 10, "M1", "event_supply_chain", "foreign_earnings_surprise,global_peer_relative_strength,supply_chain_exposure", {"opening_confirm_minutes": [5,15]}, []),
    ("SF07", "currency_exposure", "汇率暴露轮动", "人民币或外币变动达到标准化阈值且公司净收入成本暴露方向明确", "汇率状态反转或预登记持有周期结束", 1, 20, "D1", "macro_exposure_rotation", "fx_exposure,fundamental_quality_value", {"fx_lookback_sessions": [5,20]}, []),
    ("SF07", "commodity_margin", "商品价差利润映射", "投入产出价差改善且产能收入暴露明确，在行业内选择估值和趋势合格公司", "价差反转或公司生产假设改变", 1, 30, "D1", "macro_exposure_rotation", "input_output_spread,energy_return,metal_return,production_capacity", {"spread_window": [5,20]}, []),
    ("SF07", "offshore_state_filter", "境外市场状态过滤", "用当时已可得A50港股ADR和全球波动状态调整既有A股候选权重，后续可交易价格执行", "全球状态或国内确认条件改变则重算目标权重", 1, 10, "M1", "market_state_router", "offshore_china_signal,global_risk_appetite,global_yield_curve", {"state_refresh_minutes": [5,15]}, []),

    ("SF08", "quality_value", "行业内质量价值", "用当时已公布财报在行业内排序盈利现金流负债和估值，选择综合靠前者", "基本面版本变化导致落出缓冲区或调仓期限", 20, 120, "D1", "fundamental_multifactor", "fundamental_quality_value", {"rebalance_sessions": [20,60], "top_fraction": [0.1,0.2]}, ["T07", "T19"]),
    ("SF08", "earnings_growth", "盈利成长改善", "历史已公布营收与盈利增长改善，排除由重述或低基数单独造成的表观增长候选", "成长信号变负或复核周期结束", 20, 120, "D1", "fundamental_multifactor", "fundamental_quality_value,earnings_revision", {"reports_lookback": [4,8]}, ["T07"]),
    ("SF08", "cashflow_quality", "现金流质量", "经营现金流与利润匹配且负债状态符合注册阈值，行业中性排序", "新财报质量下降或持有上限", 20, 120, "D1", "fundamental_multifactor", "fundamental_quality_value,audit_opinion", {"rebalance_sessions": [20,60]}, ["T07"]),
    ("SF08", "dividend_quality", "可持续分红", "按已公告现金分红计算股息率，并结合现金流和行业盈利稳定性", "分红或盈利假设改变、排名落出缓冲区或复核期限", 20, 120, "D1", "dividend_value", "fundamental_quality_value,security_status_change", {"rebalance_sessions": [20,60]}, ["T07", "T19"]),

    ("SF09", "small_liquid", "小流通市值与流动性", "在时点主板股票池按流通市值排序并约束成交能力，不将100亿元视为先验最优", "排名落出缓冲区或调仓期限", 5, 20, "D1", "size_liquidity", "volume_turnover,fundamental_quality_value", {"float_cap_threshold_cny": [5000000000,10000000000], "threshold_enabled": [True,False]}, ["T19"]),
    ("SF09", "low_volatility", "低波动组合", "按过去已完成窗口波动率在行业内排序，使用相同成本内核构建组合", "按预登记周期再平衡或状态失效", 5, 40, "D1", "defensive_factor", "oscillator_volatility,volume_turnover", {"lookback": [20,60], "rebalance_sessions": [5,20]}, []),
    ("SF09", "industry_neutral_momentum", "行业中性动量", "按行业和规模匹配后残余动量排名选股，跳过期的长度固定在实验参数", "动量排名跌出缓冲区或调仓期限", 5, 40, "D1", "trend_price_transform", "trend_momentum,volume_turnover", {"lookback": [20,60], "skip_sessions": [1,5]}, []),
    ("SF09", "factor_ensemble", "正交因子组合", "使用训练窗选择的不同因子组组合评分，权重和标准化参数在样本外冻结", "排名缓冲触发或新的已验证组合发布", 5, 40, "D1", "multifactor_ensemble", "trend_momentum,oscillator_volatility,volume_turnover,fundamental_quality_value", {"feature_group_budget": [3,4]}, ["T08", "T09", "T10"]),

    ("SF10", "expiry_interaction", "交割窗口交互", "用核实后的到期日距今变量与市场状态交互学习方向；不设交割日必跌规则", "到期窗口结束或状态交互失效", 1, 5, "D1", "calendar_state_interaction", "expiry_calendar,limit_up_breadth,global_risk_appetite", {"window_sessions": [1,3,5]}, ["A27"]),
    ("SF10", "scheduled_theme", "会议展会预期与兑现", "官方日程已公布且主题传播与业务暴露确认，在事前窗口按注册条件入场", "实际发布后预期兑现、主题失效或窗口结束", 1, 15, "D1", "narrative_attention", "scheduled_event_distance,attention_growth,industrial_policy_strength", {"lead_sessions": [3,5,10]}, []),
    ("SF10", "holiday_state", "假期与月末状态交互", "假期月末位置仅作为状态变量，与流动性趋势共同构建候选规则", "窗口结束或学习到的条件失效", 1, 5, "D1", "calendar_state_interaction", "holiday_month_end,volume_turnover,trend_momentum", {"window_sessions": [1,3]}, ["A04", "A05"]),
    ("SF10", "year_end_narrative", "跨年叙事与抱团", "跨年主题已被公开提及并形成相对强度和成交集中度，按当时龙头状态选股", "主题扩散退潮或候选地位失效", 1, 15, "M1", "narrative_attention", "year_end_cluster,zodiac_homophone,leader_episode_state,attention_growth", {"attention_window_sessions": [3,5]}, []),

    ("SF11", "tree_ranker", "树模型收益排序", "用冻结训练窗的树模型预测同一持有期成本后目标，按样本外分数排序", "排名缓冲或持有上限，退出与训练标签保持一致", 1, 10, "D1", "supervised_return_ranker", "trend_momentum,volume_turnover,oscillator_volatility,fundamental_quality_value", {"horizon_sessions": [1,5,10]}, ["T10", "T19"]),
    ("SF11", "learning_to_rank", "同日横截面排序学习", "以日期分组训练排序模型，标签为预登记可执行持有期收益，不混不同目标周期", "样本外名次跌出缓冲区或期限到达", 1, 10, "D1", "supervised_return_ranker", "trend_momentum,volume_turnover,theme_diffusion,fundamental_quality_value", {"horizon_sessions": [5,10], "group_key": ["decision_date"]}, ["T10"]),
    ("SF11", "temporal_model", "时序多尺度模型", "使用截至决策时点的多尺度已完成K线预测同一目标，完整预热及标签隔离", "预测排序缓冲或持有期限", 1, 10, "D1", "supervised_return_ranker", "trend_momentum,macd_state,oscillator_volatility,volume_turnover", {"sequence_sessions": [60,120]}, ["T10"]),
    ("SF11", "event_graph_model", "事件与产业图排序", "把当时已知事件特征和关系图版本与价格特征联合输入模型，按同目标排序", "相关事件修订导致重评或期限结束", 1, 20, "M1", "event_multimodal_ranker", "supply_chain_exposure,attention_growth,earnings_surprise,trend_momentum", {"edge_validity_required": [True]}, ["T10", "T19"]),

    ("SF12", "llm_event_assessor", "模型事件影响判断", "模型读取固定证据包输出结构化方向期限影响对象，经程序计算后形成候选", "新证据推翻前提或计划TTL到期；不直接使用自由文本下单", 1, 20, "M1", "event_multimodal_ranker", "sentiment_disagreement,supply_chain_exposure,disclosure_correction", {"model_policy_id": ["to_be_registered"]}, ["T17"]),
    ("SF12", "llm_plan_proposer", "模型条件计划", "模型提交AST条件价格范围期限和证据，经PlanCompiler用最新规则库存价格重算才生效", "按编译后的版本化退出树执行，修订保留旧成交归属", 1, 20, "M1", "model_plan_policy", "trend_momentum,macd_state,volume_turnover,calendar_session_status", {"model_policy_id": ["to_be_registered"]}, ["T17", "T18"]),
    ("SF12", "allocation_rl", "仓位分配强化学习", "基于已训练且冻结的策略在可行动候选间输出目标权重，奖励扣除真实费用并约束可执行库存", "下一决策时点重算目标权重，成交适用T+1和整手规则", 1, 20, "D1", "allocation_optimizer", "trend_momentum,oscillator_volatility,volume_turnover,leader_episode_state", {"training_run_required": [True]}, ["T16"]),
    ("SF12", "execution_rl", "委托执行强化学习", "仅对既有OrderIntent学习拆单与时机，动作不改变投资判断和目标总量", "委托目标完成失效或合法交易窗口结束，未成交不假设成功", 0, 1, "L2", "execution_optimizer", "spread_depth,order_flow_imbalance,queue_ahead_estimate,realized_market_impact", {"training_run_required": [True], "parent_intent_required": [True]}, ["T15", "T16"]),

    ("SF13", "fixed_hold_baseline", "固定持有基线", "消费被测入场系统信号，按统一成交内核买入", "自首笔成交后计满N个交易会话的首个可卖时点退出", 1, 20, "D1", "exit_policy_baseline", "calendar_session_status", {"hold_sessions": [1,3,5,10,20]}, ["A04"]),
    ("SF13", "rank_buffer", "排名缓冲退出", "消费既有排名，进入前K名且有足够现金时开仓", "已有仓位落至K加buffer名之后才退出，所有现金统一预留", 1, 40, "D1", "portfolio_turnover_control", "trend_momentum,volume_turnover", {"top_k": [5,10], "buffer": [3,5]}, []),
    ("SF13", "atr_trailing", "ATR移动失效位", "消费被测入场信号，记录真实首笔成交和ATR", "从可卖库存起按历史最高可见价减固定ATR倍数触发，跌停未成交顺延", 1, 40, "M1", "exit_trailing_stop", "oscillator_volatility,breakout_structure", {"atr_window": [14], "atr_multiple": [2,3]}, []),
    ("SF13", "staged_exit", "分批退出", "消费被测入场，登记每批股数和固定利润或结构阈值", "达到各批条件才提交对应订单，整手舍入和最低费用分别计入", 1, 20, "M1", "exit_scaling", "oscillator_volatility,calendar_session_status", {"stages": [2,3]}, []),
    ("SF13", "inventory_positive_t", "已有底仓正T", "账户已有可卖底仓且资金足够，先买新增份额，再仅用原可卖库存执行后续卖出腿", "两腿各自撮合与费用，第二腿未成交则保留额外隔夜暴露", 0, 1, "M1", "inventory_overlay", "vwap_distance,intraday_liquidity_shape,calendar_session_status", {"max_overlay_fraction": [0.1,0.2]}, ["A04"]),
    ("SF13", "inventory_reverse_t", "已有底仓倒T", "账户已有可卖库存，先在注册条件下卖出，后在更低或重新确认价格条件下买回", "买回腿独立成交；未买回保留减仓结果，不填造闭环收益", 0, 1, "M1", "inventory_overlay", "vwap_distance,intraday_liquidity_shape,calendar_session_status", {"max_overlay_fraction": [0.1,0.2]}, ["A04"]),
    ("SF13", "regime_router", "市场状态策略路由", "按已冻结的状态到策略权重映射分配候选，不在同一测试段追溯选择最佳策略", "状态变化按滞回或最低驻留规则换权重，成交账本保留原版本", 1, 20, "D1", "market_state_router", "leader_episode_state,limit_up_breadth,global_risk_appetite,oscillator_volatility", {"minimum_dwell_sessions": [3,5], "hysteresis_enabled": [True]}, ["T08", "T09"]),
]


def make_source_catalog(references):
    entries = []
    provider = {
        **{x:"Shanghai Stock Exchange" for x in ["A01","A02","A06"]},
        **{x:"Shenzhen Stock Exchange" for x in ["A03","A04","A05"]},
        "A07":"HKEX", "A08":"HKEX", "A09":"SSE Infonet", "A10":"Tushare", "A11":"Tushare",
        "A12":"National Bureau of Statistics of China", "A13":"Federal Reserve Board",
        "A14":"Federal Reserve Bank of St. Louis", "A15":"Federal Reserve Bank of St. Louis",
        **{x:"US Bureau of Labor Statistics" for x in ["A16","A17","A30"]},
        "A18":"US Energy Information Administration", "A19":"US Energy Information Administration",
        "A20":"GDELT", "A21":"Google YouTube", "A22":"Reddit", "A23":"Reddit", "A24":"CNINFO",
        "A25":"Douyin", "A26":"Douyin", "A27":"CFFEX", "A28":"Ministry of Finance of China", "A29":"NDRC",
    }
    integration_refs = {"A07","A08","A09","A10","A11","A12","A13","A14","A15","A16","A17","A18","A19","A20","A21","A22","A23","A24","A25","A26","A28","A29","A30"}
    methods = {
        "A07":["public_disclosure"], "A08":["public_disclosure"], "A09":["licensed_feed"],
        "A10":["api_key","subscription"], "A11":["api_key","subscription"],
        "A12":["public_page"], "A13":["rss","public_page"], "A14":["api_key"], "A15":["api_key"],
        "A16":["public_api"], "A17":["public_api"], "A18":["api_key"], "A19":["api_key","release_server"],
        "A20":["public_dataset"], "A21":["api_key"], "A22":["oauth","application_approval"], "A23":["oauth","application_approval"],
        "A24":["public_page","subscription_api_to_be_confirmed"], "A25":["application_approval"], "A26":["application_approval"],
        "A28":["public_page"], "A29":["public_page"], "A30":["public_page","ics"],
    }
    notes = {
        "A01":"正式规则有部分此前条文继续暂缓，实施须检查附件和后续通知。",
        "A02":"规则变化说明，不能替代具体证券当日交易参数。",
        "A07":"北向实时买卖额和总成交披露已改变，不能配置为官方实时净流入接口。",
        "A08":"年报用于确认历史披露调整，不是实时数据接口。",
        "A09":"产品文档已核实；专网或授权服务商接入、历史覆盖及权限均未验收。",
        "A10":"新闻接口独立权限；文档存在不代表已获授权、已抓取或达到延迟目标。",
        "A11":"权限方案说明；任何价格频率须实施时复核，不把示例套餐当已购。",
        "A12":"新闻首发与统计数据库更新分开；使用已成功核查的官方日程入口。",
        "A13":"RSS目录已核实，feed连接与端到端延迟未测试。",
        "A14":"支持当时已知版本；具体系列覆盖和发布时间精度须逐项验收。",
        "A17":"API文档说明存在发布与可取时间差；不能当秒级首发通道。",
        "A19":"部分库存报告API可晚于新闻首发；采集服务须分别保存到达时间。",
        "A20":"GDELT可作国际事件发现，15分钟级更新不等于全网或秒级覆盖。",
        "A21":"配额和凭证以用户实际项目为准；无已连接凭证。",
        "A22":"需对应批准和认证；无现成用户访问权限证据。",
        "A23":"访问条件文档，不能视为用户已批准使用的证明。",
        "A24":"数据API商城入口已核实；具体产品权限、字段和历史版本仍待确认。",
        "A25":"视频搜索接口文档存在；结合A26，不能认定普遍开放或当前应用可调用。",
        "A26":"能力页面仍说明实验及开放条件；当前登记未连接。",
        "A27":"2020文件仅作历史背景，未核实为2026现行完整规则；不得据此直接编排当前到期日。",
        "A30":"美国东部时区和日程修订需保留，不能以固定UTC偏移覆盖夏令时。",
    }
    for ref in references:
        row = dict(ref)
        rid = row.pop("id")
        row["source_id"] = rid
        if rid == "A12":
            row["url"] = "https://www.stats.gov.cn/sj/fbrc/bnxxfb/"
        if rid == "A27":
            row["title"] = "CFFEX 2020沪深300股指期货交易细则 历史背景"
            row["supports"] = "仅支持历史规则背景；2026现行合约日历和规则仍须另行核实"
        technical = rid.startswith("T")
        applicable = rid in integration_refs
        row.update({
            "provider": provider.get(rid, "Primary author or official project documentation"),
            "reference_kind": "engineering_or_research_reference" if technical else ("market_rule_reference" if rid in {"A01","A02","A03","A04","A05","A06","A27"} else "data_provider_or_disclosure_reference"),
            "temporal_scope": "historical_background" if rid in {"A08","A27"} else "checked_as_of_research_date",
            "documentation_status": "verified_documentation",
            "documentation_checked_on": AS_OF,
            "verification_basis": "primary_source_research_record",
            "integration_applicable": applicable,
            "connection_status": "unconfigured" if applicable else None,
            "integration_test_status": "not_attempted",
            "live_endpoint_verified": False,
            "user_entitlement_verified": False,
            "access_methods_described_or_proposed": methods.get(rid, []),
            "measured_latency_ms": None,
            "measured_coverage": None,
            "historical_coverage_verified": False,
            "notes": notes.get(rid, "仅核实资料内容，未实现在线集成，也未验证相关策略收益。"),
        })
        entries.append(row)
    return {
        "schema_version":"1.0.0", "catalog_type":"source_reference_catalog", "as_of":AS_OF,
        "timezone":"Asia/Shanghai", "reference_count":len(entries),
        "scope":"50个研究参考记录；不是50个已经连接的数据流。技术文档和规则也列在其中。",
        "global_integration_status":"no_live_integrations_completed_in_this_delivery",
        "paid_access_authorized":False, "entries":entries,
    }


def make_factor_catalog():
    assert len(FACTOR_ROWS) == 96
    entries = []
    ids = {}
    family_names = dict(FACTOR_FAMILIES)
    for i, row in enumerate(FACTOR_ROWS):
        slug,name,scope,clock,fields,unit,definition,refs,feed = row
        fid = f"F{i+1:03d}"
        family = f"F{i//6+1:02d}"
        ids[slug] = fid
        readiness = "requires_higher_granularity_data" if family=="F14" or slug=="break_reseal_rate" else ("requires_research_definition_and_data" if family=="F16" or unit in {"score","probability_vector"} else "definition_specified_data_not_connected")
        entries.append({
            "factor_id":fid, "slug":slug, "name":name, "family_id":family, "family_name":family_names[family],
            "scope":scope.split(","), "clock":{"trigger":clock, "timezone":"Asia/Shanghai", "as_of_rule":"all inputs available_at <= decision_at; use only versions known at that time", "bar_requirement":"is_final=true for declared completed timeframe" if clock=="bar_close" else "not_applicable", "revision_policy":"append_only_revisions"},
            "required_fields":fields.split(","), "output_unit":unit, "definition":definition,
            "source_refs":refs, "source_refs_role":"supports_source_access_or_market_mechanics_not_predictive_efficacy",
            "proposed_feed":feed, "data_readiness":readiness, "implementation_status":"not_implemented",
            "validation_status":"unvalidated", "availability_status":"not_connected",
            "missing_handling":{"output":None, "quality_state":"unknown", "reason_required":True, "missing_is_zero":False, "trigger_behavior":"unknown does not satisfy an entry condition", "fallback":"only explicitly registered proxy with distinct feature_id and provenance; otherwise no substitution"},
            "minimum_acceptance":["字段单位与源时间可复核", "修订及available_at回放一致", "缺值不会触发交易", "定义版本及输入manifest有记录"],
        })
    return {
        "schema_version":"1.0.0", "catalog_type":"factor_catalog", "as_of":AS_OF,
        "scope":"16个规格因素族各6个候选，共96个；全部为待接入待验证设计定义。",
        "clock_semantics":"event_available uses parsed/model output availability; source publication is not system availability",
        "global_status":"design_candidates_only", "families":[{"family_id":a,"name":b} for a,b in FACTOR_FAMILIES],
        "count":len(entries), "entries":entries,
    }, ids


def make_strategy_catalog(factor_ids):
    names = dict(STRATEGY_FAMILIES)
    executions = {
        "D1": {"profile_id":"D1_NEXT_ELIGIBLE_SESSION", "signal_clock":"bar_close", "fill_semantics":"在信号完成后的下一合格交易会话、按当时原始价和真实可成交条件撮合", "limitations":["不能推断日内触价先后", "不能证明涨停队列成交"]},
        "M1": {"profile_id":"M1_NEXT_ELIGIBLE_EVENT", "signal_clock":"bar_close_or_event_available", "fill_semantics":"信号完成并加声明延迟后的下一可观察事件成交；仅有分钟数据时采用注册的同分钟路径假设", "limitations":["分钟内触价顺序未知", "封板排队结果不能由一分钟OHLC证明"]},
        "L2": {"profile_id":"L2_QUEUE_REPLAY", "signal_clock":"quote_or_event_available", "fill_semantics":"订单确认后按可用盘口和订单流估计队列及部分成交；记录自身延迟和流动性消耗假设", "limitations":["可见队列不等于自身实际完整队列", "订单流缺失须降级并禁止伪造排队成交"]},
    }
    entries=[]
    for i,row in enumerate(STRATEGY_ROWS):
        family,slug,name,entry,exit_rule,min_hold,max_hold,gran,dup,factors,params,refs=row
        overlay = slug in {"inventory_positive_t","inventory_reverse_t"}
        execution_only = slug=="execution_rl"
        metadata={
            "strategy_id":f"S{i+1:03d}", "slug":slug,"name":name,"family_id":family,"family_name":names[family],
            "hypothesis":"本条候选机制是否在相同股票池、时点信息和成交费用条件下改善对应基线，必须通过实验判定。",
            "universe_policy_ref":"CN_MAINBOARD_DEFAULT", "side_policy":"long_only_equities",
            "signal_spec":{"entry_rule":entry,"exit_rule":exit_rule,"clock":executions[gran]["signal_clock"],"decision_availability_rule":"decision_at is after all required inputs and any model output available_at","condition_format_target":"versioned_condition_ast","is_executable_implementation":False},
            "holding_period":{"unit":"trading_sessions","min_candidate":min_hold,"max_candidate":max_hold,"anchor":"first_fill_at","counting":"complete N sessions after first fill unless trial explicitly selects entry_session_inclusive","intraday_scope":"existing_sellable_inventory_overlay" if overlay else ("parent_order_lifetime" if execution_only else None)},
            "position_policy":{"mode":"existing_inventory_overlay" if overlay else ("parent_order_execution" if execution_only else "portfolio_allocator_target_weight"),"account_cash_reservation":"atomic_shared_account","same_day_new_buy_resell":False,"lot_size":"MarketRules effective version","sizing_parameters":"must be registered before test; no user capital assumed"},
            "data_requirements":{"minimum_granularity":gran,"factor_ids":[factor_ids[x] for x in factors.split(",")],"pit_universe_required":True,"company_actions_required":True,"cost_profile_required":True,"historical_receive_time_required_for_live_claims":True},
            "execution_model":executions[gran], "parameter_candidates":params,
            "parameter_status":"research_candidates_not_optimized_or_recommended",
            "duplicate_family":dup, "overlap_policy":"share family trial budget and report factor-group ablations; do not count parameter variants as independent evidence",
            "source_refs":refs, "source_refs_role":"method_or_data_mechanics_background_not_validation_of_this_candidate",
            "provenance":{"origin_type":"architecture_design_candidate","external_author_attribution":None,"external_strategy_snapshot_required_before_claiming_reproduction":True},
            "specification_status":"candidate_requires_ast_compilation_and_fixtures",
            "implementation_status":"not_implemented", "lifecycle_state":"discovered", "validation_status":"unvalidated",
            "evaluation_status":{"backtest_run_ids":[],"forward_run_ids":[],"training_run_ids":[],"profitability_claim":False},
            "required_validation":["时点数据和股票池无未来信息", "T+1整手涨跌停停牌及费用执行一致", "与基线同样本同成本比较", "按市场状态和龙头生命周期切片", "记录所有参数trial及最终留出访问"],
        }
        if family in {"SF11","SF12"}:
            metadata["required_validation"].append("训练验证测试按时间隔离，模型和提示版本固定；历史模型污染单列并积累前向证据")
        if overlay:
            metadata["required_validation"].append("两腿各自成交，已有可卖库存账本可证，未完成第二腿仍计持仓和损益")
        if gran=="L2":
            metadata["required_validation"].append("数据完整性队列假设自身延迟部分成交和流动性消耗明示")
        entries.append(metadata)
    assert len(entries)==60
    return {
        "schema_version":"1.0.0", "catalog_type":"strategy_catalog", "as_of":AS_OF,
        "count":len(entries), "families":[{"family_id":a,"name":b} for a,b in STRATEGY_FAMILIES],
        "universe_policies":{"CN_MAINBOARD_DEFAULT":{"venues":["SSE","SZSE"],"board":"mainboard","instrument_type":"ordinary_A_share","excluded":["ChiNext","STAR","BSE","new_listing"],"new_listing_exclusion_window":"must be registered in UniversePolicy before first experiment","st_exclusion":"configurable_default_exclude","price_cap":{"enabled":True,"operator":"lte","value":"100","currency":"CNY","adjustment":"raw"},"float_market_cap":{"enabled":True,"operator":"lte","value":"10000000000","currency":"CNY","basis":"circulating_market_cap"},"capital_amount":None}},
        "scope":"60个待研究候选，未实现、未跑回测、未训练、未接券商、未验证超额；不声称收集完所有已知策略。",
        "entries":entries,
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--references",required=True,help="Path to catalogs/reference_inputs.json")
    args=parser.parse_args()
    refs=json.loads(Path(args.references).read_text(encoding="utf-8"))
    if len(refs)!=50 or len({x["id"] for x in refs})!=50:
        raise ValueError("Expected the supplied 50 distinct reference IDs")
    sources=make_source_catalog(refs)
    factors,factor_ids=make_factor_catalog()
    strategies=make_strategy_catalog(factor_ids)
    source_ids={x["source_id"] for x in sources["entries"]}
    for catalog in [factors,strategies]:
        for row in catalog["entries"]:
            unknown=set(row["source_refs"])-source_ids
            if unknown:
                raise ValueError(f"Unknown source refs {unknown}")
    assert len(Counter(x["family_id"] for x in factors["entries"]))==16
    assert len(Counter(x["family_id"] for x in strategies["entries"]))==13
    for name,data in [("source_catalog.json",sources),("factor_catalog.json",factors),("strategy_catalog.json",strategies)]:
        (HERE/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"source_references":len(sources["entries"]),"factors":len(factors["entries"]),"factor_families":16,"strategies":len(strategies["entries"]),"strategy_families":13,"strategy_distribution":dict(Counter(x["family_id"] for x in strategies["entries"]))},ensure_ascii=False))


if __name__=="__main__":
    main()
