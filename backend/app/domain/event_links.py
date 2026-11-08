"""Calendar event -> stocks: a light rule table from an event's name to 申万 level-2 industries, then to their
main-board members on the decision date. M3's `catalyst_pending` only counts events linked to the stock.

Rules are hand-written v1 judgements (2026-11-02), candidate parameters like the board_rules thresholds: a change
is a new RULES_VERSION and a new ledger entry. Events too broad to point at an industry (elections, APEC
leaders, 高交会, derivative expiries, macro releases) are deliberately not linked. Linking is derived at read
time with `link`, never written back into the calendar store, so a rule change re-links history without new
revisions. Membership is point in time: a stock counts only from its join date (`sw_industry.parquet` holds
today's membership with join dates, so a stock that left an industry earlier is missing, not misplaced).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable

import polars as pl

from contracts.calendar import CalendarEvent

RULES_VERSION = "event-links-v1"
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
# (name pattern, 申万二级 industries, theme label)
RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (r"NVIDIA|英伟达|AMD", ("半导体", "元件", "通信设备", "计算机设备"), "AI算力"),
    (r"进口博览会|广交会", ("贸易Ⅱ", "航运港口"), "外贸"),
    (r"互联网大会|互联网之光", ("软件开发", "IT服务Ⅱ", "通信服务"), "互联网"),
    (r"百度世界", ("软件开发", "IT服务Ⅱ", "数字媒体"), "AI应用"),
    (r"物理AI", ("软件开发", "自动化设备"), "AI应用"),
    (r"全光", ("通信设备",), "光通信"),
    (r"半导体", ("半导体",), "半导体"),
    (r"航空", ("航空装备Ⅱ",), "航空"),
    (r"城市更新", ("房屋建设Ⅱ", "装修装饰Ⅱ", "基础建设"), "城市更新"),
    (r"农产品", ("种植业", "农产品加工"), "农业"),
    (r"光伏|储能", ("光伏设备", "电池"), "光伏储能"),
    (r"智能眼镜|AR", ("消费电子", "光学光电子"), "AI眼镜"),
    (r"具身智能|机器人", ("自动化设备", "电机Ⅱ"), "机器人"),
    (r"自动驾驶|汽车零部件", ("汽车零部件",), "智能汽车"),
    (r"美博会|美丽产业", ("化妆品", "医疗美容"), "美妆"),
    (r"双十一", ("互联网电商", "一般零售", "物流"), "电商"),
)
SKIP_CATEGORIES = {"macro_release", "market_rule"}


def members(sw: pl.DataFrame, as_of: date) -> dict[str, frozenset[str]]:
    """申万二级 industry -> main-board codes that had joined it by `as_of`."""
    rows = sw.filter(pl.col("code").str.slice(0, 3).is_in(MAIN_PREFIX)
                     & (pl.col("joined").is_null() | (pl.col("joined") <= as_of.isoformat())))
    return {k: frozenset(v) for k, v in rows.group_by("sw2").agg(pl.col("code")).iter_rows()}


def load_members(path: Path, as_of: date) -> dict[str, frozenset[str]]:
    return members(pl.read_parquet(path, columns=["code", "sw2", "joined"]), as_of)


def rule_for(ev: CalendarEvent) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Industries and themes of every rule whose pattern the name matches (AUTO TECH 自动驾驶与具身智能 hits two)."""
    if ev.category.value in SKIP_CATEGORIES:
        return None
    hits = [(industries, theme) for pattern, industries, theme in RULES if re.search(pattern, ev.name)]
    if not hits:
        return None
    return tuple(dict.fromkeys(i for ind, _ in hits for i in ind)), tuple(dict.fromkeys(t for _, t in hits))


def link(events: Iterable[CalendarEvent], industry_members: dict[str, frozenset[str]]) -> list[CalendarEvent]:
    """Copies of the events with `instruments` and `themes` filled from the rules (events without a rule keep theirs)."""
    out = []
    for ev in events:
        hit = rule_for(ev)
        if hit is None:
            out.append(ev)
            continue
        industries, themes = hit
        codes = sorted(set(ev.instruments).union(*(industry_members.get(i, frozenset()) for i in industries)))
        out.append(ev.model_copy(update={"instruments": tuple(codes), "themes": tuple(dict.fromkeys((*ev.themes, *themes)))}))
    return out
