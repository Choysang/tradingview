"""M2-E01: one deterministic economic kernel behind every entry point."""
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path

from pulse.domain.execution import (BacktestReplay, BrokerReportAdapter, DailyBarMatcher, ExecutionReport,
                                    LedgerReducer, OrderIntent, PaperSession, replay_events)

from .support import S, Scripted, at, daily_bar, make_kernel, tz_day

HERE = Path(__file__).parent
SPEC_FIXTURE = HERE.parents[1] / 'spec_package' / 'execution' / 'golden_fixtures.json'
CASES = json.loads((HERE / 'golden_fixtures.json').read_text(encoding='utf-8'))['cases']

BARS = [daily_bar('2026-11-10', '10.2', '10.8', '9.9', '10.6', up='11', down='9'),
        daily_bar('2026-11-11', '10.7', '11.0', '10.6', '10.9', up='11.66', down='9.54')]
PREV_CLOSE = {'2026-11-10': D('10'), '2026-11-11': D('10.6')}


def script(day, bar, k):
    pc = PREV_CLOSE[day]
    if day == '2026-11-10':
        k.submit(OrderIntent('b1', S, 'buy', 200, D('10.0')), expected_account_version=k.ledger.account_version,
                 at=bar.start, prev_close=pc)
        k.submit(OrderIntent('s1', S, 'sell', 300, D('10.5')), expected_account_version=k.ledger.account_version,
                 at=bar.start, prev_close=pc)
    else:
        pending = sorted(k.ledger.pending_settlements)
        k.apply_event({'event_id': 'settle-d2', 'sequence': k.ledger.sequence + 1, 'kind': 'cash.settlement',
                       'effective_at': at(day, '09:20').isoformat(), 'payload': {'fill_ids': pending}})
        k.submit(OrderIntent('s2', S, 'sell', 200, D('10.8')), expected_account_version=k.ledger.account_version,
                 at=bar.start, prev_close=pc)


def run_backtest():
    events = []
    k = make_kernel(sink=events.append)
    log = BacktestReplay(k, DailyBarMatcher(), strategy=Scripted(script)).run(BARS)
    return k, events, log


def run_paper():
    events = []
    k = make_kernel(sink=events.append)
    session = PaperSession(k, DailyBarMatcher(), strategy=Scripted(script))
    for bar in BARS:  # bars arrive one by one
        session.on_bar(bar)
    return k, events


def run_broker(reference_events, reference_log):
    events = []
    k = make_kernel(sink=events.append)
    adapter = BrokerReportAdapter(k)
    fee = {e['payload']['fill_id']: D(e['payload']['fee']) for e in reference_events if e['kind'] == 'fill.booked'}
    for bar in BARS:
        day = tz_day(bar)
        if day > k.ledger.day.isoformat():
            k.advance_session(k.calendar.next_trading_day(k.ledger.day), at(day, '09:00'))
        script(day, bar, k)
        for f in reference_log.fills:
            if f.at == bar.end:
                report = ExecutionReport(f.fill_id, f.order_id, f.quantity, f.price, f.at, fee[f.fill_id])
                adapter.ingest(report)
                assert adapter.ingest(report).applied is False  # at-least-once delivery
    return k, events


def test_m2_e01_three_entry_points_produce_identical_canonical_events_and_ledgers():
    a, ev_a, log = run_backtest()
    b, ev_b = run_paper()
    c, ev_c = run_broker(ev_a, log)
    assert len(log.fills) == 3 and {f.order_id for f in log.fills} == {'b1', 's1', 's2'}
    assert json.dumps(ev_a, sort_keys=True) == json.dumps(ev_b, sort_keys=True) == json.dumps(ev_c, sort_keys=True)
    assert a.ledger.journal_digest() == b.ledger.journal_digest() == c.ledger.journal_digest()
    marks = {S: '10.9'}
    assert a.ledger.snapshot(marks) == b.ledger.snapshot(marks) == c.ledger.snapshot(marks)


def test_m2_e01_golden_events_identical_across_reference_transports_and_kernel_apply():
    for case in CASES:
        digests = {replay_events(case['initial'], case['events'], t).journal_digest()
                   for t in ('historical', 'paper', 'broker_report_fixture')}
        k = make_kernel(cash=case['initial']['opening_cash'], qty=0)
        k.ledger = LedgerReducer(**case['initial'])
        for e in case['events']:
            k.apply_event(e)
            assert k.apply_event(e) is False  # redelivery through the kernel is a no-op
        digests.add(k.ledger.journal_digest())
        assert len(digests) == 1, case['case_id']


def test_m2_e01_golden_fixture_copy_is_byte_identical_to_the_package():
    assert hashlib.sha256((HERE / 'golden_fixtures.json').read_bytes()).hexdigest() == \
        hashlib.sha256(SPEC_FIXTURE.read_bytes()).hexdigest()
    assert len(CASES) == 10
