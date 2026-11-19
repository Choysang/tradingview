"""The fixtures the lab pages are tested with (apps/web/src/pages/bf/lab/fixtures) pass the lane's contract, so a page test can never pass on a shape the server would not send."""
from __future__ import annotations

import json
from pathlib import Path

from pulse import contracts

FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "pages" / "bf" / "lab" / "fixtures"


def test_every_web_fixture_passes_the_lab_contract():
    index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 8
    for name, definition in index.items():
        payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        contracts.check(definition, payload, contracts.backfill_id("lab"))
    on_disk = {p.stem for p in FIXTURES.glob("*.json")} - {"index"}
    assert on_disk == set(index)                                       # no fixture without a contract check


def test_the_fixtures_show_each_state_the_pages_must_handle():
    ld = lambda n: json.loads((FIXTURES / f"{n}.json").read_text(encoding="utf-8"))  # noqa: E731
    assert ld("ledger-empty")["ledger"] is None and ld("ledger-list")["ledger"]["is_latest"] is True
    rows = ld("batch-run")["result"]["rows"]
    assert {r["status"] for r in rows} == {"ok", "no_trades", "skipped"} and rows[2]["tearsheet"] is None
    assert ld("tearsheet-book")["tearsheet"]["trades"] is None
    assert any(m["reason"] for m in ld("tearsheet-book")["tearsheet"]["metrics"].values())
    assert ld("batch-list")["items"][1]["error_code"] == "NO_MATCHING_ROWS"
