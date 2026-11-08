"""Strategy catalogue. Each entry is a screening preset plus a backtest signal.

`inspiration` and `source_url` name the public project whose idea was adopted;
`methodology` states exactly what this system computes itself.
"""
from __future__ import annotations

from contracts.schema import (
    MacroPoolType,
    ScreenerFilterRequest,
    StrategyCatalog,
    StrategyDefinition,
    TurnoverRegime,
)

QLIB = "https://github.com/microsoft/qlib"
LEAN = "https://github.com/QuantConnect/Lean"
OPENBB = "https://github.com/OpenBB-finance/OpenBB"


def _filters(**overrides) -> ScreenerFilterRequest:
    return ScreenerFilterRequest(**overrides)


STRATEGIES: list[StrategyDefinition] = [
    StrategyDefinition(
        id="momentum_ignition",
        name="首板加速",
        subtitle="首板 + 高封成比 + 缩量",
        description="只保留今日首板、封单相对成交额足够大、且换手未失控的标的，用于观察资金第一次形成合力的位置。",
        category="动量",
        signal="momentum",
        lookback=20,
        holding_days=5,
        filters=_filters(
            pool_type=MacroPoolType.LIMIT_UP_LADDER,
            limit_streak_exact=[1],
            min_seal_to_amount_ratio=8,
            max_open_board_count=1,
            turnover_regime=TurnoverRegime.SHRINK_ACCEL,
            min_amount_today=2,
        ),
        factor_weights={"momentum": 0.35, "liquidity": 0.25, "quality": 0.30, "trend": 0.10},
        inspiration="Qlib Alpha158 的动量与量价因子分组方式",
        source_url=QLIB,
        methodology="封成比 = 封单金额 / 今日成交额；缩量定义为昨换比 < 0.75；综合分按因子权重线性加权。",
    ),
    StrategyDefinition(
        id="ladder_leader",
        name="梯队龙头",
        subtitle="三板以上 + 封板质量 + 题材共振",
        description="在三板及以上梯队中，按封板时间、炸板次数与历史封板成功率筛选出梯队内质量最高的标的。",
        category="情绪",
        signal="momentum",
        lookback=10,
        holding_days=3,
        filters=_filters(
            pool_type=MacroPoolType.LIMIT_UP_LADDER,
            limit_streak_exact=[3, 4, 5, 6, 7, 8],
            max_open_board_count=1,
            min_seal_success_rate_1y=60,
            min_zt_count_1y=3,
        ),
        factor_weights={"momentum": 0.20, "liquidity": 0.15, "quality": 0.45, "trend": 0.20},
        inspiration="A 股连板梯队与封板质量口径（本地经验规则），因子加权参考 Qlib",
        source_url=QLIB,
        methodology="晋级率按昨日同梯队个股今日是否继续封板计算；封板质量由封成比、炸板次数、近一年封板成功率合成。",
    ),
    StrategyDefinition(
        id="breakout_volume",
        name="放量突破",
        subtitle="距 60 日高点 5% 内 + 放量",
        description="价格贴近 60 日高点且换手放大，用于观察突破是否由真实成交推动。",
        category="趋势",
        signal="breakout",
        lookback=20,
        holding_days=5,
        filters=_filters(
            only_near_60d_high=True,
            turnover_regime=TurnoverRegime.EXPAND_DIV,
            min_amount_today=5,
            min_zt_count_1y=1,
        ),
        factor_weights={"momentum": 0.30, "liquidity": 0.30, "quality": 0.10, "trend": 0.30},
        inspiration="LEAN 的突破信号时序与 Donchian 通道思路",
        source_url=LEAN,
        methodology="距 60 日高点 = 现价 / 60 日最高价 - 1；放量定义为昨换比 > 1.35；回测信号使用前 N 日最高价，不含未来数据。",
    ),
    StrategyDefinition(
        id="shrink_pullback",
        name="缩量回踩",
        subtitle="炸板池 + 缩量 + 未破 60 日高点",
        description="今日冲击涨停后开板、但成交明显收缩的标的，用于观察抛压是否衰减。",
        category="反转",
        signal="mean_reversion",
        lookback=20,
        holding_days=3,
        filters=_filters(
            pool_type=MacroPoolType.BROKEN_OR_ATTACK,
            turnover_regime=TurnoverRegime.SHRINK_ACCEL,
            max_open_board_count=2,
            min_amount_today=2,
        ),
        factor_weights={"momentum": 0.20, "liquidity": 0.20, "quality": 0.35, "trend": 0.25},
        inspiration="Qlib 的均值回归因子组",
        source_url=QLIB,
        methodology="炸板池 = 今日曾触及涨停且当前未封板；回测入场条件为收盘价低于 N 日均价的 97%。",
    ),
    StrategyDefinition(
        id="growth_climb",
        name="连阳爬坡",
        subtitle="五连阳以上 + 温和换手",
        description="连续收涨五日以上、换手保持温和的标的，用于观察趋势的持续性而非单日爆发。",
        category="趋势",
        signal="momentum",
        lookback=20,
        holding_days=7,
        filters=_filters(
            pool_type=MacroPoolType.CONTINUOUS_GROWTH,
            min_growth_streak=5,
            allow_doji_tolerance=True,
            turnover_ratio_min=0.75,
            turnover_ratio_max=1.35,
        ),
        factor_weights={"momentum": 0.30, "liquidity": 0.15, "quality": 0.10, "trend": 0.45},
        inspiration="OpenBB screener 的字段组织与可解释筛选顺序",
        source_url=OPENBB,
        methodology="假阴容错：涨幅 ≥ -0.4% 且昨换比 < 0.9 仍计入连阳；连阳累计涨幅按复利计算。",
    ),
]

CATALOG = {strategy.id: strategy for strategy in STRATEGIES}


def catalog() -> StrategyCatalog:
    return StrategyCatalog(strategies=STRATEGIES)


def resolve(strategy_id: str) -> StrategyDefinition | None:
    return CATALOG.get(strategy_id)
