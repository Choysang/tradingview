"""Run the bounded reference and policy-schema fixtures; no network/database IO."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent


def main() -> int:
    schema = json.loads((ROOT / "input_alignment_policy.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    policy = dict(alignment_policy_version="quote-state-v1", selection_mode="state_hold",
                  max_age_seconds=3, strategy="backward", latest_invalid_policy="surface_latest")
    fixtures = [
        ("state", policy, True),
        ("exact", dict(policy, selection_mode="exact_event", max_age_seconds=0), True),
        ("forward_forbidden", dict(policy, strategy="forward"), False),
        ("fallback_forbidden", dict(policy, latest_invalid_policy="last_good"), False),
        ("negative_age", dict(policy, max_age_seconds=-1), False),
        ("unknown_property", dict(policy, use_future=True), False),
        ("missing_policy_identity", {k: v for k, v in policy.items() if k != "alignment_policy_version"}, False),
        ("empty_policy_identity", dict(policy, alignment_policy_version="  "), False),
    ]
    schema_results = [
        dict(case=name, expected_valid=expected, actual_valid=validator.is_valid(instance),
             passed=validator.is_valid(instance) == expected)
        for name, instance, expected in fixtures
    ]
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_alignment.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (ROOT / "validation.log").write_text(stream.getvalue())
    report = {
        "scope": "bounded observed-mode pure-Python reference; synthetic data only",
        "unit_test_cases": result.testsRun,
        "unit_test_failures": len(result.failures),
        "unit_test_errors": len(result.errors),
        "unit_test_skips": len(result.skipped),
        "policy_schema_fixture_checks": schema_results,
        "passed": result.wasSuccessful() and all(x["passed"] for x in schema_results),
        "production_claims": [],
        "file_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("alignment.py", "test_alignment.py", "input_alignment_policy.schema.json")},
    }
    (ROOT / "validation_result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
