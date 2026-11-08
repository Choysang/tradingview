"""Backtest execution and archive. Results are computed, never replayed from seeds."""
from statistics import median

from fastapi import APIRouter, HTTPException

from contracts.schema import (
    BacktestBatchRequest,
    BacktestBatchResponse,
    BacktestBatchRow,
    BacktestRequest,
    BacktestResponse,
    BacktestRunsResponse,
)
from ..core.engine import engine
from ..core.storage import storage
from ..research.backtest import run
from ..research.strategies import resolve

router = APIRouter(tags=["research"])


@router.post("/research/backtests", response_model=BacktestResponse)
def create_backtest(request: BacktestRequest) -> BacktestResponse:
    market = engine()
    bars, history_provenance = market.history.bars(request.code, market.price_of(request.code))
    try:
        result = run(request, bars, history_provenance)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    storage.save_run(result.model_dump(mode="json"))
    return result


@router.get("/research/backtests", response_model=BacktestRunsResponse)
def list_backtests() -> BacktestRunsResponse:
    return BacktestRunsResponse(runs=storage.list_runs())


@router.post("/research/backtests/batch", response_model=BacktestBatchResponse)
def batch_backtest(request: BacktestBatchRequest) -> BacktestBatchResponse:
    """Run one strategy across a short list under the same cost model.

    Per-code failures are returned as rows with `error` instead of aborting the
    whole comparison, so one short history does not hide the other results.
    """
    if resolve(request.strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"未知策略：{request.strategy_id}")
    market = engine()
    rows: list[BacktestBatchRow] = []
    for code in request.codes:
        record = market.stock(code)
        name = record.name if record else None
        bars, history_provenance = market.history.bars(code, market.price_of(code))
        single = BacktestRequest(
            strategy_id=request.strategy_id,
            code=code,
            lookback=request.lookback,
            holding_days=request.holding_days,
            commission_bps=request.commission_bps,
            slippage_bps=request.slippage_bps,
            stamp_duty_bps=request.stamp_duty_bps,
            initial_cash=request.initial_cash,
        )
        try:
            result = run(single, bars, history_provenance)
        except (ValueError, LookupError) as error:
            rows.append(BacktestBatchRow(code=code, name=name, error=str(error)))
            continue
        rows.append(
            BacktestBatchRow(
                code=code,
                name=name,
                total_return=result.total_return,
                benchmark_return=result.benchmark_return,
                excess_return=round(result.total_return - result.benchmark_return, 3),
                max_drawdown=result.max_drawdown,
                trade_count=result.trade_count,
                win_rate=result.win_rate,
            )
        )
    completed = [row for row in rows if row.error is None]
    returns = [row.total_return for row in completed if row.total_return is not None]
    return BacktestBatchResponse(
        provenance=market.provenance(),
        strategy_id=request.strategy_id,
        requested=len(request.codes),
        completed=len(completed),
        median_return=round(median(returns), 3) if returns else None,
        beat_benchmark=sum(1 for row in completed if (row.excess_return or 0) > 0),
        rows=rows,
    )


@router.get("/research/backtests/{run_id}", response_model=BacktestResponse)
def read_backtest(run_id: str) -> BacktestResponse:
    payload = storage.get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {run_id} 的回测记录")
    return BacktestResponse.model_validate(payload)
