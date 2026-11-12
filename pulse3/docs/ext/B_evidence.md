# Extension group B - evidence

Scope: PEX10 (screening), PEX04 (analogs, interval statistics), PEX06 (tactic replay), PEX11 (strategy draft / version),
plus the old-site features (pattern radar, factor lab, 52 playbooks, exit lab).
Nothing here is committed. Contract data files are untouched.

## 1. Card acceptance -> proving test

Backend tests: `tests/ext_b/` (52 tests, real PG via `pg_dsn`, responses checked by `respond_ext`).
Web tests: `apps/web/tests/ext_b_pages.test.tsx` (11 tests).

| Card / requirement | Proof |
|---|---|
| PEX10 parseScreenText: deterministic, unmatched fragments flagged, never a stock list | `test_screening_domain.py::test_sentence_becomes_typed_ast_and_passes_the_contract`, `::test_unmatched_fragments_are_flagged_not_dropped`, `::test_or_groups_ranges_and_sort`, `::test_parser_never_returns_a_stock_list_or_executes_anything`; API: `test_pg_api.py::test_parse_endpoint_returns_flagged_fragments_and_contract_valid_ast` |
| PEX10 ScreenAst validation (no scripts, unknown fields, units, budget) | `::test_ast_validation_rejects_scripts_unknown_fields_units_and_budget` |
| PEX10 runScreen: frozen scan, equals direct evaluation, idempotent, changed payload / stale input detected | `test_pg_api.py::test_run_screen_freezes_a_scan_and_matches_direct_evaluation`, `::test_run_screen_is_idempotent_and_detects_changed_payload_and_stale_input` |
| Missing is never 0 / false (Kleene three-valued) | `::test_missing_value_is_unknown_never_zero_and_kleene_logic_holds`, `test_pg_api.py::test_missing_history_gives_unknown_rows_not_false_and_not_zero`, web: runs a screen ... shows unknown rows as 未知 |
| D13 research.screen_run immutable after close, owner scoped, cursor bound, CSV whole run | `::test_closed_run_rows_are_immutable_in_the_database`, `::test_run_is_owner_scoped_cursor_bound_and_csv_exports_whole_run` |
| D12 saveScreen / listSavedScreens (CAS, owner) | `::test_save_screen_cas_owner_and_listing` |
| Stable sort, explicit nulls, instrument tiebreak | `::test_stable_sort_with_explicit_nulls_and_instrument_tiebreak` |
| 5 presets parse completely, catalog fields only | `::test_presets_all_parse_completely_and_use_catalog_fields_only` |
| Pattern radar: 12 rules, PIT, insufficient history is unknown | `::test_twelve_rules_with_original_semantics_documented`, `::test_every_pattern_is_point_in_time`, `::test_turtle_breakout_and_insufficient_history_is_unknown_not_no`, `::test_ma_bull_volume_surge_and_macd_cross_fire_on_constructed_data` |
| Factor lab: 6 factors, rank IC exact, windows never cross slice end | `::test_six_factors_registered`, `::test_rank_ic_is_exact_on_a_perfect_factor_and_missing_on_thin_cross_sections`, `::test_forward_windows_never_cross_the_slice_end`, `test_pg_api.py::test_factor_lab_metrics_are_stored_per_slice_and_horizon` |
| BOLL / KDJ (and roc, hhv, llv, ret_std, up_streak, limit_*) kernels | `test_analog_interval_kernels.py::test_boll_matches_hand_values_and_population_std`, `::test_kdj_seed_recursion_and_reset_after_gap`, `::test_new_kernels_are_registered_and_keys_pass_the_contract`, `::test_limit_streak_kernel_counts_consecutive_limit_closes` |
| 52 playbooks in 8 families, registered as strategy definitions, lifecycle specified, label 未检验 | `test_library_pit.py::test_registry_has_52_playbooks_in_8_families`, `test_pg_api.py::test_register_all_is_idempotent_52_playbooks_6_factors_none_active` |
| Playbook no-look-ahead (truncation equals prefix) | `test_library_pit.py::test_no_look_ahead_truncation_equals_prefix` |
| Missing-data playbooks are failed trials with reason, not skipped | `test_library_pit.py::test_unavailable_playbooks_declare_why_and_return_nothing`, `test_pg_api.py::test_missing_data_playbooks_are_recorded_failed_trials_with_the_reason` |
| Experiment path: EvaluationContext + ExecutionKernel + user_account_2026 costs + x2/x3 + benchmark; blind slice never read | `test_pg_api.py::test_playbook_screening_goes_through_context_kernel_costs_and_never_touches_blind`, `test_replay.py::*` (ledger balances, higher cost never improves, one-word limit-up not assumed fillable) |
| getEvaluation serves experiments in v3 contract | `test_pg_api.py::test_get_evaluation_serves_the_experiment_with_the_v3_contract` |
| PEX11 registerStrategyDraft / queryStrategyVersion (D14, CAS, duplicates, visibility) | `test_pg_api.py::test_register_strategy_draft_versions_cas_duplicates_and_visibility`, `::test_screen_can_use_a_research_strategy_signal_without_changing_anything_else` |
| PEX06 scheduleTacticReplay: exit rules vs same-inventory hold baseline, all variants returned | `test_tactics.py::test_exit_rule_is_measured_against_hold_with_exact_bridge`, `::test_all_exit_variants_are_returned_not_just_the_best`, `test_pg_api.py::test_tactic_replay_exit_rules_vs_hold_baseline_with_full_family` |
| Intraday T forward/reverse: unknown path cannot complete, declared paths show spread | `test_tactics.py::test_intraday_t_unknown_path_cannot_complete_but_declared_paths_report_a_spread`, `test_pg_api.py::test_tactic_replay_intraday_t_reports_path_sensitivity` |
| Tactic replay rejections (D07 no_fit unsupported) | `test_pg_api.py::test_tactic_replay_rejections` |
| PEX04 queryAnalogCases: de-overlap, outcomes never used for selection, no future leakage, query never builds | `test_analog_interval_kernels.py::test_selection_ignores_outcomes_and_dedups_same_stock_cases`, `::test_adding_future_bars_does_not_change_old_neighbours_and_immature_outcomes_are_none`, `test_pg_api.py::test_analog_study_is_built_by_a_job_and_queries_never_build` |
| PEX04 queryIntervalStatistics: hand-calculated, missing carries a reason, no implicit adjustment | `::test_interval_metrics_hand_calculated_and_missing_has_a_reason`, `test_pg_api.py::test_interval_statistics_match_hand_calculation_and_refuse_implicit_adjustment` |
| Web catalog.json in sync with code | `test_library_pit.py::test_web_catalog_json_is_in_sync_with_the_code`, web: catalog carries 12 patterns, 6 factors, 5 presets and 52 playbooks |
| Frontend (Chinese): screen / playbooks / lab | web tests: AST nodes + Chinese description, rail -> condition, chips + unmatched flag, run + poll + 未知, failed run is error state, 52 playbooks 未检验 + failures shown, radar honest zero counts, outcome summary not a probability |

## 2. Real-data screening pass (dev DB, `postgresql://postgres@127.0.0.1:55432/pulse`)

Command: `uv run python -m pulse.workers.research ext-b-run` (exit 0). Purpose `screening`. 61 experiment runs in
`research.experiment_run`:

- completed: 48 = 42 playbooks (with trades) + 6 factors.
- failed: 13, recorded, not hidden:
  - `data_not_available:float_cap_history_not_point_in_time` 5 (only a single latest cap snapshot exists; using it for history would look ahead)
  - `data_not_available:lhb_seat_trades_history` 2
  - `data_not_available:sector_membership_history` 2
  - `data_not_available:instrument_name_history` 1 (ST history)
  - `data_not_available:fundamentals_pit` 1
  - `strategy_not_implemented` 1 (earlier trial)
  - `evaluator_crash_before_error_capture` 1 (earlier crash; fixed by wrapping evaluator errors into `EvaluationUnavailable`, the failed row is kept as evidence)

Headline (net of `user_account_2026` costs, overall slice, forward-adjusted research prices, about 1 year of history):

- Playbooks net_return > 0: 3 of 42. Median net_return -14.8 percent. Range -59.5 to +46.1 percent.
- Beat CSI300 (overall): 4 of 42. Validation slice: 13 of 42 beat the benchmark.
- Still positive at cost x3: 3 of 42 (overall), 7 of 42 (validation).
- Factors (rank IC mean, average over 6): h5 train +0.006, validation -0.023; h20 train +0.014, validation -0.082.
  Sign flips between train and validation. No factor is supported by this pass. `icir` overall slice has no value (not computed, shown as missing).
- Conclusion: no playbook or factor is promoted. All remain lifecycle `specified`, label 未检验. Old backtest numbers are not carried over.

## 3. Commands and exit codes (final run)

- `uv run pytest tests/ext_b -q`: 52 passed.
- `uv run pytest tests/db/test_roles.py -q`: 6 passed (count updated to 104, see gaps).
- `uv run pytest -q` (full): exit 1, 716 passed, 1 failed, 48 subtests passed. The failure is group C's `tests/ext_c/test_pg_plans.py::test_propose_is_async_idempotent_and_compiles_into_a_separate_user_chain` (its `drain()` got 13 failed ops jobs instead of 1 succeeded). It passes alone and with all of `tests/ext_c` (65 passed), so it depends on queued ops jobs left by earlier tests in the full run. `ops_service.run_next` only claims its own group's job definitions, so ext_b job ids are not involved (not proven by a bisect).
- `cd apps/web && npx tsc --noEmit`: exit 0.
- `pnpm test`: exit 0.
- `pnpm build`: exit 0.

## 4. Gaps and synthetic parts

- Tests use a synthetic world (I1 fixture); real-data evidence is only the screening pass above.
- Prices are the legacy forward-adjusted series (research only, quality `partial`; metric `price_basis_forward_adjusted_research_only`).
- Only about 1 year of history: 250-day and monthly rules are mostly unknown (reported as unknown, not no).
- Playbooks needing float cap, fundamentals, sector membership, name history or seat trades fail with `data_not_available`.
- ST is not excluded inside playbooks (no name history).
- Analog study needs a worker run (`analog-study`); queries return RESULT_NOT_READY until it exists.
- Tactic replay `no_fit` context (D07) unsupported and rejected. Sale proceeds are not reusable on the same day. Intraday T needs declared OHLC/OLHC path scenarios.
- Feature results are not registered in `market.feature_result`; the screen panel is cached in process only.
- Scopes (`screen:*` etc.) map to the authenticated principal only.
- `tests/db/test_roles.py` expected table count changed 87 -> 104 (integrator-owned file; counts group A/B/C tables). Grants themselves were already in migration 0030.

## 5. Proposed contract / schema changes (not applied)

1. TypedFeature has no inline value. Values ride in the artifact ref as `screen-value://run/instrument/field?value=..&unit=..` (or `missing=..`). Add an inline value field.
2. Register the `pulse.screen_feature_value` row contract.
3. Add a supplementary ops contract for `/v1/x/b/*` reads (context, instrument-labels, analog-key, screen-run-csv).
4. Define the D07 no_fit context for tactic replay.
5. Define analog query-key semantics (policy `:hN` suffix, matured vs all-prior).
