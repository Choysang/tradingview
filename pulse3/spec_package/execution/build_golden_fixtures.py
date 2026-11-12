"""Hand-specified expected amounts; never calls the implementation under test."""
from pathlib import Path
import json

ROOT=Path(__file__).resolve().parent
S='SAMPLE_MAIN_001'
def event_list(commands):
    out=[]; day='2026-11-10'
    for n,(kind,p) in enumerate(commands,1):
        if kind=='session.open':day=p['trading_day']
        out.append({'event_id':f'e{n:03d}','sequence':n,'kind':kind,
                    'effective_at':f'{day}T10:00:{n:02d}+08:00','payload':p})
    return out

def reserve(oid,side,qty,price,fee='5'):
    return ('order.reserve',dict(order_id=oid,instrument_id=S,side=side,quantity=qty,limit_price=price,fee_budget=fee))
def fill(fid,oid,qty,price,fee='5',buy=False):
    return ('fill.booked',dict(fill_id=fid,order_id=oid,quantity=qty,price=price,fee=fee,eligible_on='2026-11-11' if buy else None))
def initial(cash='10000',qty=0,cost='0',reusable=True):
    return dict(trading_day='2026-11-10',opening_cash=cash,positions={S:dict(quantity=qty,cost=cost)} if qty else {},sale_proceeds_reusable=reusable)
def case(cid,title,init,commands,mark,expected,note,**extra):
    return dict(case_id=cid,title=title,synthetic=True,initial=init,events=event_list(commands),marks={S:mark},expected=expected,manual_derivation=note,**extra)

roundtrip=[reserve('b1','buy',100,'10'),fill('fb','b1',100,'10',buy=True),('session.open',{'trading_day':'2026-11-11'}),('cash.settlement',{'fill_ids':['fb']}),reserve('s1','sell',100,'11'),fill('fs','s1',100,'11'),('session.open',{'trading_day':'2026-11-12'}),('cash.settlement',{'fill_ids':['fs']})]
fixtures=[
case('B01','买卖完整往返',initial(),roundtrip,'11',{'cash_settled':'10090','nav':'10090','cost_basis':'0','realized_gross':'100','fees':'10','pnl_from_cost_origin':'90','positions':{S:{'quantity':0,'sellable':0,'reserved_sell':0}}},'10000 - 100×10 - 5 + 100×11 - 5 = 10090；毛已实现100，费用10。'),
case('B02','部分成交后确认撤单',initial(qty=1000,cost='10000'),[reserve('s1','sell',300,'10'),fill('fs','s1',100,'10.5'),('order.cancel_confirmed',{'order_id':'s1','cumulative_filled':100})],'10',{'cash_settled':'10000','trade_receivable':'1045','cash_available':'11045','nav':'20045','cost_basis':'9000','realized_gross':'50','fees':'5','pnl_from_cost_origin':'45','positions':{S:{'quantity':900,'sellable':900,'reserved_sell':0}}},'卖100股净应收100×10.5-5=1045；剩900股按10计值9000；撤单不产生盈亏。'),
case('B03','做T卖出后未买回',initial(qty=1000,cost='10000'),[reserve('s1','sell',200,'10.5'),fill('fs','s1',200,'10.5')],'12',{'nav':'21695','realized_gross':'100','unrealized':'1600','fees':'5','cost_basis':'8000','pnl_from_cost_origin':'1695','positions':{S:{'quantity':800,'sellable':800,'reserved_sell':0}}},'主动净值10000+200×10.5-5+800×12=21695；持续持有净值10000+1000×12=22000；增量-305。',passive_nav='22000',expected_increment='-305'),
case('B04','复用卖出款完成一次T',initial(cash='0',qty=1000,cost='10000'),[reserve('s1','sell',200,'10.5'),fill('fs','s1',200,'10.5'),reserve('b1','buy',200,'10'),fill('fb','b1',200,'10',buy=True)],'10',{'cash_settled':'0','trade_receivable':'2095','trade_payable':'2005','cash_available':'90','nav':'10090','cost_basis':'10000','realized_gross':'100','fees':'10','pnl_from_cost_origin':'90','positions':{S:{'quantity':1000,'sellable':800,'reserved_sell':0}}},'经济净现金2095-2005=90；老底仓可卖量从1000降为800，新买200当日不恢复可卖额度。',passive_nav='10000',expected_increment='90'),
case('B05','分红应收到账与税款',initial(qty=1000,cost='10000'),[('dividend.accrued',{'action_id':'div1','instrument_id':S,'entitlement_quantity':1000,'cash_per_share':'0.2','entitlement_snapshot_id':'record-date-v1'}),('dividend.paid',{'action_id':'div1'}),('tax.withheld',{'action_id':'div1','amount':'20'})],'9.8',{'cash_settled':'10180','dividend_receivable':'0','dividend_income':'200','withholding_tax':'20','unrealized':'-200','nav':'19980','pnl_from_cost_origin':'-20'},'价格从10降为9.8，市值少200；应收红利200到账仅变资产形态；税款20使总净值少20。税率仅为假设输入。'),
case('B06','纯拆股不制造收益',initial(cash='0',qty=100,cost='1000'),[('corporate.split_effective',{'action_id':'split1','instrument_id':S,'factor':'2','eligibility_policy':'inherit_existing_lot_dates'})],'5',{'cost_basis':'1000','nav':'1000','pnl_from_cost_origin':'0','positions':{S:{'quantity':200,'sellable':200,'reserved_sell':0}}},'100股×10拆成200股×5，成本总额1000和净值1000不变；是纯拆股样例，A股送转股上市日另定。'),
case('B07','外部入金与时间加权收益分开',initial(cash='0',qty=1000,cost='10000'),[('capital.deposit',{'amount':'9000'})],'13',{'cash_settled':'9000','net_external_capital':'19000','nav':'22000','unrealized':'3000','pnl_from_cost_origin':'3000'},'入金前1000股从10涨到11，NAV11000；入金9000后NAV20000；期末股价13，NAV22000。TWR=(11000/10000)×(22000/20000)-1=21%，不能把入金算收益。',twr_segments=[['10000','11000'],['20000','22000']],expected_twr='0.21'),
case('B08','先买后卖使用旧可卖库存',initial(qty=1000,cost='10000'),[reserve('b1','buy',200,'9.8'),fill('fb','b1',200,'9.8',buy=True),reserve('s1','sell',200,'10.1'),fill('fs','s1',200,'10.1')],'10',{'cash_available':'10050','nav':'20050','cost_basis':'9966.666666666667','realized_gross':'26.666666666667','unrealized':'33.333333333333','fees':'10','pnl_from_cost_origin':'50','positions':{S:{'quantity':1000,'sellable':800,'reserved_sell':0}}},'净现金变化200×(10.1-9.8)-10=50；加权平均成本只改变已实现/未实现拆分，不改变总利润50。',passive_nav='20000',expected_increment='50'),
case('B09','同订单三次成交累计费用一次',initial(),[reserve('b1','buy',100,'10'),fill('fb1','b1',30,'10','5',True),fill('fb2','b1',30,'10','0',True),fill('fb3','b1',40,'10','0',True)],'10',{'cash_available':'8995','trade_payable':'1005','cost_basis':'1000','fees':'5','nav':'9995','pnl_from_cost_origin':'-5','positions':{S:{'quantity':100,'sellable':0,'reserved_sell':0}}},'同一订单100股成交拆30/30/40，已由费用策略产生增量费用5/0/0；总成本1005，不对每笔成交再扣最低佣金。此样例不实现费用策略本身。'),
case('B10','追加费用退款保留旧分录',initial(),roundtrip+[('fee.cash_adjustment',{'related_fill_id':'fb','expense_delta':'-1'})],'11',{'cash_settled':'10091','fees':'9','realized_gross':'100','nav':'10091','pnl_from_cost_origin':'91'},'原往返利润90，费用退款1通过新分录使净值10091；原Fill和分录不覆写。'),
]
(ROOT/'golden_fixtures.json').write_text(json.dumps({'schema_version':'1.0','policy_version':'pulse-economic-cny-weighted-average-gross-v1','fixture_kind':'synthetic_hand_calculated','cases':fixtures},ensure_ascii=False,indent=2)+'\n')
print(f'Wrote {len(fixtures)} hand-calculated fixtures')
