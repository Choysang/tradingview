"""Review fix H4: plans written by a FAILED release attempt are invisible and never pollute the public version chain."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import db
from pulse.application.release_service import BuildFailed, build_release

from . import world as W
from .test_release import cfg

pytestmark = pytest.mark.pg
FAIL = frozenset({"workspace_projection"})


def _plans(conn, release_id):
    return {(r["plan_id"], r["plan_version"]): r["plan_payload"] for r in conn.execute(
        "select plan_id, plan_version, plan_payload from decision.trade_plan where based_on_release_id=%s", (release_id,))}


@pytest.fixture()
def api(world, monkeypatch):
    monkeypatch.setenv("PULSE_DSN", world["dsn"])
    from pulse.api import deps
    from pulse.api.app import create_app
    deps.close_pool()
    with TestClient(create_app()) as c:
        yield c
    deps.close_pool()


def test_failed_release_plans_are_not_served_and_do_not_enter_the_public_chain(world, api):
    conn, lake = db.connect(world["dsn"], autocommit=True), LakeStore(world["lake_root"])
    try:
        with pytest.raises(BuildFailed) as ex:
            build_release(conn, lake, cfg=cfg(world, slot_key="main-h4a", fail_components=FAIL), clock=W.clock())
        dead1 = _plans(conn, ex.value.release_id)
        assert dead1
        (pid, ver), = [next(iter(dead1))]
        assert api.get(f"/v1/plans/{pid}/versions/{ver}").status_code == 404           # the failed attempt's plan is not served
        pub = build_release(conn, lake, cfg=cfg(world, slot_key="main-h4a"), clock=W.clock())
        pub_plans = _plans(conn, pub.release_id)
        dead_by_id: dict = {}
        for (p, v) in dead1:
            dead_by_id.setdefault(p, set()).add(v)
        for (p, v), payload in pub_plans.items():
            dead_versions = dead_by_id.get(p, set())
            assert v > max(dead_versions, default=0)                                    # numbers burned by the failed attempt are skipped
            sup = payload["supersedes"]
            assert sup is None or sup["plan_version"] not in dead_versions              # the chain never points at an unpublished version
        key = next(k for k in pub_plans if k[0] in dead_by_id)
        ok = api.get(f"/v1/plans/{key[0]}/versions/{key[1]}")
        assert ok.status_code == 200 and not ok.json()["meta"]["warnings"]

        # a later failed attempt (another slot, same inputs) appends on top of the published version, not of the dead one
        with pytest.raises(BuildFailed) as ex2:
            build_release(conn, lake, cfg=cfg(world, slot_key="main-h4b", fail_components=FAIL), clock=W.clock())
        dead2 = _plans(conn, ex2.value.release_id)
        newest = max(v for (p, v) in dead2 if p == key[0])
        assert newest > key[1] and dead2[(key[0], newest)]["supersedes"] == {"plan_id": str(key[0]), "plan_version": key[1]}
        still = api.get(f"/v1/plans/{key[0]}/versions/{key[1]}")
        assert still.status_code == 200 and not still.json()["meta"]["warnings"]       # an unpublished newer version is not "newer"
        assert api.get(f"/v1/plans/{key[0]}/versions/{newest}").status_code == 404

        pub2 = build_release(conn, lake, cfg=cfg(world, slot_key="main-h4b"), clock=W.clock())
        nxt = {k: v for k, v in _plans(conn, pub2.release_id).items() if k[0] == key[0]}
        (k2, p2), = nxt.items()
        assert k2[1] > newest and p2["supersedes"] == {"plan_id": str(key[0]), "plan_version": key[1]}
        warned = api.get(f"/v1/plans/{key[0]}/versions/{key[1]}").json()["meta"]["warnings"]
        assert [w["code"] for w in warned] == ["PLAN_SUPERSEDED"] and str(k2[1]) in warned[0]["message"]
    finally:
        conn.close()
