"""The single list of things an outside agent can do with Pulse.

Each capability points at the *same* route function FastAPI serves, so the HTTP
API, the manifest, the MCP tools and llms.txt cannot drift apart: adding a
route to an agent means adding one line here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.schema import (
    AgentToolDefinition,
    AnalogForecast,
    BacktestBatchRequest,
    BacktestBatchResponse,
    BacktestRequest,
    BacktestResponse,
    CrowdSnapshot,
    DailyReport,
    ExitLabReport,
    ExitRules,
    FactorLabRequest,
    FactorLabResponse,
    MacroDashboardSummary,
    MarketEventsResponse,
    ParseRequest,
    ParseResponse,
    PatternScanResponse,
    PlaybookBoard,
    PlaybookEvaluation,
    PlaybookExitStudy,
    PlaybookRunRequest,
    ProvidersResponse,
    PushStatus,
    QualityResponse,
    RegimeHistoryResponse,
    ReportIndex,
    RotationReport,
    ScreenerFilterRequest,
    ScreenerQueryResponse,
    StockDetailResponse,
    StrategyCatalog,
    StreamEvent,
    SizingRule,
    SyncJob,
    SyncRequest,
    TrackRecordResponse,
    WarehouseStatus,
    WatchlistRequest,
    WatchlistResponse,
)
from ..api import crowd, data, events, lab, market, notify, ops, picks, playbooks, regime, research, screener, strategies, watchlist


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StockCode(_Query):
    code: str = Field(..., pattern=r"^\d{6}$", description="6 位股票代码，如 002085")


class EventsQuery(_Query):
    since: int | None = Field(None, ge=0, description="已读到的最大 sequence；只返回更新的事件")
    limit: int = Field(100, ge=1, le=300)


class PatternQuery(_Query):
    limit: int = Field(30, ge=1, le=200, description="每个形态最多返回的命中数")


class AnalogQuery(StockCode):
    horizon: int = Field(20, ge=5, le=60, description="向后观察的交易日数")


class RegimeQuery(_Query):
    days: int = Field(500, ge=60, le=2000, description="返回最近多少个交易日的周期序列")


class PlaybookId(_Query):
    playbook_id: str = Field(..., pattern=r"^[a-z0-9_]{2,40}$", description="战法 id，见 playbook_board")


class PlaybookRunInput(PlaybookId):
    params: dict[str, float] = Field(default_factory=dict)
    exits: ExitRules | None = None
    sizing: SizingRule | None = None


class ReportDate(_Query):
    trade_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="报告交易日 YYYY-MM-DD")


def _run_playbook(payload: PlaybookRunInput) -> PlaybookEvaluation:
    body = PlaybookRunRequest(params=payload.params, exits=payload.exits, sizing=payload.sizing)
    return playbooks.playbook_run(payload.playbook_id, body)


@dataclass(frozen=True)
class Capability:
    """`handler=None` marks a stream: documented, but not callable as a tool."""

    name: str
    description: str
    method: Literal["GET", "POST", "PUT"]
    path: str
    output_model: type[BaseModel]
    handler: Callable[..., BaseModel] | None = None
    input_model: type[BaseModel] | None = None
    writes: bool = False  # changes stored state; a POST that only computes does not
    destructive: bool = False  # overwrites existing data

    @property
    def read_only(self) -> bool:
        return not self.writes

    @property
    def idempotent(self) -> bool:
        """Repeating the call changes nothing more: reads, and PUT replacement."""
        return not self.writes or self.method == "PUT"

    def invoke(self, arguments: dict[str, Any] | None = None) -> BaseModel:
        """Validate `arguments` against the input model, then run the route function.

        Body models are passed whole (POST/PUT); GET inputs are path/query
        parameters, so they are spread as keyword arguments.
        """
        if self.handler is None:
            raise NotImplementedError(f"{self.name} is a stream, not a callable capability")
        if self.input_model is None:
            return self.handler()
        payload = self.input_model.model_validate(arguments or {})
        return self.handler(payload) if self.method != "GET" else self.handler(**payload.model_dump())

    def definition(self) -> AgentToolDefinition:
        body = self.input_model is not None and self.method != "GET"
        return AgentToolDefinition(
            name=self.name,
            description=self.description,
            method=self.method,
            path=self.path,
            input_schema=self.input_model.__name__ if body and self.input_model else None,
            output_schema=self.output_model.__name__,
        )


API = "/api/v1"

CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "market_overview",
        "市场结构快照：涨停数、炸板率、最高板、情绪分、连板梯队（含成员）、题材共振、封板时间线与情绪曲线。",
        "GET", f"{API}/market/overview", MacroDashboardSummary, market.overview,
    ),
    Capability(
        "market_events",
        "盘口异动流：封板、回封、炸板、急涨、急跌、新高度。由相邻快照比对得出，最新的在前。",
        "GET", f"{API}/market/events", MarketEventsResponse, events.market_events, EventsQuery,
    ),
    Capability(
        "screen_stocks",
        "多因子筛选。请求体为 ScreenerFilterRequest，返回记录含每个因子的分数与解释。",
        "POST", f"{API}/screener/query", ScreenerQueryResponse, screener.query, ScreenerFilterRequest,
    ),
    Capability(
        "parse_question",
        "把中文选股问句（如“2连板以上 换手低于15% AI算力”）确定性地解析为 ScreenerFilterRequest；无法识别的片段原样返回。",
        "POST", f"{API}/screener/parse", ParseResponse, screener.parse_query, ParseRequest,
    ),
    Capability(
        "stock_detail",
        "单只股票快照、日线、技术指标、形态命中与事实性观察文本；历史数据来源单独标记。",
        "GET", f"{API}/stocks/{{code}}", StockDetailResponse, market.stock_detail, StockCode,
    ),
    Capability(
        "strategy_catalog",
        "策略目录：筛选条件、因子权重、回测信号族、方法论与来源。",
        "GET", f"{API}/strategies", StrategyCatalog, strategies.strategies,
    ),
    Capability(
        "run_backtest",
        "按策略在指定标的上运行确定性回测，返回净值、回撤、逐笔成交与统计指标；结果会归档。",
        "POST", f"{API}/research/backtests", BacktestResponse, research.create_backtest, BacktestRequest, writes=True,
    ),
    Capability(
        "compare_backtests",
        "同一策略与费用模型下批量对比多只标的，返回中位收益、跑赢基准数与逐行结果。",
        "POST", f"{API}/research/backtests/batch", BacktestBatchResponse, research.batch_backtest, BacktestBatchRequest,
    ),
    Capability(
        "pattern_scan",
        "经典选股形态扫描（趋势、放量、突破、反转、波动）：每个形态的规则、来源与命中股票。",
        "GET", f"{API}/lab/patterns", PatternScanResponse, lab.scan_patterns, PatternQuery,
    ),
    Capability(
        "factor_lab",
        "因子 IC 与分位收益分析（动量、反转、波动、量能、距高点、均线偏离），含 ICIR 与多空净值。",
        "POST", f"{API}/lab/factors", FactorLabResponse, lab.analyse_factor, FactorLabRequest,
    ),
    Capability(
        "analog_forecast",
        "相似情景：在历史日线里找与该股今天最像的样本，返回之后 N 日收益的分位路径、上涨概率与最相似样本。不是预测。",
        "GET", f"{API}/stocks/{{code}}/analogs", AnalogForecast, market.stock_analogs, AnalogQuery,
    ),
    Capability(
        "market_regime",
        "市场周期：趋势（牛/熊/震荡）、资金（增量/存量/缩量）、情绪（冰点/修复/高潮/退潮）、风格（大/小盘），含当前判定、历史序列与判定规则。",
        "GET", f"{API}/regime", RegimeHistoryResponse, regime.regime, RegimeQuery,
    ),
    Capability(
        "playbook_board",
        "战法库总览：每套战法的全周期与样本外表现、分周期归因、当前周期适配度与结论（有效/依赖周期/偏弱/过拟合/样本不足/无法回测）。",
        "GET", f"{API}/playbooks", PlaybookBoard, playbooks.playbook_board,
    ),
    Capability(
        "crowd_snapshot",
        "抱团快照：今日行业集体涨停（龙头与跟风）、名称同字联动、龙虎榜强势席位与机构净买入、今日上榜席位的历史战绩。",
        "GET", f"{API}/crowd", CrowdSnapshot, crowd.crowd_snapshot,
    ),
    Capability(
        "exit_lab",
        "卖法实验室：每套战法在默认卖法上分别加次日不及预期、断板、冲高回落、跌破 5 日线、放量滞涨、吊灯止损、情绪退潮等规则，样本内选、样本外验。",
        "GET", f"{API}/playbooks/exits", ExitLabReport, playbooks.playbook_exit_lab,
    ),
    Capability(
        "playbook_exits",
        "单个战法的卖法对比与选中卖法的中文操作要点。",
        "GET", f"{API}/playbooks/{{playbook_id}}/exits", PlaybookExitStudy, playbooks.playbook_exits, PlaybookId,
    ),
    Capability(
        "playbook_rotation",
        "战法轮换：每 20 个交易日按近期已平仓交易重新推选战法，检验“跟着近期有效的战法做”是否跑赢全部战法一起做；含各设置对照、逐期结果与当前推选。",
        "GET", f"{API}/playbooks/rotation", RotationReport, playbooks.playbook_rotation,
    ),
    Capability(
        "playbook_detail",
        "单套战法的组合回测详情：净值、样本内外统计、分周期表现、适配度构成、最近成交与执行假设。",
        "GET", f"{API}/playbooks/{{playbook_id}}", PlaybookEvaluation, playbooks.playbook_detail, PlaybookId,
    ),
    Capability(
        "run_playbook",
        "用自定义参数、退出规则（持有天数/止损/止盈/移动止损）与仓位规则重跑一套战法；结果不缓存。",
        "POST", f"{API}/playbooks/{{playbook_id}}/run", PlaybookEvaluation, _run_playbook, PlaybookRunInput,
    ),
    Capability(
        "daily_picks",
        "今日推荐：按当前周期加权适配战法得到的短线、波段、长线与总榜，每只含理由、子分、止损止盈与风险提示；缺失时自动生成。",
        "GET", f"{API}/picks/today", DailyReport, picks.picks_today,
    ),
    Capability(
        "regenerate_picks",
        "按数据仓最新交易日重新计算当日推荐报告（同一数据下结果确定，可随时重算）。",
        "POST", f"{API}/picks/generate", DailyReport, picks.picks_generate, writes=True,
    ),
    Capability(
        "pick_history",
        "历史推荐报告索引：日期、周期标签与总榜前几名。",
        "GET", f"{API}/picks/history", ReportIndex, picks.picks_history,
    ),
    Capability(
        "pick_report",
        "读取某个交易日的推荐报告。",
        "GET", f"{API}/picks/{{trade_date}}", DailyReport, picks.picks_by_date, ReportDate,
    ),
    Capability(
        "pick_track_record",
        "推荐的历史成绩：实盘记录（已到期报告）与样本外回放（权重只用样本内确定），含胜率、平均收益与相对中证全指的超额。",
        "GET", f"{API}/picks/track-record", TrackRecordResponse, picks.picks_track_record,
    ),
    Capability(
        "warehouse_status",
        "历史数据仓状态：研究当前使用真实还是合成历史、各表行数与日期范围、同步任务进度。",
        "GET", f"{API}/data/warehouse", WarehouseStatus, data.warehouse_status,
    ),
    Capability(
        "sync_history",
        "启动历史日线同步（daily 增量或 backfill 回补 N 年）；同一时间只运行一个任务，已有任务时返回 409。",
        "POST", f"{API}/data/sync", SyncJob, data.start_sync, SyncRequest, writes=True,
    ),
    Capability(
        "push_status",
        "每日推送配置状态（企业微信/飞书/钉钉/通用 webhook）与上次推送时间。",
        "GET", f"{API}/notify/status", PushStatus, notify.notify_status,
    ),
    Capability(
        "read_watchlist",
        "读取自选代码列表。",
        "GET", f"{API}/watchlist", WatchlistResponse, watchlist.read_watchlist,
    ),
    Capability(
        "write_watchlist",
        "整体替换自选代码列表（会覆盖原有内容）。",
        "PUT", f"{API}/watchlist", WatchlistResponse, watchlist.write_watchlist, WatchlistRequest, writes=True, destructive=True,
    ),
    Capability(
        "list_providers",
        "列出全部数据边界（场景 / 实时 / 历史 / 存储）及可用性、是否在用。",
        "GET", f"{API}/providers", ProvidersResponse, ops.providers,
    ),
    Capability(
        "data_quality",
        "返回当前快照每个字段的覆盖率与缺失行数；空值不填 0。",
        "GET", f"{API}/quality", QualityResponse, ops.quality,
    ),
    Capability(
        "stream_ticks",
        "SSE 心跳：每个快照推送 sequence、as_of、数据模式与本次产生的异动事件数。",
        "GET", f"{API}/stream/sse", StreamEvent,
    ),
)

BY_NAME = {capability.name: capability for capability in CAPABILITIES}
assert len(BY_NAME) == len(CAPABILITIES), "capability names must be unique"
