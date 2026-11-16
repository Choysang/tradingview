"""Operations backfill: release-build job, agent access manifest / llms.txt (legacy triage #15, #92, #63, #64).

Why: the old site could re-run its analysis from a button and described itself to agents. pulse3 gets both the pulse3 way:
the release build is a registered manual job (queue, idempotency, fenced worker) that keeps every guard of
``release_service.build_release``; the agent manifest and ``/llms.txt`` are generated from what is actually registered.
Invariants:
* the job never bypasses ``build_release``: raw-coverage, research-window, advisory-lock and idempotent-replay behaviour are
  the function's own; a guard that refuses makes the run ``failed`` with the exception class as ``error_code`` (never a
  fake success) and nothing is published;
* job parameters are closed: ``as_of`` (``latest`` | ``YYYY-MM-DD`` | ISO instant with offset), ``top_n`` (1..50),
  ``availability`` (``observed`` | ``reconstructed``; reconstructed needs an explicit ``as_of``); anything else is refused;
  the slot is always ``main`` and the legacy warehouse path comes from the server environment, never from a request;
* the manifest lists every ``core.api_binding`` row (core v3 + extension contract) enriched from the shipped contract files,
  plus the backfill routes actually mounted under ``/v1/x/bf/`` and ``/llms.txt``; nothing is hand-listed.
Not covered: the MCP server (deferred, see docs/LEGACY_TRIAGE.md), per-operation request schemas in the manifest.
"""
from __future__ import annotations

import inspect
import os
from datetime import date, datetime, timezone

import psycopg

from pulse import config, contracts
from pulse.application import bindings as B
from pulse.application import ops_service

ALLOWED_JOB_PARAMS = {"as_of", "top_n", "availability"}
BF_PREFIX = "/v1/x/bf/"
LLMS_PATH = "/llms.txt"


class BadJobParameters(ValueError):
    """The release-build job was submitted with parameters outside the closed set."""


# ---- release build job (#15, #92) ----------------------------------------------------------------------------------------------
def parse_job_params(params: dict, now: datetime | None = None) -> dict:
    unknown = sorted(set(params) - ALLOWED_JOB_PARAMS)
    if unknown:
        raise BadJobParameters("unknown parameters: " + ", ".join(unknown))
    as_of_text = params.get("as_of", "latest")
    top_n = params.get("top_n", 10)
    availability = params.get("availability", "observed")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 50:
        raise BadJobParameters("top_n must be an integer between 1 and 50")
    if availability not in ("observed", "reconstructed"):
        raise BadJobParameters("availability must be observed or reconstructed")
    if not isinstance(as_of_text, str):
        raise BadJobParameters("as_of must be a string")
    if availability == "reconstructed" and as_of_text == "latest":
        raise BadJobParameters("a reconstructed (history) build needs an explicit as_of")
    as_of: datetime | None
    if as_of_text == "latest":
        as_of = None
    elif len(as_of_text) == 10:                         # a date = the end of that day in Asia/Shanghai
        try:
            d = date.fromisoformat(as_of_text)
        except ValueError as e:
            raise BadJobParameters("as_of is not a valid date") from e
        as_of = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=B.CST)
    else:
        try:
            dt = datetime.fromisoformat(as_of_text.replace("Z", "+00:00"))
        except ValueError as e:
            raise BadJobParameters("as_of is not a valid instant") from e
        if dt.tzinfo is None:
            raise BadJobParameters("as_of needs a timezone offset")
        as_of = dt.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if as_of is not None and len(as_of_text) == 10:
        as_of = min(as_of, now)                          # today's date means "now", never the coming midnight
    if as_of is not None and as_of > now:
        raise BadJobParameters("as_of is in the future; a release cannot claim data that has not been recorded")
    return {"as_of": as_of, "top_n": top_n, "availability": availability}


def _legacy_warehouse() -> str | None:
    env = os.environ.get("PULSE_LEGACY_WAREHOUSE")
    if env:
        return env
    from pulse.workers.ingest import DEFAULT_WAREHOUSE          # same default as the analysis CLI
    return str(DEFAULT_WAREHOUSE) if DEFAULT_WAREHOUSE.exists() else None


def run_release_build(conn: psycopg.Connection, params: dict) -> dict:
    """Parse the closed parameter set and call ``build_release`` for the ``main`` slot. Exceptions of the build propagate."""
    from pulse.adapters.lake import LakeStore
    from pulse.application import account_service as acct
    from pulse.application.release_service import BuildConfig, build_release

    p = parse_job_params(params)
    cfg = BuildConfig(slot_key=B.SLOT_KEY, top_n_plans=p["top_n"], legacy_warehouse=_legacy_warehouse(),
                      availability=B.RECONSTRUCTED_POLICY if p["availability"] == "reconstructed" else B.AVAILABILITY_POLICY)
    account = conn.execute("SELECT 1 FROM execution.account_head WHERE account_id=%s", (acct.PAPER_ACCOUNT_ID,)).fetchone()
    r = build_release(conn, LakeStore(config.load().lake_root), as_of=p["as_of"], cfg=cfg,
                      account_id=acct.PAPER_ACCOUNT_ID if account else None)
    return contracts.to_wire({"release_id": r.release_id, "replayed": r.replayed, "decision_as_of": r.decision_as_of,
                              "session_date": r.session_date, "pointer_generation": r.pointer_generation,
                              "degraded_components": list(r.degraded), "counts": r.counts})


@ops_service.register_handler("ReleaseService.build_release")
def _job_build_release(conn, run, args) -> dict:
    return run_release_build(conn, args["params"])


def ensure_command_binding(conn: psycopg.Connection, operation_id: str, method: str, path: str, service: str) -> None:
    """Register a backfill command in ``core.api_binding`` (``ops.command_receipt`` references it). Idempotent; a row that exists is
    only read, so a runtime role without INSERT on the table still works once the row is there."""
    if conn.execute("SELECT 1 FROM core.api_binding WHERE operation_id=%s", (operation_id,)).fetchone():
        return
    sid = contracts.backfill_id("ops")
    version = contracts._load("backfill_ops.schema.json")["x-contract-version"]
    conn.execute("INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id, response_schema_version,"
                 " application_service, read_model, freshness_policy_version) VALUES (%s,%s,%s,%s,%s,%s,'none','backfill-v1') ON CONFLICT (operation_id) DO NOTHING",
                 (operation_id, method, path, sid, version, service))


# ---- agent access (#63, #64) -----------------------------------------------------------------------------------------------------
def _core_auth(scope: str | None) -> tuple[str, str | None]:
    if scope == "public":
        return "public", scope
    if scope == "operator":
        return "operator", scope
    return "session", scope


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    line = inspect.cleandoc(text).splitlines()[0].strip()
    return line or None


def mounted_backfill_operations(openapi_paths: dict) -> list[dict]:
    """Operations under ``/v1/x/bf/`` (and ``/llms.txt``) as the running app documents them (``app.openapi()['paths']``).

    A route declares its auth with ``openapi_extra={"x-auth": "public"|"session"|"operator"}``; one that does not is listed
    as ``unspecified`` rather than guessed."""
    out = []
    for path, methods in openapi_paths.items():
        if not (path.startswith(BF_PREFIX) or path == LLMS_PATH):
            continue
        lane = path[len(BF_PREFIX):].split("/", 1)[0] if path.startswith(BF_PREFIX) else "ops"
        for method, op in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            declared = op.get("x-auth")
            out.append({
                "operation_id": op.get("operationId") or f"{method}:{path}", "method": method.upper(), "path": path,
                "summary": op.get("summary") or _first_line(op.get("description")),
                "auth": declared or ("session" if op.get("security") else "unspecified"),
                "auth_basis": "declared" if declared else ("contract" if op.get("security") else "none"),
                "auth_detail": None, "contract_id": contracts.backfill_id(lane) if path.startswith(BF_PREFIX) else None,
                "source": "backfill", "idempotency_key_required": method.upper() == "POST"})
    return out


def build_manifest(conn: psycopg.Connection, openapi_paths: dict) -> dict:
    """Every registered operation: ``core.api_binding`` rows + the mounted backfill routes. Generated, never hand-written."""
    core = {o["operation_id"]: o for o in contracts.api_bindings()["operations"]}
    ext = {op["operationId"]: op for ms in contracts._load("extension.openapi.json")["paths"].values() for op in ms.values()}
    rows = conn.execute("SELECT operation_id, http_method, path_template, response_contract_id FROM core.api_binding"
                        " ORDER BY path_template, http_method, operation_id").fetchall()
    ops: list[dict] = []
    for r in rows:
        oid = r["operation_id"]
        if r["response_contract_id"].startswith(contracts.BACKFILL_PREFIX):
            continue                                                  # backfill commands are listed from the mounted routes below
        if r["response_contract_id"] == contracts.EXT_ID and oid in ext:
            text = ext[oid].get("x-authorization") or ""
            auth, detail, summary, source = ("public" if text.lower().startswith("public") else "session"), (text or None), ext[oid].get("summary"), "ext"
        else:
            b = core.get(oid, {})
            (auth, detail), summary, source = _core_auth(b.get("authorization_scope")), b.get("application_service"), "core"
        ops.append({"operation_id": oid, "method": r["http_method"], "path": r["path_template"], "summary": summary, "auth": auth,
                    "auth_basis": "contract", "auth_detail": detail, "contract_id": r["response_contract_id"], "source": source,
                    "idempotency_key_required": r["http_method"] == "POST"})
    n_bound = len(rows)
    bf = sorted(mounted_backfill_operations(openapi_paths), key=lambda o: (o["path"], o["method"]))
    ops.extend(bf)
    return {"manifest_version": "1.0.0", "contract_version": contracts.CONTRACT_VERSION, "operation_count": len(ops),
            "generated_from": {"core_api_binding_rows": n_bound, "mounted_backfill_routes": len(bf)}, "operations": ops}


def render_llms_txt(manifest: dict, *, manifest_path: str = BF_PREFIX + "ops/manifest") -> str:
    """Plain-text description for agents (llms.txt convention): conventions first, then every operation."""
    lines = [
        "# Pulse3", "",
        "> Research workbench for China A-share main-board analysis. Read-only research output, not investment advice.", "",
        "## Conventions", "",
        "- Every JSON response is an envelope {meta, data}; failures are {meta, error:{code,message,retryable,field_errors}}.",
        "- Money, prices and ratios are decimal strings; missing values are null (never 0); instants are UTC strings ending in Z.",
        "- Sessions use the cookie `pulse_session`; commands (POST) need an `Idempotency-Key` header; the same key replays the first answer.",
        f"- Machine-readable list of all operations: GET {manifest_path} (JSON, contract-checked).", "",
        "## Operations", "",
    ]
    for source, title in (("core", "Core v3"), ("ext", "Extension"), ("backfill", "Backfill")):
        group = [o for o in manifest["operations"] if o["source"] == source]
        if not group:
            continue
        lines += [f"### {title} ({len(group)})", ""]
        for o in group:
            summary = (o["summary"] or "").replace("\n", " ").strip()
            lines.append(f"- {o['method']} {o['path']} - {o['operation_id']}" + (f": {summary}" if summary else "") + f" [auth: {o['auth']}]"
                         + (" [Idempotency-Key]" if o["idempotency_key_required"] else ""))
        lines.append("")
    return "\n".join(lines)

