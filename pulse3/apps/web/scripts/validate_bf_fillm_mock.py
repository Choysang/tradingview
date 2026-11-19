"""Validate the fill-market backfill mock responses (mock/out/bf_fillm.json, from `pnpm mock:dump:fillm`) against
backfill_fillm.schema.json and the v3 ResponseMeta. Run from pulse3/:
    uv run python apps/web/scripts/validate_bf_fillm_mock.py
Exit code 0 only if every dumped response is contract-valid."""
import json
import sys
from pathlib import Path

from pulse.contracts import ContractViolation, backfill_id, check

root = Path(__file__).resolve().parents[1]
rows = json.loads((root / "mock" / "out" / "bf_fillm.json").read_text(encoding="utf-8"))
sid = backfill_id("fillm")
bad = 0
for row in rows:
    body, definition = row["body"], row["definition"]
    try:
        if definition == "ErrorResponse":
            check("ErrorResponse", body)
            continue
        check("ResponseMeta", body["meta"])
        check(definition, body["data"], sid)
    except ContractViolation as e:
        bad += 1
        print(f"INVALID {definition}: {getattr(e, 'errors', e)}")
print(f"checked={len(rows)} invalid={bad}")
sys.exit(1 if bad or not rows else 0)
