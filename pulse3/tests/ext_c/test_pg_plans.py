"""PEX05 on real PostgreSQL: proposals, the single compile step, plan versions/origins, ownership, stale inputs, idempotency."""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pulse import contracts
from pulse.application import ops_service
from pulse.application import portfolio_service as PS

from . import support as S

pytestmark = pytest.mark.pg
UTC = timezone.utc


@pytest.fixture()
def plans(conn, principal):
    iid = S.seed_instrument(conn, "600951", "计划股")
    rel = S.publish(conn)
    sys_plan = S.make_plan(iid, rel)
    S.insert_plan(conn, sys_plan)
    return {"iid": iid, "release": rel, "plan": sys_plan}


def conds(plan, value="9.9"):
    c = copy.deepcopy(plan["entry_conditions"])
    walk = [c]
    while walk:
        x = walk.pop()
        if isinstance(x, dict):
            if x.get("kind") == "constant":
                x["value"] = value
            walk.extend(x.values())
        elif isinstance(x, list):
            walk.extend(x)
    return c


def propose_body(plans, **kw):
    body = {"instrument_id": str(plans["iid"]), "based_on_release_id": str(plans["release"]), "base_plan": None, "expected_plan_version": 0,
            "account_id": None, "expected_account_version": None, "requested_conditions": conds(plans["plan"]), "strategy_instances": []}
    return {**body, **kw}


def drain(conn):
    """Run only the plan-compile jobs (the queue is shared with every other suite in the session database)."""
    out = []
    job = ops_service.DEFINITIONS["PortfolioService.compile_proposal"].job_id
    while (r := ops_service.run_next(conn, job_id=job)) is not None:
        out.append(r)
    return out


def test_propose_is_async_idempotent_and_compiles_into_a_separate_user_chain(make_api, conn, principal, plans):
    api = make_api(principal)
    r = api.post("propose-trade-plan", propose_body(plans), key="p1")
    assert r.status_code == 202, r.text
    receipt = r.json()["data"]["receipt"]
    assert receipt["poll_operation_id"] == "queryPlanProposal" and receipt["run_state"] == "queued"
    pid = receipt["resource_id"]
    # same payload, another key: the same proposal and no second plan
    r2 = api.post("propose-trade-plan", propose_body(plans), key="p2")
    assert r2.json()["data"]["receipt"]["resource_id"] == pid
    # same key, other payload
    assert api.post("propose-trade-plan", propose_body(plans, expected_plan_version=3), key="p1").status_code == 409
    q = api.get("query-plan-proposal", proposal_id=pid).json()["data"]
    assert q["proposal_state"] == "queued" and q["compiled_plan"] is None
    done = drain(conn)
    assert done and all(d["state"] == "succeeded" for d in done)
    q = api.get("query-plan-proposal", proposal_id=pid).json()["data"]
    assert q["proposal_state"] == "compiled" and q["job"]["run_state"] == "succeeded" and q["reason_code"] is None
    cp = q["compiled_plan"]
    assert cp["plan_id"] != plans["plan"]["plan_id"] and cp["plan_version"] == 1
    # the system plan is untouched; the user's version names it in `supersedes`, origin user_edit
    assert conn.execute("SELECT count(*) n FROM decision.trade_plan WHERE plan_id=%s", (plans["plan"]["plan_id"],)).fetchone()["n"] == 1
    v = api.get("query-plan-versions", plan_id=cp["plan_id"], page={"limit": 10}).json()["data"]
    assert v["plans"][0]["entry_conditions"] != plans["plan"]["entry_conditions"] and v["plans"][0]["supersedes"]["plan_id"] == plans["plan"]["plan_id"]
    assert v["origins"][0]["origin"] == "user_edit" and v["origins"][0]["change_reason"]
    assert conn.execute("SELECT count(*) n FROM decision.trade_plan WHERE instrument_id=%s", (plans["iid"],)).fetchone()["n"] == 2
    # an unauthorized user cannot read someone else's edited plan by id; the system plan stays public
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.get("query-plan-versions", plan_id=cp["plan_id"], page={"limit": 10}).status_code == 404
    assert api2.get("query-plan-proposal", proposal_id=pid).status_code == 404
    pub = api2.get("query-plan-versions", plan_id=plans["plan"]["plan_id"], page={"limit": 10})
    assert pub.status_code == 200 and pub.json()["data"]["origins"][0]["origin"] == "system"
    # listing by instrument: owner sees both chains with explicit origins, the stranger only the system plan
    mine = api.get("list-trade-plans", instrument_id=plans["iid"], page={"limit": 10}).json()["data"]
    assert sorted(o["origin"] for o in mine["origins"]) == ["system", "user_edit"]
    theirs = api2.get("list-trade-plans", instrument_id=plans["iid"], page={"limit": 10}).json()["data"]
    assert [o["origin"] for o in theirs["origins"]] == ["system"]


def test_edit_of_an_edit_appends_to_the_users_chain_and_a_stale_base_is_a_version_conflict(make_api, conn, principal, plans):
    api = make_api(principal)
    api.post("propose-trade-plan", propose_body(plans), key="a")
    drain(conn)
    user_plan = conn.execute("SELECT plan_id FROM decision.plan_revision_metadata WHERE principal_id=%s", (principal,)).fetchone()["plan_id"]
    base = {"plan_id": str(user_plan), "plan_version": 1}
    r = api.post("propose-trade-plan", propose_body(plans, base_plan=base, expected_plan_version=1, requested_conditions=conds(plans["plan"], "9.5")), key="b")
    assert r.status_code == 202, r.text
    drain(conn)
    assert conn.execute("SELECT max(plan_version) v FROM decision.trade_plan WHERE plan_id=%s", (user_plan,)).fetchone()["v"] == 2
    stale = api.post("propose-trade-plan", propose_body(plans, base_plan=base, expected_plan_version=1, requested_conditions=conds(plans["plan"], "9.4")), key="c")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "VERSION_CONFLICT"
    wrong = api.post("propose-trade-plan", propose_body(plans, expected_plan_version=2), key="d")
    assert wrong.json()["error"]["code"] == "VERSION_CONFLICT"                                              # creating needs 0
    assert api.post("propose-trade-plan", propose_body(plans, based_on_release_id=str(uuid.uuid4())), key="e").status_code == 404


def test_account_version_is_checked_when_proposing_and_again_when_compiling(make_api, conn, principal, plans):
    api = make_api(principal)
    acct = S.open_account(conn)
    PS.grant(conn, principal, acct, "plan", datetime.now(UTC) - timedelta(days=1))
    PS.grant(conn, principal, acct, "read", datetime.now(UTC) - timedelta(days=1))
    ok = api.post("propose-trade-plan", propose_body(plans, account_id=str(acct), expected_account_version=0), key="x1")
    assert ok.status_code == 202, ok.text
    # the account moves before the worker runs: the proposal is rejected with a reason, no plan is invented
    conn.execute("SELECT 1")
    S_ledger_advance(conn, acct)
    done = drain(conn)
    assert done and all(d["state"] == "succeeded" for d in done)
    q = api.get("query-plan-proposal", proposal_id=ok.json()["data"]["receipt"]["resource_id"]).json()["data"]
    assert q["proposal_state"] == "rejected" and q["reason_code"] == "ACCOUNT_VERSION_STALE" and q["compiled_plan"] is None
    assert conn.execute("SELECT count(*) n FROM decision.trade_plan WHERE instrument_id=%s", (plans["iid"],)).fetchone()["n"] == 1
    # stale at propose time
    assert api.post("propose-trade-plan", propose_body(plans, account_id=str(acct), expected_account_version=0), key="x2").json()["error"]["code"] == "VERSION_CONFLICT"
    # no plan grant: the account does not exist for you
    other = S.open_account(conn)
    assert api.post("propose-trade-plan", propose_body(plans, account_id=str(other), expected_account_version=0), key="x3").status_code == 404


def S_ledger_advance(conn, acct):
    from pulse.adapters.postgres.ledger_repo import EventContext, LedgerRepo
    from pulse.domain.execution import KERNEL_VERSION
    from pulse.domain.market_rules import MARKET_RULES_VERSION
    LedgerRepo(conn).append(acct, {"event_id": "dep1", "sequence": 1, "kind": "capital.deposit", "effective_at": "2026-10-01T10:00:00+08:00", "payload": {"amount": "1000"}},
                            EventContext("t", MARKET_RULES_VERSION, "v", KERNEL_VERSION, "none"))


def test_a_superseded_base_ends_superseded_not_as_a_stale_plan(make_api, conn, principal, plans):
    api = make_api(principal)
    r = api.post("propose-trade-plan", propose_body(plans, base_plan={"plan_id": plans["plan"]["plan_id"], "plan_version": 1}, expected_plan_version=1), key="s1")
    assert r.status_code == 202, r.text
    v2 = S.make_plan(plans["iid"], plans["release"], plan_id=uuid.UUID(plans["plan"]["plan_id"]), version=2)
    v2["supersedes"] = {"plan_id": plans["plan"]["plan_id"], "plan_version": 1}
    S.insert_plan(conn, v2)                                                  # a new system version lands before the worker runs
    drain(conn)
    q = api.get("query-plan-proposal", proposal_id=r.json()["data"]["receipt"]["resource_id"]).json()["data"]
    assert q["proposal_state"] == "superseded" and q["reason_code"] == "BASE_PLAN_SUPERSEDED" and q["compiled_plan"] is None


def test_plan_proposal_is_never_an_order(conn, principal, plans):
    n0 = conn.execute("SELECT count(*) n FROM execution.account_event").fetchone()["n"]
    PS.propose_trade_plan(conn, principal, {"instrument_id": plans["iid"], "based_on_release_id": plans["release"], "base_plan": None, "expected_plan_version": 0,
                                           "account_id": None, "expected_account_version": None, "requested_conditions": conds(plans["plan"]), "strategy_instances": []}, "k")
    drain(conn)
    assert conn.execute("SELECT count(*) n FROM execution.account_event").fetchone()["n"] == n0
    contracts.check("TradePlan", conn.execute("SELECT plan_payload p FROM decision.trade_plan t JOIN decision.plan_revision_metadata m USING (plan_id, plan_version)"
                                              " WHERE m.principal_id=%s", (principal,)).fetchone()["p"])


def test_get_trade_plan_hides_another_users_edit(make_api, conn, principal, plans):
    """getTradePlan (v3) serves system plans publicly but a user_edit version only to its owner."""
    api = make_api(principal)
    pid = api.post("propose-trade-plan", propose_body(plans), key="gtp1").json()["data"]["receipt"]["resource_id"]
    drain(conn)
    cp = api.get("query-plan-proposal", proposal_id=pid).json()["data"]["compiled_plan"]
    path = f"/v1/plans/{cp['plan_id']}/versions/{cp['plan_version']}"
    assert api.c.get(path, cookies={"pulse_session": f"tok-{principal}"}).status_code == 200
    stranger = uuid.uuid4()
    api2 = make_api(principal, stranger).as_(stranger)
    assert api2.c.get(path, cookies={"pulse_session": f"tok-{stranger}"}).status_code == 404
    assert api2.c.get(path).status_code == 404                      # anonymous
    sys_path = f"/v1/plans/{plans['plan']['plan_id']}/versions/{plans['plan']['plan_version']}"
    assert api2.c.get(sys_path).status_code == 200                  # system plan stays public


def test_a_failed_compile_leaves_no_partial_state(make_api, conn, principal, plans, monkeypatch):
    """The compile step is one transaction: a failure while writing the version rolls back the 'running' mark too."""
    from pulse.adapters.postgres import decision_repo

    api = make_api(principal)
    pid = api.post("propose-trade-plan", propose_body(plans), key="tx1").json()["data"]["receipt"]["resource_id"]

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(decision_repo, "append_plan_version", boom)
    done = drain(conn)
    assert done and done[0]["state"] == "failed"
    row = conn.execute("SELECT state, compiled_plan_id FROM decision.plan_proposal WHERE proposal_id=%s", (pid,)).fetchone()
    assert row["state"] == "queued" and row["compiled_plan_id"] is None
    assert conn.execute("SELECT count(*) n FROM decision.plan_revision_metadata m JOIN decision.trade_plan t USING (plan_id, plan_version)"
                        " WHERE t.instrument_id=%s", (plans["iid"],)).fetchone()["n"] == 0
