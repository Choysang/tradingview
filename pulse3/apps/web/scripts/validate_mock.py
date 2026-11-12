"""Validate every mock response (mock/out/responses.json, from `pnpm mock:dump`)
against the one contract with pulse.contracts.check. Run from pulse3/:
    uv run python apps/web/scripts/validate_mock.py
Exit code 0 only if every dumped response is contract-valid and all 31 operations
were covered.
"""
import json
import sys
from pathlib import Path

from pulse.contracts import ContractViolation, check, openapi

root = Path(__file__).resolve().parents[1]
rows = json.loads((root / "mock" / "out" / "responses.json").read_text(encoding="utf-8"))
expected_ops = {
    op["operationId"]
    for item in openapi()["paths"].values()
    for method, op in item.items()
    if method in ("get", "post", "put", "patch", "delete")
}
seen = set()
bad = 0
for row in rows:
    seen.add(row["operation"])
    try:
        check(row["definition"], row["body"])
    except ContractViolation as e:  # noqa: PERF203
        bad += 1
        print(f"INVALID {row['operation']} ({row['definition']}): {e.errors[:4]}")
missing = sorted(expected_ops - seen)
print(f"checked={len(rows)} invalid={bad} operations_covered={len(seen & expected_ops)}/{len(expected_ops)} missing={missing}")
sys.exit(1 if bad or missing else 0)
