"""Versioned daily price-limit registry (A01/A02 in the research references).

Why: price limits depend on board, risk-warning status and an effective date;
they must not be scattered strategy constants (execution_standard.md section 3).
Rows: main board 10%, ChiNext/STAR 20%, BSE 30% (task brief). SSE main-board
risk-warning shares were 5% before 2026-07-06 and are 10% from that day (SSE
notices A01/A02, retrieved 2026-11-10); history is not rewritten.
Invariants: lookup is by half-open ``[effective_from, effective_to)`` interval;
overlapping rows are rejected at construction; a combination without a verified
row raises ``RuleNotVerified`` instead of guessing.
Not covered / unverified in the package: SZSE main-board risk-warning band
after 2026-07-06; risk-warning bands on ChiNext/STAR/BSE (registered as the board
ratio with ``verified=False``); IPO no-limit windows; board history before each
board's launch; ex-rights reference prices. ``strict=False`` exposes unverified
rows explicitly for research use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .instruments import Board, Exchange, InstrumentSpec

PRICE_LIMIT_RULES_VERSION = "pulse-price-limits-2026-07-06-v1"
D = Decimal


class RuleNotVerified(LookupError):
    """No verified rule covers this combination; the caller must decide."""


@dataclass(frozen=True)
class PriceLimitRule:
    board: Board
    risk_warning: bool
    ratio: Decimal
    exchange: Exchange | None = None  # None: any exchange
    effective_from: date | None = None
    effective_to: date | None = None  # exclusive
    verified: bool = True
    source: str = ""

    def covers(self, spec: InstrumentSpec, day: date) -> bool:
        return (spec.board is self.board and spec.is_risk_warning == self.risk_warning
                and (self.exchange is None or spec.exchange is self.exchange)
                and (self.effective_from is None or day >= self.effective_from)
                and (self.effective_to is None or day < self.effective_to))


@dataclass(frozen=True)
class PriceBand:
    limit_up: Decimal
    limit_down: Decimal
    ratio: Decimal
    rule_version: str


_SWITCH = date(2026, 7, 6)
DEFAULT_RULES: tuple[PriceLimitRule, ...] = (
    PriceLimitRule(Board.MAIN, False, D("0.10"), source="task brief: main board 10%"),
    PriceLimitRule(Board.CHINEXT, False, D("0.20"), source="task brief: ChiNext 20%"),
    PriceLimitRule(Board.STAR, False, D("0.20"), source="task brief: STAR 20%"),
    PriceLimitRule(Board.BSE, False, D("0.30"), source="task brief: BSE 30%"),
    PriceLimitRule(Board.MAIN, True, D("0.05"), Exchange.SSE, None, _SWITCH,
                   source="SSE notices A01/A02: main-board risk warning 5% before 2026-07-06"),
    PriceLimitRule(Board.MAIN, True, D("0.10"), Exchange.SSE, _SWITCH, None,
                   source="SSE notices A01/A02: main-board risk warning 10% from 2026-07-06"),
    PriceLimitRule(Board.MAIN, True, D("0.05"), Exchange.SZSE, None, _SWITCH, verified=False,
                   source="not in package: SZSE main-board risk warning before 2026-07-06"),
    PriceLimitRule(Board.CHINEXT, True, D("0.20"), verified=False,
                   source="assumption: no risk-warning-specific band on ChiNext"),
    PriceLimitRule(Board.STAR, True, D("0.20"), verified=False,
                   source="assumption: no risk-warning-specific band on STAR"),
    PriceLimitRule(Board.BSE, True, D("0.30"), verified=False,
                   source="assumption: no risk-warning-specific band on BSE"),
)


class PriceLimitRegistry:
    def __init__(self, rules: tuple[PriceLimitRule, ...] = DEFAULT_RULES,
                 version: str = PRICE_LIMIT_RULES_VERSION):
        self.rules, self.version = rules, version
        self._check_no_overlap()

    def _check_no_overlap(self) -> None:
        lo, hi = date.min, date.max
        for i, a in enumerate(self.rules):
            for b in self.rules[i + 1:]:
                if (a.board, a.risk_warning) != (b.board, b.risk_warning):
                    continue
                if a.exchange and b.exchange and a.exchange is not b.exchange:
                    continue
                if ((a.effective_from or lo) < (b.effective_to or hi)
                        and (b.effective_from or lo) < (a.effective_to or hi)):
                    raise ValueError(f"overlapping price-limit rows: {a} / {b}")

    def rule_for(self, spec: InstrumentSpec, day: date, *, strict: bool = True) -> PriceLimitRule:
        spec.require_ordinary_stock()
        hits = [r for r in self.rules if r.covers(spec, day)]
        if not hits:
            raise RuleNotVerified(f"no price-limit rule for {spec.exchange.value}/{spec.board.value} "
                                  f"risk_warning={spec.is_risk_warning} on {day}")
        rule = hits[0]
        if strict and not rule.verified:
            raise RuleNotVerified(f"price-limit rule unverified ({rule.source}); pass strict=False to use it")
        return rule

    def band(self, spec: InstrumentSpec, day: date, prev_close: Decimal, *, strict: bool = True) -> PriceBand:
        """Limit-up/down prices: previous close x (1 +/- ratio), rounded half-up to the tick."""
        if not isinstance(prev_close, Decimal) or prev_close <= 0:
            raise ValueError("prev_close must be a positive Decimal")
        rule = self.rule_for(spec, day, strict=strict)
        tick = spec.tick_size
        up = (prev_close * (1 + rule.ratio) / tick).to_integral_value(ROUND_HALF_UP) * tick
        down = (prev_close * (1 - rule.ratio) / tick).to_integral_value(ROUND_HALF_UP) * tick
        return PriceBand(up, down, rule.ratio, self.version)
