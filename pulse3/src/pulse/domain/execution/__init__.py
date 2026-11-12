"""One economic ledger and execution kernel for Pulse v3 (work package W07).

Why: FINAL_SYSTEM_SPEC section 8 - backtest replay, online paper and normalized
broker reports share one ExecutionKernel and one LedgerReducer. Pure domain code.
Invariants and non-coverage: see ledger.py, kernel.py, matcher.py, entrypoints.py.
"""
from .comparison import HoldComparison, compare_with_hold, passive_events
from .entrypoints import (BacktestReplay, BrokerReportAdapter, NullStrategy, PaperSession, RunLog,
                          process_bar, replay_events, replay_fixture)
from .events import ExecutionReport, IngestResult, OrderIntent, SubmitResult, make_event
from .kernel import KERNEL_VERSION, ExecutionKernel
from .ledger import Entry, Holding, Ledger, LedgerReducer, Lot, Order, Posting
from .legs import Leg, LegPlan, LegPlanStrategy, PlanReport
from .matcher import (NO_FILL_QUEUE, Bar, BarMatcher, DailyBarMatcher, MatchOrder, MatchResult,
                      MinuteBarMatcher, NoFill, PathScenario, ProposedFill, QueueScenario)
from .numeric import MARKET_TIMEZONE, NUMERIC_POLICY_VERSION, POLICY_VERSION
from .performance import true_time_weighted_return, twr_by_flow_segments

__all__ = [n for n in dir() if not n.startswith("_")]
