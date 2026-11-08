"""战法库 v3: 抱团与联动——板块扩散、名称联动、龙虎榜席位（5 套）。

These read the crowd columns built in crowd.py: industry limit-up counts, name
clusters and Dragon-Tiger seat track records, all point-in-time. A playbook whose
source table is missing (the SIM warehouse has no boards or Dragon-Tiger list)
lists its columns in `requires_history`, so the engine reports "cannot test"
instead of a result built on nulls.
"""
from __future__ import annotations

import polars as pl

from contracts.schema import PlaybookSpec
from .library import COMMUNITY, add, c, emit, equal, liquid, param, short_exits

SEAT_SOURCE = (
    "龙虎榜席位滚动战绩（本系统用数据给席位打分，不依赖游资名单）；思路参考上海财经大学学报龙虎榜实证"
    "（上榜后短期动量、部分账户次日抛售）"
)


def _not_limit(share: float) -> pl.Expr:
    """Today's change is below `share` of the board's limit, and the limit was not touched."""
    return (c("pct_chg") < c("limit_ratio") * 100 * share) & ~c("touched_limit")


def _industry_follower(panel, p):
    hit = (
        (c("ind_limit_ups") >= p["min_boards"]) & ~c("is_limit_up") & _not_limit(0.95)
        & (c("pct_chg") >= c("limit_ratio") * 100 * p["min_move"]) & (c("volume") >= p["vol_ratio"] * c("vol_ma5"))
    )
    return emit(panel, hit, c("ind_limit_ups") + c("pct_chg") / 100)


add(PlaybookSpec(
    id="industry_follower", name="板块跟风", style="sentiment", horizon="short",
    summary="同一行业当天至少 3 只涨停，买涨得不错但没封板的跟风股，博次日补涨。",
    rule="当日所属新浪行业涨停 ≥ 3 只；本股未涨停、未触及涨停，涨幅 ≥ 涨停幅度的 50%；成交量 ≥ 5 日均量 1.5 倍。"
         "得分 = 行业涨停数。次日开盘买入，持有 2 日，止损 6%。行业用当前成分快照（行业变化慢，前视很小）。",
    source=COMMUNITY + "；板块效应、跟风滞后一天", params=[
        param("min_boards", "行业涨停数下限", 3, 2, 8, 1), param("min_move", "涨幅占涨停幅度下限", 0.5, 0.3, 0.9, 0.05),
        param("vol_ratio", "量比下限（对 5 日均量）", 1.5, 1.0, 4.0, 0.1)],
    exits=short_exits(2, 6), sizing=equal(5, 2)), _industry_follower, requires_history=("ind_limit_ups",))


def _industry_first_board(panel, p):
    hit = (c("ind_limit_ups") >= p["min_boards"]) & (c("limit_streak") == 1) & ~c("one_word")
    return emit(panel, hit, c("ind_limit_ups") * 1.0)


add(PlaybookSpec(
    id="industry_first_board", name="板块首板", style="sentiment", horizon="short",
    summary="行业集体涨停的当天，只做其中的首板，博板块发酵的第二天。",
    rule="当日所属新浪行业涨停 ≥ 3 只；本股为首板（limit_streak = 1）、非一字板。得分 = 行业涨停数。"
         "次日开盘买入，持有 2 日，止损 6%。",
    source=COMMUNITY + "；板块热度首板", params=[param("min_boards", "行业涨停数下限", 3, 2, 8, 1)],
    exits=short_exits(2, 6), sizing=equal(5, 2)), _industry_first_board, requires_history=("ind_limit_ups",))


def _name_cluster(panel, p):
    hit = (c("name_heat") >= p["min_peers"]) & ~c("is_limit_up") & (c("pct_chg") >= p["min_pct"]) & _not_limit(0.95)
    return emit(panel, hit, c("name_heat") + c("pct_chg") / 100)


add(PlaybookSpec(
    id="name_cluster_follower", name="名称联动", style="sentiment", horizon="short",
    summary="名字里有同一个少见字的股票当天已有 2 只以上涨停（生肖、谐音、字辈），买还没涨停的同名股。",
    rule="名称中某个少见汉字（全市场不超过 20 个名称含有，排除“股份、科技、集团”等常用字）当天已有 ≥ 2 只其他股票涨停；"
         "本股未涨停、未触及涨停，涨幅 ≥ 3%。得分 = 同字涨停数。次日开盘买入，持有 2 日，止损 6%。"
         "名称用当前快照，改过名的股票有轻微前视。",
    source=COMMUNITY + "；题材谐音、生肖字辈联动", params=[
        param("min_peers", "同字涨停数下限", 2, 1, 6, 1), param("min_pct", "本股涨幅下限(%)", 3, 0, 8, 0.5)],
    exits=short_exits(2, 6), sizing=equal(5, 2)), _name_cluster, requires_history=("name_heat",))


def _smart_seats(panel, p):
    hit = c("lhb_on") & (c("smart_buyers") >= p["min_smart"]) & (c("trip_buyers") == 0) & liquid(0.5)
    return emit(panel, hit, c("smart_score"))


add(PlaybookSpec(
    id="smart_seat_follow", name="强势席位合力", style="sentiment", horizon="short",
    summary="龙虎榜买方里有 2 个以上历史战绩好的营业部、且没有一日游席位，跟随次日开盘买入。",
    rule="当日上龙虎榜；买方前五席位中“强势席位”≥ 2 个，“一日游席位”为 0。强势席位：除机构专用外，"
         "过去上榜买入且已到期的交易 ≥ 5 笔，次日开盘买、第 3 根 K 线收盘卖的收缩平均收益 ≥ 1%；"
         "一日游席位：已到期买入 ≥ 5 笔、其中 ≥ 60% 次日就出现在同一只股票的卖方。只用当天以前已到期的交易。"
         "得分 = 强势席位战绩之和。次日开盘买入，持有 3 日，止损 7%。",
    source=SEAT_SOURCE, params=[param("min_smart", "强势席位数下限", 2, 1, 5, 1)],
    exits=short_exits(3, 7), sizing=equal(5, 2)), _smart_seats, requires_history=("smart_buyers",))


def _institution(panel, p):
    hit = (
        c("lhb_on") & (c("inst_net") >= p["min_share"] / 100 * c("amount"))
        & (c("inst_net") > 0) & _not_limit(0.95)
    )
    return emit(panel, hit, c("inst_net") / c("amount"))


add(PlaybookSpec(
    id="institution_net_buy", name="机构净买入", style="sentiment", horizon="swing",
    summary="龙虎榜上机构专用席位净买入超过当日成交额 3%，且当天没有涨停（不追高），中期跟随。",
    rule="当日上龙虎榜；机构专用席位净额 = Σ(买入额 − 卖出额)（前五席位口径，买、卖两榜合并，同一席位在两榜重复出现只算一次）≥ 当日成交额 × 3%；当日涨幅低于涨停幅度的 95%、未触及涨停。"
         "得分 = 机构净买入 / 成交额。次日开盘买入，持有 5 日，止损 7%。",
    source=COMMUNITY + "；机构席位偏中期", params=[param("min_share", "净买入占成交额下限(%)", 3, 1, 10, 0.5)],
    exits=short_exits(5, 7), sizing=equal(5, 2)), _institution, requires_history=("inst_net",))
