"""The fixtures the fusion page is tested with pass the lane's contract, so a page test never passes on a shape the server would not send."""
from __future__ import annotations

import json
from pathlib import Path

from pulse import contracts

FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "fusion" / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_every_web_fixture_passes_the_fusion_contract():
    index = load("index")
    assert len(index) == 7
    for name, definition in index.items():
        contracts.check(definition, load(name), contracts.backfill_id("fusion"))
    assert {p.stem for p in FIXTURES.glob("*.json")} - {"index"} == set(index)


def test_the_fixtures_show_the_states_the_page_must_handle():
    assert load("status-empty")["last_run"] is None and load("scores-empty")["rows"] == []
    assert all(b["rows"] == [] for b in load("books-empty")["books"])
    exps = {e["key"]: e for e in load("experiments")["experiments"]}
    assert exps["monitor"]["rows"][1]["flagged"] is True and exps["x20"]["rows"][1]["turnover_x20"] is None
    assert exps["os20"]["rows"][0]["groups"][2]["ex"] is None
    assert load("scores")["rows"][2]["display_name"] is None


def test_the_contract_refuses_a_made_up_number_for_a_missing_value():
    bad = load("books")
    bad["books"][0]["rows"][0]["nav"] = 168.1                     # a float, not a decimal string
    try:
        contracts.check("FusionBooks", bad, contracts.backfill_id("fusion"))
    except contracts.ContractViolation:
        return
    raise AssertionError("a float NAV must not pass")
