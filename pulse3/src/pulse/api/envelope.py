"""Response envelope and error mapping for every /v1 operation.

Why: FINAL_SYSTEM_SPEC §11 fixes stable response metadata (request_id,
contract_version, release_id, decision_as_of, quality/freshness, snapshot refs)
and structured Error.code. Every response leaves through ``respond`` so it is
checked against the operation's response definition in the one contract.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

from pulse import contracts

VALIDATE = os.environ.get("PULSE_VALIDATE_RESPONSES", "1") != "0"
OPERATIONAL_COVERAGE = {"policy_version": "operational-v1", "expected_count": None, "observed_count": 0,
                        "missing_count": None, "unverifiable_reasons": []}


def meta(*, release_id: Any = None, decision_as_of: datetime | None = None,
         availability_policy_version: str | None = None, input_snapshot_refs: list | None = None,
         quality_status: str = "valid", freshness_state: str = "not_expected", coverage: dict | None = None,
         warnings: list | None = None, next_cursor: str | None = None, actionability_id: Any = None) -> dict:
    return {
        "request_id": str(uuid.uuid4()),
        "generated_at": contracts.utc_z(datetime.now(timezone.utc)),
        "trace_id": uuid.uuid4().hex,
        "contract_version": contracts.CONTRACT_VERSION,
        "release_id": release_id,
        "decision_as_of": decision_as_of,
        "availability_policy_version": availability_policy_version,
        "input_snapshot_refs": input_snapshot_refs or [],
        "quality_status": quality_status,
        "freshness_state": freshness_state,
        "coverage": coverage or OPERATIONAL_COVERAGE,
        "warnings": warnings or [],
        "next_cursor": next_cursor,
        "actionability_id": actionability_id,
    }


def respond(operation_id: str, data: Any, m: dict, status: int = 200, headers: dict | None = None,
            schema_id: str = contracts.V3_ID) -> JSONResponse:
    """Wire-encode and validate against ``<operationId>Response`` in the v3 or extension contract."""
    body = contracts.to_wire({"meta": m, "data": data})
    if VALIDATE:
        contracts.check(f"{operation_id}Response", body, schema_id)
    return JSONResponse(body, status_code=status, headers=headers)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 field_errors: list | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.field_errors = retryable, field_errors or []


def respond_ext(operation_id: str, data: Any, m: dict, status: int = 200) -> JSONResponse:
    """Extension operations (PEX cards, contract urn:pulse:contracts:ext:v1)."""
    return respond(operation_id, data, m, status, schema_id=contracts.EXT_ID)


def error_response(err: ApiError) -> JSONResponse:
    body = contracts.to_wire({"meta": meta(quality_status="invalid", freshness_state="unknown"),
                              "error": {"code": err.code, "message": err.message, "retryable": err.retryable,
                                        "field_errors": err.field_errors}})
    contracts.check("ErrorResponse", body)
    return JSONResponse(body, status_code=err.status)
