"""Research-registry records of the fusion forward experiments (purpose ``shadow``), one per pre-registered record.

Why: legacy triage batch 3 asks that a forward experiment is registered with its pre-registration document's sha256 before it
records anything (docs/LEGACY_TRIAGE.md, backfill brief). The five records are the frozen main book (E036/E040), the 10-day shadow
(E065), the forward monitor (E068), the X20 basket (E085) and OS20 (E081). All five use the frozen score ``fusion_static40_safe``.
Invariants: ids are derived from names and digests, so running ``register`` twice changes nothing; a run stays ``queued`` (a forward
record never completes; the datasets hold its numbers); the implementation digest is the sha256 of the frozen-data manifest (a changed expression set is a new
experiment); the strategy instances are ``specified``, never promoted here (promotion is the registry's own policy).
Not covered: metrics (the datasets hold the numbers), league pairing, promotion decisions.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import research_repo
from pulse.domain.research import experiments as E
from pulse.domain.strategy.canonical import canonical_hash, stable_uuid
from pulse.domain.fusion import scorer as SC

FAMILY = "fusion_static40_safe_forward"
TARGET = "rel_return_5td_vs_universe_mean_open_to_open_v1"
TAXONOMY = {"main": "frozen main book, hold 5 days, curve gate", "h10": "shadow book, hold 10 days", "monitor": "rank IC monitor",
            "x20": "X20 exclusion basket", "os20": "20-day shock-type oversold rebound"}
HORIZON = {"main": "5td", "h10": "10td", "monitor": "5td", "x20": "10td", "os20": "10td"}
CLAIMS = {
    "main": "The frozen static40 score, traded as a 5-day book with a 20-day equity-curve gate, keeps a positive net excess in forward data",
    "h10": "Holding 10 days halves turnover and is not worse than the 5-day book after costs (ungated NAV vs ungated NAV)",
    "monitor": "The frozen score keeps a positive rank IC (mean IC > 0 with Newey-West t >= 2 at 100 unflagged decision dates)",
    "x20": "Dropping the lowest 20% of scores and rebalancing every 10 sessions beats the equal-weight pool after a 0.126% side cost",
    "os20": "20-day oversold stocks that suffered a shock beat their size x volatility cell over the next 10 sessions, more in falling markets",
}


def _implementation_digest() -> str:
    return hashlib.sha256((SC.DATA / "manifest.json").read_bytes()).hexdigest()


def register_forward_experiments(conn: psycopg.Connection, lake: LakeStore, *, now: datetime | None = None) -> dict[str, Any]:
    """Create the strategy definition / instances, hypotheses, the sealed context and the five shadow runs (idempotent)."""
    from pulse.workers.research import fixed_context

    now = now or datetime.now(timezone.utc)
    manifest = json.loads((SC.DATA / "manifest.json").read_text(encoding="utf-8"))
    digest = _implementation_digest()
    spec = {"score": "fusion_static40_safe", "frozen_data": manifest["files"], "kind": "forward record", "boards": "main"}
    sid, sver = stable_uuid("strategy", "fusion_static40_safe"), 1
    dhash = canonical_hash(spec)
    out: dict[str, Any] = {}
    with conn.transaction():
        if conn.execute("SELECT 1 FROM strategy.definition WHERE strategy_id=%s AND strategy_version=%s", (sid, sver)).fetchone() is None:
            conn.execute(
                """INSERT INTO strategy.definition(strategy_id,strategy_version,display_name,role,method_family,forecast_target,
                       horizon_id,specification,definition_hash,created_at) VALUES (%s,%s,%s,'selection','statistical',%s,'5td',%s,%s,%s)""",
                (sid, sver, "Fusion static40 (round 5, main board, level-safe)", TARGET, Jsonb(spec), dhash, now))
        ctx_spec = fixed_context(conn, lake, now=now)
        ctx_id = research_repo.seal_context(conn, ctx_spec, created_at=now, sealed_at=now)
        for key, claim in CLAIMS.items():
            prereg = manifest["preregistrations"][key]
            iid = stable_uuid("instance", "fusion_static40_safe", key)
            params = {"experiment": key, "preregistration_sha256": prereg}
            conn.execute(
                """INSERT INTO strategy.instance(instance_id,strategy_id,strategy_version,parameters,parameter_hash,universe_policy_version,
                       lifecycle,implementation_status,reproduction_status,health,created_at)
                   VALUES (%s,%s,%s,%s,%s,'cn-mainboard-rules-v1+default-pref-v1','specified','implemented','running','healthy',%s)
                   ON CONFLICT (instance_id) DO NOTHING""", (iid, sid, sver, Jsonb(params), canonical_hash(params), now))
            hid = stable_uuid("hypothesis", "fusion_static40_safe", key)
            research_repo.register_hypothesis(
                conn, hypothesis_id=hid, hypothesis_version=1, claim=claim, mechanism=TAXONOMY[key], forecast_target=TARGET,
                horizon_id=HORIZON[key], input_requirements={"frozen_data": manifest["files"]},
                falsification_conditions={"preregistration": manifest["preregistrations"][key]}, available_at=now, strategy=(sid, sver))
            rec = E.new_run(instance_id=iid, hypothesis_id=hid, hypothesis_version=1, evaluation_context_id=ctx_id, run_purpose="shadow",
                            implementation_digest=digest, random_seed=0,
                            run_manifest={"kind": "forward_record", "key": key, "preregistration_sha256": prereg,
                                          "datasets": ["fusion_scores_current", "fusion_scores_asof", "fusion_forward_log"]},
                            family_key=FAMILY)
            created = research_repo.register_experiment(conn, rec)
            out[key] = {"experiment_id": str(rec.experiment_id), "created": bool(created), "preregistration_sha256": prereg}
    return out
