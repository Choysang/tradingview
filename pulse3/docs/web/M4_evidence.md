# W08 web app: page field -> operationId -> response path (M4-E01/E02/E05 support)

Status: implemented against the contract and **synthetic fixtures only** (`pnpm dev:mock`). Nothing here proves M4 exit; it
records which contract field each page element reads, and the gaps found. `meta.*` is `ResponseMeta`; `data.*` the typed payload.

## 1. Mapping

### Shell / every page
| UI element | operationId | Path |
| --- | --- | --- |
| Release bar: id, published time, decision time | getCurrentRelease, getRelease | `data.current_release_id`; `data.published_at`, `data.decision_as_of`, `data.components[]` |
| Follow latest vs pinned | getCurrentRelease | `data.current_release_id` vs URL `?release=` (pin carried in links) |
| Freshness / quality / coverage / exclusion strip | any | `meta.freshness_state`, `meta.quality_status`, `meta.coverage.{expected,observed,missing}_count`, `meta.coverage.unverifiable_reasons[]`, `meta.warnings[]` |
| Synthetic banner | any | `meta.warnings[].code == SYNTHETIC_FIXTURE` (or `VITE_USE_MOCK=1`) |
| Diagnostics drawer | any | `meta.request_id/trace_id/contract_version/release_id/decision_as_of/availability_policy_version/input_snapshot_refs[]` |
| Global data-health pills (stale / abnormal / disabled counts) | getDataStatus | `data.items[].freshness_state/runtime_state/enabled` |
| Update channel, cursor, dup/out-of-order | pollUpdates | `data.stream_epoch`, `data.events[].{event_id,event_type,aggregate_id,aggregate_version}`, `data.resume_cursor`, `data.reset_required` |

### 1 Home
| Element | operationId | Path |
| --- | --- | --- |
| 全市场精选 list (pinned) | getMarketRanking | `data.ranking_id`, `data.release_id`, `data.entries[].{instrument_id,alpha_rank,action_rank,relative_rank_score,calibrated_probability,action_status,plan_ref,explanation_ref}`, `meta.next_cursor` |
| Row name / risk flag | getStockWorkspace | `data.instrument.{display_name,source_symbols[0],is_risk_warning,trading_status}` (gap G1) |
| 板块机会 | getSectorOverview | `data.entries[].{taxonomy_node,rank_kind,rank,score,member_count,observed_member_count,quality_status}` |
| 快速升温: sectors | getSectorOverview (`rank_kind=sector_rising_rank`) | as above |
| 快速升温: pool | getCandidatePool | `data.items[].{event_sequence,instrument_id,taxonomy_node,current_state,trigger_code,plan_ref,available_at}`; pool id from `getRelease.data.components[].component_key = candidate_pool:<id>` (gap G5) |
| 快速升温: components, 缺失 | getRelease | `data.components[component_key=heating.*].component_state` (READY / MISSING / STALE / FAILED; absent = 缺失) |
| 自选 | listWatchlists / saveWatchlist | `data.items[].members[]`; write `WatchlistUpdate` (`expected_watchlist_version`, `Idempotency-Key`) |
| 账户可行动 (pinned) | getStockWorkspace(view=account_holding) then getAccountActionability | `data.actionability_id`; `data.entries[].{account_action_rank,actionability_status,side,reason_codes,checked_quantity_shares}`, `data.valid_until` |

### 2 Sector page
| Element | operationId | Path |
| --- | --- | --- |
| In-sector ranking (pinned) | getSectorRanking | `data.{ranking_id,membership_snapshot_id,taxonomy_node}`, `data.entries[]` as market ranking |
| Heating pool for this sector | getCandidatePool | `data.items[taxonomy_node.node_id == node]` |
| Surge components (7) | getRelease | `heating.<key>` components, see above |

### 3 Stock workbench
| Element | operationId | Path |
| --- | --- | --- |
| Header: identity, release, horizon, plan state | getStockWorkspace, getTradePlan | `data.instrument`, `meta.decision_as_of`, release time via getRelease, `plan.state` |
| Chart bars (分钟/日/周/月/年) | getStockBars | `data.bars[]` (CanonicalBar `*_price_raw`, AdjustedBar `*_price`), `volume_shares`, `is_final`, `quality_status`, `data.window_complete`, `data.adjustment_mode/adjustment_version`; query params derived from workspace `meta.input_snapshot_refs`, `meta.decision_as_of`, `meta.availability_policy_version`, `features[0].key.session_policy_version` |
| Indicator panes MA/EMA/MACD | getStockWorkspace | `data.features[].{key.feature_id,result_state,computed_at}` only (status); no values in v3.0.0 (gap G2) |
| Evidence 消息 | getStockEvidence | `data.documents[]`, `data.claims[]` (`claim_status`, `assertion_type`, `evidence[]`), `meta.next_cursor` |
| Evidence 情绪/社区 | getStockCommunity | `data.observations[]` (`coverage_status`, `like/reply/unique_participant_count`, `analysis_payload`) |
| Evidence 资金 | getStockFundFlow | `data.observations[]` (`measurement_class`, `methodology_id`, `canonical_value`, `missing_reason`) |
| Evidence 龙虎榜席位 | getStockSeats | `data.reports[].rows[]` (`resolution_status`, `canonical_amount_cny`) |
| Trade plan card | getTradePlan | `data.{state,validation,valid_from,expires_at,entry_price_zone,sizing,holding_horizon,trigger_policy,entry_conditions,exit_conditions,invalidation,strategy_refs,supersedes}` |
| TRUE/FALSE/UNKNOWN tree | getTradePlan + getStockBars + getStockQuote | structure from `data.*_conditions`; values looked up from last/previous final raw bar or `quote.last_price_raw` (gap G3) |
| T / grid sub-plans | getStockWorkspace, getTradePlan | `data.plan_refs[]`; kind from registered strategy `method_family` (gap G4) |
| Quote overlay card | getStockQuote (initial: workspace `data.quote_overlay`) | `data.quote.{last_price_raw,bid/ask,source_event_time}`, `data.freshness_state`, `data.freshness_evaluated_at` |
| Strategy contributions + grade | getStockWorkspace, listStrategies | `data.strategy_contributions[]`; grade from `instances[].{lifecycle,reproduction_status,implementation_status}` |
| Layout (timeframe, adjustment, panes, tab) | getLayout / saveLayout | `data.layout_document` (client schema `pulse.web.layout/1`), `expected_layout_version` |
| Notes / horizontal lines | listAnnotations / saveAnnotation | `data.items[].body` (`kind: note|hline`), `anchor_release_id`, `anchor_snapshot_id` |

### 4 Research / data / account
| Element | operationId | Path |
| --- | --- | --- |
| Strategy catalogue | listStrategies | `data.items[].{display_name,role,method_family,instances[]}` |
| League | getLeague | `data.rounds[].pairings[]` (`outcome`, experiment ids) |
| Evaluation | getEvaluation | `data.{run_state,metrics[],context,policy_versions}` |
| Submit experiment | createExperiment, getJobRun, getEvaluation | `ExperimentRequest`; receipt `data.{job_run_id,resource_id,run_state}`; poll `getJobRun.data.run_state` |
| Papers | listPapers | `data.items[].{read_depth,publication_status,primary_url}` |
| Data status | getDataStatus | `data.items[]` all 4 timestamps, `enabled`, `runtime_state`, `freshness_state`, `consecutive_failures` |
| Account | getAccountProjection | `data.{nav_cny,cash_available_cny,unrealized_cny,withdrawable_cash_cny,positions[]}` |

`getModelRun` is implemented in `ops.ts` and mocked but not yet shown in the UI (plan diagnostics list `model_run_id` only). All other
30 operations are called by a page or hook.

## 2. Contract gaps found (no contract change was made; proposals for the integrator)

* **G1** `RankingEntry`, `CandidatePoolItem`, watchlist members carry only `instrument_id`. Names come from one `getStockWorkspace` per visible row (cached). Propose embedding `{display_name, source_symbol, is_risk_warning}` in list entries or a batch label operation.
* **G2** No operation or field carries MA/EMA/MACD (or other indicator) series. The panes show 缺失 and the feature status; the browser does not compute them. Propose indicator columns in `BarWindow` or `getStockIndicators`.
* **G3** `TradePlan` conditions have no evaluation trace. The page previews TRUE/FALSE/UNKNOWN from server-supplied confirmed bars/quote (Kleene logic, unknown stays unknown) and labels it as a preview. Propose a server-side `condition_trace` per node.
* **G4** No explicit plan kind (main / T / grid). Classified from the plan strategy's `method_family`; propose a `plan_kind` field.
* **G5** No list of candidate pools, taxonomies, horizons, accounts or leagues; `pool_id` is parsed from release component keys `candidate_pool:<id>`, the others are deployment config / URL params (see README). Taxonomy nodes also have no display name (node id is shown).
* **G6** `ResponseMeta` for lists has no per-component breakdown of heating (surge) factors; the page reads `heating.*` release components. Propose a typed field on `SectorRankEntry`.
* **G7** No evidence-grade field (已验证/检验中/未检验/已证伪). `lib/grades.ts` maps registry fields conservatively (only reproduced + implemented + validated/active reads 已验证). Propose an explicit field from the strategy registry.
* **G8** No delete operations for annotations/watchlists (extension `DeleteWorkspaceItemCommand` pending); drawings are hidden by a new version.
* `layout_document` / annotation `body` are unregistered objects in the contract; the client uses `pulse.web.layout/1`, `{kind:'note'|'hline'}` until schemas are registered.

## 3. What is synthetic

Every response in `pnpm dev:mock` is synthetic: 12 fictional instruments ("合成·…", `SAMPLE_nnn`), generated prices, rankings, plans,
evidence, strategies, league, papers, source health and an account. `mock:validate` checks 77 mock responses covering all 31
operations against `pulse.contracts.check` (all valid at time of writing). No real data, no alpha claim, no performance claim.
