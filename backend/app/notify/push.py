"""Webhook push of the daily report to WeCom / Feishu / DingTalk / any endpoint.

Configuration is environment only (the webhook URL is a credential):
`PULSE_PUSH_WEBHOOK` and `PULSE_PUSH_KIND` (wecom | feishu | dingtalk | generic,
default generic). Nothing is ever sent while the webhook is unset, and the URL
is never logged or returned: status shows the kind and the host only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx

from contracts.schema import DailyReport, PushStatus, StockPick

logger = logging.getLogger("pulse")
CST = timezone(timedelta(hours=8))
KINDS = ("wecom", "feishu", "dingtalk", "generic")
KIND_NAMES = {"wecom": "企业微信", "feishu": "飞书", "dingtalk": "钉钉", "generic": "通用 Webhook"}
HORIZON_NAMES = {"short": "短线", "swing": "波段", "long": "中长线"}
OVERALL_TOP = 5
HORIZON_TOP = 3
MAX_CHARS = 3400  # WeCom markdown caps at 4096 bytes; Chinese text is 3 bytes/char in UTF-8
TIMEOUT_SECONDS = 10.0
DISCLAIMER = "研究用途，不构成投资建议"

_LOCK = threading.Lock()
_LAST_PUSHED: str | None = None


class PushError(RuntimeError):
    """A send that reached the network layer but did not succeed."""


def _settings() -> tuple[str | None, str, str | None]:
    """(webhook, kind, problem). A malformed URL counts as not configured."""
    url = (os.environ.get("PULSE_PUSH_WEBHOOK") or "").strip()
    kind = (os.environ.get("PULSE_PUSH_KIND") or "generic").strip().lower()
    problem = None
    if kind not in KINDS:
        problem = f"PULSE_PUSH_KIND={kind!r} 不受支持，按 generic 处理"
        kind = "generic"
    if not url:
        return None, kind, problem
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, kind, "PULSE_PUSH_WEBHOOK 不是有效的 http(s) 地址，推送未启用"
    return url, kind, problem


def status() -> PushStatus:
    url, kind, problem = _settings()
    if url is None:
        detail = problem or "未配置：设置环境变量 PULSE_PUSH_WEBHOOK（以及 PULSE_PUSH_KIND）后重启后端即可启用推送。"
        return PushStatus(configured=False, kind=None, last_pushed=_LAST_PUSHED, detail=detail)
    host = urlsplit(url).hostname or "?"
    detail = f"已配置：{KIND_NAMES[kind]}（{host}）。" + (f"注意：{problem}。" if problem else "")
    return PushStatus(configured=True, kind=kind, last_pushed=_LAST_PUSHED, detail=detail)


# --- message building ----------------------------------------------------------------


def _top_reason(pick: StockPick) -> str:
    if not pick.reasons:
        return pick.entry_note
    reason = max(pick.reasons, key=lambda item: item.weight * item.signal)
    return f"{reason.playbook_name}：{reason.note}"


def _levels(pick: StockPick) -> str:
    parts = []
    if pick.stop_loss is not None:
        parts.append(f"止损 {pick.stop_loss:.2f}")
    if pick.take_profit is not None:
        parts.append(f"止盈 {pick.take_profit:.2f}")
    return "，".join(parts)


def _line(rank: int, pick: StockPick, with_horizon: bool) -> str:
    horizon = f"[{HORIZON_NAMES.get(pick.horizon, pick.horizon)}] " if with_horizon else ""
    levels = _levels(pick)
    return (
        f"{rank}. **{pick.name}**（{pick.code}）{horizon}评分 {pick.score:.1f}"
        f" · {_top_reason(pick)}" + (f" · {levels}" if levels else "")
    )


def build_digest(report: DailyReport) -> tuple[str, str]:
    """(title, markdown body) in Chinese. Truncated to the platform limits."""
    title = f"Pulse 每日选股 {report.trade_date}"
    lines = [f"**{title}**", f"市场状态：{report.regime.label}"]
    if report.provenance.mode != "LIVE":
        lines.append("数据为模拟/回退数据（SIM），仅供流程演示。")
    lines.append("")
    lines.append(f"**综合 Top {OVERALL_TOP}**")
    lines += [_line(index, pick, True) for index, pick in enumerate(report.overall[:OVERALL_TOP], 1)] or [
        "（今日无入选，观望；原因见报告备注）"
    ]
    for key in ("short", "swing", "long"):
        picks = getattr(report, key)[:HORIZON_TOP]
        if not picks:
            continue
        lines += ["", f"**{HORIZON_NAMES[key]} Top {HORIZON_TOP}**"]
        lines += [_line(index, pick, False) for index, pick in enumerate(picks, 1)]
    lines += ["", f"{DISCLAIMER}。"]
    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        cut = body[: MAX_CHARS - len(DISCLAIMER) - 12].rsplit("\n", 1)[0]
        body = f"{cut}\n…（已截断）\n{DISCLAIMER}。"
    return title, body


def _plain(markdown: str) -> str:
    return markdown.replace("**", "")


def build_payload(kind: str, report: DailyReport) -> dict[str, Any]:
    title, body = build_digest(report)
    if kind == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": body}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": _plain(body)}}
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": body}}
    return {**report.model_dump(mode="json"), "text": _plain(body)}


# --- sending --------------------------------------------------------------------------


def _accepted(kind: str, response: httpx.Response) -> str | None:
    """None when the platform accepted the message, otherwise a reason (no URL)."""
    if response.status_code >= 400:
        return f"HTTP {response.status_code}"
    if kind == "generic":
        return None
    try:
        data = response.json()
    except ValueError:
        return "响应不是 JSON"
    # WeCom/DingTalk: errcode; Feishu: code or StatusCode. 0 means accepted.
    for key in ("errcode", "code", "StatusCode"):
        if key in data and data[key] not in (0, "0"):
            message = data.get("errmsg") or data.get("msg") or data.get("StatusMessage") or ""
            return f"{key}={data[key]} {message}".strip()
    return None


def push_report(report: DailyReport) -> PushStatus:
    """Send the digest. Unconfigured is a no-op that returns the status; a
    failed send raises PushError (the message never contains the webhook)."""
    global _LAST_PUSHED
    url, kind, _ = _settings()
    if url is None:
        return status()
    payload = build_payload(kind, report)
    try:
        response = httpx.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        raise PushError(f"推送失败：{type(error).__name__}") from None
    reason = _accepted(kind, response)
    if reason is not None:
        raise PushError(f"推送失败：{reason}")
    with _LOCK:
        _LAST_PUSHED = datetime.now(CST).isoformat(timespec="seconds")
    logger.info("daily report pushed via %s", kind)
    return status()


def reset_for_tests() -> None:
    global _LAST_PUSHED
    with _LOCK:
        _LAST_PUSHED = None
