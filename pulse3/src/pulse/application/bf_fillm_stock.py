"""Stock statistics additions (legacy row #43): volume ratio, circulating cap, seal success rate over the sessions the seal facts cover.

Why: the old stock sheet showed 量比, the free-float cap and the 1-year seal success rate. ``bf_stock_service`` keeps the closed set of
card fields of ``backfill_stock``; these three are served as ``StockStatsPlus`` of ``backfill_fillm`` and drawn on the same card.
Rules:
* volume ratio = volume of the newest raw daily bar / mean volume of the 5 bars before it. At the close this equals the quote
  vendor's intraday 量比 definition (volume per minute against the 5-day average per minute). Needs 6 consecutive bars in the window
  with a volume; otherwise unavailable with the reason. Bars are the very window the stats card reads (visible at the cutoff).
* circulating cap = the newest cap version visible at the cutoff (``bf_stock_service.load_cap_fact``): a legacy snapshot with its own
  as-of date, unit assumed 100 million CNY by the importer. It is the circulating cap, NOT the old free-float cap (large-holder
  locked shares are not available), and it is not the value of the session: always ``estimated`` with that reason.
* seal success rate = sessions-with-limit-touch that closed sealed / sessions with a touch, over the sessions of the seal facts dataset
  (policy ``seal-facts-v2``, estimated from 60 s snapshots) that were READY at the cutoff, newest ``WINDOW_SESSIONS`` of them. The
  coverage block says how many sessions back it reaches; with fewer than ``WINDOW_SESSIONS`` it is a partial-window figure and the
  basis says so, it is never presented as a 1-year value. Sessions in which the stock has no snapshot do not count as covered.
Invariants: missing = unavailable with a reason, never 0; a version READY after the cutoff is not read.
Not covered: seal facts of other policy versions (counted and reported as excluded), tick data.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping, Sequence

import polars as pl

from pulse.adapters.lake import LakeStore
from pulse.domain.market_state import seal as SEAL

VOLUME_RATIO_BARS = 5
WINDOW_SESSIONS = 250
DATASET_SEAL = "intraday_seal_facts"
_Q4, _Q2 = Decimal("0.0001"), Decimal("0.01")


def _field(key: str, label: str, *, value: str | None = None, kind: str | None = None, unit: str | None = None, as_of: str | None = None,
           basis: str | None = None, reason: str | None = None, estimated: bool = False, coverage: dict | None = None) -> dict:
    status = "unavailable" if value is None else ("estimated" if estimated else "ok")
    if status != "ok" and not reason:
        raise ValueError(f"{key}: a missing or estimated statistic needs a reason")
    return {"key": key, "label": label, "status": status, "value": value, "value_kind": kind if value is not None else None, "unit": unit,
            "as_of": as_of, "basis": basis, "reason": reason, "coverage": coverage}


def volume_ratio(raw_window: Mapping[str, Any] | None, missing_reason: str | None) -> dict:
    label = "量比"
    if raw_window is None or not raw_window.get("bars"):
        return _field("volume_ratio", label, reason=missing_reason or "截点前没有可见的原始价日线", unit="倍")
    bars = raw_window["bars"]
    need = VOLUME_RATIO_BARS + 1
    if len(bars) < need:
        return _field("volume_ratio", label, unit="倍", reason=f"窗口内只有 {len(bars)} 根日线，不足 {need} 根")
    tail = bars[-need:]
    vols = [b.get("volume_shares") for b in tail]
    if any(v is None for v in vols):
        return _field("volume_ratio", label, unit="倍", reason="最近6根日线里有缺失的成交量，缺口不补")
    base = sum(Decimal(v) for v in vols[:-1]) / VOLUME_RATIO_BARS
    if base <= 0:
        return _field("volume_ratio", label, unit="倍", reason="前5日平均成交量不为正，无法计算")
    val = (Decimal(vols[-1]) / base).quantize(_Q4, rounding=ROUND_HALF_EVEN)
    return _field("volume_ratio", label, value=format(val, "f"), kind="decimal", unit="倍", as_of=tail[-1]["trade_date"],
                  basis=f"最新日线成交量 ÷ 此前{VOLUME_RATIO_BARS}日平均成交量（原始价日线；收盘时等同行情商的量比定义）")


def circulating_cap(cap: Any, cap_missing_reason: str | None, cutoff: datetime) -> dict:
    label = "流通市值"
    if cap is None:
        return _field("float_cap", label, unit="亿元", reason=cap_missing_reason or "截点前没有可见的流通市值事实")
    if cap.float_cap_cny is None or cap.float_cap_cny <= 0:
        return _field("float_cap", label, unit="亿元", reason=f"{cap.as_of} 的流通市值不为正，不使用")
    yi = (cap.float_cap_cny / Decimal(100_000_000)).quantize(_Q2, rounding=ROUND_HALF_EVEN)
    why = (f"取自 {cap.as_of} 的历史快照（不是当日值）；单位由导入器按亿元解释，数据源没有声明；这是流通市值，"
           "不是旧站的实际自由流通市值（扣除大股东锁定筹码的数据 pulse3 没有）")
    return _field("float_cap", label, value=format(yi, "f"), kind="decimal", unit="亿元", as_of=cap.as_of, basis="截点前可见的最新流通市值版本",
                  reason=why, estimated=True)


def seal_sessions(conn, cutoff: datetime) -> tuple[list[dict], int]:
    """(newest READY manifest per session of the current policy, number of sessions whose newest version has another policy)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (coverage_document->>'session_date') coverage_document->>'session_date' AS session_date, content_hash, coverage_document->>'policy_version' AS policy"
        " FROM market.dataset_manifest WHERE dataset_id=%s AND manifest_state='READY' AND ready_at <= %s"
        " ORDER BY coverage_document->>'session_date', manifest_version DESC", (DATASET_SEAL, cutoff)).fetchall()
    cur = [r for r in rows if r["policy"] == SEAL.POLICY_VERSION]
    return sorted(cur, key=lambda r: r["session_date"]), len(rows) - len(cur)


def seal_success(conn, lake: LakeStore, instrument_id, cutoff: datetime) -> dict:
    label = "封板成功率"
    sessions, other = seal_sessions(conn, cutoff)
    if not sessions:
        return _field("seal_success_rate_1y", label, unit="%", reason="截点前没有已发布的封板事实数据集（盘中记录未导入），无法计算",
                      coverage={"window_sessions": WINDOW_SESSIONS, "sessions_covered": 0, "first_session": None, "last_session": None, "touched": 0,
                                "sealed_at_close": 0, "policy_version": SEAL.POLICY_VERSION, "sessions_excluded_other_policy": other})
    sessions = sessions[-WINDOW_SESSIONS:]
    covered: list[str] = []
    touched = sealed = 0
    for s in sessions:
        df = lake.read_snapshot_frames(s["content_hash"])
        if df is None or df.height == 0:
            continue
        hit = df.filter(pl.col("instrument_id") == str(instrument_id))
        if hit.height == 0:
            continue
        covered.append(s["session_date"])
        r = hit.row(0, named=True)
        if r["touched_limit"]:
            touched += 1
            sealed += 1 if r["sealed_at_close"] else 0
    cov = {"window_sessions": WINDOW_SESSIONS, "sessions_covered": len(covered), "first_session": date.fromisoformat(covered[0]) if covered else None,
           "last_session": date.fromisoformat(covered[-1]) if covered else None, "touched": touched, "sealed_at_close": sealed,
           "policy_version": SEAL.POLICY_VERSION, "sessions_excluded_other_policy": other}
    if not covered:
        return _field("seal_success_rate_1y", label, unit="%", reason="封板事实数据集的各交易日里都没有这只股票的快照", coverage=cov)
    if touched == 0:
        return _field("seal_success_rate_1y", label, unit="%", coverage=cov, as_of=covered[-1],
                      reason=f"在覆盖的 {len(covered)} 个交易日里没有触及涨停，成功率没有分母，不写 0")
    rate = (Decimal(sealed) * 100 / touched).quantize(_Q2, rounding=ROUND_HALF_EVEN)
    partial = len(covered) < WINDOW_SESSIONS
    why = (f"只覆盖 {len(covered)} 个交易日（{covered[0]} 至 {covered[-1]}），不是{WINDOW_SESSIONS}个交易日（约1年）的值；" if partial else "")
    why += "封板事实由约60秒一次的快照估算（触及涨停后收盘仍封住算成功）"
    return _field("seal_success_rate_1y", label, value=format(rate, "f"), kind="decimal", unit="%", as_of=covered[-1], estimated=True, coverage=cov,
                  basis=f"触及涨停 {touched} 次，其中收盘封住 {sealed} 次；封板事实策略 {SEAL.POLICY_VERSION}", reason=why)


def stats_plus(conn, lake: LakeStore, instrument_id, cutoff: datetime, raw_window: Mapping[str, Any] | None, raw_missing: str | None, cap: Any,
               cap_missing: str | None) -> list[dict]:
    return [volume_ratio(raw_window, raw_missing), circulating_cap(cap, cap_missing, cutoff), seal_success(conn, lake, instrument_id, cutoff)]
