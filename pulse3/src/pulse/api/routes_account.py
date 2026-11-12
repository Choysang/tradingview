"""Account projection and analysis-run reads: getAccountProjection, getModelRun.

Why: FINAL_SYSTEM_SPEC §5/§11 and execution_standard §2 - an account view is a pure function of
(account_id, account_version, valuation snapshot): the canonical account events up to ``account_version`` are
replayed through the one ledger reducer, then marked with raw closes from the named READY market snapshot.
Invariants:
* NAV = external capital + realized + unrealized + dividend income - fees - tax is asserted by the reducer when every
  position has a mark; with a missing mark NAV, unrealized and the cost-origin bridge stay null (never 0) and the
  position's ``mark_quality_status`` is ``invalid``;
* ``withdrawable_cash_cny`` is null: the withdrawal channel policy is not modelled;
* a historical version is replayed from events, never read from the mutable current projection.
Not covered: account ownership (single tenant: any valid principal), non-bar valuation snapshots, quote marks.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from pulse.adapters.postgres import market_repo
from pulse.adapters.postgres.ledger_repo import LedgerRepo, instrument_uuid
from pulse.api.auth import principal
from pulse.api.deps import conn_dep
from pulse.api.envelope import ApiError, meta, respond
from pulse.domain.execution import LedgerReducer

router = APIRouter(prefix="/v1")
VALUATION_POLICY_VERSION = "last-raw-close-in-snapshot-v1"
ZERO = Decimal(0)


def _replay(conn, account_id: UUID, version: int) -> LedgerReducer:
    head = conn.execute("SELECT current_sequence FROM execution.account_head WHERE account_id=%s", (account_id,)).fetchone()
    if head is None:
        raise ApiError(404, "NOT_FOUND", "账户不存在")
    if version > head["current_sequence"]:
        raise ApiError(409, "ACCOUNT_VERSION_UNKNOWN", f"账户当前版本是 {head['current_sequence']}，没有版本 {version}")
    events = [e for e in LedgerRepo(conn).events(account_id) if e["sequence"] <= version]
    if not events or events[0]["kind"] != "opening":
        raise ApiError(404, "NOT_FOUND", "账户没有开户事件")
    ledger = LedgerReducer(**events[0]["payload"])
    for e in events[1:]:
        ledger.apply(e)
    return ledger


def _mark(conn, snapshot_id: UUID, instrument_id: UUID, ledger_day) -> tuple[Decimal, str] | None:
    """Latest non-null raw daily close of the instrument inside the valuation snapshot on or before the ledger day
    (a later bar is the future of this account version), with its quality."""
    row = conn.execute(
        "SELECT close_price_raw, quality_status, is_final FROM market.bar_projection WHERE snapshot_id=%s AND instrument_id=%s"
        " AND timeframe='1d' AND close_price_raw IS NOT NULL AND trade_date <= %s"
        " ORDER BY trade_date DESC, bar_open_time DESC LIMIT 1",
        (snapshot_id, instrument_id, ledger_day)).fetchone()
    if row is None:
        return None
    return row["close_price_raw"], (row["quality_status"] if row["is_final"] else "partial")


def project_account(conn, account_id: UUID, version: int, snapshot_id: UUID) -> tuple[dict, list[str]]:
    manifest = market_repo.get_manifest(conn, snapshot_id)
    if manifest is None:
        raise ApiError(404, "NOT_FOUND", "估值快照不存在")
    if manifest["manifest_state"] != "READY":
        raise ApiError(409, "SNAPSHOT_NOT_READY", "估值快照尚未发布")
    ledger = _replay(conn, account_id, version)
    head = conn.execute("SELECT ledger_policy_version, numeric_policy_version FROM execution.account_head WHERE account_id=%s",
                        (account_id,)).fetchone()
    marks = {s: _mark(conn, snapshot_id, instrument_uuid(s), ledger.day) for s, h in ledger.holdings.items() if h.quantity}
    try:                                               # all money math is the ledger's: NAV is None while any mark is missing
        snap = ledger.snapshot_partial({s: None if m is None else m[0] for s, m in marks.items()})
    except AssertionError as e:
        raise ApiError(500, "LEDGER_IDENTITY_BROKEN", "账户净值恒等式不成立", retryable=False) from e
    missing = [str(instrument_uuid(s)) for s in snap["missing_marks"]]
    positions = []
    for symbol, p in snap["positions"].items():
        mark = marks[symbol]
        positions.append({
            "instrument_id": instrument_uuid(symbol), "quantity_shares": p["quantity"], "sellable_quantity_shares": p["sellable"],
            "reserved_sell_quantity_shares": p["reserved_sell"], "cost_basis_cny": p["cost"],
            "mark_price_raw": None if mark is None else mark[0], "market_value_cny": p["market_value"],
            "mark_quality_status": "invalid" if mark is None else mark[1]})
    reserved = sum((o.reserved_cash for o in ledger.orders.values()), ZERO)
    nav, unrealized, pnl = snap["nav"], snap["unrealized"], snap["pnl_from_cost_origin"]
    cost, realized, income = snap["cost_basis"], snap["realized_gross"], snap["dividend_income"]
    fees, tax, capital = snap["fees"], snap["withholding_tax"], snap["net_external_capital"]
    data = {
        "account_id": account_id, "account_version": ledger.account_version, "base_currency": "CNY", "trading_date": ledger.day,
        "ledger_policy_version": head["ledger_policy_version"], "numeric_policy_version": head["numeric_policy_version"],
        "valuation_policy_version": VALUATION_POLICY_VERSION, "valuation_snapshot_id": snapshot_id,
        "cash_settled_cny": snap["cash_settled"], "trade_receivable_cny": snap["trade_receivable"], "trade_payable_cny": snap["trade_payable"],
        "dividend_receivable_cny": snap["dividend_receivable"], "reserved_cash_cny": reserved,
        "cash_available_cny": snap["cash_available"], "withdrawable_cash_cny": None, "nav_cny": nav,
        "realized_gross_cny": realized, "dividend_income_cny": income, "pnl_from_cost_origin_cny": pnl, "cost_basis_cny": cost,
        "unrealized_cny": unrealized, "transaction_cost_cny": fees, "withholding_tax_cny": tax,
        "net_external_capital_cny": capital, "positions": positions}
    return data, missing


@router.get("/accounts/{account_id}")
def get_account_projection(account_id: UUID, account_version: int, valuation_snapshot_id: UUID, request: Request,
                           conn=Depends(conn_dep)):
    principal(request)
    if account_version < 0:
        raise ApiError(400, "BAD_REQUEST", "请求参数无效", field_errors=[
            {"path": "/account_version", "code": "minimum", "message": "account_version 不能为负"}])
    data, missing = project_account(conn, account_id, account_version, valuation_snapshot_id)
    manifest = market_repo.get_manifest(conn, valuation_snapshot_id)
    held = len(data["positions"])
    warnings = [{"code": "MARK_MISSING", "message": f"{len(missing)} 个持仓在估值快照中没有收盘价，净值与浮动盈亏保持为空",
                 "affected_component": "valuation", "evidence_ref": ",".join(missing)}] if missing else []
    return respond("getAccountProjection", data, meta(
        quality_status="partial" if missing else "valid", input_snapshot_refs=[market_repo.snapshot_ref(manifest)],
        warnings=warnings, freshness_state="not_expected",
        coverage={"policy_version": VALUATION_POLICY_VERSION, "expected_count": held, "observed_count": held - len(missing),
                  "missing_count": len(missing), "unverifiable_reasons": []}))


@router.get("/analysis-runs/{model_run_id}")
def get_model_run(model_run_id: UUID, request: Request, conn=Depends(conn_dep)):
    principal(request)
    r = conn.execute(
        "SELECT r.*, p.response_contract_id, p.response_schema_version FROM analysis.model_run r JOIN analysis.model_policy p"
        " ON p.model_policy_id=r.model_policy_id AND p.model_policy_version=r.model_policy_version WHERE r.model_run_id=%s",
        (model_run_id,)).fetchone()
    if r is None:
        raise ApiError(404, "NOT_FOUND", "分析运行不存在")
    output: dict[str, Any] | None = None
    if r["output_ref"] is not None:
        output = {"ref": r["output_ref"], "content_hash": r["output_hash"], "contract_id": r["response_contract_id"],
                  "schema_version": r["response_schema_version"]}
    data = {k: r[k] for k in ("model_run_id", "model_policy_id", "model_policy_version", "stage_id", "input_context_hash",
                              "analysis_as_of", "run_state", "publication_eligibility", "started_at", "completed_at", "error_code")}
    data["output"] = output
    warns = []
    if r["publication_eligibility"] in ("superseded", "expired"):
        warns.append({"code": "RUN_NOT_PUBLISHABLE", "message": f"该分析结果的发布资格是 {r['publication_eligibility']}",
                      "affected_component": "analysis_run", "evidence_ref": None})
    return respond("getModelRun", data, meta(warnings=warns, quality_status="valid" if r["run_state"] == "succeeded" else "partial"))

