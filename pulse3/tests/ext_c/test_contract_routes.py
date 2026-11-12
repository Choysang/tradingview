"""All 25 operations of group C is mounted at its contract path, registered in core.api_binding, authenticated and contract-checked."""
from __future__ import annotations

import json

import pytest

from pulse import contracts

pytestmark = pytest.mark.pg
MINE = """proposeTradePlan queryPlanVersions listTradePlans queryPlanProposal queryEventClusters queryCalendarEvents querySeatDirectory querySeatProfile querySeatReports
queryCommunityOpinions listAuthorizedAccounts importBrokerFills queryAccountOrders queryAccountPerformance queryOpportunities saveAlertRule listUserAlerts deleteWorkspaceItem
scheduleWorkspaceExport queryWorkspaceRevision queryExportArtifact queryWorkspaceIndex runRegisteredJob queryDatasetStatus queryJobRuns""".split()


def test_all_25_operations_are_mounted_with_the_contract_method_and_registered(make_api, conn, principal):
    api = make_api(principal)
    spec = contracts._load("extension.openapi.json")["paths"]
    by_op = {op["operationId"]: (path, method) for path, ms in spec.items() for method, op in ms.items()}
    assert len(MINE) == 25
    for op in MINE:
        path, method = by_op[op]
        r = api.c.request(method.upper(), path)
        assert r.status_code in (400, 403, 422) and r.headers["content-type"].startswith("application/json"), op        # mounted (not the SPA fallback)
        row = conn.execute("SELECT http_method, response_contract_id FROM core.api_binding WHERE operation_id=%s", (op,)).fetchone()
        assert row and row["response_contract_id"] == contracts.EXT_ID and row["http_method"] == method.upper(), op


def test_queries_need_a_session_commands_need_a_key_and_bad_input_is_a_contract_error(make_api, principal):
    api = make_api(principal)
    spec = contracts._load("extension.openapi.json")["paths"]
    for path, ms in spec.items():
        for method, op in ms.items():
            if op["operationId"] not in MINE:
                continue
            if method == "get":
                assert api.c.get(path).status_code == 403, path                                 # no session at all
                bad = api.c.get(path, params={"nope": "1"}, cookies=api._cookies())
                assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_CONTRACT", path
            else:
                assert api.c.post(path, json={}, cookies=api._cookies()).status_code in (400, 422), path        # no Idempotency-Key
                r = api.c.post(path, json={"junk": 1}, headers={"Idempotency-Key": "k"}, cookies=api._cookies())
                assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CONTRACT", path


def test_responses_are_validated_so_a_drifted_service_would_fail_loudly(make_api, conn, principal, monkeypatch):
    api = make_api(principal)
    import pulse.api.routes_ext_c as R
    monkeypatch.setattr(R.portfolio_service, "list_authorized_accounts", lambda *a, **k: ([{"account_id": "not-a-uuid", "account_version": 1, "base_currency": "CNY"}], None))
    with pytest.raises(contracts.ContractViolation):
        api.get("list-authorized-accounts", page={"limit": 5})


def test_runtime_roles_get_exactly_the_grants_these_cards_need(pg_dsn):
    import psycopg
    c = psycopg.connect(pg_dsn, autocommit=False)

    def can(role: str, priv: str, table: str) -> bool:
        return c.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, priv)).fetchone()[0]
    for t in ("ops.ext_artifact", "notification.alert_intent", "calendar.event_revision", "access.account_grant", "execution.broker_import"):
        assert all(can(r, "SELECT", t) for r in ("pulse_api", "pulse_worker", "pulse_coordinator")), t
    assert can("pulse_api", "INSERT", "ops.ext_artifact") and can("pulse_api", "INSERT", "decision.plan_proposal") and can("pulse_api", "INSERT", "ui.workspace_revision")
    assert can("pulse_api", "INSERT", "notification.alert_rule") and can("pulse_api", "UPDATE", "notification.alert_rule_head")
    assert not can("pulse_api", "INSERT", "intelligence.cluster_revision") and not can("pulse_api", "INSERT", "calendar.event_revision")      # derived rows are workers'
    assert not can("pulse_api", "DELETE", "ops.ext_artifact") and not can("pulse_api", "UPDATE", "notification.alert_intent")
    assert can("pulse_worker", "INSERT", "notification.alert_intent") and can("pulse_worker", "INSERT", "calendar.event_revision") and can("pulse_worker", "INSERT", "research.opportunity_event")
    c.close()
