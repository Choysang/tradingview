"""Committed decision fixtures are contract-valid and reproducible (M3 evidence)."""
import json
from pathlib import Path

import pytest

from pulse import contracts

from .build_fixtures import OUT, build

FILES = sorted(OUT.glob("*.json"))


def test_fixtures_exist():
    assert len(FILES) >= 6, "run: uv run python -m tests.decision.build_fixtures"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_fixture_matches_its_contract_definition(path: Path):
    definition = path.name.split("__")[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    contracts.check(definition, payload)


def test_fixtures_regenerate_byte_identically():
    fresh = build()
    for name, payload in fresh.items():
        stored = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert stored == json.loads(json.dumps(contracts.to_wire(payload))), name
