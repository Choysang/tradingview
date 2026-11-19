"""Lab CLI (import-ledger / run-batch / show) on a private database, and the wire encoding of a tear sheet."""
from __future__ import annotations

import json
import shutil
from decimal import Decimal as D

import pytest

from pulse import contracts
from pulse.application import bf_lab_ledger as LED
from pulse.application import bf_lab_service as LAB
from pulse.domain.lab import tearsheet as T
from pulse.workers import lab as CLI


def out_of(capsys) -> object:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.pg
def test_cli_imports_the_ledger_idempotently_and_refuses_a_changed_file(fresh, capsys, monkeypatch, tmp_path):
    _, dsn = fresh
    assert CLI.run(["--dsn", dsn, "import-ledger"]) == 0
    first = out_of(capsys)
    assert first[0]["entry_count"] == 92 and first[0]["created"] is True
    assert CLI.run(["--dsn", dsn, "import-ledger"]) == 0 and out_of(capsys)[0]["created"] is False
    assert CLI.run(["--dsn", dsn, "run-batch"]) == 0 and out_of(capsys) == []                       # nothing queued
    assert CLI.run(["--dsn", dsn, "show"]) == 0
    shown = out_of(capsys)
    assert len(shown["imports"]) == 1 and shown["not_imported"] == [] and shown["batch_runs"] == []

    bad = tmp_path / "lab_data"
    shutil.copytree(LED.DATA_DIR, bad, ignore=shutil.ignore_patterns("__pycache__"))
    (bad / "experiments.jsonl").write_bytes((bad / "experiments.jsonl").read_bytes().replace(b"E001", b"E00X", 1))
    monkeypatch.setattr(LED, "DATA_DIR", bad)
    assert CLI.run(["--dsn", dsn, "import-ledger"]) == 2
    err = out_of(capsys)
    assert err["error"] == "LEDGER_HASH_MISMATCH" and "sha256" in err["message"]


def test_wire_encoding_quantises_to_12_digits_and_never_turns_missing_into_zero():
    nav = [("2026-03-02", D("100.5")), ("2026-03-03", D("101")), ("2026-03-04", None), ("2026-03-05", D("99"))]
    ts = T.compute(nav, [T.TradeIn("2026-03-02", "2026-03-03", D("3.3333333333333333"), D("0.0333333333333333"))], initial=D(100))
    wire = contracts.to_wire(LAB.tearsheet_wire(ts))
    contracts.check("TearSheet", wire, contracts.backfill_id("lab"))
    assert wire["metrics"]["win_rate"] == {"value": "1", "reason": None} and wire["metrics"]["profit_factor"] == {"value": None, "reason": "no_losses"}
    assert wire["metrics"]["avg_win"]["value"] == "3.333333333333"
    assert wire["missing_nav_sessions"] == 1 and wire["trades"] == {"closed": 1, "open": 0, "breakeven": 0}


def test_a_value_beyond_the_contract_range_is_missing_with_a_reason_not_an_invalid_payload():
    huge = T.compute([("2026-03-02", D(100)), ("2026-03-03", D(100000000000))], initial=D(100))                    # 1e9x in one session, annualised: astronomically large
    w = LAB.metric_wire(huge.metrics["annualised_return"])
    assert w == {"value": None, "reason": "out_of_range"}
    assert LAB.metric_wire(huge.metrics["total_return"])["value"] is not None
    contracts.check("TearSheet", contracts.to_wire(LAB.tearsheet_wire(huge)), contracts.backfill_id("lab"))
