"""Pulse v3 executable accounting specification, single CNY account.

This is a deterministic reducer for already-normalized, ordered business facts.
It is NOT an exchange matcher, broker connector, statutory IFRS ledger or GIPS
compliance implementation. All entry adapters use this one reducer. Production
persists its equivalent event/quantity/journal/reservation update atomically.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import hashlib
import json
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

D = Decimal
ZERO = D('0')
SCALE = D('0.000000000001')
POLICY_VERSION = 'pulse-economic-cny-weighted-average-gross-v1'
NUMERIC_POLICY_VERSION = 'decimal50-scale12-half-even-v1'
MARKET_TIMEZONE = ZoneInfo('Asia/Shanghai')


def decimal(value: str | Decimal, name: str = 'amount', *, positive=False,
            nonnegative=True) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise TypeError(f'{name}: require decimal string or Decimal, never float')
    result = D(value)
    if not result.is_finite():
        raise ValueError(f'{name}: finite value required')
    if (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f'{name}: invalid sign')
    if result.as_tuple().exponent < -12:
        raise ValueError(f'{name}: at most 12 decimal places')
    if result != 0 and result.adjusted() >= 26:
        raise ValueError(f'{name}: outside numeric(38,12) range')
    return result


def quantity(value: int, name='quantity', *, positive=False) -> int:
    if type(value) is not int or value < 0 or value > 2**63-1 or (positive and value == 0):
        raise ValueError(f'{name}: positive/nonnegative integer shares required')
    return value


def identity(value: str, name='id') -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name}: nonempty string required')
    return value


def quantize(value: Decimal) -> Decimal:
    return value.quantize(SCALE, rounding=ROUND_HALF_EVEN)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def display(value: Decimal) -> str:
    return format(value, 'f')


@dataclass(frozen=True)
class Posting:
    account: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass(frozen=True)
class Entry:
    event_id: str
    sequence: int
    kind: str
    effective_at: str
    currency: str
    postings: tuple[Posting, ...]


@dataclass
class Lot:
    quantity: int
    eligible_on: date


@dataclass
class Holding:
    cost: Decimal
    lots: list[Lot]

    @property
    def quantity(self):
        return sum(x.quantity for x in self.lots)


@dataclass
class Order:
    instrument_id: str
    side: str
    quantity: int
    limit_price: Decimal
    fee_budget: Decimal
    filled: int = 0
    fees_paid: Decimal = ZERO
    cancelled: bool = False

    @property
    def remaining(self):
        return 0 if self.cancelled else self.quantity - self.filled

    @property
    def reserved_cash(self):
        if self.side != 'buy' or self.remaining == 0:
            return ZERO
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            return self.remaining * self.limit_price + max(ZERO, self.fee_budget - self.fees_paid)


class Ledger:
    """One writer; copy-and-commit is only for small reference state."""
    def __init__(self, *, trading_day: str, opening_cash: str,
                 positions: Mapping[str, Mapping] | None = None,
                 sale_proceeds_reusable: bool = True):
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            self._initialize(trading_day, opening_cash, positions, sale_proceeds_reusable)

    def _initialize(self, trading_day, opening_cash, positions, sale_proceeds_reusable):
        self.day = date.fromisoformat(trading_day)
        if type(sale_proceeds_reusable) is not bool:
            raise TypeError('sale_proceeds_reusable must be bool')
        self.sale_proceeds_reusable = sale_proceeds_reusable
        self.balances: dict[str, Decimal] = {}
        self.holdings: dict[str, Holding] = {}
        self.orders: dict[str, Order] = {}
        self.fills: dict[str, dict] = {}
        self.pending_settlements: dict[str, Decimal] = {}
        self.dividends: dict[str, Decimal] = {}
        self.applied_splits: set[str] = set()
        self.entries: list[Entry] = []
        self._events: dict[str, str] = {}
        self.sequence = 0
        self.last_effective_at: datetime | None = None
        cash = decimal(opening_cash)
        posts = [Posting('asset:cash_settled', debit=cash)]
        initial_cost = ZERO
        for symbol, p in (positions or {}).items():
            identity(symbol, 'instrument_id')
            qty = quantity(p['quantity'], positive=True)
            cost = decimal(p['cost'], 'cost')
            self.holdings[symbol] = Holding(cost, [Lot(qty, self.day)])
            initial_cost += cost
            posts.append(Posting(f'asset:security_cost:{symbol}', debit=cost))
        posts.append(Posting('equity:external_capital', credit=cash + initial_cost))
        self._post({'event_id': 'opening', 'sequence': 0, 'kind': 'opening',
                    'effective_at': trading_day + 'T00:00:00+08:00'}, posts)
        self._check()

    def balance(self, account: str) -> Decimal:
        return self.balances.get(account, ZERO)

    def _post(self, event: Mapping, postings: list[Posting]) -> None:
        postings = [p for p in postings if p.debit != 0 or p.credit != 0]
        if sum((p.debit for p in postings), ZERO) != sum((p.credit for p in postings), ZERO):
            raise ValueError('unbalanced journal')
        for p in postings:
            decimal(p.debit, 'debit'); decimal(p.credit, 'credit')
            if p.debit < 0 or p.credit < 0 or (p.debit and p.credit):
                raise ValueError('invalid debit/credit')
            self.balances[p.account] = self.balance(p.account) + p.debit - p.credit
        self.entries.append(Entry(event['event_id'], event['sequence'], event['kind'],
                                  event['effective_at'], 'CNY', tuple(postings)))

    def _trade_cash_post(self, net: Decimal) -> Posting:
        if net >= 0:
            return Posting('asset:trade_receivable', debit=net)
        return Posting('liability:trade_payable', credit=-net)

    def settled_cash(self):
        return self.balance('asset:cash_settled')

    def receivable(self):
        return self.balance('asset:trade_receivable')

    def payable(self):
        return self.balance('liability:trade_payable').copy_negate()

    def cash_available(self):
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            reusable = self.receivable() if self.sale_proceeds_reusable else ZERO
            reserves = sum((o.reserved_cash for o in self.orders.values()), ZERO)
            return self.settled_cash() + reusable - self.payable() - reserves

    def sellable(self, symbol):
        p = self.holdings.get(symbol)
        return sum(lot.quantity for lot in p.lots if lot.eligible_on <= self.day) if p else 0

    def reserved_sell(self, symbol):
        return sum(o.remaining for o in self.orders.values()
                   if o.instrument_id == symbol and o.side == 'sell')

    def available_to_sell(self, symbol):
        return self.sellable(symbol) - self.reserved_sell(symbol)

    def apply(self, event: Mapping) -> bool:
        """Return False for exact redelivery; fail without mutation otherwise.

        All accepted canonical events have monotonically increasing account
        sequence and effective time. Late broker messages must be reconciled by
        the adapter before this bounded reference reducer. Production correction
        and historical-restatement protocols are specified separately.
        """
        event = deepcopy(dict(event))
        required = {'event_id', 'sequence', 'kind', 'effective_at', 'payload'}
        if set(event) != required:
            raise ValueError('canonical event field mismatch')
        identity(event['event_id'], 'event_id')
        if event['event_id'] == 'opening':
            raise ValueError('opening event id is reserved')
        identity(event['kind'], 'kind')
        if not isinstance(event['payload'], dict):
            raise TypeError('payload must be object')
        raw = canonical(event)
        if event['event_id'] in self._events:
            if raw != self._events[event['event_id']]:
                raise ValueError('event_id content conflict')
            return False
        if type(event['sequence']) is not int or event['sequence'] != self.sequence + 1:
            raise ValueError('account sequence gap or conflict')
        when = datetime.fromisoformat(event['effective_at'])
        if when.utcoffset() is None:
            raise ValueError('timezone required')
        if self.last_effective_at is not None and when < self.last_effective_at:
            raise ValueError('canonical events require reconciled time order')
        effective_day = when.astimezone(MARKET_TIMEZONE).date()
        target_day = (date.fromisoformat(event['payload'].get('trading_day', ''))
                      if event['kind'] == 'session.open' else self.day)
        if effective_day != target_day:
            raise ValueError('effective date must match the Asia/Shanghai account session')
        candidate = deepcopy(self)
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = ROUND_HALF_EVEN
            candidate._reduce(event)
            candidate.sequence = event['sequence']
            candidate.last_effective_at = when
            candidate._events[event['event_id']] = raw
            candidate._check()
        self.__dict__ = candidate.__dict__
        return True

    @staticmethod
    def _keys(payload, expected):
        if set(payload) != set(expected):
            raise ValueError(f'payload fields must be {sorted(expected)}')

    def _reduce(self, e):
        p, kind = e['payload'], e['kind']
        if kind in {'capital.deposit', 'capital.withdraw'}:
            self._keys(p, {'amount'})
            amount = decimal(p['amount'], positive=True)
            if kind == 'capital.deposit':
                posts = [Posting('asset:cash_settled', debit=amount),
                         Posting('equity:external_capital', credit=amount)]
            else:
                if amount > min(self.cash_available(), self.settled_cash()):
                    raise ValueError('insufficient available settled cash for withdrawal')
                posts = [Posting('equity:external_capital', debit=amount),
                         Posting('asset:cash_settled', credit=amount)]
            self._post(e, posts)
        elif kind == 'order.reserve':
            self._keys(p, {'order_id', 'instrument_id', 'side', 'quantity', 'limit_price', 'fee_budget'})
            oid, symbol = identity(p['order_id']), identity(p['instrument_id'])
            if oid in self.orders:
                raise ValueError('order_id already exists; retry original event')
            if p['side'] not in {'buy', 'sell'}:
                raise ValueError('unsupported side')
            order = Order(symbol, p['side'], quantity(p['quantity'], positive=True),
                          decimal(p['limit_price'], positive=True), decimal(p['fee_budget']))
            if order.side == 'buy' and order.reserved_cash > self.cash_available():
                raise ValueError('insufficient unreserved cash')
            if order.side == 'sell' and order.quantity > self.available_to_sell(symbol):
                raise ValueError('insufficient unreserved sellable inventory')
            self.orders[oid] = order
        elif kind == 'order.cancel_confirmed':
            self._keys(p, {'order_id', 'cumulative_filled'})
            order = self.orders[p['order_id']]
            cum = quantity(p['cumulative_filled'])
            if cum != order.filled:
                raise ValueError('reconcile cumulative fills before releasing reservation')
            order.cancelled = True
        elif kind == 'fill.booked':
            self._keys(p, {'fill_id', 'order_id', 'quantity', 'price', 'fee', 'eligible_on'})
            fid = identity(p['fill_id'], 'fill_id')
            if fid in self.fills:
                raise ValueError('fill_id already booked; retry canonical event')
            order = self.orders[p['order_id']]
            qty = quantity(p['quantity'], positive=True)
            price, fee = decimal(p['price'], positive=True), decimal(p['fee'])
            if qty > order.remaining:
                raise ValueError('overfill or closed order')
            if (order.side == 'buy' and price > order.limit_price) or (order.side == 'sell' and price < order.limit_price):
                raise ValueError('fill outside limit')
            gross = quantize(qty * price)
            symbol = order.instrument_id
            holding = self.holdings.setdefault(symbol, Holding(ZERO, []))
            if order.side == 'buy':
                unlock = date.fromisoformat(p['eligible_on'])
                if unlock <= self.day:
                    raise ValueError('ordinary A-share buy must become sellable later')
                holding.cost += gross
                holding.lots.append(Lot(qty, unlock))
                net = -(gross + fee)
                posts = [Posting(f'asset:security_cost:{symbol}', debit=gross),
                         Posting('expense:transaction_cost', debit=fee), self._trade_cash_post(net)]
            else:
                if p['eligible_on'] is not None:
                    raise ValueError('sell fill must not declare acquisition eligibility')
                if qty > self.sellable(symbol):
                    raise ValueError('sale exceeds eligible inventory')
                released_cost = holding.cost if qty == holding.quantity else quantize(holding.cost * qty / holding.quantity)
                holding.cost -= released_cost
                left = qty
                for lot in holding.lots:
                    if lot.eligible_on <= self.day:
                        take = min(left, lot.quantity)
                        lot.quantity -= take
                        left -= take
                    if left == 0:
                        break
                holding.lots = [x for x in holding.lots if x.quantity]
                pnl = gross - released_cost
                net = gross - fee
                posts = [self._trade_cash_post(net), Posting(f'asset:security_cost:{symbol}', credit=released_cost),
                         Posting('expense:transaction_cost', debit=fee)]
                posts.append(Posting('income:realized', credit=pnl) if pnl >= 0 else Posting('income:realized', debit=-pnl))
            order.filled += qty
            order.fees_paid += fee
            self.pending_settlements[fid] = net
            self.fills[fid] = deepcopy(p)
            self._post(e, posts)
        elif kind == 'cash.settlement':
            self._keys(p, {'fill_ids'})
            ids = p['fill_ids']
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise ValueError('distinct settlement fill ids required')
            values = [self.pending_settlements[fid] for fid in ids]
            inflow = sum((x for x in values if x > 0), ZERO)
            outflow = -sum((x for x in values if x < 0), ZERO)
            if self.settled_cash() + inflow - outflow < 0:
                raise ValueError('settlement requires funded or netted cash')
            self._post(e, [Posting('asset:cash_settled', debit=inflow),
                           Posting('asset:trade_receivable', credit=inflow),
                           Posting('liability:trade_payable', debit=outflow),
                           Posting('asset:cash_settled', credit=outflow)])
            for fid in ids:
                del self.pending_settlements[fid]
        elif kind == 'session.open':
            self._keys(p, {'trading_day'})
            day = date.fromisoformat(p['trading_day'])
            if day <= self.day:
                raise ValueError('session must advance on validated CalendarService day')
            self.day = day
        elif kind == 'dividend.accrued':
            self._keys(p, {'action_id', 'instrument_id', 'entitlement_quantity', 'cash_per_share', 'entitlement_snapshot_id'})
            action = identity(p['action_id'])
            identity(p['instrument_id']); identity(p['entitlement_snapshot_id'])
            if action in self.dividends:
                raise ValueError('corporate action already accrued')
            qty = quantity(p['entitlement_quantity'], positive=True)
            amount = quantize(qty * decimal(p['cash_per_share'], positive=True))
            self.dividends[action] = amount
            self._post(e, [Posting('asset:dividend_receivable', debit=amount), Posting('income:dividend', credit=amount)])
        elif kind == 'dividend.paid':
            self._keys(p, {'action_id'})
            amount = self.dividends[p['action_id']]
            if amount <= 0:
                raise ValueError('dividend already paid')
            self._post(e, [Posting('asset:cash_settled', debit=amount), Posting('asset:dividend_receivable', credit=amount)])
            self.dividends[p['action_id']] = ZERO
        elif kind == 'tax.withheld':
            self._keys(p, {'action_id', 'amount'})
            if p['action_id'] not in self.dividends:
                raise ValueError('unknown entitlement')
            amount = decimal(p['amount'], positive=True)
            self._post(e, [Posting('expense:withholding_tax', debit=amount), Posting('asset:cash_settled', credit=amount)])
        elif kind == 'fee.cash_adjustment':
            self._keys(p, {'related_fill_id', 'expense_delta'})
            if p['related_fill_id'] not in self.fills:
                raise ValueError('unknown related fill')
            delta = decimal(p['expense_delta'], nonnegative=False)
            if delta == 0:
                raise ValueError('nonzero adjustment required')
            posts = ([Posting('expense:transaction_cost', debit=delta), Posting('asset:cash_settled', credit=delta)]
                     if delta > 0 else [Posting('asset:cash_settled', debit=-delta), Posting('expense:transaction_cost', credit=-delta)])
            self._post(e, posts)
        elif kind == 'corporate.split_effective':
            self._keys(p, {'action_id', 'instrument_id', 'factor', 'eligibility_policy'})
            identity(p['action_id'])
            if p['action_id'] in self.applied_splits:
                raise ValueError('split already applied')
            symbol = p['instrument_id']
            if p['eligibility_policy'] != 'inherit_existing_lot_dates':
                raise ValueError('reference supports pure split only, not bonus-share listing policy')
            if any(o.instrument_id == symbol and o.remaining for o in self.orders.values()):
                raise ValueError('reconcile outstanding orders before corporate action')
            factor = decimal(p['factor'], positive=True)
            holding = self.holdings[symbol]
            for lot in holding.lots:
                adjusted = lot.quantity * factor
                if adjusted != adjusted.to_integral_value():
                    raise ValueError('fractional shares require explicit cash-in-lieu policy')
                lot.quantity = int(adjusted)
            # Quantity and unit cost change; total economic cost does not.
            self.applied_splits.add(p['action_id'])
            self._post(e, [])
        else:
            raise ValueError(f'unsupported event kind: {kind}')

    def _check(self):
        for balance in self.balances.values():
            decimal(balance, 'account_balance', nonnegative=False)
        if sum(self.balances.values(), ZERO) != 0:
            raise AssertionError('trial balance fails')
        for entry in self.entries:
            if sum((p.debit - p.credit for p in entry.postings), ZERO) != 0:
                raise AssertionError('journal entry not balanced')
        if self.settled_cash() < 0 or self.receivable() < 0 or self.payable() < 0 or self.cash_available() < 0:
            raise ValueError('negative cash or unavailable funding in reference profile')
        for symbol, h in self.holdings.items():
            quantity(h.quantity, 'position_quantity')
            for lot in h.lots:
                quantity(lot.quantity, 'lot_quantity', positive=True)
            if h.quantity < 0 or h.cost < 0 or any(x.quantity <= 0 for x in h.lots):
                raise AssertionError('quantity/cost invariant')
            if h.quantity == 0 and h.cost != 0:
                raise AssertionError('residual cost on zero position')
            if self.balance(f'asset:security_cost:{symbol}') != h.cost:
                raise AssertionError('quantity cost and money subledger disagree')
            if not 0 <= self.reserved_sell(symbol) <= self.sellable(symbol) <= h.quantity:
                raise AssertionError('sellability/reservation invariant')
        positive = sum((x for x in self.pending_settlements.values() if x > 0), ZERO)
        negative = -sum((x for x in self.pending_settlements.values() if x < 0), ZERO)
        if positive != self.receivable() or negative != self.payable():
            raise AssertionError('trade settlements do not reconcile')
        if sum(self.dividends.values(), ZERO) != self.balance('asset:dividend_receivable'):
            raise AssertionError('dividend receivable does not reconcile')

    def snapshot(self, marks: Mapping[str, str | Decimal]):
        with localcontext() as ctx:
            ctx.prec = 50
            value = ZERO
            for symbol, h in self.holdings.items():
                if h.quantity:
                    value += h.quantity * decimal(marks[symbol], 'raw_mark')
            cost = sum((h.cost for h in self.holdings.values()), ZERO)
            nav = self.settled_cash() + self.receivable() - self.payable() + self.balance('asset:dividend_receivable') + value
            realized = -self.balance('income:realized')
            unrealized = value - cost
            income = -self.balance('income:dividend')
            fees, tax = self.balance('expense:transaction_cost'), self.balance('expense:withholding_tax')
            capital = -self.balance('equity:external_capital')
            pnl = realized + unrealized + income - fees - tax
            if nav != capital + pnl:
                raise AssertionError('economic equity bridge fails')
            return {'policy_version': POLICY_VERSION, 'numeric_policy_version': NUMERIC_POLICY_VERSION,
                    'sequence': self.sequence, 'trading_day': self.day.isoformat(),
                    'cash_settled': display(self.settled_cash()), 'trade_receivable': display(self.receivable()),
                    'trade_payable': display(self.payable()), 'dividend_receivable': display(self.balance('asset:dividend_receivable')),
                    'cash_available': display(self.cash_available()), 'cost_basis': display(cost),
                    'realized_gross': display(realized), 'unrealized': display(unrealized), 'dividend_income': display(income),
                    'fees': display(fees), 'withholding_tax': display(tax), 'net_external_capital': display(capital),
                    'nav': display(nav), 'pnl_from_cost_origin': display(pnl),
                    'positions': {s: {'quantity': h.quantity, 'sellable': self.sellable(s),
                                     'reserved_sell': self.reserved_sell(s), 'cost': display(h.cost)}
                                  for s, h in sorted(self.holdings.items())}, 'journal_entry_count': len(self.entries)}

    def journal_digest(self):
        data = [{'event_id': e.event_id, 'sequence': e.sequence, 'kind': e.kind, 'effective_at': e.effective_at,
                 'currency': e.currency, 'postings': [{'account': p.account, 'debit': display(p.debit), 'credit': display(p.credit)}
                                                     for p in e.postings]} for e in self.entries]
        return hashlib.sha256(canonical(data).encode()).hexdigest()


def true_time_weighted_return(segments: Iterable[tuple[str, str]]) -> Decimal:
    """Each segment has no external flow inside; exact boundary valuations.

    Income and transaction costs are already inside NAV, never removed as
    capital flows. No annualization and no full GIPS compliance claim.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        product, n = D('1'), 0
        for begin, end in segments:
            b = decimal(begin, 'begin_nav', positive=True)
            v = decimal(end, 'end_nav')
            product *= v / b
            n += 1
        if not n:
            raise ValueError('at least one subperiod required')
        return product - 1


def replay_fixture(initial: Mapping, events: list[dict], transport: str = 'historical') -> Ledger:
    """Three transport harnesses, one reducer; not three trading simulators."""
    ledger = Ledger(**initial)
    if transport == 'historical':
        stream = iter(events)
    elif transport == 'paper':
        stream = (json.loads(canonical(e)) for e in events)
    elif transport == 'broker_report_fixture':
        # Model at-least-once delivery after normalization and sequencing.
        stream = (e for original in events for e in (original, deepcopy(original)))
    else:
        raise ValueError('unknown fixture transport')
    for event in stream:
        ledger.apply(event)
    return ledger
