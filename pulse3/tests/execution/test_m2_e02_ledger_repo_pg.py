"""M2-E02 (real PostgreSQL 18): concurrent reservation, idempotency, replay, balance, correction references."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path

import psycopg
import pytest

from pulse.adapters.postgres import db
from pulse.adapters.postgres.ledger_repo import (AccountVersionConflict, EventConflict, EventContext, EventRejected,
                                                 LedgerRepo, instrument_uuid, stable_uuid)
from pulse.application.execution_service import ExecutionService
from pulse.domain.execution import ExecutionReport, OrderIntent, replay_events

from .support import CAL, COSTS, S, SPEC, at

pytestmark = pytest.mark.pg
CTX = EventContext('test-suite', 'rules-test', 'cost-test', 'kernel-test', 'none')
CASES = json.loads((Path(__file__).with_name('golden_fixtures.json')).read_text(encoding='utf-8'))['cases']
INITIAL = {'trading_day': '2026-11-10', 'opening_cash': '1000', 'positions': {}, 'sale_proceeds_reusable': True}
INITIAL_INV = {'trading_day': '2026-11-10', 'opening_cash': '0',
               'positions': {S: {'quantity': 100, 'cost': '1000'}}, 'sale_proceeds_reusable': True}


def register_instrument(conn, text=S):
    """Execution tables reference core.instrument (migration 0004); readable ids map to uuid5."""
    conn.execute("insert into core.instrument(instrument_id,instrument_type,venue_code,currency,exchange_timezone,created_at) "
                 "values (%s,'equity','XSHG','CNY','Asia/Shanghai',now()) on conflict do nothing", (instrument_uuid(text),))


@pytest.fixture
def repo(pg_dsn):
    conn = db.connect(pg_dsn, autocommit=True)
    register_instrument(conn)
    yield LedgerRepo(conn)
    conn.close()


@pytest.fixture
def service(pg_dsn, repo):  # repo fixture registers the instrument first
    return ExecutionService(pg_dsn, {S: SPEC}, CAL, COSTS)


def sql_checks(conn, acct):
    unbalanced = conn.execute(
        "select count(*) n from (select entry_id from execution.journal_line where account_id=%s "
        "group by entry_id having sum(debit)<>sum(credit)) x", (acct,)).fetchone()['n']
    total = conn.execute("select coalesce(sum(debit_balance),0) s from execution.trial_balance where account_id=%s",
                         (acct,)).fetchone()['s']
    return unbalanced, total


def buy(oid, qty=100, price='5'):
    return OrderIntent(oid, S, 'buy', qty, D(price))


# -- replay / projections / balance -------------------------------------------------------------
@pytest.mark.parametrize('case', CASES, ids=[c['case_id'] for c in CASES])
def test_golden_cases_persist_replay_identically_and_journal_balances_in_sql(repo, case):
    acct = uuid.uuid4()
    repo.create_account(acct, case['initial'], CTX)
    for e in case['events']:
        assert repo.append(acct, e, CTX).applied
    memory = replay_events(case['initial'], case['events'])
    rebuilt = repo.load(acct)
    assert rebuilt.snapshot(case['marks']) == memory.snapshot(case['marks'])
    assert {k: v for k, v in rebuilt.balances.items() if v} == {k: v for k, v in memory.balances.items() if v}
    unbalanced, total = sql_checks(repo.conn, acct)
    assert unbalanced == 0 and total == 0
    tb = repo.trial_balance(acct)
    assert {k: v for k, v in tb.items() if v} == {k: v for k, v in memory.balances.items() if v}
    head = repo.conn.execute("select current_sequence from execution.account_head where account_id=%s", (acct,)).fetchone()
    assert head['current_sequence'] == len(case['events']) == rebuilt.sequence
    for sym, h in memory.holdings.items():
        row = repo.conn.execute("select quantity,sellable_quantity,reserved_sell_quantity,cost_basis from "
                                "execution.position_projection where account_id=%s and instrument_id=%s",
                                (acct, instrument_uuid(sym))).fetchone()
        assert (row['quantity'], row['sellable_quantity'], row['reserved_sell_quantity'], row['cost_basis']) == (
            h.quantity, memory.sellable(sym), memory.reserved_sell(sym), h.cost)
    before = repo.projection_snapshot(acct)
    repo.rebuild_projections(acct)
    assert repo.projection_snapshot(acct) == before


def test_lots_record_t_plus_1_eligibility(repo):
    acct = uuid.uuid4()
    case = next(c for c in CASES if c['case_id'] == 'B01')
    repo.create_account(acct, case['initial'], CTX)
    for e in case['events'][:2]:
        repo.append(acct, e, CTX)
    lot = repo.conn.execute("select acquired_on,eligible_on,remaining_quantity from execution.quantity_lot where account_id=%s",
                            (acct,)).fetchone()
    assert (str(lot['acquired_on']), str(lot['eligible_on']), lot['remaining_quantity']) == ('2026-11-10', '2026-11-11', 100)
    pos = repo.projection_snapshot(acct)['position'][0]
    assert pos[1:3] == (100, 0)  # quantity 100, sellable 0 today
    for e in case['events'][2:6]:
        repo.append(acct, e, CTX)
    after = repo.conn.execute("select remaining_quantity from execution.quantity_lot where account_id=%s", (acct,)).fetchone()
    assert after['remaining_quantity'] == 0  # sold lot kept as a zero row, not deleted


# -- idempotency / conflicts / immutability -----------------------------------------------------------
def test_duplicate_fill_insert_is_idempotent_and_changed_redelivery_is_a_conflict(repo):
    acct = uuid.uuid4()
    repo.create_account(acct, INITIAL, CTX)
    reserve = {'event_id': 'r1', 'sequence': 1, 'kind': 'order.reserve', 'effective_at': '2026-11-10T10:00:00+08:00',
               'payload': {'order_id': 'o1', 'instrument_id': S, 'side': 'buy', 'quantity': 60, 'limit_price': '10',
                           'fee_budget': '5'}}
    fill = {'event_id': 'fill:f1', 'sequence': 2, 'kind': 'fill.booked', 'effective_at': '2026-11-10T10:00:01+08:00',
            'payload': {'fill_id': 'f1', 'order_id': 'o1', 'quantity': 60, 'price': '10', 'fee': '5',
                        'eligible_on': '2026-11-11'}}
    repo.append(acct, reserve, CTX)
    assert repo.append(acct, fill, CTX).applied is True
    counts = lambda: tuple(repo.conn.execute(  # noqa: E731
        "select (select count(*) from execution.account_event where account_id=%(a)s) e,"
        "(select count(*) from execution.journal_entry where account_id=%(a)s) j,"
        "(select count(*) from execution.journal_line where account_id=%(a)s) l", {'a': acct}).fetchone().values())
    before = counts()
    assert repo.append(acct, fill, CTX).applied is False and counts() == before
    changed = json.loads(json.dumps(fill))
    changed['payload']['quantity'] = 50
    with pytest.raises(EventConflict):
        repo.append(acct, changed, CTX)
    assert counts() == before
    # the same business fill under a new envelope is rejected by the reducer, not double booked
    again = dict(fill, event_id='other-envelope', sequence=3)
    with pytest.raises(EventRejected):
        repo.append(acct, again, CTX)
    assert counts() == before
    with pytest.raises(psycopg.errors.UniqueViolation):  # database guard under the same source identity
        repo.conn.execute(
            "insert into execution.account_event(account_id,account_sequence,event_id,event_type,source_event_id,source_id,"
            "payload_digest,payload,effective_at,market_rules_version,cost_policy_version,execution_policy_version,"
            "input_manifest_id,ledger_policy_version,numeric_policy_version) select account_id,99,gen_random_uuid(),"
            "event_type,source_event_id,source_id,payload_digest,payload,effective_at,market_rules_version,"
            "cost_policy_version,execution_policy_version,input_manifest_id,ledger_policy_version,numeric_policy_version "
            "from execution.account_event where account_id=%s and account_sequence=2", (acct,))


def test_history_is_immutable(repo):
    acct = uuid.uuid4()
    case = next(c for c in CASES if c['case_id'] == 'B01')
    repo.create_account(acct, case['initial'], CTX)
    repo.append(acct, case['events'][0], CTX)
    repo.append(acct, case['events'][1], CTX)
    for sql in ("update execution.journal_line set debit=debit+1 where account_id=%s",
                "delete from execution.journal_entry where account_id=%s",
                "update execution.account_event set payload='{}'::jsonb where account_id=%s"):
        with pytest.raises(psycopg.errors.RaiseException):
            repo.conn.execute(sql, (acct,))


def test_stale_expected_version_is_a_conflict_and_version_is_monotonic(repo):
    acct = uuid.uuid4()
    repo.create_account(acct, INITIAL, CTX)
    e1 = {'event_id': 'c1', 'sequence': 1, 'kind': 'capital.deposit', 'effective_at': '2026-11-10T10:00:00+08:00',
          'payload': {'amount': '100'}}
    e2 = dict(e1, event_id='c2', sequence=2, effective_at='2026-11-10T10:00:01+08:00')
    assert repo.append(acct, e1, CTX, expected_version=0).sequence == 1
    with pytest.raises(AccountVersionConflict):
        repo.append(acct, e2, CTX, expected_version=0)
    assert repo.append(acct, e2, CTX, expected_version=1).sequence == 2
    stale_seq = dict(e2, event_id='c3')  # sequence 2 again
    with pytest.raises(EventRejected):
        repo.append(acct, stale_seq, CTX)
    assert repo.conn.execute("select current_sequence from execution.account_head where account_id=%s",
                             (acct,)).fetchone()['current_sequence'] == 2


# -- concurrency -----------------------------------------------------------------------------------------
def race(fn_a, fn_b):
    barrier = threading.Barrier(2)

    def run(fn):
        barrier.wait()
        return fn()
    with ThreadPoolExecutor(2) as pool:
        return [f.result(timeout=60) for f in (pool.submit(run, fn_a), pool.submit(run, fn_b))]


def test_two_strategies_cannot_spend_the_same_cash(service, repo):
    acct = uuid.uuid4()
    service.open_account(acct, INITIAL)  # cash 1000; each 100 x 5 buy reserves 505
    when = at('2026-11-10', '09:20')
    results = race(lambda: service.submit_order(acct, buy('strategy-a'), when, prev_close=D('5')),
                   lambda: service.submit_order(acct, buy('strategy-b'), when, prev_close=D('5')))
    assert sorted(r.accepted for r in results) == [False, True]
    loser = next(r for r in results if not r.accepted)
    assert loser.reason == 'insufficient unreserved cash'
    rows = repo.conn.execute("select status,reserved_cash from execution.order_reservation where account_id=%s",
                             (acct,)).fetchall()
    assert [(r['status'], r['reserved_cash']) for r in rows] == [('active', D('505'))]
    assert D(service.snapshot(acct, {})['cash_available']) == D('495')


def test_two_strategies_cannot_sell_the_same_inventory(service, repo):
    acct = uuid.uuid4()
    service.open_account(acct, INITIAL_INV)  # 100 sellable shares
    when = at('2026-11-10', '09:20')
    sell = lambda oid: service.submit_order(  # noqa: E731
        acct, OrderIntent(oid, S, 'sell', 100, D('10')), when, prev_close=D('10'))
    results = race(lambda: sell('a'), lambda: sell('b'))
    assert sorted(r.accepted for r in results) == [False, True]
    assert next(r for r in results if not r.accepted).reason == 'insufficient unreserved sellable inventory'
    pos = repo.projection_snapshot(acct)['position'][0]
    assert pos[1:4] == (100, 100, 100)  # quantity, sellable, reserved_sell: never above sellable


def test_stale_expected_version_race_lets_exactly_one_writer_win(service, repo):
    acct = uuid.uuid4()
    service.open_account(acct, INITIAL)
    when = at('2026-11-10', '09:20')
    small = lambda oid: service.submit_order(  # noqa: E731
        acct, buy(oid), when, prev_close=D('5'), expected_version=0)
    results = race(lambda: small('a'), lambda: small('b'))
    assert sorted(r.accepted for r in results) == [False, True]
    assert next(r for r in results if not r.accepted).reason == 'account_version_conflict'
    assert repo.conn.execute("select current_sequence from execution.account_head where account_id=%s",
                             (acct,)).fetchone()['current_sequence'] == 1


def test_many_parallel_buys_never_over_reserve(service, repo):
    acct = uuid.uuid4()
    service.open_account(acct, dict(INITIAL, opening_cash='3000'))  # room for five 505 reservations
    when = at('2026-11-10', '09:20')
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        return service.submit_order(acct, buy(f'o{i}'), when, prev_close=D('5'))
    with ThreadPoolExecutor(8) as pool:
        results = [f.result(timeout=120) for f in [pool.submit(go, i) for i in range(8)]]
    assert sum(r.accepted for r in results) == 5
    reserved = repo.conn.execute("select sum(reserved_cash) s from execution.order_reservation where account_id=%s "
                                 "and status='active'", (acct,)).fetchone()['s']
    assert reserved == D('2525') <= D('3000')
    assert repo.load(acct).cash_available() == D('475')


# -- partial fills, cancel, corrections -------------------------------------------------------------------
def test_partial_fill_cancel_confirm_releases_only_the_remainder_and_correction_references_the_fill(service, repo):
    acct = uuid.uuid4()
    service.open_account(acct, dict(INITIAL_INV, opening_cash='0', positions={S: {'quantity': 1000, 'cost': '10000'}}))
    t = at('2026-11-10', '10:00')
    assert service.submit_order(acct, OrderIntent('s', S, 'sell', 300, D('10')), t, prev_close=D('10')).accepted
    res = service.ingest_report(acct, ExecutionReport('f1', 's', 100, D('10.5'), at('2026-11-10', '10:01')))
    assert res.applied
    assert service.ingest_report(acct, ExecutionReport('f1', 's', 100, D('10.5'), at('2026-11-10', '10:01'))).applied is False
    pos = lambda: repo.projection_snapshot(acct)['position'][0]  # noqa: E731
    assert pos()[1:4] == (900, 900, 200)  # 200 still reserved for the open remainder
    with pytest.raises(ValueError):  # cancel confirmation must carry the cumulative fills
        service.confirm_cancel(acct, 's', 0, at('2026-11-10', '10:02'))
    assert pos()[3] == 200
    assert service.confirm_cancel(acct, 's', 100, at('2026-11-10', '10:03'))
    assert pos()[1:4] == (900, 900, 0)
    row = repo.conn.execute("select status,remaining_quantity,reserved_cash,original_quantity from "
                            "execution.order_reservation where account_id=%s", (acct,)).fetchone()
    assert (row['status'], row['remaining_quantity'], row['reserved_cash'], row['original_quantity']) == ('cancelled', 0, 0, 300)
    # a fee correction appends a new event + journal entry that points at the original fill event
    fill_uuid = stable_uuid(acct, 'event', 'fill:f1')
    before = repo.conn.execute("select count(*) n from execution.journal_entry where account_id=%s", (acct,)).fetchone()['n']
    settle = {'event_id': 'settle-1', 'sequence': repo.load(acct).sequence + 1, 'kind': 'cash.settlement',
              'effective_at': '2026-11-10T10:03:30+08:00', 'payload': {'fill_ids': ['f1']}}
    assert service.apply_event(acct, settle)
    adj = {'event_id': 'fee-adj-1', 'sequence': repo.load(acct).sequence + 1, 'kind': 'fee.cash_adjustment',
           'effective_at': '2026-11-10T10:04:00+08:00', 'payload': {'related_fill_id': 'f1', 'expense_delta': '1'}}
    assert service.apply_event(acct, adj)
    ref = repo.conn.execute("select correction_of_event_id from execution.account_event where account_id=%s and "
                            "source_event_id='fee-adj-1'", (acct,)).fetchone()['correction_of_event_id']
    assert ref == fill_uuid
    assert repo.conn.execute("select count(*) n from execution.journal_entry where account_id=%s", (acct,)).fetchone()['n'] == before + 2  # settlement + fee adjustment, both appended
    assert sql_checks(repo.conn, acct) == (0, 0)


def test_failed_persistence_leaves_no_partial_account_state(repo):
    acct = uuid.uuid4()
    repo.create_account(acct, INITIAL, CTX)
    bad = {'event_id': 'x1', 'sequence': 1, 'kind': 'capital.withdraw', 'effective_at': '2026-11-10T10:00:00+08:00',
           'payload': {'amount': '5000'}}  # more than the cash
    with pytest.raises(EventRejected):
        repo.append(acct, bad, CTX)
    n = repo.conn.execute("select count(*) n from execution.account_event where account_id=%s", (acct,)).fetchone()['n']
    assert n == 1 and repo.load(acct).sequence == 0
