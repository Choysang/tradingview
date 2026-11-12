import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from w02_helpers import ready_all, staged


@pytest.mark.pg
def test_release_pointer_release_and_updates(client, pg_dsn):
    from pulse.adapters.postgres.release import PgAnalysisRelease

    slot = f"api-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        rel = PgAnalysisRelease(conn)
        rid, stage = staged(rel, slot)
        ready_all(rel, rid, ["ranking", "plans"])
        rel.publish(stage.stage_id, producer_version="t")
        conn.commit()
    cur = client.get("/v1/releases/current", params={"slot_key": slot})
    assert cur.status_code == 200, cur.text
    assert cur.json()["data"]["current_release_id"] == str(rid)
    r = client.get(f"/v1/releases/{rid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["release_state"] == "READY"
    first = client.get("/v1/updates", params={"slot_key": slot}).json()["data"]
    assert first["reset_required"] is True
    page = client.get("/v1/updates", params={"slot_key": slot, "cursor": "AAAA"})
    assert page.status_code == 400 and page.json()["error"]["code"] == "CURSOR_INVALID"
    assert client.get("/v1/releases/current", params={"slot_key": "nope"}).status_code == 404
    assert client.get(f"/v1/jobs/{uuid.uuid4()}").status_code == 404
