"""Push channel status and test sends."""
from fastapi import APIRouter, HTTPException

from contracts.schema import PushStatus
from ..notify import push

router = APIRouter(tags=["notify"])


def _with_scheduler(current: PushStatus) -> PushStatus:
    from ..scheduler import scheduler_state

    state = scheduler_state()
    parts = [current.detail]
    if state["enabled"]:
        parts.append(f"调度器：最近运行 {state['last_run'] or '尚未'}" + (f"；最近错误：{state['last_error']}" if state["last_error"] else ""))
    else:
        parts.append("调度器未运行（PULSE_SCHEDULER=0 或尚未启动）。")
    return current.model_copy(update={"detail": " ".join(parts)})


@router.get("/notify/status", response_model=PushStatus)
def notify_status() -> PushStatus:
    return _with_scheduler(push.status())


@router.post("/notify/test", response_model=PushStatus)
def notify_test() -> PushStatus:
    """Send the latest report (generating it when none exists) to the webhook."""
    if not push.status().configured:
        raise HTTPException(status_code=409, detail="推送未配置：请设置环境变量 PULSE_PUSH_WEBHOOK 后重启后端。")
    from ..playbooks import picks

    report = picks.latest_report() or picks.generate_report()
    try:
        return _with_scheduler(push.push_report(report))
    except push.PushError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
