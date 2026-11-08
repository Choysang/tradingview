"""Historical warehouse status and sync jobs."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from contracts.schema import DataProvenance, SyncJob, SyncRequest, WarehouseStatus
from ..warehouse import sync
from ..warehouse.store import active, warehouse

router = APIRouter(tags=["data"])

LIVE_SOURCE = "新浪日线 · 按比例前复权（数据仓）"
SIM_SOURCE = "合成历史（场景生成器）"
NOTES = [
    "成交额无逐日值：按 典型价（高+低+收）/3 × 成交量 估算，单位亿元；日更的收盘快照也用同一估算，保持口径一致。",
    "历史 ST 状态未知：研究中的涨跌停一律按板块比例判定，不按今天的名称回溯；只有今日推荐按当前名称剔除 ST。"
    "2026-07-06 起主板 ST 涨跌幅为 10%，与板块比例一致。",
    "日线为前复权（qfq）：日更读取全市场收盘快照，若某股公布的昨收与库内收盘价相差超过 0.5%（除权日），重新拉取该股完整历史。",
    "交易时段内（工作日 15:05 前）同步不会写入当日未完成的日线。",
    "幸存者偏差：仓库只含同步时仍在上市的股票，此前退市的股票没有历史。",
    "腾讯接口按请求次数限流（约 700–1000 次后拒绝 30 分钟以上）：被拒绝时任务立即停止，已写入的数据保留，重新运行会续传；"
    "命令行加 --wait-hours 24 可自动等待并续传。",
    "腾讯行情对部分北交所代码没有日线数据，这些代码会缺少历史。",
]


@router.get("/data/warehouse", response_model=WarehouseStatus)
def warehouse_status() -> WarehouseStatus:
    store = active()  # may build the SIM dataset on first call
    dataset = store.dataset
    codes, _, last = store.coverage()
    now = datetime.now().isoformat(timespec="seconds")
    if dataset == "live":
        provenance = DataProvenance(
            mode="LIVE",
            source=LIVE_SOURCE,
            as_of=now,
            trade_date=last or "",
            status="ready",
            detail=f"真实历史日线已同步，覆盖 {codes} 只股票，最新交易日 {last}。",
            coverage=codes,
        )
    else:
        partial = warehouse("live").coverage()[0]
        progress = f"真实历史回填尚未完成（已有 {partial} 只），" if partial else "尚未同步真实历史，"
        provenance = DataProvenance(
            mode="SIM",
            source=SIM_SOURCE,
            as_of=now,
            trade_date=last or "",
            status="fallback",
            detail=progress + "研究功能正在使用合成历史。"
            "执行 POST /api/v1/data/sync（kind=backfill）或 python scripts/pulse.py sync --kind backfill --wait-hours 24，"
            "全市场回填完成后自动切换；中断后重新运行会续传。",
            coverage=codes,
        )
    return WarehouseStatus(
        active=dataset,  # type: ignore[arg-type]
        provenance=provenance,
        datasets={name: warehouse(name).tables() for name in ("live", "sim")},
        latest_trade_date=last,
        coverage_codes=codes,
        job=sync.current_job(),
        notes=list(NOTES),
    )


@router.post("/data/sync", response_model=SyncJob)
def start_sync(request: SyncRequest) -> SyncJob:
    job, started = sync.begin_sync(request)
    if not started:
        raise HTTPException(status_code=409, detail=f"已有同步任务正在运行（{job.id}：{job.message}），请等待其完成。")
    return job
