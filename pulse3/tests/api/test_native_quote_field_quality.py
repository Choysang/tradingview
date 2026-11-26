"""Native quote overlays must satisfy the CanonicalQuote contract (regression: 2026-11-24 every workspace returned 500).

The native intraday recorder stored field_quality entries without ``reason`` and ``raw_object_id``; the workspace response then
failed the contract check. New rows carry both keys; stored rows are completed on read.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import jsonschema

from pulse.api import routes_market as RM

SCHEMA = json.loads((Path(RM.__file__).parents[1] / "contracts" / "data" / "v3.schema.json").read_text(encoding="utf-8"))
OLD_ENTRY = {"status": "observed", "source_id": "pulse_intraday_recorder", "conversion": "native-recording:lots x 100 = shares",
             "endpoint_id": "intraday_quotes", "value_origin": "primary_source", "mapping_version": "1.0.0"}


def _quote(fq: dict) -> dict:
    return {"instrument_id": str(uuid.uuid4()), "source_id": "pulse_intraday_recorder", "endpoint_id": "intraday_quotes",
            "source_schema_version": "native-intraday-20261119", "mapping_version": "1.0.0", "observation_id": str(uuid.uuid4()),
            "live_quote_version": 1, "trade_date": "2026-11-24", "source_event_time": "2026-11-24T06:57:59Z",
            "first_seen_at": "2026-11-24T06:56:04.880000Z", "available_at": "2026-11-24T06:58:03.140000Z", "currency": "CNY",
            "adjustment_mode": "raw", "last_price_raw": "19.83", "bid_price_raw": "19.84", "ask_price_raw": "19.84",
            "bid_volume_shares": 94800, "ask_volume_shares": 94800, "quality_status": "valid", "field_quality": fq}


def _errors(q: dict) -> list[str]:
    v = jsonschema.Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/CanonicalQuote"})
    return [e.message for e in v.iter_errors(q)]


def test_stored_native_entry_fails_the_contract_without_completion():
    assert _errors(_quote({"*": dict(OLD_ENTRY)}))


def test_completed_entry_passes_and_keeps_the_raw_object():
    raw = uuid.uuid4()
    fq = RM._complete_fq({"*": dict(OLD_ENTRY)}, raw)
    assert fq["*"]["raw_object_id"] == str(raw) and fq["*"]["reason"] is None
    assert _errors(_quote(fq)) == []


def test_completion_never_overwrites_present_values():
    entry = {**OLD_ENTRY, "reason": "kept", "raw_object_id": None}
    assert RM._complete_fq({"*": entry}, uuid.uuid4())["*"] == entry
    assert RM._complete_fq(None) is None
