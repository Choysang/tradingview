"""A-Share Dragon-Tiger List (LHB) Brokerage Seat Personas & 4D Fake-Institution Detector.

Why: Different brokerage branches on the Dragon-Tiger List exhibit persistent, empirically
verifiable microstructure footprints (Pulse Ledger E048, E094, E098, E099; JFQA 2025; JBF 2025):
* Lhasa Retail Corps ("拉萨天团" 8 Eastmoney branches): D+1 gap -3.15%, D+5 excess -2.52% (t=-9.5).
* One-Day Raiders ("上塘路/联储浙江/佛山无影脚/成都帮"): aggressive next-open dump / T+1 arbitrage.
* Pattern/Synergy Leaders ("陈小群/呼家楼/章盟主/作手新一/方新侠/消闲派/宁波桑田路/上海孙哥养家系/深圳益田路荣超"):
  compatible with the April 2025 Guotai-Junan + Haitong merger rename to ``国泰海通证券股份有限公司``.
* Retired/Hijacked Seats ("福州六一中路" post-2024-12-19 public retirement declaration).
* High-Frequency Quant & Foreign Channels ("华鑫上海分公司/开源西安西大街/中金上海/瑞银花园石桥路/高盛世纪大道").
* 4D Fake-Institution Detector: Separates genuine long-only ``机构专用`` accumulation from
  quant/hot-money borrowed institutional seats on small-cap pure-theme boards (explaining E094/E098).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SeatPersonaSpec:
    persona_id: str
    persona_code: str
    persona_label: str
    category: str
    regex_pattern: str
    empirical_d1_gap_pct: str | None
    empirical_d5_excess_pct: str | None
    style_summary: str
    counter_tactic: str
    effective_note: str


SEAT_PERSONA_REGISTRY: tuple[SeatPersonaSpec, ...] = (
    SeatPersonaSpec(
        persona_id="seat:lhasa_tiantuan",
        persona_code="lhasa_retail_corps",
        persona_label="东方财富拉萨天团（8大散户大本营席位）",
        category="lhasa_retail_corps",
        regex_pattern=r"东方财富证券.*(拉萨.*(团结路|东环路|金融城南环路|江苏大道)|山南.*香曲东路|昌都.*两江大道)",
        empirical_d1_gap_pct="-3.15",
        empirical_d5_excess_pct="-2.52",
        style_summary="全网散户跟风盘集中席位（含团结路第一/第二、东环路第一/第二、金融城南环路、东城区江苏大道、山南香曲东路、昌都两江大道8席）；分歧日买榜扎堆(>=3家)意味着筹码彻底散户化（E048实证D+1 -3.15%、D+5 -2.52%）。",
        counter_tactic="买榜出现>=2家拉萨席位严禁次日追高；持仓股见拉萨3+霸榜，次日早盘冲高果断分批止盈/减仓，谨防盘中核按钮踩踏。",
        effective_note="Active 2024-2026 (verified in Pulse Ledger E048 & E099)",
    ),
    SeatPersonaSpec(
        persona_id="seat:shangtang_raider",
        persona_code="shangtang_one_day_dump",
        persona_label="杭州上塘路/体育馆路（高频一日游砸盘）",
        category="one_day_dump_raider",
        regex_pattern=r"财通证券.*杭州.*(上塘路|体育馆路)",
        empirical_d1_gap_pct="-1.85",
        empirical_d5_excess_pct="-4.54",
        style_summary="典型扫板套利一日游席位，点火封板凶悍但隔日兑现极度坚决，次日高开或冲高常伴随不计成本砸盘（E048实证D+5均值-4.54%，t=-3.3）。",
        counter_tactic="绝不次日高开接力其买一重仓独食股；若持仓同车，次日集合竞价或开盘前15分钟抢先兑现。",
        effective_note="Active 2024-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:foshan_shadowkick",
        persona_code="raider_foshan_shadowkick",
        persona_label="佛山无影脚（绿景路/季华六路·大资金封板次日核按钮）",
        category="one_day_dump_raider",
        regex_pattern=r"光大证券.*佛山.*(绿景路|季华六路)",
        empirical_d1_gap_pct="-1.65",
        empirical_d5_excess_pct="-3.95",
        style_summary="经典“佛山无影脚”大资金扫板独食流派，惯用单日巨额封死首板/低位板制造盘口假象，次日开盘或冲高瞬间不计成本核按钮砸盘派发。",
        counter_tactic="见其大额独食买一封板切忌次日竞价追接；有底仓者在次日早盘脉冲高点优先挂单兑现。",
        effective_note="Active 2024-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:chengdu_gang",
        persona_code="raider_chengdu_gang",
        persona_label="成都帮（蜀金路/北一环路·首板挖掘与次日兑现）",
        category="one_day_dump_raider",
        regex_pattern=r"(华泰证券.*成都.*蜀金路|(国泰海通|国泰君安)证券.*成都.*北一环路)",
        empirical_d1_gap_pct="-0.85",
        empirical_d5_excess_pct="-2.90",
        style_summary="敏锐捕捉日内首板与超跌反抽题材，擅长午后低位快速封板，但次日纪律极严、冲高即兑现离场（兼顾国泰海通更名）。",
        counter_tactic="首板溢价仅限于次日早盘第一波冲高，若未能高开换手转强则不恋战。",
        effective_note="Compatible with 2025-04 Guotai-Haitong merger rename",
    ),
    SeatPersonaSpec(
        persona_id="seat:lianchu_raider",
        persona_code="lianchu_short_arb",
        persona_label="联储浙江/宁波分公司（快进快出套利）",
        category="one_day_dump_raider",
        regex_pattern=r"联储证券.*(浙江分公司|宁波分公司)",
        empirical_d1_gap_pct="-1.40",
        empirical_d5_excess_pct="-3.60",
        style_summary="偏爱短线弹性标的与日内扫板套利，隔日留仓意愿极低。",
        counter_tactic="次日溢价预期下调，避免开盘盲目追涨接盘。",
        effective_note="Active 2024-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:chen_xiaoqun",
        persona_code="hotmoney_chen_xiaoqun",
        persona_label="陈小群常用席位（主线总龙头格局接力）",
        category="pattern_leader_synergy",
        regex_pattern=r"(中国银河证券.*大连.*(金马路|黄河路)|东亚前海证券.*苏州.*留园路)",
        empirical_d1_gap_pct="+1.80",
        empirical_d5_excess_pct="+0.65",
        style_summary="新生代主线核心龙头代表，擅长高辨识度总龙头分歧反包与主升重仓接力，市场跟风溢价显著，但退潮期高位波动剧烈。",
        counter_tactic="处于主升初期且多路格局资金合力时可顺势持有看溢价；若高标缩量加速后单席位高位大额锁仓/加仓且跟风断板，警惕退潮补跌。",
        effective_note="Active 2025-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:hujialou",
        persona_code="hotmoney_hujialou",
        persona_label="北京呼家楼/总部系（大容量主线平铺合力）",
        category="pattern_leader_synergy",
        regex_pattern=r"(中信证券.*北京.*(呼家楼|总部)|中信建投.*北京.*(朝外大街|东三环))",
        empirical_d1_gap_pct="+1.45",
        empirical_d5_excess_pct="+0.40",
        style_summary="重金大开大合，常多席位协同平铺主线板块前排与容量中军，擅长引导板块级共振。",
        counter_tactic="观察同板块是否有多只个股同步现身该席位；板块合力期对容量中军偏正面，单票孤立高位巨量则防次日分化。",
        effective_note="Active 2025-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:zhang_mengzhu",
        persona_code="hotmoney_zhang_mengzhu",
        persona_label="章盟主常用席位（容量趋势核心善庄）",
        category="pattern_leader_synergy",
        regex_pattern=r"((国泰海通|国泰君安|海通)证券.*(上海.*江苏路|宁波.*彩虹北路|上海.*海阳西路))",
        empirical_d1_gap_pct="+0.95",
        empirical_d5_excess_pct="+0.85",
        style_summary="百亿级元老席位（已兼容2025年4月国泰海通更名），偏好百亿市值以上硬科技/赛道容量中军，分歧低吸、较少恶性核按钮。",
        counter_tactic="在大容量趋势主升股中可作中军承接参考，适合配合分时均价线做正T或波段持有。",
        effective_note="Compatible with 2025-04 Guotai-Haitong merger rename",
    ),
    SeatPersonaSpec(
        persona_id="seat:zuoshou_xinyi",
        persona_code="hotmoney_zuoshou_xinyi",
        persona_label="作手新一常用席位（情绪与逻辑共振）",
        category="pattern_leader_synergy",
        regex_pattern=r"((国泰海通|国泰君安)证券.*南京.*太平南路)",
        empirical_d1_gap_pct="+1.10",
        empirical_d5_excess_pct="+0.50",
        style_summary="兼顾产业逻辑与短线情绪节点（已兼容国泰海通更名），板块切换敏锐。",
        counter_tactic="结合板块升温阶段判断：首板/二板启动期偏积极，高位缩量加速段谨慎接力。",
        effective_note="Compatible with 2025-04 Guotai-Haitong merger rename",
    ),
    SeatPersonaSpec(
        persona_id="seat:fang_xinxia",
        persona_code="hotmoney_fang_xinxia",
        persona_label="方新侠常用席位（大成交趋势锁仓）",
        category="pattern_leader_synergy",
        regex_pattern=r"(中信证券.*西安.*朱雀大街|兴业证券.*陕西分公司)",
        empirical_d1_gap_pct="+1.05",
        empirical_d5_excess_pct="+0.60",
        style_summary="偏好50亿+大成交额主线中军，敢于主升段大手笔格局锁仓。",
        counter_tactic="适合容量中军趋势跟踪，忌在板块退潮期盲目抄底。",
        effective_note="Active 2025-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:xiaoxianpai",
        persona_code="hotmoney_xiaoxianpai",
        persona_label="消闲派常用席位（首板挖掘与高换手自救）",
        category="pattern_leader_synergy",
        regex_pattern=r"((国泰海通|国泰君安)证券.*宜昌.*(沿江大道|珍珠路))",
        empirical_d1_gap_pct="+0.80",
        empirical_d5_excess_pct="-0.40",
        style_summary="日内首板扫板效率极高，次日倾向高换手冲高兑现，持仓周期偏短（1-2日）。",
        counter_tactic="首板次日有冲高溢价预期，但若次日未能弱转强晋级连板，应及时兑现防回落。",
        effective_note="Compatible with 2025-04 Guotai-Haitong merger rename",
    ),
    SeatPersonaSpec(
        persona_id="seat:ningbo_sangtian",
        persona_code="hotmoney_ningbo_sangtian",
        persona_label="宁波桑田路/解放南路（老牌连板情绪博弈）",
        category="pattern_leader_synergy",
        regex_pattern=r"(国盛证券.*宁波.*桑田路|光大证券.*宁波.*解放南路)",
        empirical_d1_gap_pct="+1.15",
        empirical_d5_excess_pct="+0.35",
        style_summary="顶级老牌宁波帮情绪博弈席位，擅长连板分歧弱转强与核心妖股卡位引导，辨识度高但高位换手剧烈。",
        counter_tactic="多路格局资金同框且承接充分时偏正面溢价；高标断板日若见大额净流出则果断离场。",
        effective_note="Active 2025-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:shanghai_sunge_yangjia",
        persona_code="hotmoney_shanghai_sunge_yangjia",
        persona_label="上海孙哥/养家系（溧阳路/宛平南路/东川路/漕溪北路）",
        category="pattern_leader_synergy",
        regex_pattern=r"(中信证券.*上海.*(溧阳路|漕溪北路)|华鑫证券.*上海.*宛平南路|(国泰海通|国泰君安|海通)证券.*上海.*东川路|华泰证券.*上海.*武定路)",
        empirical_d1_gap_pct="+0.65",
        empirical_d5_excess_pct="-0.25",
        style_summary="沪上老牌顶级游资阵营（含中信溧阳路/漕溪北路、华鑫宛平南路、国泰海通东川路等），善拉高分歧博弈、盘中点火引导与高位顺势派发。",
        counter_tactic="适合观察分歧承接强度：低位启动共振可跟随做正T，连续加速高潮后谨防其高位拉高派发。",
        effective_note="Compatible with 2025-04 Guotai-Haitong merger rename",
    ),
    SeatPersonaSpec(
        persona_id="seat:shenzhen_rongchao",
        persona_code="hotmoney_shenzhen_rongchao",
        persona_label="深圳益田路荣超商务中心（龙头高位接力与趋势锁仓）",
        category="pattern_leader_synergy",
        regex_pattern=r"华泰证券.*深圳.*益田路荣超",
        empirical_d1_gap_pct="+1.20",
        empirical_d5_excess_pct="+0.75",
        style_summary="南派核心资金旗舰营业部，偏好硬主线大龙头的高位确定性接力与趋势波段锁仓。",
        counter_tactic="出现大额净买入且伴随机构/容量席位共振时，中短线趋势支撑较强。",
        effective_note="Active 2025-2026",
    ),
    SeatPersonaSpec(
        persona_id="seat:liuyi_retired",
        persona_code="legacy_liuyi_hijacked",
        persona_label="福州六一中路等（2024-12声明弃用·防跟风借壳出货）",
        category="legacy_hijacked_seat",
        regex_pattern=r"(招商证券.*福州.*六一中路|(国泰海通|国泰君安)证券.*南京.*(清凉门|双龙大道))",
        empirical_d1_gap_pct="-0.90",
        empirical_d5_excess_pct="-2.80",
        style_summary="原游资已于2024年12月19日公开声明弃用该批席位；现多为套利资金利用散户刻板印象吸引接盘的通道。",
        counter_tactic="切勿当作真游资格局合力盲目顶一字或高开秒追，按普通短线套利盘防范次日冲高派发。",
        effective_note="Retired/Hijacked warning active since 2024-12-19",
    ),
    SeatPersonaSpec(
        persona_id="seat:quant_huaxin_kaiyuan",
        persona_code="quant_high_freq_t0",
        persona_label="华鑫上海/开源西安/中金上海（高频量化打板与T0集群）",
        category="quant_high_freq",
        regex_pattern=r"(华鑫证券.*(上海分公司|杭州飞云江路)|开源证券.*西安.*(西大街|太华路)|中国国际金融.*上海分公司)",
        empirical_d1_gap_pct="-0.45",
        empirical_d5_excess_pct="-1.65",
        style_summary="量化多因子扫板与高频日内融券/底仓T0通道，常出现买卖双向同框，助涨助跌显著。",
        counter_tactic="盘中分时脉冲切忌追高，反而适合利用量化日内高波动在VWAP上下做逆向正T/倒T。",
        effective_note="Active 2025-2026 under CSRC programmatic trading rules",
    ),
    SeatPersonaSpec(
        persona_id="seat:qfii_ubs_gs",
        persona_code="qfii_quant_channel",
        persona_label="瑞银花园石桥路/高盛世纪大道（外资QFII与程序化通道）",
        category="quant_high_freq",
        regex_pattern=r"(瑞银证券.*上海.*花园石桥路|高盛.*上海.*世纪大道)",
        empirical_d1_gap_pct="-0.20",
        empirical_d5_excess_pct="-0.55",
        style_summary="外资对冲与QFII程序化算法席位，在大盘核心资产上偏波段配置，在小盘题材上偏高频量化套利。",
        counter_tactic="大市值蓝筹净买入视作外资回流参考；小盘涨停股现身则按量化短线对待。",
        effective_note="Active 2025-2026",
    ),
)

_COMPILED = [(spec, re.compile(spec.regex_pattern)) for spec in SEAT_PERSONA_REGISTRY]


def match_seat_persona(seat_name: str, report_date: date | None = None) -> SeatPersonaSpec | None:
    text = (seat_name or "").strip()
    if not text:
        return None
    for spec, rx in _COMPILED:
        if rx.search(text):
            if spec.persona_code == "legacy_liuyi_hijacked" and report_date and report_date < date(2024, 12, 19):
                continue
            return spec
    if "机构专用" in text:
        return SeatPersonaSpec(
            persona_id="seat:institution_dedicated",
            persona_code="institution_dedicated",
            persona_label="机构专用席位（需结合四维真伪机构鉴别）",
            category="institution_channel",
            regex_pattern="机构专用",
            empirical_d1_gap_pct="-0.35",
            empirical_d5_excess_pct="-1.81",
            style_summary="公募/私募/险资及部分借道量化专户席位；台账E094实证全样本大额净买入后5日均值-1.81%，须甄别真机构建仓与量化假机构一日游。",
            counter_tactic="使用下方四维真伪机构检验：大中盘低位缩量分歧买入偏真机构；小市值纯题材+华鑫/开源量化共生高度警惕次日砸盘。",
            effective_note="Verified against Ledger E094 / E098",
        )
    if "沪股通专用" in text or "深股通专用" in text:
        return SeatPersonaSpec(
            persona_id="seat:northbound_connect",
            persona_code="northbound_hk_connect",
            persona_label="沪/深股通专用（北向高频窗口）",
            category="institution_channel",
            regex_pattern="(沪股通|深股通)专用",
            empirical_d1_gap_pct="+0.25",
            empirical_d5_excess_pct="+0.15",
            style_summary="2024年8月北向停披日度净流入后，龙虎榜沪深股通席位是个股级外资与离岸量化博弈的关键公开窗口，常做日内双边做T。",
            counter_tactic="观察买卖双边差额：单边大幅净买入大市值龙头偏配置信号；买卖等额同现则为外资日内做T。",
            effective_note="Post-2024-08 Northbound disclosure regime",
        )
    return None


def analyze_lhb_battle(
    seat_rows: Sequence[Mapping[str, Any]],
    *,
    report_date: date | None = None,
    float_cap_cny: float | None = None,
    turnover_cny: float | None = None,
) -> dict[str, Any]:
    """Produce complete opponent-profiling, net buy totals, lhasa ratio & 4D Fake-Institution plan."""
    if not seat_rows:
        return {
            "has_recent_lhb": False,
            "report_date": report_date.isoformat() if report_date else None,
            "lhasa_buyer_count": 0,
            "one_day_raider_count": 0,
            "synergy_leader_count": 0,
            "quant_seat_count": 0,
            "net_buy_amount_cny": None,
            "lhasa_ratio": None,
            "battle_verdict": "no_lhb_data",
            "fake_institution_check": {
                "institution_present": False,
                "verdict": "none",
                "risk_score": None,
                "reasons": [],
            },
            "seats": [],
            "opponent_strategy": "近期未触发龙虎榜异动，筹码处于常态二级市场博弈，优先参考量价结构、VPVR筹码密集区与资金流。",
        }

    lhasa_buyers = 0
    raiders = 0
    leaders = 0
    quants = 0
    retired_hijacked = 0
    inst_buy_total = 0.0
    inst_sell_total = 0.0
    inst_both_sides = 0
    inst_count = 0

    total_buy_cny = 0.0
    total_sell_cny = 0.0
    lhasa_buy_cny = 0.0
    matched: list[dict[str, Any]] = []

    for r in seat_rows:
        name = str(r.get("seat_name") or r.get("raw_name") or "").strip()
        buy_amt = float(r.get("buy_cny") or r.get("buy_amount_cny") or 0.0)
        sell_amt = float(r.get("sell_cny") or r.get("sell_amount_cny") or 0.0)
        net_amt = buy_amt - sell_amt
        total_buy_cny += buy_amt
        total_sell_cny += sell_amt

        side = "both" if (buy_amt > 0 and sell_amt > 0) else ("buy" if buy_amt >= sell_amt else "sell")
        spec = match_seat_persona(name, report_date)

        if "机构专用" in name:
            inst_count += 1
            inst_buy_total += buy_amt
            inst_sell_total += sell_amt
            if buy_amt > 0 and sell_amt > 0:
                inst_both_sides += 1

        if spec is not None:
            if spec.category == "lhasa_retail_corps" and buy_amt > sell_amt:
                lhasa_buyers += 1
                lhasa_buy_cny += buy_amt
            elif spec.category == "one_day_dump_raider" and buy_amt > sell_amt:
                raiders += 1
            elif spec.category == "pattern_leader_synergy" and buy_amt > sell_amt:
                leaders += 1
            elif spec.category == "quant_high_freq":
                quants += 1
            elif spec.category == "legacy_hijacked_seat" and buy_amt > sell_amt:
                retired_hijacked += 1
            matched.append({
                "seat_name": name,
                "side": side,
                "category": spec.category,
                "net_amount_cny": f"{net_amt:.2f}",
                "persona_code": spec.persona_code,
                "persona_label": spec.persona_label,
                "style_summary": spec.style_summary,
                "empirical_d1_gap_pct": spec.empirical_d1_gap_pct,
                "empirical_d5_excess_pct": spec.empirical_d5_excess_pct,
                "counter_tactic": spec.counter_tactic,
            })
        else:
            matched.append({
                "seat_name": name or "普通营业部席位",
                "side": side,
                "category": "regular_branch",
                "net_amount_cny": f"{net_amt:.2f}",
                "persona_code": "regular_branch",
                "persona_label": "常规区域营业部席位",
                "style_summary": "常规市场参与营业部，未命中特定知名游资、一日游砸盘或拉萨散户席位。",
                "empirical_d1_gap_pct": None,
                "empirical_d5_excess_pct": None,
                "counter_tactic": "结合个股主趋势与VPVR筹码密集区执行常规策略。",
            })

    net_total = total_buy_cny - total_sell_cny
    lhasa_ratio_val = (lhasa_buy_cny / total_buy_cny) if total_buy_cny > 0 else (lhasa_buyers / max(1, len(seat_rows)))
    lhasa_ratio_val = max(0.0, min(1.0, lhasa_ratio_val))

    # 4D Fake-Institution Detection (explaining Ledger E094 / E098)
    fake_reasons: list[str] = []
    fake_risk = 0.0
    if inst_count > 0:
        if float_cap_cny is not None and float_cap_cny < 4_500_000_000:
            fake_risk += 0.35
            fake_reasons.append("维度1(市值错配)：流通市值低于45亿元的小微盘现身机构席位，偏离传统公募风控池")
        if quants >= 1:
            fake_risk += 0.30
            fake_reasons.append("维度2(量化共生)：买榜与华鑫上海/开源西安等高频量化席位同框，高度疑似量化专户协同扫板")
        if inst_both_sides >= 1:
            fake_risk += 0.25
            fake_reasons.append("维度3(双向T0做市)：机构专用席位同日既大买又大卖，呈典型程序化日内回转特征")
        if turnover_cny and turnover_cny > 0 and (inst_buy_total - inst_sell_total) / turnover_cny >= 0.05:
            fake_risk += 0.20
            fake_reasons.append("维度4(E094/E098高占比预警)：机构净买占成交额>=5%的历史样本D+1~D+5平均跑输-1.81%，需防次日高开兑现")

        if fake_risk >= 0.50:
            inst_verdict = "quant_fake_institution_risk"
        elif fake_risk >= 0.25:
            inst_verdict = "mixed_uncertain"
        else:
            inst_verdict = "real_institution_likely"
            fake_reasons.append("中大市值容量标的且无高频一日游/量化同框，倾向真实机构配置通道")
        fake_check = {
            "institution_present": True,
            "verdict": inst_verdict,
            "risk_score": f"{min(1.0, fake_risk):.2f}",
            "reasons": fake_reasons,
        }
    else:
        fake_check = {"institution_present": False, "verdict": "none", "risk_score": None, "reasons": []}

    # Determine unified battle_verdict enum
    if lhasa_buyers >= 3 or (lhasa_buyers >= 2 and lhasa_ratio_val >= 0.35) or fake_check["verdict"] == "quant_fake_institution_risk" or retired_hijacked >= 1:
        battle_verdict = "retail_trap_bearish"
    elif raiders >= 1:
        battle_verdict = "raider_dump_warning"
    elif (leaders >= 1 and lhasa_buyers < 2 and net_total > 0) or (fake_check["verdict"] == "real_institution_likely" and net_total > 0 and lhasa_buyers == 0):
        battle_verdict = "synergy_bullish"
    else:
        battle_verdict = "neutral_turnover"

    # Synthesize overall opponent game-theory strategy
    strat_parts: list[str] = []
    if lhasa_buyers >= 3:
        strat_parts.append(f"【最高级散户接盘预警】买方出现{lhasa_buyers}家拉萨天团席位（金额占比{lhasa_ratio_val*100:.1f}%，E048实证次日跳空-3.15%、5日-2.52%），严禁开盘追涨，持仓宜逢高撤退。")
    elif lhasa_buyers >= 1:
        strat_parts.append(f"【散户跟风升温】买榜现{lhasa_buyers}家拉萨席位（占买方{lhasa_ratio_val*100:.1f}%），筹码分歧加大，不宜高位重仓追击。")
    if retired_hijacked >= 1:
        strat_parts.append("【退役席位借壳警示】买榜现已声明弃用的福州六一中路等席位，谨防套利资金诱多接盘。")
    if raiders >= 1:
        strat_parts.append(f"【一日游砸盘防范】存在{raiders}家上塘路/佛山/成都/联储等短线套利席位，次日早盘冲高兑现抛压重，买点须等首波分歧换手充分承接后再看。")
    if fake_check["verdict"] == "quant_fake_institution_risk":
        strat_parts.append("【量化假机构警示】机构专用席位呈现高量化/小票错配特征，谨防借机构名义诱多出货。")
    if leaders >= 1 and lhasa_buyers < 3 and raiders == 0:
        strat_parts.append(f"【格局游资合力】买方含{leaders}家知名主线格局席位且无明显一日游砸盘方，若次日竞价换手良性且站稳分时均线，可顺势格局或正T。")
    if quants >= 1 and not strat_parts:
        strat_parts.append("【量化主导盘口】量化席位活跃，盘中易现脉冲急拉急跌，最佳应对为挂单做T而非追突破。")
    if not strat_parts:
        strat_parts.append("龙虎榜席位以普通营业部换手为主，无极端拉萨扎堆或知名砸盘席位，按技术面枢轴支撑与量能结构执行。")

    return {
        "has_recent_lhb": True,
        "report_date": report_date.isoformat() if report_date else None,
        "lhasa_buyer_count": lhasa_buyers,
        "one_day_raider_count": raiders,
        "synergy_leader_count": leaders,
        "quant_seat_count": quants,
        "net_buy_amount_cny": f"{net_total:.2f}",
        "lhasa_ratio": f"{lhasa_ratio_val:.3f}",
        "battle_verdict": battle_verdict,
        "fake_institution_check": fake_check,
        "seats": matched,
        "opponent_strategy": " ".join(strat_parts),
    }
