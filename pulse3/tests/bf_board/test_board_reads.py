"""Board lane reads on real PostgreSQL (synthetic world): watch cards, theme map, instrument search.

Proves wiring, contract validity (respond_backfill validates every payload), the release-state rule, and that missing data
is reported as missing (never 0). Does not prove market behaviour: the world is synthetic.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from pulse import contracts

pytestmark = pytest.mark.pg
SLOT = "main"
BASE = "/v1/x/bf/board"


@pytest.fixture()
def release_id(built):
    conn = built[1]
    return str(conn.execute("select current_release_id from decision.release_slot where slot_key=%s", (SLOT,)).fetchone()["current_release_id"])


def ranking_of(conn, release_id):
    return conn.execute("select ranking_id, horizon_id, forecast_target from decision.ranking_run where release_id=%s and ranking_scope='market'"
                        " order by created_at desc limit 1", (release_id,)).fetchone()


def test_watch_cards_cover_requested_ids_in_order_with_rank_and_spark(client, built, release_id):
    conn = built[1]
    run = ranking_of(conn, release_id)
    top = conn.execute("select instrument_id, alpha_rank from decision.ranking_entry where ranking_id=%s order by alpha_rank limit 3", (run["ranking_id"],)).fetchall()
    ids = [str(r["instrument_id"]) for r in top]
    r = client.get(f"{BASE}/watch-cards", params={"release_id": release_id, "ids": ",".join(ids), "horizon_id": run["horizon_id"], "forecast_target": run["forecast_target"]})
    assert r.status_code == 200, r.text
    body = r.json()
    contracts.check("WatchCards", body["data"], contracts.backfill_id("board"))
    cards = body["data"]["cards"]
    assert [c["instrument_id"] for c in cards] == ids
    assert body["data"]["amount_is_estimate"] is True
    assert body["meta"]["release_id"] == release_id
    for c, t in zip(cards, top):
        assert c["rank"]["alpha_rank"] == t["alpha_rank"] and c["rank"]["total"] >= 3
        assert c["last_close"] is not None and Decimal(c["last_close"]) > 0
        assert len(c["spark"]) > 1 and c["spark"][-1]["c"] == c["last_close"] and c["session"] == c["spark"][-1]["d"]
        assert c["amount_estimate_cny"] is not None
        assert c["pct_change"] is not None or c["pct_missing_reason"] is not None


def test_watch_cards_unknown_instrument_is_missing_not_zero(client, release_id):
    ghost = str(uuid.uuid4())
    r = client.get(f"{BASE}/watch-cards", params={"release_id": release_id, "ids": ghost})
    assert r.status_code == 200, r.text
    c = r.json()["data"]["cards"][0]
    assert c["instrument_id"] == ghost
    assert c["last_close"] is None and c["pct_change"] is None and c["amount_estimate_cny"] is None
    assert c["pct_missing_reason"] == "no_bar" and c["spark"] == [] and c["rank"] is None
    assert r.json()["meta"]["coverage"]["missing_count"] == 1


def test_watch_cards_reject_bad_input_and_non_public_release(client, release_id):
    assert client.get(f"{BASE}/watch-cards", params={"release_id": release_id, "ids": "not-a-uuid"}).status_code == 400
    assert client.get(f"{BASE}/watch-cards", params={"release_id": release_id, "ids": ""}).status_code == 400
    too_many = ",".join(str(uuid.uuid4()) for _ in range(61))
    assert client.get(f"{BASE}/watch-cards", params={"release_id": release_id, "ids": too_many}).status_code == 400
    # a release that is not published (unknown id here; BUILDING / FAILED take the same branch in public_release) is not served
    r = client.get(f"{BASE}/watch-cards", params={"release_id": str(uuid.uuid4()), "ids": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def sector_run(conn, release_id):
    return conn.execute("select taxonomy_id, taxonomy_version from decision.ranking_run where release_id=%s and ranking_scope='sector_overview'"
                        " order by created_at desc limit 1", (release_id,)).fetchone()


def test_theme_map_has_amount_and_average_change_per_sector_of_the_overview(client, built, release_id):
    conn = built[1]
    run = sector_run(conn, release_id)
    assert run is not None, "the synthetic release must carry a sector overview"
    r = client.get(f"{BASE}/theme-map", params={"release_id": release_id, "taxonomy_id": run["taxonomy_id"], "taxonomy_version": run["taxonomy_version"]})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    contracts.check("ThemeMap", d, contracts.backfill_id("board"))
    assert d["nodes"] and d["amount_is_estimate"] is True and d["session"] is not None
    overview_nodes = {x["node_id"] for x in conn.execute(
        "select e.node_id from decision.sector_rank_entry e join decision.ranking_run r on r.ranking_id=e.ranking_id where r.release_id=%s"
        " and r.ranking_scope='sector_overview'", (release_id,))}
    assert overview_nodes <= {n["node_id"] for n in d["nodes"]}
    with_amount = [n for n in d["nodes"] if n["amount_estimate_cny"] is not None]
    assert with_amount, "at least one sector must have an amount"
    for n in d["nodes"]:
        assert n["observed"] <= n["members"] and n["pct_samples"] <= n["observed"]
        assert (n["avg_pct"] is None) == (n["pct_samples"] == 0)
        if n["amount_estimate_cny"] is not None:
            assert Decimal(n["amount_estimate_cny"]) > 0


def test_theme_map_unknown_taxonomy_is_404_not_empty(client, release_id):
    r = client.get(f"{BASE}/theme-map", params={"release_id": release_id, "taxonomy_id": "nope", "taxonomy_version": 1})
    assert r.status_code == 404
    r = client.get(f"{BASE}/theme-map", params={"release_id": str(uuid.uuid4()), "taxonomy_id": "x", "taxonomy_version": 1})
    assert r.status_code == 404


def test_instrument_search_matches_code_prefix_and_name_and_is_contract_valid(client, built):
    conn = built[1]
    one = conn.execute("select a.source_symbol code, (select display_name from core.instrument_state s where s.instrument_id=i.instrument_id"
                       " order by effective_from desc, state_version desc limit 1) name from core.instrument i join core.instrument_alias a"
                       " on a.instrument_id=i.instrument_id and a.source_id='legacy_warehouse' where i.instrument_type='equity' order by a.source_symbol limit 1").fetchone()
    r = client.get(f"{BASE}/instrument-search", params={"q": one["code"][:4]})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    contracts.check("InstrumentSearch", d, contracts.backfill_id("board"))
    assert one["code"] in [x["code"] for x in d["items"]]
    exact = client.get(f"{BASE}/instrument-search", params={"q": one["code"]}).json()["data"]["items"]
    assert exact[0]["code"] == one["code"]                                           # exact code ranks first
    if one["name"]:
        by_name = client.get(f"{BASE}/instrument-search", params={"q": one["name"], "limit": 20}).json()["data"]["items"]
        assert one["code"] in [x["code"] for x in by_name]


def test_instrument_search_edge_inputs(client):
    assert client.get(f"{BASE}/instrument-search", params={"q": "   "}).json()["data"]["items"] == []
    assert client.get(f"{BASE}/instrument-search", params={"q": "%"}).json()["data"]["items"] == []      # wildcard characters are literal
    assert client.get(f"{BASE}/instrument-search", params={"q": "x" * 41}).status_code == 400
