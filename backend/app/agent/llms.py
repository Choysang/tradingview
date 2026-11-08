"""`/llms.txt`: what an LLM agent needs to know before calling Pulse."""
from __future__ import annotations

from .capabilities import CAPABILITIES

API_PREFIX = "/api/v1"


def llms_txt(version: str, mcp_path: str) -> str:
    lines = [
        "# A-Share Pulse",
        "",
        f"> A 股盘中市场结构与研究工作台（版本 {version}）：连板梯队、盘口异动、可解释多因子筛选、"
        "形态与因子研究、确定性回测。以下接口都是只依赖内存快照与确定性计算的 JSON 接口。",
        "",
        "## 数据诚实规则",
        "- 先看每个响应里的 `provenance.mode`：`SIM` 是内置场景模拟器，不是真实行情；`LIVE` 才来自行情源。",
        "- `provenance.status` 为 `fallback`/`stale` 时数据已降级或过期，结论要带上这一条。",
        "- 字段为 `null` 表示未知，不是 0；筛选时未知值不参与该条件。",
        "- 金额与市值单位为亿元；涨跌幅、换手率等比率单位为百分点（5 表示 5%）。",
        "- 历史日线有独立的 `history_provenance`，可能与实时快照来源不同。",
        "",
        "## 调用约定",
        f"- REST 前缀 `{API_PREFIX}`，请求与响应均为 JSON；完整契约见 `/openapi.json`，能力清单见 `{API_PREFIX}/agent/manifest`。",
        f"- MCP：Streamable HTTP 端点 `{mcp_path}`，每个能力对应一个同名工具，入参与 REST 请求体一致。",
        "- 校验失败返回 422，未知代码返回 404；不要重试同样的无效请求。",
        "",
        "## 能力",
    ]
    for capability in CAPABILITIES:
        lines.append(f"- `{capability.name}` — {capability.method} `{capability.path}` — {capability.description}")
    return "\n".join(lines) + "\n"
