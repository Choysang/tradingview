# M2 evidence (work package W07: execution kernel and economic ledger)

Environment: Windows 11, Python 3.13.15, `uv run` from `pulse3/`, PostgreSQL 18.3 at
`127.0.0.1:55432` (fresh migrated database per pytest session via the `pg_dsn` fixture;
migrations 0001-0004 are the package DDL, 0002 = `execution/ledger_schema.sql`).
All data are synthetic (golden fixtures and hand-built bars). Nothing here claims
profitability, broker/exchange fidelity or a complete cost schedule. Not committed.

Code: `src/pulse/domain/execution/` (ledger, kernel, matcher, legs, entrypoints,
comparison, performance), `src/pulse/domain/market_rules/`,
`src/pulse/adapters/postgres/ledger_repo.py`, `src/pulse/application/execution_service.py`.
Reference port: `ledger.py` is `spec_package/execution/unified_ledger.py` (behaviour identical;
additions are read-only accessors and non-economic lot metadata). The 43 reference unit tests
run unchanged in `tests/execution/test_ledger_reference_port.py` against a byte-identical copy
of `golden_fixtures.json` (checked by sha256 in the E01 suite).

## Commands and exit codes (run 2026-11-11)

| Command (from `pulse3/`) | Exit | Result |
|---|---|---|
| `uv run pytest tests/execution/test_ledger_reference_port.py tests/execution/test_m2_e01_entry_points.py -q` | 0 | 46 passed, 30 subtests |
| `uv run pytest tests/execution/test_m2_e02_ledger_repo_pg.py -q` | 0 | 20 passed (real PG; repeated 3x, all green) |
| `uv run pytest tests/execution/test_m2_e03_rules_and_unfillable.py -q` | 0 | 13 passed |
| `uv run pytest tests/execution/test_m2_e04_costs_and_twr.py -q` | 0 | 9 passed |
| `uv run pytest tests/execution/test_m2_e05_hold_comparison.py -q` | 0 | 9 passed |
| `uv run pytest tests/execution -q` | 0 | 98 passed, 30 subtests |

Whole-repo `uv run pytest -q` at the same time: 221 passed, 4 failed in `tests/db/test_jobs.py`
(another work package); those 4 pass when that file runs alone (11 passed), none touches W07 code.

## Evidence IDs

### M2-E01 one deterministic kernel behind every entry point
- `test_m2_e01_three_entry_points_produce_identical_canonical_events_and_ledgers`: a scripted two-day
  scenario (limit buy/sell, partial T+1 inventory, settlement, next-day sell) is run through
  `BacktestReplay`, `PaperSession` (bar by bar) and `BrokerReportAdapter` (actual fills, delivered twice);
  canonical event streams, `journal_digest` and snapshots are identical.
- `test_m2_e01_golden_events_identical_across_reference_transports_and_kernel_apply`: all 10 golden cases give one digest across historical / paper / broker-redelivery harnesses and `ExecutionKernel.apply_event`.
- `test_m2_e01_golden_fixture_copy_is_byte_identical_to_the_package`.
- Golden hand calculations + independent cash/share identity + atomic-failure tests: the 43 ported reference tests (`test_ledger_reference_port.py`).

### M2-E02 concurrent reservation, partial fill, cancel, correction (real PostgreSQL)
- Concurrency: `test_two_strategies_cannot_spend_the_same_cash`, `test_two_strategies_cannot_sell_the_same_inventory`,
  `test_stale_expected_version_race_lets_exactly_one_writer_win`, `test_many_parallel_buys_never_over_reserve` (8 threads, 5 fit, reservations never exceed cash).
- Idempotency/conflict: `test_duplicate_fill_insert_is_idempotent_and_changed_redelivery_is_a_conflict` (no new rows on redelivery, `EventConflict` on changed content, reducer rejects same fill under a new envelope, DB `UNIQUE(account_id, source_id, source_event_id)` fires).
- Replay: `test_golden_cases_persist_replay_identically_and_journal_balances_in_sql[B01..B10]` (rebuild from stored events equals the in-memory ledger; projection tables dropped and re-derived are identical incl. `applied_sequence`; SQL checks: every entry debit=credit, `trial_balance` sums to 0 and equals reducer balances; position rows equal reducer state).
- Version/CAS/atomicity: `test_stale_expected_version_is_a_conflict_and_version_is_monotonic`, `test_failed_persistence_leaves_no_partial_account_state`; immutability: `test_history_is_immutable`.
- Partial fill / cancel / correction trail: `test_partial_fill_cancel_confirm_releases_only_the_remainder_and_correction_references_the_fill` (cancel without cumulative fills is refused, confirm releases only the remainder, fee correction is appended with `correction_of_event_id` -> original fill event; prior journal untouched). Lots: `test_lots_record_t_plus_1_eligibility`.
- Scope limit: correction support is the reference's `fee.cash_adjustment`; a general trade bust/restatement is explicitly unsupported (rejected atomically), as in the package.

### M2-E03 trading day, T+1, settlement, corporate actions, non-fillable states
- Versioned rules: `test_price_limits_by_board_and_effective_date`, `test_sse_main_board_risk_warning_5_percent_before_and_10_percent_from_2026_07_06`, `test_unverified_combinations_raise_instead_of_guessing`, `test_kernel_rejects_price_outside_the_versioned_band_and_missing_reference`, `test_lot_sizes`.
- T+1/settlement/dividends: `test_t_plus_1_buy_is_not_sellable_until_the_next_trading_day_and_skips_non_trading_days`, `test_intraday_t_buying_back_does_not_restore_used_sellable_quantity`, `test_dividend_payment_after_session_change_is_asset_conversion_not_second_income` (+ golden B01/B05/B06).
- Unknown queue / suspension / capacity / no forced liquidation: `test_limit_up_queue_is_never_assumed_filled_and_explicit_scenarios_are_recorded`, `test_limit_down_queue_blocks_sells_and_zero_range_bar_without_limits_is_treated_as_possibly_locked`, `test_suspension_and_untouched_limits_produce_no_fills_with_reasons`, `test_volume_cap_gives_partial_fill_in_lot_steps_shared_across_orders_on_the_bar`, `test_delisting_style_unsold_inventory_is_kept_not_force_liquidated` (inventory kept, no fabricated cash, missing mark raises instead of valuing 0).

### M2-E04 cost profile, weighted-average authority, external-flow TWR
- `test_cumulative_minimum_fee_is_charged_once_across_partial_fills_golden_b09`, `test_unknown_fee_components_are_flagged_never_booked_as_zero_silently`, `test_kernel_reports_incomplete_fee_status_and_books_only_the_known_part`, `test_commission_that_includes_exchange_fee_does_not_double_count`, `test_fill_granularity_cannot_change_total_fee_and_rounding_tail_reconciles`, `test_cost_profile_registry_resolves_by_effective_date`.
- `test_realized_pnl_is_weighted_average_gross_lots_only_decide_eligibility` (average-cost release 1033.33 per 100 of 1200 shares, not the FIFO 1000).
- `test_external_flow_twr_matches_golden_b07_and_flows_are_not_income` (0.21 by flow-cut segments; deposit moves NAV, not P&L), `test_withdrawal_and_empty_account_segments`.
- Cost profile actually adopted: `a_share_documented_partial` has only the documented exchange handling fee 0.00341% per side (EXS09/EXS10). Commission, stamp duty, transfer and regulatory fees are NOT in the package and stay `unknown` (flagged `incomplete`). `synthetic_fixed_fee` (5 CNY per order) reproduces the golden fixtures and is synthetic. Rounding 0.01 CNY half-even is an engineering decision, unverified against any statement.

### M2-E05 holding baseline vs T / grid
- `test_golden_increment_equals_active_minus_hold_with_exact_bridge[B03,B04,B08]` (fixture `expected_increment` reproduced; NAV difference bridged exactly into cash and inventory-value deltas), `test_golden_b03_unfinished_sell_keeps_the_inventory_shortfall_in_the_result` (-305).
- Through kernel + minute matcher: `test_completed_t_round_trip_is_measured_net_of_fees_against_hold` (+70 = 80 - 2x5), `test_unfinished_t_leg_is_reported_and_priced_at_the_terminal_mark` (-245 at terminal 12, shortfall 200), `test_same_bar_round_trip_needs_a_declared_path_and_the_path_changes_the_answer`, `test_grid_chain_with_incomplete_last_leg_is_reported`, `test_dividend_streams_require_an_explicit_baseline`.

## Known limits (honest status)
- Matchers are explicit scenario models (default 10% bar-volume participation, a declared parameter), not exchange queue simulations; no auctions, latency or impact.
- Lot sizes and ST bands other than SSE main board are conventional/unverified and flagged in docstrings; SZSE main-board ST after 2026-07-06 deliberately raises `RuleNotVerified`.
- `WeekdayOnlyCalendar` is synthetic; production needs the CalendarService output via `ExplicitCalendar`.
- Fee status (`complete`/`incomplete`/`actual`) is returned by `ingest_execution` but has no column in `execution.*`; only the cost policy version is persisted per event.
- Not implemented (package pending items too): trade busts and late restatement, bonus-share listing policy, raw broker fact storage/reconciliation of missing fills, outbox, margin, multi-currency.
