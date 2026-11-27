"""Validate the Nexus backfill mock responses (mock/out/bf_nexus.json, from `npx tsx mock/bf_nexus_dump.ts`)
against backfill_nexus.schema.json (v2.0.0) and the v3 ResponseMeta. Run from pulse3/:
    .\\.venv\\Scripts\\python.exe apps/web/scripts/validate_bf_nexus_mock.py
Exit code 0 only if every dumped response is contract-valid."""
import json
import sys
from pathlib import Path

from pulse.contracts import ContractViolation, backfill_id, check

root = Path(__file__).resolve().parents[1]
rows = json.loads((root / "mock" / "out" / "bf_nexus.json").read_text(encoding="utf-8"))
sid = backfill_id("nexus")
bad = 0
for idx, row in enumerate(rows):
    body, definition = row["body"], row["definition"]
    try:
        if definition == "ErrorResponse":
            check("ErrorResponse", body)
            continue
        check("ResponseMeta", body["meta"])
        check(definition, body["data"], sid)
    except ContractViolation as e:
        bad += 1
        print(f"INVALID [{idx}] {definition}: {getattr(e, 'errors', e)}")
print(f"checked={len(rows)} invalid={bad}")
sys.exit(1 if bad or not rows else 0)
