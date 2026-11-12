"""The five screening presets (old: research/strategies, five presets), restated on fields pulse3 can answer.

Why: LEGACY inventory "Presets (策略预设)": first-board acceleration, ladder leader, breakout volume, shrink pullback,
growth climb. Each used seal quality / turnover-regime fields that pulse3 has no PIT source for; here each preset is a
Chinese SENTENCE that the deterministic parser turns into an AST (single source of truth: the parser), using only
catalog fields. A preset is a screen, not a strategy: it carries no evidence label and changes no ranking.
Invariants: every preset sentence must parse ``complete`` (tested); the web uses these texts verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    subtitle: str
    text: str
    note: str


PRESETS: tuple[Preset, ...] = (
    Preset("first_board_accel", "首板加速", "首板 + 放量", "首板，量比大于1.5，剔除ST，剔除次新",
           "旧版另要求封成比与缩量换手；日线无封单/换手数据，改用量比。"),
    Preset("ladder_leader", "梯队龙头", "三板以上", "3连板以上，剔除ST",
           "旧版另按封板时间/炸板次数/封板成功率筛选；这些字段暂无来源，只保留连板高度。"),
    Preset("breakout_volume", "放量突破", "创60日新高 + 放量", "创60日新高，量比大于2，剔除ST",
           "旧版用昨换比判断放量；改用量比（当日量/前5日均量）。"),
    Preset("shrink_pullback", "缩量回踩", "多头排列中的缩量回落", "均线多头，量比小于0.7，5日涨幅小于0",
           "旧版取炸板池；改为均线多头趋势中的缩量小幅回落。"),
    Preset("growth_climb", "连阳爬坡", "五连阳以上", "5连阳以上，20日乖离小于15%，剔除ST",
           "旧版用温和换手；改用乖离率防止追高。"),
)
BY_ID = {p.id: p for p in PRESETS}
