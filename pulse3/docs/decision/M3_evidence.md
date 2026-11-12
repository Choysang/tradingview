# M3 decision package evidence (W05 ranking + sectors, W06 plans + portfolio, W09 strategy registry + research league)

Scope: backend pure domain, PostgreSQL repositories and thin application services. Everything here is
**synthetic / fixture-driven**: it proves state machines, invariants, contract shape and persistence,
not market edge. No strategy is `active`; no probability is claimed (`calibrated_probability` is null
unless a caller supplies a calibrated value).

## Commands and exit codes (run 2026-11-11, PostgreSQL 18.3 at 127.0.0.1:55432)

| Command (from `pulse3/`) | Result | Exit |
| --- | --- | --- |
| `uv run pytest tests/decision -q` | 102 passed, 18 subtests | 0 |
| `uv run pytest -q --ignore=tests/db` (whole repo except another package's tests that import `w02_helpers`) | 283 passed | 0 |
| `uv run --with coverage python -m coverage run --branch -m pytest tests/decision` + `report` | 91 % line+branch over the 12 decision modules/packages (lowest: universe.py 83 %) | 0 |
| `uv run --with pyflakes python -m pyflakes <decision paths>` | no findings | 0 |
| `uv run python -m tests.decision.build_fixtures` | 6 fixtures written to `fixtures/decision/`, byte-identical on re-run | 0 |

`uv run pytest -q` over the *whole* repo currently stops at collection in `tests/db/*` (7 errors,
`ModuleNotFoundError: w02_helpers`), which is the DB package's test layout and not this package.

## Evidence map

Test files are under `tests/decision/`. Contract checks use `pulse.contracts.check(<$defs name>, payload)`.

### M3-E01 shared chain: market ranking, sector ranking, fast-heating pool, deterministic re-run, tie/missing
| Requirement | Test |
| --- | --- |
| Explicit single target; group normalisation inside the universe; direction | `test_ranking.py::test_group_normalization_and_direction` |
| Default universe (main board SH/SZ, price <= 100, circ. cap <= 100e8, new listing, unknown never passes); preference versioned apart from rules | `test_default_universe_excludes_with_reasons`, `test_preference_is_versioned_separately_from_rules` |
| Same-source / correlated feature de-duplication | `test_dedup_same_source_and_correlated` |
| Missing feature => row excluded with reason, no per-row re-weighting, one fixed weight vector | `test_missing_feature_excludes_row_no_reweight` |
| Deterministic output, unique alpha_rank 1..N, ties by instrument id, contract `Ranking` and `getMarketRankingResponse` | `test_ranking_contract_and_rank_invariants` |
| alpha_rank vs action_rank (blocked top research rank keeps alpha) | `test_alpha_rank_vs_action_rank` |
| Frozen ranking pinned to ranking_id/release_id; stable cursor; foreign cursor refused | `test_pagination_stable_and_pinned`; PG: `test_pg_decision.py::test_ranking_roundtrip_pagination_and_published_immutability` |
| Sector opportunity overview (`SectorOverview`), partial quality, zero-observed excluded, taxonomies not merged, overlap kept and reported, in-sector rankings | `test_sectors.py::test_sector_opportunity_overview_contract_and_partial_quality`, `test_taxonomies_are_not_merged`, `test_overlap_kept_and_reported`, `test_in_sector_stock_ranking_and_overlap_in_two_sector_rankings` |
| Membership only after its available_at | `test_m3_extras.py::test_membership_known_only_after_decision_time_is_excluded` |
| Surge components (7, each missing shown not 0), hysteresis, dwell, hard invalidation, candidate_pool_event history, `CandidatePool` contract | `test_sectors.py::test_surge_components_missing_is_shown_not_zero`, `test_pool_hysteresis_dwell_hard_invalidation_and_history`, `test_hard_invalidation_overrides_dwell_and_actionable_needs_plan`; PG: `test_candidate_pool_history_idempotent_and_cas`, `test_sector_overview_and_in_sector_rankings_overlap` |

### M3-E02 baseline, trial budget, fallback
| Requirement | Test |
| --- | --- |
| Port of `reference_transition/test_continuity.py` (13 cases + 18 subtests): 0.10 stays 0.10, residual -> baseline, never renormalised, baseline outage raises | `test_continuity.py` |
| Ineligible candidate (incomplete coverage / wrong score scale) returns its share to baseline; cap 0.10 enforced | `test_ranking.py::test_trial_budget_fusion_and_ineligible_return_to_baseline` |
| Baseline invalid or coverage thin => `RankingBlocked`; nothing stored/registered | `test_baseline_invalid_blocks_ready_even_with_candidate`, `test_coverage_below_policy_blocks`; PG: `test_ranking_service_stores_registers_component_and_blocks_without_side_effects` |
| A stale ranking cannot seed a new plan | `test_m3_extras.py::test_stale_ranking_cannot_seed_a_new_plan` |

### M3-E03 conditional plans, account capability, validation trail
| Requirement | Test |
| --- | --- |
| TRUE/FALSE/UNKNOWN, missing MACD is UNKNOWN not 0, bar-close needs confirmed bar, Kleene all/any/not, cross needs two confirmed points, session/event nodes, unit mismatch is an error | `test_plans.py::test_missing_value_is_unknown_not_zero` .. `test_session_event_nodes_and_unit_check` |
| TriggerPolicy: single, repeat, cooldown, per-day count, final bar, pending-order block / net delta | `test_trigger_*`, `test_single_fire_never_rearms`, `test_rearm_after_false_cooldown_and_per_day_count`, `test_pending_order_policies` |
| PlanCompiler from role outputs (`TradePlan`, `getTradePlanResponse`); actionable only with passed validation and admitted strategies | `test_compile_*`, `test_actionable_needs_passed_validation_and_admitted_strategies` |
| Version append-only, supersedes chain, late slow result cannot overwrite a newer plan; SQL immutability | `test_versions_append_only_and_supersede_chain`, `test_slow_model_result_cannot_overwrite_newer_plan`; PG: `test_plan_versions_append_only_supersede_and_late_results` |
| T plan (sell leg, buy-back leg, leftover, expiry; buy-back never restores sellable) and grid plan (anchor, step, levels, max inventory, cash budget, re-anchor = new version, gap / multi-level) | `test_t_plan_legs_leftover_and_sellable_accounting`, `test_grid_*` |
| PortfolioCoordinator: one account, sleeves attribution only, shared sellable inventory, declared priority, empty position does not block a buy, no same-cycle proceeds reuse | `test_portfolio.py::test_empty_position_*`, `test_shared_sellable_inventory_*`, `test_sleeves_are_attribution_only_*`, `test_exit_before_new_buy_*` |
| Unapproved predictive veto/trim is shadow only | `test_m3_extras.py::test_unapproved_predictive_policy_is_shadow_only` |
| ActionabilitySnapshot bound to release/account_version/quote/policy/validity; buy cash+permission, sell sellable, T legs separate, unknown stays unknown; `ActionabilitySnapshot`, `getAccountActionabilityResponse`; PG guards (quote READY at checked_at, immutable) | `test_portfolio.py::test_buy_checks_*`, `test_sell_needs_*`, `test_snapshot_binding_*`; PG `test_actionability_snapshot_binding_and_immutability` |

### M3-E04 registry, experiments, pairing, results (and M0-E04)
| Requirement | Test |
| --- | --- |
| 61 strategy + 105 factor candidates seeded as `discovered / not_implemented`, none active, idempotent, tags in versioned taxonomies | `test_strategy_research.py::test_seed_catalogs_are_candidates_only`; PG `test_seed_registry_counts_candidates_and_idempotent` (166 `discovered`) |
| `Strategy`/`StrategyList` contract | `test_strategy_wire_passes_contract` |
| EvaluationContext fixes snapshots, availability, universe, policies, benchmarks, budget, intervals; hash changes with any of them; `EvaluationContext` contract | `test_context_hash_fixes_everything_and_wire_is_valid`; PG `test_context_trials_blind_budget_and_failures_recorded` (M0-E04) |
| All trials recorded incl. failures; blind budget; `Evaluation` contract | `test_all_trials_recorded_including_failures_and_blind_budget`, `test_evaluation_wire_passes_contract`; PG same test |
| Swiss pairing only in comparable groups, saved seed, opponent cap, bye without points, duplicate identities merged | `test_pairing_only_inside_comparable_groups_and_seed_saved`, `test_bye_handling_no_points_and_rotation`, `test_opponent_repeat_cap_and_duplicate_identities_merged` |
| Results win/loss/draw/insufficient_evidence/pending/execution_failed/incomparable; no inflation of small differences; `League` contract | `test_result_classification_never_inflates_small_differences`, `test_league_wire_contract`; PG `test_league_rounds_seed_bye_results_and_contract` |

### M3-E05 promotion, pause, retire, lineage, increment
| Requirement | Test |
| --- | --- |
| Lifecycle discovered->...->active, no skipping, paused/retired paths, revival needs a reference | `test_lifecycle_chain_pause_retire_revive`; PG `test_lifecycle_cas_and_legality` |
| Versioned PromotionPolicy; unknown evidence blocks; league has no input to promotion | `test_promotion_requires_all_gates_unknown_blocks`, `test_league_rank_never_enters_promotion` |
| Demotion needs data-outage and cost-change ruled out | `test_demotion_needs_ruling_out_data_and_cost_causes` |
| promotion_decision events with old/new state, gates and reason; apply = CAS once; rejected cannot apply | PG `test_promotion_decision_apply_and_league_independence` |
| Composite = new StrategyVersion with parents/weights/routing/constraints; marginal increment only from unified-account rerun; correlated families | `test_m3_extras.py::test_composite_is_a_new_version_*`, `test_correlated_families_*`; PG `test_composite_lineage_is_stored_and_immutable` |

Fixture-sample verification only; real promotion needs registered out-of-sample, cost/execution sensitivity
and forward evidence (M5).

## Contract definitions exercised

`Ranking`, `SectorOverview`, `CandidatePool`, `TradePlan`, `TriggerPolicy`, `ActionabilitySnapshot`, `Strategy`,
`StrategyList`, `EvaluationContext`, `Evaluation`, `League`, and the envelopes `getMarketRankingResponse`,
`getCandidatePoolResponse`, `getTradePlanResponse`, `getAccountActionabilityResponse`, `listStrategiesResponse`,
`getEvaluationResponse`, `getLeagueResponse`. Committed samples: `fixtures/decision/*.json` (`test_fixtures.py`).

Contract names differ from the task wording: there is no `SectorRanking` def (sector stock ranking is `Ranking`
with `ranking_scope=sector`; the sector-vs-sector ranking is `SectorOverview`), `StrategyDefinition` is `Strategy`,
and **there is no `PromotionDecision` def** (the decision is persisted in `research.promotion_decision`; not an API
payload yet).

## Not done / only synthetic
* No real feature matrix, membership, quote or account: ports (`FeatureMatrixPort`, `ReleaseComponentWriter`,
  `AccountSnapshotPort`) are Protocols; tests use fakes and direct-SQL prerequisite rows (`tests/decision/pg_seed.py`).
* Explanation bodies are content-addressed (`explain:<sha256>`) and returned, but no artifact store persists them.
* ActionabilitySnapshot entries and T/grid bodies have no table: only hash refs are stored (proposal SQL below).
* Rank-change attribution (new facts vs revisions vs weights vs account) is not implemented.
* Surge thresholds, universe thresholds and promotion thresholds are test parameters, not calibrated values.
* Condition runner feeds on an `EvalContext` port; no feature-store binding yet.

## Proposed schema/contract changes (not applied; verified to apply cleanly in a rolled-back transaction)
* `migrations/proposals/decision_plan_body_and_actionability_result.sql`: `decision.plan_body`, `decision.actionability_result`.
* `migrations/proposals/decision_strategy_lifecycle_event_and_pairing_enum.sql`: `strategy.lifecycle_event` (history for
  steps without an experiment) and `insufficient_evidence` / `execution_failed` in `research.league_pairing.outcome`
  (+ the same enum values in the `League` contract). Until then they are stored as `pending` with a reason prefix.
* Contract: add a `PromotionDecision` def if promotions become an API payload.
