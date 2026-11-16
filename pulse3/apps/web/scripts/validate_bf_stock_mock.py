"""Validate the stock backfill mock responses (mock/out/bf_stock.json, from `pnpm mock:dump:bf`) against
backfill_stock.schema.json and the v3 ResponseMeta. Run from pulse3/:
    uv run python apps/web/scripts/validate_bf_stock_mock.py
Exit code 0 only if every dumped response is contract-valid and every indicator series has one value per bar."""
import json
import sys
from pathlib import Path

from pulse.contracts import ContractViolation, backfill_id, check

root = Path(__file__).resolve().parents[1]
rows = json.loads((root / "mock" / "out" / "bf_stock.json").read_text(encoding="utf-8"))
sid = backfill_id("stock")
bad = 0
for row in rows:
    body, definition = row["body"], row["definition"]
    try:
        if definition == "ErrorResponse":
            check("ErrorResponse", body)
            continue
        check("ResponseMeta", body["meta"])
        check(definition, body["data"], sid)
        if definition == "StockIndicators":
            n = len(body["data"]["bars"])
            for s in body["data"]["series"]:
                assert len(s["values"]) == n, f"{s['key']}: {len(s['values'])} values for {n} bars"
    except (ContractViolation, AssertionError) as e:
        bad += 1
        print(f"INVALID {definition}: {getattr(e, 'errors', e)}")
print(f"checked={len(rows)} invalid={bad}")
sys.exit(1 if bad or not rows else 0)
