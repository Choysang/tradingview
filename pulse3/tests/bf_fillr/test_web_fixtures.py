"""The fixtures the fill-research pages are tested with (apps/web/src/pages/bf/fillr/fixtures) pass the lane's contract, so a page test can never pass on a
shape the server would not send."""
from __future__ import annotations

import json
from pathlib import Path

from pulse import contracts

FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "fillr" / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_every_web_fixture_passes_the_fillr_contract():
    index = load("index")
    assert len(index) == 13
    for name, definition in index.items():
        contracts.check(definition, load(name), contracts.backfill_id("fillr"))
    on_disk = {p.stem for p in FIXTURES.glob("*.json")} - {"index"}
    assert on_disk == set(index)                                       # no fixture without a contract check


def test_the_fixtures_show_each_state_the_pages_must_handle():
    board = load("board")
    assert {i["state"] for i in board["items"]} == {"evaluated", "unavailable", "not_run", "failed"}
    assert any(i["evaluation"] and i["evaluation"]["split_day"] is None and i["evaluation"]["verdict"] is None for i in board["items"])       # no recorded split: no verdict
    assert load("custom-run-queued")["result"] is None and load("custom-run-done")["result"]["exploratory"] is True
    assert any(m["value"] is None and m["reason"] for m in load("monthly")["months"])
    assert {load(n)["state"] for n in ("factor-not-computed", "factor-queued", "factor-ready")} == {"not_computed", "queued", "ready"}
    assert {load(n)["state"] for n in ("analog-not-built", "analog-queued", "analog-ready")} == {"not_built", "queued", "ready"}
