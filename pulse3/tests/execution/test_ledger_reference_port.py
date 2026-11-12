"""Port of spec_package/execution/test_unified_ledger.py (43 reference tests) onto pulse.domain.execution.

Only the import line and the explicit utf-8 read differ; expectations are unchanged
and run against the byte-identical golden_fixtures.json next to this file.
"""
from copy import deepcopy
from decimal import Decimal as D, localcontext
import json
from pathlib import Path
import unittest

from pulse.domain.execution import Ledger, Posting, replay_fixture, true_time_weighted_return

CASES=json.loads((Path(__file__).with_name('golden_fixtures.json')).read_text(encoding='utf-8'))['cases']
BY_ID={c['case_id']:c for c in CASES}
S='SAMPLE_MAIN_001'


def event(book,kind,payload,*,event_id=None,day=None):
    n=book.sequence+1
    return {'event_id':event_id or f'x{n}','sequence':n,'kind':kind,
            'effective_at':f'{day or book.day.isoformat()}T11:00:{n:02d}+08:00','payload':payload}


def reserve(book,oid,side,qty,price='10',fee='5'):
    return event(book,'order.reserve',{'order_id':oid,'instrument_id':S,'side':side,'quantity':qty,'limit_price':price,'fee_budget':fee})


def fill(book,fid,oid,qty,price='10',fee='5',buy=False):
    return event(book,'fill.booked',{'fill_id':fid,'order_id':oid,'quantity':qty,'price':price,'fee':fee,'eligible_on':'2026-11-11' if buy else None})


def assert_expected(test,actual,expected):
    for k,v in expected.items():
        if isinstance(v,dict):assert_expected(test,actual[k],v)
        elif isinstance(v,str):test.assertEqual(D(actual[k]),D(v),k)
        else:test.assertEqual(actual[k],v,k)


class GoldenTests(unittest.TestCase):
    pass


def make_golden(case):
    def check(self):
        for adapter in ['historical','paper','broker_report_fixture']:
            with self.subTest(adapter=adapter):
                book=replay_fixture(case['initial'],case['events'],adapter)
                snap=book.snapshot(case['marks'])
                assert_expected(self,snap,case['expected'])
                if 'expected_increment' in case:
                    self.assertEqual(D(snap['nav'])-D(case['passive_nav']),D(case['expected_increment']))
                if 'expected_twr' in case:
                    self.assertEqual(true_time_weighted_return(case['twr_segments']),D(case['expected_twr']))
    return check

for case in CASES:
    setattr(GoldenTests,'test_'+case['case_id'].lower(),make_golden(case))


class InvariantTests(unittest.TestCase):
    def opening(self,cash='10000',qty=1000,cost='10000',reusable=True):
        return Ledger(trading_day='2026-11-10',opening_cash=cash,
                      positions={S:{'quantity':qty,'cost':cost}} if qty else {},
                      sale_proceeds_reusable=reusable)

    def assert_rejected_without_mutation(self,book,e,error=Exception):
        before=deepcopy(book.__dict__)
        with self.assertRaises(error):book.apply(e)
        self.assertEqual(book.__dict__,before)

    def test_three_transport_harnesses_have_identical_journals(self):
        for c in CASES:
            books=[replay_fixture(c['initial'],c['events'],a) for a in ['historical','paper','broker_report_fixture']]
            self.assertEqual(len({b.journal_digest() for b in books}),1)
            self.assertEqual(books[0].snapshot(c['marks']),books[2].snapshot(c['marks']))

    def test_every_entry_balances_and_quantity_cost_reconciles(self):
        for c in CASES:
            b=replay_fixture(c['initial'],c['events'])
            self.assertEqual(sum(b.balances.values(),D(0)),D(0))
            for entry in b.entries:
                self.assertEqual(sum((p.debit for p in entry.postings),D(0)),sum((p.credit for p in entry.postings),D(0)))
            for symbol,h in b.holdings.items():
                self.assertEqual(h.cost,b.balance('asset:security_cost:'+symbol))

    def test_independent_cash_and_share_identity_matches_nav(self):
        # This oracle does not use journal, cost allocation, or Ledger helpers.
        for c in CASES:
            cash=D(c['initial']['opening_cash'])
            qty={s:p['quantity'] for s,p in c['initial']['positions'].items()}
            orders={}
            for e in c['events']:
                k,p=e['kind'],e['payload']
                if k=='order.reserve':orders[p['order_id']]=p
                elif k=='fill.booked':
                    o=orders[p['order_id']]; sign=1 if o['side']=='buy' else -1
                    qty[o['instrument_id']]=qty.get(o['instrument_id'],0)+sign*p['quantity']
                    cash-=sign*D(p['price'])*p['quantity']+D(p['fee'])
                elif k=='capital.deposit':cash+=D(p['amount'])
                elif k=='capital.withdraw':cash-=D(p['amount'])
                elif k=='dividend.accrued':cash+=p['entitlement_quantity']*D(p['cash_per_share'])
                elif k=='tax.withheld':cash-=D(p['amount'])
                elif k=='fee.cash_adjustment':cash-=D(p['expense_delta'])
                elif k=='corporate.split_effective':qty[p['instrument_id']]=int(qty[p['instrument_id']]*D(p['factor']))
            oracle=cash+sum((q*D(c['marks'][s]) for s,q in qty.items()),D(0))
            self.assertEqual(D(replay_fixture(c['initial'],c['events']).snapshot(c['marks'])['nav']),oracle)

    def test_exact_redelivery_is_idempotent_and_conflict_is_rejected(self):
        b=self.opening();e=reserve(b,'s','sell',100)
        self.assertTrue(b.apply(e));self.assertFalse(b.apply(e))
        changed=deepcopy(e);changed['payload']['quantity']=101
        self.assert_rejected_without_mutation(b,changed,ValueError)

    def test_sequence_gap_is_rejected(self):
        b=self.opening();e=reserve(b,'s','sell',100);e['sequence']=2
        self.assert_rejected_without_mutation(b,e,ValueError)

    def test_timezone_and_reconciled_time_order_required(self):
        b=self.opening();e=reserve(b,'s','sell',100);e['effective_at']='2026-11-10T11:00:00'
        self.assert_rejected_without_mutation(b,e,ValueError)
        b.apply(reserve(b,'s','sell',100));e=reserve(b,'s2','sell',100);e['effective_at']='2026-11-10T10:00:00+08:00'
        self.assert_rejected_without_mutation(b,e,ValueError)

    def test_session_date_cannot_unlock_inventory_before_effective_day(self):
        b=self.opening(qty=0)
        early=reserve(b,'early','buy',1);early['effective_at']='2026-11-09T11:00:00+08:00'
        self.assert_rejected_without_mutation(b,early,ValueError)
        b.apply(reserve(b,'b','buy',100));b.apply(fill(b,'fb','b',100,buy=True))
        self.assert_rejected_without_mutation(b,event(b,'session.open',{'trading_day':'2026-11-11'}),ValueError)
        self.assertEqual(b.sellable(S),0)
        self.assert_rejected_without_mutation(b,reserve(b,'s','sell',100),ValueError)
        # A UTC instant on the next Shanghai date is valid, without requiring +08 text.
        valid=event(b,'session.open',{'trading_day':'2026-11-11'},day='2026-11-11')
        valid['effective_at']='2026-11-10T16:00:00+00:00'
        b.apply(valid);self.assertEqual(b.sellable(S),100)

    def test_public_amount_reads_ignore_callers_decimal_context(self):
        b=self.opening(cash='200000.01',qty=0)
        b.apply(reserve(b,'b','buy',1,price='123456.789',fee='0'))
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(b.orders['b'].reserved_cash,D('123456.789'))
            self.assertEqual(b.cash_available(),D('76543.221'))
        b.apply(fill(b,'f','b',1,price='123456.789',fee='0',buy=True))
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(b.payable(),D('123456.789'))
            self.assertEqual(b.cash_available(),D('76543.221'))

    def test_two_buy_strategies_cannot_reserve_same_cash(self):
        b=self.opening(cash='1000',qty=0)
        b.apply(reserve(b,'a','buy',60))
        self.assertEqual(b.cash_available(),D('395'))
        self.assert_rejected_without_mutation(b,reserve(b,'b','buy',50),ValueError)

    def test_two_sell_strategies_cannot_reserve_same_inventory(self):
        b=self.opening(qty=100,cost='1000')
        b.apply(reserve(b,'a','sell',60))
        self.assert_rejected_without_mutation(b,reserve(b,'b','sell',50),ValueError)

    def test_today_buy_has_no_today_sellable_inventory(self):
        b=self.opening(qty=0)
        b.apply(reserve(b,'b','buy',100));b.apply(fill(b,'fb','b',100,buy=True))
        self.assert_rejected_without_mutation(b,reserve(b,'s','sell',100),ValueError)
        b.apply(event(b,'session.open',{'trading_day':'2026-11-11'},day='2026-11-11'))
        b.apply(reserve(b,'s','sell',100));self.assertEqual(b.reserved_sell(S),100)

    def test_buy_cannot_declare_same_day_unlock(self):
        b=self.opening(qty=0);b.apply(reserve(b,'b','buy',100));e=fill(b,'fb','b',100,buy=True)
        e['payload']['eligible_on']='2026-11-10'
        self.assert_rejected_without_mutation(b,e,ValueError)

    def test_cancel_with_missing_fills_preserves_reservation(self):
        b=self.opening();b.apply(reserve(b,'s','sell',300));b.apply(fill(b,'f1','s',100))
        self.assert_rejected_without_mutation(b,event(b,'order.cancel_confirmed',{'order_id':'s','cumulative_filled':150}),ValueError)
        self.assertEqual(b.reserved_sell(S),200)
        b.apply(fill(b,'f2','s',50,fee='0'))
        b.apply(event(b,'order.cancel_confirmed',{'order_id':'s','cumulative_filled':150}))
        self.assertEqual(b.reserved_sell(S),0);self.assertEqual(b.holdings[S].quantity,850)

    def test_overfill_and_fill_after_cancel_do_not_mutate(self):
        b=self.opening();b.apply(reserve(b,'s','sell',100))
        self.assert_rejected_without_mutation(b,fill(b,'f1','s',101),ValueError)
        b.apply(event(b,'order.cancel_confirmed',{'order_id':'s','cumulative_filled':0}))
        self.assert_rejected_without_mutation(b,fill(b,'f1','s',1),ValueError)

    def test_sale_cash_policy_changes_availability_not_wealth(self):
        c=BY_ID['B02'];a=replay_fixture(c['initial'],c['events']);init=deepcopy(c['initial']);init['sale_proceeds_reusable']=False
        b=replay_fixture(init,c['events'])
        self.assertEqual(a.snapshot(c['marks'])['nav'],b.snapshot(c['marks'])['nav'])
        self.assertEqual(a.cash_available(),D('11045'));self.assertEqual(b.cash_available(),D('10000'))

    def test_non_reusable_proceeds_cannot_fund_same_day_buy(self):
        c=BY_ID['B04'];init=deepcopy(c['initial']);init['sale_proceeds_reusable']=False
        b=replay_fixture(init,c['events'][:2])
        self.assert_rejected_without_mutation(b,c['events'][2],ValueError)

    def test_settlement_changes_no_nav_or_income(self):
        c=BY_ID['B04'];b=replay_fixture(c['initial'],c['events']);before=b.snapshot(c['marks'])
        b.apply(event(b,'cash.settlement',{'fill_ids':['fs','fb']}))
        after=b.snapshot(c['marks'])
        self.assertEqual(before['nav'],after['nav']);self.assertEqual(before['pnl_from_cost_origin'],after['pnl_from_cost_origin'])
        self.assertEqual(D(after['cash_settled']),D('90'));self.assertEqual(D(after['trade_receivable']),D(0))

    def test_same_partial_fills_with_cumulative_fee_match_one_fill(self):
        c=BY_ID['B09'];many=replay_fixture(c['initial'],c['events']);one=self.opening(qty=0)
        one.apply(reserve(one,'b','buy',100));one.apply(fill(one,'fb','b',100,buy=True))
        for key in ['nav','cash_available','cost_basis','realized_gross','unrealized','fees']:
            self.assertEqual(D(one.snapshot(c['marks'])[key]),D(many.snapshot(c['marks'])[key]))

    def test_negative_zero_float_and_nonfinite_inputs_rejected(self):
        for bad in [-1,0,True,1.5]:
            b=self.opening();self.assert_rejected_without_mutation(b,reserve(b,'s','sell',bad),ValueError)
        for bad in ['NaN','Infinity',10.5]:
            b=self.opening();self.assert_rejected_without_mutation(b,reserve(b,'s','sell',1,bad),Exception)

    def test_database_numeric_and_quantity_bounds_apply_to_aggregates(self):
        b=self.opening(cash='90000000000000000000000000',qty=0)
        self.assert_rejected_without_mutation(b,event(b,'capital.deposit',{'amount':'20000000000000000000000000'}),ValueError)
        b=self.opening(qty=2**62,cost='10000')
        p={'action_id':'overflow','instrument_id':S,'factor':'4','eligibility_policy':'inherit_existing_lot_dates'}
        self.assert_rejected_without_mutation(b,event(b,'corporate.split_effective',p),ValueError)

    def test_unknown_fields_fail_instead_of_silently_changing_semantics(self):
        b=self.opening();e=reserve(b,'s','sell',10);e['payload']['assume_fill']=True
        self.assert_rejected_without_mutation(b,e,ValueError)

    def test_fill_price_limit_is_checked(self):
        b=self.opening(qty=0);b.apply(reserve(b,'b','buy',100))
        self.assert_rejected_without_mutation(b,fill(b,'fb','b',100,price='10.01',buy=True),ValueError)

    def test_dividend_entitlement_is_not_recomputed_from_current_holding(self):
        b=self.opening(qty=900,cost='9000');b.apply(event(b,'dividend.accrued',{'action_id':'d','instrument_id':S,'entitlement_quantity':1000,'cash_per_share':'0.2','entitlement_snapshot_id':'record-date-before-sale'}))
        self.assertEqual(D(b.snapshot({S:'10'})['dividend_receivable']),D('200'))

    def test_dividend_payment_is_not_second_income(self):
        c=BY_ID['B05'];b=replay_fixture(c['initial'],c['events'][:1]);before=b.snapshot(c['marks'])
        b.apply(c['events'][1]);after=b.snapshot(c['marks'])
        self.assertEqual(before['nav'],after['nav']);self.assertEqual(before['dividend_income'],after['dividend_income'])

    def test_split_replay_cannot_double_quantity(self):
        c=BY_ID['B06'];b=replay_fixture(c['initial'],c['events'])
        e=deepcopy(c['events'][0]);e.update(event_id='new-envelope',sequence=2,effective_at='2026-11-10T11:00:02+08:00')
        self.assert_rejected_without_mutation(b,e,ValueError)

    def test_split_with_pending_order_or_fraction_requires_explicit_policy(self):
        b=self.opening(qty=101,cost='1010');b.apply(reserve(b,'s','sell',100))
        p={'action_id':'sp','instrument_id':S,'factor':'2','eligibility_policy':'inherit_existing_lot_dates'}
        self.assert_rejected_without_mutation(b,event(b,'corporate.split_effective',p),ValueError)
        b.apply(event(b,'order.cancel_confirmed',{'order_id':'s','cumulative_filled':0}));p['factor']='0.5'
        self.assert_rejected_without_mutation(b,event(b,'corporate.split_effective',p),ValueError)

    def test_missing_mark_is_not_zero(self):
        b=self.opening()
        with self.assertRaises(KeyError):b.snapshot({})

    def test_external_flow_twr_uses_pre_and_post_flow_nav(self):
        c=BY_ID['B07'];b=Ledger(**c['initial'])
        pre=b.snapshot({S:'11'})['nav'];b.apply(c['events'][0]);post=b.snapshot({S:'11'})['nav'];end=b.snapshot(c['marks'])['nav']
        self.assertEqual(true_time_weighted_return([('10000',pre),(post,end)]),D('.21'))
        self.assertNotEqual(D(end)/D('10000')-1,D('.21'))

    def test_twr_rejects_missing_or_nonpositive_capital(self):
        for bad in [[],[('0','100')],[('-1','100')]]:
            with self.assertRaises(ValueError):true_time_weighted_return(bad)

    def test_numeric_policy_is_independent_of_callers_decimal_context(self):
        c=BY_ID['B08'];baseline=replay_fixture(c['initial'],c['events']).snapshot(c['marks'])
        with localcontext() as context:
            context.prec=6
            other=replay_fixture(c['initial'],c['events']).snapshot(c['marks'])
        self.assertEqual(baseline,other)

    def test_adjustment_does_not_overwrite_prior_journal(self):
        c=BY_ID['B10'];b=replay_fixture(c['initial'],c['events'][:-1]);prefix=deepcopy(b.entries)
        b.apply(c['events'][-1]);self.assertEqual(b.entries[:-1],prefix)

    def test_unsupported_trade_bust_is_explicit_and_atomic(self):
        b=self.opening()
        self.assert_rejected_without_mutation(b,event(b,'trade.corrected',{'fill_id':'unknown'}),ValueError)

    def test_snapshot_caller_mutation_cannot_change_book(self):
        b=self.opening();s=b.snapshot({S:'10'});s['positions'][S]['quantity']=0
        self.assertEqual(b.snapshot({S:'10'})['positions'][S]['quantity'],1000)


if __name__=='__main__':unittest.main()
