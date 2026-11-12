"""I1: paper account, quote snapshot, ActionabilitySnapshot, T/grid sub-plans, research worker and the analysis CLI (real PG)."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from pulse import contracts
from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import decision_repo, research_repo
from pulse.adapters.postgres.jobs import PgJobStore
from pulse.adapters.postgres.ledger_repo import LedgerRepo
from pulse.adapters.sources.http import RateLimitedClient
from pulse.adapters.sources.sina import SinaAdapter
from pulse.application import account_service as A
from pulse.application import bindings as B
from pulse.application.data_service import DataService
from pulse.application.release_service import BuildConfig, build_release, build_inventory_subplans, strategy_instance_refs
from pulse.workers import analysis, research

from . import world as W

pytestmark = pytest.mark.pg


def sina(transport):
    return SinaAdapter(RateLimitedClient(min_interval_seconds=0, transport=transport))


@pytest.fixture(scope="module")
def all_plans_release(world):
    from pulse.adapters.postgres import db
    conn = db.connect(world["dsn"], autocommit=True)
    A.open_paper_account(conn, trading_day=date(2026, 11, 11))
    r = build_release(conn, LakeStore(world["lake_root"]), cfg=BuildConfig(slot_key="main-account", top_n_plans=61, workspace_page=3,
                                                                       legacy_warehouse=str(world["warehouse"])),
                      account_id=A.PAPER_ACCOUNT_ID, clock=W.clock())
    yield r, conn
    conn.close()


def ids_of(conn, codes):
    return {row["instrument_id"]: row["source_symbol"] for row in conn.execute(
        "select a.instrument_id, a.source_symbol from core.instrument_alias a join core.instrument i using (instrument_id)"
        " where a.source_id='legacy_warehouse' and i.instrument_type='equity' and a.source_symbol = any(%s)", (list(codes),))}


def test_paper_account_is_opened_once_through_the_unified_ledger(all_plans_release):
    r, conn = all_plans_release
    assert A.open_paper_account(conn, trading_day=date(2026, 11, 11)) is False          # idempotent
    snap = LedgerRepo(conn).load(A.PAPER_ACCOUNT_ID).snapshot({})
    assert snap["cash_settled"] == "1000000" and snap["nav"] == "1000000" and snap["positions"] == {}
    ev = conn.execute("select event_type, cost_policy_version from execution.account_event where account_id=%s", (A.PAPER_ACCOUNT_ID,)).fetchall()
    assert [(e["event_type"], e["cost_policy_version"]) for e in ev] == [("opening", "user-account-2026-11-08-v1")]
    assert LedgerRepo(conn).trial_balance(A.PAPER_ACCOUNT_ID)


def test_actionability_is_bound_to_release_account_version_and_quote_snapshot(world, all_plans_release):
    from data.helpers import mock_transport
    r, conn = all_plans_release
    svc = DataService(conn, LakeStore(world["lake_root"]))
    real = ids_of(conn, W.REAL)
    quote = A.publish_quote_snapshot(svc, sina(mock_transport()), real)
    assert quote.manifest["manifest_state"] == "READY" and len(quote.views) == 3 and not quote.missing
    proj = conn.execute("select count(*) n from market.quote_projection where quote_snapshot_id=%s", (quote.manifest["snapshot_id"],)).fetchone()["n"]
    assert proj == 3                                                                  # G4: quote_projection is populated
    ranking = conn.execute("select ranking_id from decision.ranking_run where release_id=%s and ranking_scope='market' and horizon_id='20td'",
                           (r.release_id,)).fetchone()["ranking_id"]
    series = B.load_raw_series(conn, list(real), W.LAST, datetime.now(timezone.utc))
    payload = A.build_actionability(conn, release_id=r.release_id, ranking_id=ranking, account_id=A.PAPER_ACCOUNT_ID, quote=quote,
                                    raw_series=series, checked_at=datetime.now(timezone.utc))
    contracts.check("ActionabilitySnapshot", payload)
    hdr = decision_repo.actionability_header(conn, uuid.UUID(payload["actionability_id"]))
    assert (hdr["release_id"], hdr["ranking_id"], hdr["account_id"], hdr["account_version"], hdr["quote_snapshot_id"]) == (
        r.release_id, ranking, A.PAPER_ACCOUNT_ID, 0, quote.manifest["snapshot_id"])
    by_inst = {e["instrument_id"]: e for e in payload["entries"]}
    for iid in real:
        e = by_inst[str(iid)]
        assert "QUOTE_MISSING" not in e["reason_codes"]
        assert e["actionability_status"] in ("eligible", "not_triggered", "blocked", "unknown")
    unquoted = [e for k, e in by_inst.items() if uuid.UUID(k) not in real]
    assert unquoted
    for e in unquoted:               # without a quote nothing is eligible; a TRUE trigger stays unknown for lack of a quote
        assert e["actionability_status"] in ("not_triggered", "unknown") and e["account_action_rank"] is None
        if e["actionability_status"] == "unknown":
            assert any(c in e["reason_codes"] for c in ("QUOTE_MISSING", "TRIGGER_UNKNOWN"))
    with pytest.raises(Exception):                                                     # immutable
        conn.execute("update decision.actionability_snapshot set valid_until=valid_until + interval '1 day' where actionability_id=%s", (hdr["actionability_id"],))


def test_inventory_subplans_only_where_inventory_exists(world, all_plans_release):
    r, conn = all_plans_release
    refs = strategy_instance_refs(conn)
    as_of = datetime.now(timezone.utc)
    iid = next(iter(ids_of(conn, [W.CODES[10]])))
    series = B.load_raw_series(conn, [iid], W.LAST, as_of)
    empty = A.PAPER_ACCOUNT_ID
    snap0 = B.LedgerAccountSnapshotPort(conn).snapshot(empty)
    assert build_inventory_subplans(conn, release_id=r.release_id, account_snapshot=snap0, series_by_instrument=series, refs=refs,
                                    decision_as_of=as_of, available_at=as_of) == []            # empty account: no T / grid plans
    held = uuid.uuid5(uuid.NAMESPACE_URL, "i1-held-account")
    A.open_paper_account(conn, trading_day=date(2026, 11, 11), account_id=held, positions={str(iid): {"quantity": 1000, "cost": "10000"}})
    snap = B.LedgerAccountSnapshotPort(conn).snapshot(held)
    assert snap.positions[iid].sellable_shares == 1000
    plans = build_inventory_subplans(conn, release_id=r.release_id, account_snapshot=snap, series_by_instrument=series, refs=refs,
                                     decision_as_of=as_of, available_at=as_of)
    assert len(plans) == 2
    for p in plans:
        contracts.check("TradePlan", p)
        assert p["account_id"] == str(held) and p["expected_account_version"] == 0 and p["state"] == "watching"
        kind, h = p["validation"]["result_ref"].split(":")[1:]
        body = conn.execute("select body_kind, body_payload from decision.plan_body where body_hash=%s", (h,)).fetchone()
        assert body["body_kind"] == kind
        if kind == "t_plan":
            assert body["body_payload"]["direction"] == "sell_first" and body["body_payload"]["base_sellable_shares"] == 1000


# ---------------------------------------------------------------------------------------------------- research worker
def test_research_worker_records_unimplemented_trial_and_completed_trial_with_fencing(world):
    from pulse.adapters.postgres import db
    conn = db.connect(world["dsn"], autocommit=True)
    lake = LakeStore(world["lake_root"])
    seeded = research.seed_all(conn)
    assert seeded["papers_new"] == 46 and conn.execute("select count(*) n from strategy.instance where lifecycle='discovered'").fetchone()["n"] == 166
    assert research.seed_all(conn)["papers_new"] == 0                                     # idempotent
    # 1. an unimplemented candidate: a recorded, failed trial (never a fabricated metric)
    receipt = research.enqueue(conn, "S001")
    out = research.run_one(conn, lake)
    assert out["state"] == "failed" and out["error"] == "strategy_not_implemented"
    run = conn.execute("select run_state, error_code, evaluation_context_id from research.experiment_run where experiment_id=%s", (out["experiment_id"],)).fetchone()
    assert run["run_state"] == "failed" and run["error_code"] == "strategy_not_implemented"
    assert PgJobStore(conn).get(uuid.UUID(receipt["job_run_id"])).run_state == "failed"
    contracts.check("Evaluation", research_repo.load_evaluation_wire(conn, out["experiment_id"]))
    # 2. an implemented instance with an injected evaluator: completed, metrics stored, same fixed context
    conn.execute("update strategy.instance set implementation_status='implemented' where strategy_id in"
                 " (select strategy_id from strategy.definition where specification->>'catalog_id'='S002')")
    research.enqueue(conn, "S002")
    metric = {"metric_name": "net_excess_return", "metric_definition_version": "v1", "slice_key": "validation", "value": "0.01", "unit": "ratio",
              "lower_bound": None, "upper_bound": None, "confidence_level": None, "sample_count": 20, "quality_status": "partial", "missing_reason": None}
    done = research.run_one(conn, lake, {"implemented": lambda c, ctx, rec: [metric]})
    assert done["state"] == "completed" and done["metrics"] == 1
    wire = research_repo.load_evaluation_wire(conn, done["experiment_id"])
    contracts.check("Evaluation", wire)
    assert wire["context"]["evaluation_context_id"] == str(run["evaluation_context_id"])        # one fixed EvaluationContext
    assert research.run_one(conn, lake) is None                                           # queue drained
    # 3. a stale worker cannot overwrite: its lease is taken over while it evaluates
    conn.execute("update strategy.instance set implementation_status='implemented' where strategy_id in"
                 " (select strategy_id from strategy.definition where specification->>'catalog_id'='S003')")
    research.enqueue(conn, "S003")

    def steal(c, ctx, rec):
        c.execute("update ops.job_run set heartbeat_at = heartbeat_at - interval '3 hours' where run_state='running'")
        assert PgJobStore(c).claim(job_id=research.JOB_ID) is None            # budget spent -> run closed as LEASE_EXPIRED
        return [metric]
    stale = research.run_one(conn, lake, {"implemented": steal})
    assert stale["state"] == "fenced_out"
    row = conn.execute("select run_state, error_code from ops.job_run where job_run_id = (select job_run_id from ops.job_run order by scheduled_for desc limit 1)").fetchone()
    assert (row["run_state"], row["error_code"]) == ("failed", "LEASE_EXPIRED")
    assert conn.execute("select run_state from research.experiment_run where experiment_id=%s", (stale["experiment_id"],)).fetchone()["run_state"] == "queued"
    conn.close()


# ---------------------------------------------------------------------------------------------------- CLI
def test_analysis_cli_build_is_idempotent_and_show_reports_the_pointer(world, capsys):
    argv = ["--dsn", world["dsn"], "--lake", str(world["lake_root"]), "--slot", "main-cli"]
    assert analysis.run(argv + ["build", "--as-of", "latest", "--top-n", "3", "--legacy-warehouse", str(world["warehouse"])]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["replayed"] is False and first["counts"]["ranked_instruments"]["5td"] > 30
    assert analysis.run(argv + ["build", "--as-of", "latest", "--top-n", "3", "--legacy-warehouse", str(world["warehouse"])]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["replayed"] is True and second["release_id"] == first["release_id"]
    assert analysis.run(argv + ["show"]) == 0
    assert json.loads(capsys.readouterr().out)["release_id"] == first["release_id"]
    assert analysis.parse_as_of("latest") is None and analysis.parse_as_of("2026-11-10").tzinfo is not None
    with pytest.raises(ValueError):
        analysis.parse_as_of("2026-11-10T10:00:00")


# ---------------------------------------------------------------------------------------------------- review fix H1
def _sized_entries(conn, world, r, account_id, monkeypatch):
    from data.helpers import mock_transport
    from pulse.domain.plans import conditions as C
    monkeypatch.setattr(A, "plan_trigger", lambda plan, series, now: C.Tri.TRUE)       # isolate sizing from trigger data
    svc = DataService(conn, LakeStore(world["lake_root"]))
    real = ids_of(conn, W.REAL)
    quote = A.publish_quote_snapshot(svc, sina(mock_transport()), real)
    ranking = conn.execute("select ranking_id from decision.ranking_run where release_id=%s and ranking_scope='market' and horizon_id='20td'",
                           (r.release_id,)).fetchone()["ranking_id"]
    series = B.load_raw_series(conn, list(real), W.LAST, datetime.now(timezone.utc))
    payload = A.build_actionability(conn, release_id=r.release_id, ranking_id=ranking, account_id=account_id, quote=quote,
                                    raw_series=series, checked_at=datetime.now(timezone.utc))
    return [e for e in payload["entries"] if uuid.UUID(e["instrument_id"]) in real and e["plan_ref"]]


def test_h1_position_without_mark_makes_percent_sizing_unknown_not_forced_lot(all_plans_release, world, monkeypatch):
    r, conn = all_plans_release
    unquoted = next(iter(ids_of(conn, [W.CODES[10]])))
    held = uuid.uuid5(uuid.NAMESPACE_URL, "h1-held-unmarked")
    A.open_paper_account(conn, trading_day=date(2026, 11, 11), account_id=held, positions={str(unquoted): {"quantity": 1000, "cost": "10000"}})
    entries = _sized_entries(conn, world, r, held, monkeypatch)
    assert entries
    for e in entries:                       # NAV is unknowable (no mark for the held name): never 0-valued, never a forced 100 shares
        assert e["actionability_status"] == "unknown" and "SIZING_UNKNOWN" in e["reason_codes"]
        assert e["checked_quantity_shares"] is None


def test_h1_zero_share_sizing_is_insufficient_not_forced_to_one_lot(all_plans_release, world, monkeypatch):
    r, conn = all_plans_release
    poor = uuid.uuid5(uuid.NAMESPACE_URL, "h1-poor-account")
    A.open_paper_account(conn, trading_day=date(2026, 11, 11), account_id=poor, initial_cash="500")
    entries = _sized_entries(conn, world, r, poor, monkeypatch)
    assert entries
    for e in entries:
        assert e["actionability_status"] == "blocked" and "INSUFFICIENT_SIZE" in e["reason_codes"]
        assert e["checked_quantity_shares"] is None
