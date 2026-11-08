"""Public SSOT. Amounts/caps: 亿元; rates: percentage points. Unknowns stay null."""
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class BoardType(str, Enum):
    MAIN_10CM = "10cm"
    CHINEXT_20CM = "20cm"
    BSE_30CM = "30cm"

class MacroPoolType(str, Enum):
    ALL = "all"
    LIMIT_UP_LADDER = "limit_up"
    N_DAYS_M_BOARDS = "n_days_m_boards"
    BROKEN_OR_ATTACK = "broken_attack"
    CONTINUOUS_GROWTH = "growth_streak"

class TurnoverRegime(str, Enum):
    ALL = "all"
    SHRINK_ACCEL = "shrink"
    NORMAL_HAND = "normal"
    EXPAND_DIV = "expand"

class DataProvenance(Contract):
    mode: Literal["LIVE", "SIM"]
    source: str
    as_of: str
    trade_date: str
    status: Literal["ready", "fallback", "stale"] = "ready"
    detail: str
    sequence: int = 0
    latency_ms: float = 0
    coverage: int = 0
    missing_fields: list[str] = Field(default_factory=list)

class FactorContribution(Contract):
    key: str
    label: str
    score: float | None = None
    weight: float
    explanation: str

class StockTickRecord(Contract):
    code: str
    name: str
    board: BoardType
    is_st: bool = False
    is_new: bool = False
    is_suspended: bool = False
    listing_date: str | None = None
    days_since_listing: int | None = None
    price: float
    pct_chg: float
    amount_today: float
    float_mkt_cap: float | None = None
    total_mkt_cap: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    free_float_mkt_cap: float | None = None
    is_limit_up: bool = False
    ever_limit_up_today: bool = False
    limit_streak: int | None = None
    n_days_m_boards_label: str | None = None
    growth_streak: int | None = None
    streak_cum_return: float | None = None
    turnover_today: float | None = None
    turnover_yesterday: float | None = None
    turnover_ratio_yd: float | None = None
    volume_ratio: float | None = None
    first_seal_time: str | None = None
    last_seal_time: str | None = None
    open_board_count: int | None = None
    seal_order_amount: float | None = None
    seal_to_amount_ratio: float | None = None
    seal_to_cap_ratio: float | None = None
    seal_span_minutes: float | None = None
    early_seal: bool | None = None
    zt_count_1y: int | None = None
    seal_success_rate_1y: float | None = None
    max_streak_1y: int | None = None
    dist_to_60d_high: float | None = None
    high_60d: float | None = None
    zt_count_1y_bar: int | None = None
    theme_tags: list[str] = Field(default_factory=list)
    intraday_sparkline: list[float] = Field(default_factory=list)
    composite_score: float | None = None
    factors: list[FactorContribution] = Field(default_factory=list)

class ScreenerFilterRequest(Contract):
    boards: list[BoardType] = Field(default_factory=lambda: list(BoardType))
    exclude_st: bool = True
    exclude_new: bool = True
    include_suspended: bool = False
    pool_type: MacroPoolType = MacroPoolType.ALL
    limit_streak_exact: list[int] | None = None
    min_growth_streak: int | None = Field(None, ge=1, le=100)
    allow_doji_tolerance: bool = False
    free_cap_min: float = Field(0, ge=0)
    free_cap_max: float = Field(20000, ge=0)
    total_cap_min: float | None = Field(None, ge=0)
    total_cap_max: float | None = Field(None, ge=0)
    pe_max: float | None = Field(None, description="市盈率(TTM)上限；亏损（负值）不满足")
    pb_max: float | None = Field(None, ge=0)
    price_min: float = Field(0, ge=0)
    price_max: float = Field(10000, ge=0)
    pct_chg_min: float | None = Field(None, ge=-100, le=100)
    pct_chg_max: float | None = Field(None, ge=-100, le=100)
    turnover_today_min: float = Field(0, ge=0)
    turnover_today_max: float = Field(100, ge=0)
    turnover_yd_min: float = Field(0, ge=0)
    turnover_yd_max: float = Field(100, ge=0)
    turnover_regime: TurnoverRegime = TurnoverRegime.ALL
    turnover_ratio_min: float | None = Field(None, ge=0)
    turnover_ratio_max: float | None = Field(None, ge=0)
    min_amount_today: float = Field(0, ge=0)
    max_first_seal_time: str | None = Field(None, pattern=r"^(09|1[0-5]):[0-5]\d:[0-5]\d$")
    max_open_board_count: int | None = Field(None, ge=0)
    min_seal_to_amount_ratio: float = Field(0, ge=0)
    only_early_seal: bool = False
    min_seal_span_minutes: float | None = Field(None, ge=0)
    min_streak_cum_return: float | None = None
    min_zt_count_1y: int = Field(0, ge=0)
    min_seal_success_rate_1y: float = Field(0, ge=0, le=100)
    only_near_60d_high: bool = False
    selected_themes: list[str] = Field(default_factory=list)
    search: str = Field("", max_length=80)
    codes: list[str] | None = None
    sort_by: Literal["composite_score", "pct_chg", "price", "limit_streak", "growth_streak", "amount_today", "turnover_today", "free_float_mkt_cap", "turnover_ratio_yd", "seal_to_amount_ratio"] = "composite_score"
    sort_desc: bool = True
    offset: int = Field(0, ge=0)
    limit: int = Field(300, ge=1, le=6000)

    @model_validator(mode="after")
    def ordered_ranges(self):
        for low, high in [("free_cap_min", "free_cap_max"), ("price_min", "price_max"), ("pct_chg_min", "pct_chg_max"), ("total_cap_min", "total_cap_max"), ("turnover_today_min", "turnover_today_max"), ("turnover_yd_min", "turnover_yd_max"), ("turnover_ratio_min", "turnover_ratio_max")]:
            a, b = getattr(self, low), getattr(self, high)
            if a is not None and b is not None and a > b:
                raise ValueError(f"{low} must not exceed {high}")
        return self

class LadderMember(Contract):
    """One stock inside a ladder tier, enough to draw it without a second request."""
    code: str
    name: str
    pct_chg: float
    is_limit_up: bool
    first_seal_time: str | None = None
    open_board_count: int | None = None
    seal_order_amount: float | None = None
    theme: str | None = None


class LadderTierStat(Contract):
    tier_key: str
    title: str
    count: int
    promotion_rate: float | None = None
    top_stock_name: str | None = None
    top_theme: str | None = None
    members: list[LadderMember] = Field(default_factory=list)


class SealPoint(Contract):
    """A limit-up (or broken) stock placed on the session clock by first seal time."""
    code: str
    name: str
    first_seal_time: str
    limit_streak: int | None = None
    is_limit_up: bool
    open_board_count: int | None = None

class ThemeResonanceStat(Contract):
    theme_name: str
    stock_count: int
    limit_up_count: int
    growth_count: int
    avg_pct_chg: float
    amount: float
    leader_code: str
    leader_name: str
    strength: float
    sparkline: list[float] = Field(default_factory=list)

class Breadth(Contract):
    up: int
    down: int
    flat: int
    buckets: list[int]
    bucket_labels: list[str]

class MarketHistoryPoint(Contract):
    time: str
    value: float

class MacroDashboardSummary(Contract):
    provenance: DataProvenance
    total_stocks: int
    total_limit_up: int
    total_broken_board: int
    broken_rate: float
    max_limit_height: int
    max_height_stock: str
    yesterday_limit_up_premium: float | None = None
    total_amount: float
    sentiment_score: float
    breadth: Breadth
    limit_ladder_tiers: list[LadderTierStat]
    growth_streak_tiers: list[LadderTierStat]
    hot_themes: list[ThemeResonanceStat]
    market_curve: list[MarketHistoryPoint]
    seal_timeline: list[SealPoint] = Field(default_factory=list)

class ScreenerQueryResponse(Contract):
    provenance: DataProvenance
    total_matched: int
    execution_ms: float
    records: list[StockTickRecord]

class OHLCBar(Contract):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float

class IndicatorSeries(Contract):
    """One indicator line aligned index-for-index with `StockDetailResponse.bars`."""
    key: str
    label: str
    pane: Literal["price", "volume", "macd", "kdj", "rsi"]
    style: Literal["line", "histogram"] = "line"
    values: list[float | None]


class PatternFamily(str, Enum):
    TREND = "trend"
    VOLUME = "volume"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    VOLATILITY = "volatility"


class PatternDefinition(Contract):
    id: str
    name: str
    family: PatternFamily
    rule: str
    source: str
    source_url: str


class PatternHit(Contract):
    pattern_id: str
    code: str
    name: str
    date: str
    price: float | None = None
    pct_chg: float | None = None
    strength: float | None = None
    note: str


class StockDetailResponse(Contract):
    provenance: DataProvenance
    history_provenance: DataProvenance
    stock: StockTickRecord
    bars: list[OHLCBar]
    observations: list[str]
    indicators: list[IndicatorSeries] = Field(default_factory=list)
    patterns: list[PatternHit] = Field(default_factory=list)

class StrategyDefinition(Contract):
    id: str
    name: str
    subtitle: str
    description: str
    category: str
    signal: Literal["momentum", "breakout", "mean_reversion"]
    lookback: int
    holding_days: int
    filters: ScreenerFilterRequest
    factor_weights: dict[str, float]
    inspiration: str
    source_url: str
    methodology: str

class StrategyCatalog(Contract):
    strategies: list[StrategyDefinition]

class BacktestRequest(Contract):
    strategy_id: str = Field("momentum_ignition", pattern=r"^[a-z][a-z0-9_]{2,40}$")
    code: str = Field("002085", pattern=r"^\d{6}$")
    lookback: int = Field(20, ge=5, le=60)
    holding_days: int = Field(5, ge=1, le=20)
    commission_bps: float = Field(3, ge=0, le=100)
    slippage_bps: float = Field(5, ge=0, le=100)
    stamp_duty_bps: float = Field(5, ge=0, le=100)
    initial_cash: float = Field(100000, ge=10000, le=1e9)
    stop_loss_pct: float | None = Field(None, ge=0.5, le=50, description="固定止损(%)")
    take_profit_pct: float | None = Field(None, ge=1, le=200, description="固定止盈(%)")
    trailing_stop_pct: float | None = Field(None, ge=0.5, le=50, description="自持有期最高收盘回撤(%)离场")

class EquityPoint(Contract):
    date: str
    strategy: float
    benchmark: float | None = Field(None, description="基准（中证全指或个股持有）重定基为 100；没有基准数据时为空")
    drawdown: float

class TradeRecord(Contract):
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    costs: float

class BacktestMetrics(Contract):
    """Tear-sheet statistics derived from the equity curve and the trade list."""
    annual_return: float | None = None
    annual_volatility: float | None = None
    benchmark_annual_return: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    max_drawdown_days: int | None = None
    profit_factor: float | None = None
    expectancy: float | None = None
    avg_holding_days: float | None = None
    best_trade: float | None = None
    worst_trade: float | None = None
    max_consecutive_losses: int = 0


class MonthlyReturn(Contract):
    month: str
    value: float


class BacktestResponse(Contract):
    run_id: str
    created_at: str
    provenance: DataProvenance
    request: BacktestRequest
    total_return: float
    benchmark_return: float
    max_drawdown: float
    sharpe_ratio: float | None = None
    win_rate: float | None = None
    trade_count: int
    total_costs: float
    exposure_pct: float
    equity: list[EquityPoint]
    trades: list[TradeRecord]
    assumptions: list[str]
    metrics: BacktestMetrics | None = None
    monthly_returns: list[MonthlyReturn] = Field(default_factory=list)

class BacktestRunSummary(Contract):
    run_id: str
    created_at: str
    strategy_id: str
    code: str
    total_return: float
    mode: Literal["LIVE", "SIM"]

class BacktestRunsResponse(Contract):
    runs: list[BacktestRunSummary]

class BacktestBatchRequest(Contract):
    """Same signal and cost model applied to a short list of stocks."""
    strategy_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{2,40}$")
    codes: list[str] = Field(..., min_length=1, max_length=20)
    lookback: int = Field(20, ge=5, le=60)
    holding_days: int = Field(5, ge=1, le=20)
    commission_bps: float = Field(3, ge=0, le=100)
    slippage_bps: float = Field(5, ge=0, le=100)
    stamp_duty_bps: float = Field(5, ge=0, le=100)
    initial_cash: float = Field(100000, ge=10000, le=1e9)

    @field_validator("codes")
    @classmethod
    def six_digit_codes(cls, value: list[str]) -> list[str]:
        for code in value:
            if not (len(code) == 6 and code.isdigit()):
                raise ValueError(f"股票代码必须是 6 位数字：{code}")
        return list(dict.fromkeys(value))

class BacktestBatchRow(Contract):
    code: str
    name: str | None = None
    total_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    max_drawdown: float | None = None
    trade_count: int = 0
    win_rate: float | None = None
    error: str | None = None

class BacktestBatchResponse(Contract):
    provenance: DataProvenance
    strategy_id: str
    requested: int
    completed: int
    median_return: float | None = None
    beat_benchmark: int = 0
    rows: list[BacktestBatchRow]

class WatchlistRequest(Contract):
    codes: list[str] = Field(max_length=300)

class WatchlistResponse(Contract):
    codes: list[str]
    updated_at: str

class ModeSwitchRequest(Contract):
    mode: Literal["LIVE", "SIM"]

class StreamEvent(Contract):
    sequence: int
    as_of: str
    mode: Literal["LIVE", "SIM"]
    events: int = 0

class HealthResponse(Contract):
    status: str
    version: str
    provenance: DataProvenance

class AgentToolDefinition(Contract):
    name: str
    description: str
    method: Literal["GET", "POST", "PUT"]
    path: str
    input_schema: str | None = None
    output_schema: str

class AgentManifest(Contract):
    name: str = "Pulse Research API"
    version: str = "3.0.0"
    contract_url: str = "/openapi.json"
    tools: list[AgentToolDefinition]

class ProviderStatus(Contract):
    key: str
    name: str
    kind: Literal["scenario", "live", "history", "store"]
    available: bool
    active: bool
    detail: str
    latency_ms: float | None = None

class ProvidersResponse(Contract):
    providers: list[ProviderStatus]

class FieldCoverage(Contract):
    field: str
    label: str
    coverage: float = Field(ge=0, le=100)
    missing: int = Field(ge=0)

class QualityResponse(Contract):
    provenance: DataProvenance
    rows: int
    fields: list[FieldCoverage]


# --- Pattern scanner (Sequoia / InStock classic A-share selection rules) ---------

class PatternSummary(Contract):
    pattern: PatternDefinition
    hit_count: int
    hits: list[PatternHit]


class PatternScanResponse(Contract):
    provenance: DataProvenance
    universe: int
    scanned: int
    execution_ms: float
    patterns: list[PatternSummary]


# --- Factor lab (alphalens-style IC and quantile analysis) -----------------------

FactorKey = Literal[
    "momentum_20", "reversal_5", "volatility_20", "volume_surge", "dist_high_60", "ma_gap_20"
]


class FactorInfo(Contract):
    key: FactorKey
    label: str
    description: str


class FactorLabRequest(Contract):
    factor: FactorKey = "momentum_20"
    horizon: int = Field(5, ge=1, le=20)
    quantiles: int = Field(5, ge=3, le=10)
    window: int = Field(120, ge=40, le=240)


class ICPoint(Contract):
    date: str
    ic: float | None = None


class QuantileReturn(Contract):
    quantile: int
    mean_return: float
    count: int


class FactorLabResponse(Contract):
    provenance: DataProvenance
    request: FactorLabRequest
    catalog: list[FactorInfo]
    universe: int
    days: int
    execution_ms: float
    ic_mean: float | None = None
    ic_std: float | None = None
    icir: float | None = None
    ic_t_stat: float | None = None
    ic_positive_ratio: float | None = None
    ic_series: list[ICPoint]
    quantile_returns: list[QuantileReturn]
    long_short: list[MarketHistoryPoint]
    top_turnover: float | None = None
    notes: list[str]


# --- Natural-language screener (iWencai-style, deterministic parser) -------------

class ParseRequest(Contract):
    text: str = Field(..., min_length=1, max_length=200)


class ParsedToken(Contract):
    text: str
    field: str
    label: str


class ParseResponse(Contract):
    filters: ScreenerFilterRequest
    tokens: list[ParsedToken]
    unmatched: list[str]
    summary: str


# --- Market event tape (snapshot-to-snapshot diff) -------------------------------

MarketEventKind = Literal["seal", "break", "reseal", "surge", "plunge", "new_high"]


class MarketEvent(Contract):
    id: str
    sequence: int
    time: str
    kind: MarketEventKind
    code: str
    name: str
    price: float
    pct_chg: float
    limit_streak: int | None = None
    detail: str
    themes: list[str] = Field(default_factory=list)


class MarketEventsResponse(Contract):
    provenance: DataProvenance
    events: list[MarketEvent]


# --- Warehouse: historical daily bars for research ------------------------------

WarehouseDataset = Literal["live", "sim"]


class WarehouseTable(Contract):
    name: str
    rows: int
    codes: int | None = None
    start: str | None = None
    end: str | None = None
    files: int
    bytes: int


class SyncJob(Contract):
    id: str
    kind: Literal["backfill", "daily", "synthetic"]
    status: Literal["running", "done", "failed"]
    started_at: str
    finished_at: str | None = None
    progress: float = Field(0, ge=0, le=1)
    done: int = 0
    total: int = 0
    errors: int = 0
    message: str = ""


class WarehouseStatus(Contract):
    active: WarehouseDataset
    provenance: DataProvenance
    datasets: dict[str, list[WarehouseTable]]
    latest_trade_date: str | None = None
    coverage_codes: int = 0
    job: SyncJob | None = None
    notes: list[str] = Field(default_factory=list)


class SyncRequest(Contract):
    kind: Literal["backfill", "daily"] = "daily"
    years: int = Field(3, ge=1, le=10)
    codes: list[str] | None = Field(None, max_length=6000, description="只同步这些代码；为空表示全市场")


# --- Market regime: trend, liquidity, sentiment, style ---------------------------

TrendRegime = Literal["bull", "bear", "range"]
LiquidityRegime = Literal["expanding", "steady", "contracting"]
SentimentPhase = Literal["freezing", "repair", "climax", "ebb"]
StyleRegime = Literal["large", "small", "balanced"]


class RegimePoint(Contract):
    date: str
    index_close: float | None = None
    trend: TrendRegime
    liquidity: LiquidityRegime
    sentiment: SentimentPhase
    style: StyleRegime
    breadth_ma20: float | None = None
    limit_up_count: int | None = None
    broken_rate: float | None = None
    max_height: int | None = None
    amount_ratio: float | None = None
    premium: float | None = None


class RegimeState(RegimePoint):
    label: str
    explanation: list[str]


class RegimeHistoryResponse(Contract):
    provenance: DataProvenance
    index_name: str
    current: RegimeState
    points: list[RegimePoint]
    rules: list[str]


# --- Playbooks (战法库): catalogued, backtested, regime-aware ---------------------

PlaybookHorizon = Literal["short", "swing", "long"]
PlaybookStyle = Literal["sentiment", "trend", "momentum", "reversal", "breakout", "volume", "volatility", "value"]


class PlaybookParam(Contract):
    key: str
    label: str
    value: float
    min: float
    max: float
    step: float


class ExitRules(Contract):
    """Hikyuu-style exit components; whichever triggers first closes the trade."""
    holding_days: int = Field(5, ge=1, le=250)
    stop_loss_pct: float | None = Field(8, ge=0.5, le=50)
    take_profit_pct: float | None = Field(None, ge=1, le=300)
    trailing_stop_pct: float | None = Field(None, ge=0.5, le=50)
    # Sell rules from A-share practice. Each is checked on a completed bar and sells at
    # the next open, except the weak-open rule, which sells at that open itself.
    weak_open_pct: float | None = Field(None, ge=0.5, le=10, description="次日不及预期：买入次日开盘低于买入价该比例，开盘卖")
    sell_if_not_limit: bool = Field(False, description="断板卖：收盘没有封住涨停，次日开盘卖（接力类）")
    sell_below_ma5: bool = Field(False, description="收盘跌破 5 日线，次日开盘卖")
    sell_on_stall: bool = Field(False, description="放量滞涨：成交量超过 5 日均量 2 倍且涨幅不足 2%，次日开盘卖")
    sell_on_fade: bool = Field(False, description="冲高回落：盘中最高涨幅 ≥ 7% 而收盘涨幅 < 3%，次日开盘卖")
    chandelier_atr: float | None = Field(None, ge=1, le=6, description="吊灯止损：收盘低于持仓期最高收盘 − N×ATR14，次日开盘卖")
    sell_on_ebb: bool = Field(False, description="情绪退潮：市场情绪判定为退潮的当天收盘后，次日开盘卖")
    sell_on_broken: bool = Field(False, description="炸板卖：盘中触及涨停但收盘没封住，次日开盘卖")
    sell_on_shadow: bool = Field(
        False, description="高位长上影：持仓盈利时，上影线占当日振幅一半以上且成交量超过 5 日均量 2 倍，次日开盘卖",
    )
    bias_exit_pct: float | None = Field(None, ge=3, le=50, description="乖离过大：收盘高于 20 日线该比例以上，次日开盘卖")
    scale_out_pct: float | None = Field(
        None, ge=1, le=100, description="分批止盈：盘中涨到买入价 +该比例时按该价卖出一半，余下按其它规则卖",
    )
    t_high_pct: float | None = Field(
        None, ge=1, le=9, description="倒T：持仓日盘中涨到昨收 +该比例时按该价卖出 t_share 的底仓，收盘买回",
    )
    t_low_pct: float | None = Field(
        None, ge=1, le=9, description="正T：持仓日盘中跌到昨收 −该比例时按该价买入 t_share 的底仓数量，收盘卖出同样数量",
    )
    t_share: float = Field(0.5, ge=0.1, le=1, description="做T 每次的数量占底仓的比例（按 100 股取整）")


class EntryRules(Contract):
    """When to buy the day after a signal; the default is the open."""
    max_gap_pct: float | None = Field(None, ge=0, le=20, description="不追高：次日开盘高于信号日收盘该比例以上就放弃")
    pullback_pct: float | None = Field(
        None, ge=0.5, le=10,
        description="回踩买：次日在信号日收盘价下方该比例挂单，盘中触及才成交；开盘已低于挂单价按开盘价成交",
    )


class SizingRule(Contract):
    method: Literal["equal", "volatility"] = "equal"
    max_positions: int = Field(5, ge=1, le=50)
    max_new_per_day: int = Field(2, ge=1, le=20)


class PlaybookSpec(Contract):
    id: str
    name: str
    style: PlaybookStyle
    horizon: PlaybookHorizon
    summary: str
    rule: str
    source: str
    source_url: str = ""
    params: list[PlaybookParam] = Field(default_factory=list)
    exits: ExitRules
    sizing: SizingRule
    entry: EntryRules = Field(default_factory=EntryRules)
    available: bool = True
    unavailable_reason: str | None = None


class PerformanceStats(Contract):
    trades: int = 0
    win_rate: float | None = None
    avg_return: float | None = None
    median_return: float | None = None
    profit_factor: float | None = None
    t_stat: float | None = None
    avg_holding_days: float | None = None
    annual_return: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    exposure_pct: float | None = None


class RegimePerformance(Contract):
    dimension: Literal["trend", "liquidity", "sentiment", "style"]
    regime: str
    label: str
    stats: PerformanceStats


PlaybookVerdict = Literal["effective", "regime_dependent", "weak", "overfit", "insufficient", "unavailable"]


class PlaybookTrade(Contract):
    code: str
    name: str | None = None
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    holding_days: int
    t_trades: int = Field(0, description="持有期间做T 的次数")
    exit_reason: Literal[
        "time", "stop", "target", "trailing", "end",
        "weak_open", "not_limit", "below_ma5", "stall", "fade", "chandelier", "ebb",
        "broken", "shadow", "bias", "t_out",
    ]
    regime: str | None = None


class PlaybookSummary(Contract):
    spec: PlaybookSpec
    full: PerformanceStats
    out_of_sample: PerformanceStats
    by_regime: list[RegimePerformance]
    fitness: float | None = Field(None, ge=0, le=100)
    verdict: PlaybookVerdict
    verdict_reason: str
    signals_today: int = 0
    equity_sparkline: list[float] = Field(default_factory=list)


class PlaybookEvaluation(PlaybookSummary):
    provenance: DataProvenance
    period_start: str
    period_end: str
    split_date: str
    in_sample: PerformanceStats
    recent: PerformanceStats
    equity: list[EquityPoint]
    trades: list[PlaybookTrade]
    fitness_reasons: list[str]
    assumptions: list[str]
    execution_ms: float


class PlaybookBoard(Contract):
    provenance: DataProvenance
    regime: RegimeState
    playbooks: list[PlaybookSummary]
    computed_at: str
    execution_ms: float


class PlaybookRunRequest(Contract):
    params: dict[str, float] = Field(default_factory=dict)
    exits: ExitRules | None = None
    sizing: SizingRule | None = None


# --- Daily picks: ensemble of fit playbooks --------------------------------------

class PickReason(Contract):
    playbook_id: str
    playbook_name: str
    weight: float
    signal: float = Field(ge=0, le=1, description="该战法当日信号中的强度分位（0–1），集成打分用的就是它")
    win_rate: float | None = None
    avg_return: float | None = None
    note: str


class StockPick(Contract):
    rank: int
    code: str
    name: str
    horizon: PlaybookHorizon
    score: float = Field(ge=0, le=100)
    technical: float | None = None
    sentiment: float | None = None
    trend: float | None = None
    agreement: int
    close: float | None = None
    pct_chg: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    holding_days: int
    entry_note: str
    reasons: list[PickReason]
    risk_flags: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    exit_plan: list[str] = Field(default_factory=list, description="卖出计划：按顺序检查的卖出条件")


class PlaybookWeight(Contract):
    playbook_id: str
    name: str
    horizon: PlaybookHorizon
    weight: float
    fitness: float | None = None
    verdict: PlaybookVerdict


class DailyReport(Contract):
    trade_date: str
    generated_at: str
    provenance: DataProvenance
    regime: RegimeState
    weights: list[PlaybookWeight]
    short: list[StockPick]
    swing: list[StockPick]
    long: list[StockPick]
    overall: list[StockPick]
    notes: list[str]
    selection: Literal["rotation", "fitness"] = Field(
        "fitness", description="投票战法的来源：rotation = 近期轮换推选（已通过检验），fitness = 适配度门槛"
    )
    market_mode: Literal["normal", "bear"] = Field(
        "normal", description="bear = 中证全指收盘低于 60 日均线：暂停短线、波段推荐，长线只保留低波动类防守战法"
    )
    market_detail: str | None = Field(None, description="熊市模式的依据（指数收盘与 60 日均线）")
    pushed: bool = False


class ReportIndexItem(Contract):
    trade_date: str
    generated_at: str
    regime_label: str
    top: list[str]


class ReportIndex(Contract):
    reports: list[ReportIndexItem]


TrackHorizon = Literal["short", "swing", "long", "overall"]


TrackKind = Literal["simulated", "recorded"]


class TrackRecordPoint(Contract):
    """`simulated`: out-of-sample replay of the ensemble with weights fixed on
    in-sample data only. `recorded`: a report that was actually published."""
    trade_date: str
    horizon: TrackHorizon
    kind: TrackKind = "recorded"
    picks: int
    matured: int
    avg_return: float | None = None
    hit_rate: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None


class TrackRecordSummary(Contract):
    horizon: TrackHorizon
    kind: TrackKind = "recorded"
    reports: int
    matured_picks: int
    avg_return: float | None = None
    hit_rate: float | None = None
    excess_return: float | None = None


class TrackRecordResponse(Contract):
    provenance: DataProvenance
    summary: list[TrackRecordSummary]
    points: list[TrackRecordPoint]
    notes: list[str]


# --- Crowd snapshot: which packs moved today ---------------------------------------

class CrowdStock(Contract):
    code: str
    name: str
    pct_chg: float | None = None
    limit_streak: int = 0
    is_limit_up: bool = False


class IndustryHeat(Contract):
    industry: str
    limit_ups: int
    members: int
    avg_pct: float | None = None
    leader: CrowdStock | None = Field(None, description="连板最高、其次成交额最大的涨停股")
    followers: list[CrowdStock] = Field(default_factory=list, description="同行业涨幅靠前但未涨停的股票")


class NameCluster(Contract):
    char: str = Field(description="名称里共有的少见字")
    limit_ups: list[CrowdStock]
    followers: list[CrowdStock] = Field(default_factory=list, description="同字、涨幅 ≥ 3% 但未涨停")


class SeatRecord(Contract):
    seat: str
    events: int = Field(description="已到期的上榜买入次数")
    avg_return: float | None = Field(None, description="次日开盘买、第 3 根收盘卖的收缩平均收益(%)")
    trip_share: float | None = Field(None, description="买入后次日即上卖方榜的比例(%)")
    smart: bool
    trip: bool
    bought_today: list[str] = Field(default_factory=list, description="今天买入的股票名称")


class LhbStock(Contract):
    code: str
    name: str
    pct_chg: float | None = None
    smart_buyers: int = 0
    trip_buyers: int = 0
    inst_net: float | None = Field(None, description="机构专用净买入（亿元，前五席位口径）")


class CrowdSnapshot(Contract):
    provenance: DataProvenance
    trade_date: str
    industries: list[IndustryHeat]
    names: list[NameCluster]
    lhb: list[LhbStock]
    seats: list[SeatRecord]
    notes: list[str]


# --- Exit lab: how to sell -------------------------------------------------------

class ExitVariantResult(Contract):
    key: str
    label: str
    exits: ExitRules
    entry: EntryRules = Field(default_factory=EntryRules)
    in_sample: PerformanceStats
    out_of_sample: PerformanceStats


class PlaybookExitStudy(Contract):
    playbook_id: str
    name: str
    horizon: PlaybookHorizon
    variants: list[ExitVariantResult]
    chosen_key: str = Field(description="样本内夏普最高的卖法；都不如默认时为 default")
    improved_out_of_sample: bool | None = Field(None, description="选中的卖法在样本外是否好过默认卖法；选中默认时为空")
    plan: list[str] = Field(default_factory=list, description="选中卖法的中文操作要点")
    entry_note: str = Field("", description="选中买法的中文操作要点")


class ExitLabReport(Contract):
    provenance: DataProvenance
    studies: list[PlaybookExitStudy]
    compared: int = Field(description="选中了非默认卖法的战法数")
    improved: int = Field(description="其中样本外也更好的战法数")
    summary: str
    notes: list[str]
    execution_ms: float


# --- Walk-forward rotation: which playbooks work now -----------------------------

class RotationScore(Contract):
    playbook_id: str
    name: str
    horizon: PlaybookHorizon
    trades: int = Field(description="已平仓交易数（推选窗口内）")
    avg_return: float | None = Field(None, description="窗口内每笔平均收益(%)，扣成本")
    score: float | None = Field(None, description="排序分：收益之和 ÷（笔数 + 收缩项）")
    selected: bool
    window: int = Field(0, description="推选窗口（交易日）")


class RotationPeriod(Contract):
    start: str
    end: str
    chosen: list[str] = Field(description="该期推选的战法名称；为空表示空仓")
    selected_return: float | None = Field(None, description="推选战法该期每笔平均收益(%)，空仓记 0")
    baseline_return: float | None = Field(None, description="全部战法同期每笔平均收益(%)")


class RotationStats(Contract):
    periods: int = 0
    selected_avg: float | None = None
    baseline_avg: float | None = None
    excess_avg: float | None = Field(None, description="每期超额的平均值（百分点）")
    hit_rate: float | None = Field(None, description="跑赢基线的期数占比(%)")
    t_stat: float | None = Field(None, description="超额的 t 值，Newey-West 滞后 1 期（相邻期持仓重叠）")
    cash_periods: int = 0
    invested_periods: int = 0
    invested_excess_avg: float | None = Field(None, description="只看有持仓的期：每期超额的平均值")


class RotationConfig(Contract):
    key: str
    label: str
    method: Literal["trailing", "regime"]
    window: int
    top_k: int
    in_sample: RotationStats = Field(description="分割日及以前结束的期：只用来选设置")
    out_of_sample: RotationStats = Field(description="分割日以后开始的期：检验选中的设置")


class RotationReport(Contract):
    provenance: DataProvenance
    step: int = Field(description="每隔多少个交易日重新推选")
    default_key: str = Field(description="样本内 t 值最高的设置；当前推选与样本外检验都用它")
    split_date: str = Field(description="样本内最后一个交易日")
    decided_on: str | None = Field(None, description="当前推选的推选日（与逐期检验同一个锚点）")
    next_in_days: int | None = Field(None, description="距下次重新推选还有几个交易日")
    configs: list[RotationConfig]
    periods: list[RotationPeriod]
    current: list[RotationScore]
    validated: bool
    verdict: str
    notes: list[str]
    execution_ms: float


class PushStatus(Contract):
    configured: bool
    kind: str | None = None
    last_pushed: str | None = None
    detail: str


# --- Analog forecast: what happened after similar setups -------------------------

class AnalogQuantiles(Contract):
    day: int
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float


class AnalogExample(Contract):
    code: str
    date: str
    distance: float
    forward_return: float


class AnalogForecast(Contract):
    provenance: DataProvenance
    code: str
    as_of: str
    features: dict[str, float | None]
    neighbors: int
    horizon: int
    prob_up: float | None = None
    mean_return: float | None = None
    path: list[AnalogQuantiles]
    examples: list[AnalogExample]
    notes: list[str]
