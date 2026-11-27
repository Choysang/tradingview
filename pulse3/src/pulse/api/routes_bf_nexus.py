"""Nexus unified API routes (``/v1/x/bf/nexus/*``): contract-validated against ``backfill_nexus.schema.json``.

Invariants:
* Every response leaves through ``respond_backfill("nexus", <Definition>, data, meta)``; zero unvalidated raw JSON.
* All read routes declare ``openapi_extra={"x-auth": "public"}`` and clean ``operation_id``s so ``agent_access``
  automatically exposes them in ``/llms.txt``, ``/v1/x/bf/ops/manifest``, and ``POST /mcp``.
* Minute timeframes (``1m``, ``5m``, ``15m``, ``1h``) normalize cleanly to ``1d`` for 4-period TV Pro ratings.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from pulse.api import routes_market as M
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond_backfill
from pulse.application import nexus_service as N

router = APIRouter(prefix="/v1/x/bf/nexus")

TRACKS = ("composite", "short", "swing", "long")
MINUTE_TIMEFRAMES = ("1m", "5m", "15m", "1h")
TIMEFRAMES = (*MINUTE_TIMEFRAMES, "1d", "1w", "1mo", "1y")
ADJUSTMENTS = ("raw", "forward_adjusted", "backward_adjusted")
GRID_MODES = ("atr_adaptive", "geometric", "arithmetic")
ARENA_LAYERS = ("stock_selection", "sector_rotation", "regime_timing", "intraday_execution")
ARENA_HORIZONS = ("intraday_t0", "ultra_short_1_3d", "short_5d", "swing_10_20d", "position_60d")
ARENA_PARADIGMS = (
    "top_paper_2026",
    "multi_factor_fusion",
    "classic_factor",
    "technical_pattern",
    "sentiment_microstructure",
    "hot_money_seat",
    "grid_intraday_t",
    "rl_distributional",
    "llm_hybrid",
)
ARENA_ROLES = ("core_alpha", "defensive_shield", "risk_veto", "cost_reducer", "exploratory_shadow")


@router.get(
    "/hub",
    operation_id="getNexusHubOverview",
    summary="正交多策略推荐总榜、火速晋升板块备选池、五维宏观因子与多源轮询状态",
    openapi_extra={"x-auth": "public"},
)
def get_nexus_hub(
    track: str = "composite",
    limit: int = 20,
    release_id: UUID | None = None,
    conn=Depends(conn_dep),
):
    if track not in TRACKS:
        raise M._bad_request("track", "invalid_enum", f"track 必须是 {' / '.join(TRACKS)}")
    if not 1 <= limit <= 50:
        raise M._bad_request("limit", "out_of_range", "limit 必须在 1..50 之间")
    data = N.get_hub_overview(conn, horizon_track=track, limit=limit, release_id=release_id)
    m = meta(
        release_id=data.get("release_id"),
        quality_status="valid" if data.get("release_id") else "partial",
        freshness_state="current" if data.get("release_id") else "not_expected",
    )
    return respond_backfill("nexus", "NexusHubOverview", data, m)


@router.get(
    "/sectors/{node_id}/deep-dive",
    operation_id="getNexusSectorDeepDive",
    summary="板块内Top推荐买入个股榜（含龙一/中军/补涨身位）与升温备选池详情",
    openapi_extra={"x-auth": "public"},
)
def get_nexus_sector_deep_dive(
    node_id: str,
    taxonomy_id: str | None = None,
    horizon_id: str = "5td",
    release_id: UUID | None = None,
    conn=Depends(conn_dep),
):
    data, rel = N.get_sector_deep_dive(
        conn, node_id=node_id, taxonomy_id=taxonomy_id, horizon_id=horizon_id, release_id=release_id
    )
    if rel is None or data is None:
        raise ApiError(404, "NOT_FOUND", "当前没有已发布的版本或板块数据")
    m = meta(release_id=rel["release_id"], decision_as_of=rel["decision_as_of"], quality_status="valid")
    return respond_backfill("nexus", "NexusSectorDeepDive", data, m)


@router.get(
    "/stocks/{instrument_id}/pro",
    operation_id="getNexusStockProAnalysis",
    summary="个股全周期TradingView评级、枢轴点、VPVR筹码、日内做T、自适应网格、股吧四时段与龙虎榜博弈工作台",
    openapi_extra={"x-auth": "public"},
)
def get_nexus_stock_pro(
    instrument_id: UUID,
    timeframe: str = "1d",
    adjustment: str | None = None,
    adjustment_mode: str | None = None,
    release_id: UUID | None = None,
    base_shares: int = 1000,
    available_cash: float | None = None,
    cash_cny: float | None = None,
    grid_mode: str = "atr_adaptive",
    conn=Depends(conn_dep),
):
    if timeframe not in TIMEFRAMES:
        raise M._bad_request("timeframe", "invalid_enum", f"timeframe 必须是 {' / '.join(TIMEFRAMES)}")
    eff_tf = "1d" if timeframe in MINUTE_TIMEFRAMES else timeframe

    eff_adjustment = adjustment if adjustment is not None else (adjustment_mode if adjustment_mode is not None else "raw")
    if eff_adjustment not in ADJUSTMENTS:
        raise M._bad_request("adjustment", "invalid_enum", f"adjustment 必须是 {' / '.join(ADJUSTMENTS)}")

    if grid_mode not in GRID_MODES:
        raise M._bad_request("grid_mode", "invalid_enum", f"grid_mode 必须是 {' / '.join(GRID_MODES)}")

    eff_cash = available_cash if available_cash is not None else (cash_cny if cash_cny is not None else 50000.0)
    if base_shares < 0 or eff_cash < 0:
        raise M._bad_request("base_shares", "invalid_value", "底仓股数与可用资金不能为负")

    data, rel = N.get_stock_pro_analysis(
        conn,
        instrument_id=instrument_id,
        timeframe=eff_tf,
        adjustment_mode=eff_adjustment,
        release_id=release_id,
        base_sellable_shares=base_shares,
        available_cash_cny=eff_cash,
        grid_mode=grid_mode,
    )
    if rel is None:
        raise ApiError(404, "NOT_FOUND", "当前没有已发布的版本")
    if data is None:
        raise ApiError(404, "NOT_FOUND", "未找到该证券标的")
    m = meta(release_id=rel["release_id"], decision_as_of=rel["decision_as_of"], quality_status="valid")
    return respond_backfill("nexus", "NexusStockProAnalysis", data, m)


@router.get(
    "/arena",
    operation_id="getNexusArenaOverview",
    summary="62策略多维分类路由、GIPS统一记账基准、瑞士轮Glicko-2天梯与2026顶会论文算子库",
    openapi_extra={"x-auth": "public"},
)
def get_nexus_arena(
    layer: str | None = None,
    horizon_bucket: str | None = None,
    horizon: str | None = None,
    paradigm: str | None = None,
    role: str | None = None,
    conn=Depends(conn_dep),
):
    eff_horizon = horizon_bucket if horizon_bucket is not None else horizon
    if layer is not None and layer != "" and layer not in ARENA_LAYERS:
        raise M._bad_request("layer", "invalid_enum", f"layer 必须是 {' / '.join(ARENA_LAYERS)}")
    if eff_horizon is not None and eff_horizon != "" and eff_horizon not in ARENA_HORIZONS:
        raise M._bad_request("horizon_bucket", "invalid_enum", f"horizon_bucket 必须是 {' / '.join(ARENA_HORIZONS)}")
    if paradigm is not None and paradigm != "" and paradigm not in ARENA_PARADIGMS:
        raise M._bad_request("paradigm", "invalid_enum", f"paradigm 必须是 {' / '.join(ARENA_PARADIGMS)}")
    if role is not None and role != "" and role not in ARENA_ROLES:
        raise M._bad_request("role", "invalid_enum", f"role 必须是 {' / '.join(ARENA_ROLES)}")

    data = N.get_arena_overview(
        conn,
        layer=layer or None,
        horizon_bucket=eff_horizon or None,
        paradigm=paradigm or None,
        role=role or None,
    )
    return respond_backfill("nexus", "NexusArenaOverview", data, meta(quality_status="valid"))


@router.get(
    "/integrity",
    operation_id="getNexusIntegrityAudit",
    summary="全平台7入口记账口径对账、多信源字段归一化矩阵、Outbox推送通道与数据库缓存健康自检",
    openapi_extra={"x-auth": "public"},
)
def get_nexus_integrity(conn=Depends(conn_dep)):
    data = N.get_integrity_audit(conn)
    return respond_backfill("nexus", "NexusIntegrityAudit", data, meta(quality_status="valid"))
