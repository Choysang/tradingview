"""Run synthetic accounting conformance, not a latency or trading performance test."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import platform

from unified_ledger import POLICY_VERSION, NUMERIC_POLICY_VERSION, replay_fixture, true_time_weighted_return

ROOT = Path(__file__).resolve().parent


def compare(actual, expected, path='snapshot'):
    for key, value in expected.items():
        at = path + '.' + key
        if isinstance(value, dict):
            compare(actual[key], value, at)
        elif isinstance(value, str):
            if Decimal(actual[key]) != Decimal(value):
                raise AssertionError((at, actual[key], value))
        elif actual[key] != value:
            raise AssertionError((at, actual[key], value))


def main():
    fixture_bytes = (ROOT / 'golden_fixtures.json').read_bytes()
    fixtures = json.loads(fixture_bytes)
    rows = []
    for case in fixtures['cases']:
        transports = {}
        for mode in ('historical', 'paper', 'broker_report_fixture'):
            book = replay_fixture(case['initial'], case['events'], mode)
            snap = book.snapshot(case['marks'])
            compare(snap, case['expected'])
            if 'expected_increment' in case:
                assert Decimal(snap['nav']) - Decimal(case['passive_nav']) == Decimal(case['expected_increment'])
            if 'expected_twr' in case:
                assert true_time_weighted_return(case['twr_segments']) == Decimal(case['expected_twr'])
            transports[mode] = {'passed': True, 'journal_sha256': book.journal_digest(),
                                'account_sequence': book.sequence}
        assert len({v['journal_sha256'] for v in transports.values()}) == 1
        rows.append({'case_id': case['case_id'], 'title': case['title'], 'passed': True,
                     'manual_derivation': case['manual_derivation'], 'expected': case['expected'],
                     'actual': snap, 'transports': transports})
    report = {
        'schema_version': '1.0', 'run_at': datetime.now(timezone.utc).isoformat(),
        'python_version': platform.python_version(), 'kind': 'synthetic_accounting_conformance',
        'policy_version': POLICY_VERSION, 'numeric_policy_version': NUMERIC_POLICY_VERSION,
        'fixture_sha256': hashlib.sha256(fixture_bytes).hexdigest(),
        'code_sha256': hashlib.sha256((ROOT / 'unified_ledger.py').read_bytes()).hexdigest(),
        'passed_cases': len(rows), 'transport_passes': len(rows) * 3,
        'independent_engines': 1,
        'scope': '10 manually specified cases, 3 transports feeding ONE reducer; not real broker, matching, SQL, latency or strategy alpha validation',
        'rows': rows,
    }
    target = ROOT / 'benchmark_results.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'PASS {len(rows)} hand-calculated cases across 3 transport harnesses; one reducer')
    print(f'Wrote {target.name}; fixture SHA256={report["fixture_sha256"]}')


if __name__ == '__main__':
    main()
