# I3 evidence: intelligence (news claims, origins, community, seats, fund flow, model runs)

Date of runs: 2026-11-12 (recorded captures 2026-11-11 20:42 CST). Working tree, not committed. Dev DB: `postgresql://postgres@127.0.0.1:55432/pulse`.
Tests: `uv run pytest tests/intel -q` -> **87 passed, exit 0** (38 s, real PostgreSQL 18 per-test databases, recorded raw fixtures, no network).

## 1. What was built

| Area | Files |
|---|---|
| Pure domain | `src/pulse/domain/intelligence/{text,feeds,origin,claims,verification,impact,schemas,community,seats,funding,forecast}.py` |
| Model port | `src/pulse/adapters/models/{port,rules,openai_compat,runner}.py` |
| Source adapters (request builders only) | `src/pulse/adapters/sources/{eastmoney_news,cls_news,guba,lhb,fundflow}.py` |
| Persistence / orchestration / CLI | `adapters/postgres/intelligence_repo.py`, `application/intelligence_service.py`, `workers/intel.py` |
| Tests / fixtures | `tests/intel/*`, `fixtures/intel/recorded/*` (+ `CAPTURE.json`: URL, sha256, fetched_at of every capture) |

Pipeline: raw bytes (lake + `ingest.raw_object` + batch member) -> RAW_COMMITTED -> `document_revision` (original ids, append-only revisions) ->
`claims` re-reads the raw record and asks the **model port** for *enums + evidence spans only* -> code validates, derives numbers / attribution / cluster
fingerprint, attaches official links, computes confirmation from independent origins -> `claim_revision` + `claim_evidence` (+ `event_revision` with the
impact mapping and `event_claim` to the exact claim revision).

Live endpoints used (all public, unauthenticated, entitlement **unconfirmed** and recorded as such in each endpoint policy; catalog says `unconfigured`):
Eastmoney 7x24 `getFastNewsList`, CLS web `v1/roll/get_roll_list` (public web endpoint with the site's request signature; *not* an official API),
Eastmoney Guba list pages, Eastmoney data-center LHB tables, Eastmoney `fflow/daykline`. Rate <= 1 request/s through the shared client; first 403/429/456/501 stops.

## 2. Real runs against the dev DB (all at 1 request/s)

| Command | Exit | Result |
|---|---|---|
| `intel news --source both --pages 2 --page-size 50` | 0 | 4 requests; 100 Eastmoney docs (9 republication, 91 unknown origin), 100 CLS docs (40 own-dispatch `confirmed_independent_report`, 50 republication, 10 unknown) |
| `intel claims` | 0 | 200 documents -> 178 claims (22 extended by a second document), 4 model runs (rules provider); 63 event revisions; status: 181 `unverified`, 19 `not_falsifiable_yet`, **0 `supported`** (no official disclosure matched, no two independent outlets) |
| `intel community --codes 000001 600000 002415 --pages 2 --window-hours 12` (x2) | 0 | 3 requests per run; 28 observations per run, `complete_for_declared_scope` (e.g. 002415: 20 posts, 18 distinct pseudonyms, late/early half 13/7); 56 rows total (a re-sample adds rows with a new `sampled_at`) |
| `intel seats --legacy` | 0 | 59,895 legacy stock rows / 598,550 seat rows -> **59,886 seat reports** (9 skipped: instrument not registered), 10,816 seat entities; rows: 400,892 resolved, 103,486 unresolved, 94,072 report-local label; 0 HTTP |
| `intel seats --days 2` / `--days 3` | 0 | live Eastmoney LHB for 2026-11-10/11 (11-12 = valid "no data" body, `success_empty`): 137 live reports; 1 `ReadTimeout` on the 11-11 sell table handled as a failed batch (health recorded), run continued |
| `intel fundflow --codes 000001 600000 002415 --limit 10` | 0 | 000001 and 002415: 200 observations; **600000 failed 3 times** with `RemoteProtocolError: Server disconnected` (vendor intermittently drops plain-http and https from this host; see section 5) |

Cross-source unit check (legacy vs live, same stock/day/reason/seat/side, 2026-11-10): **493 of 493 rows identical to 0.00 CNY**, so the legacy 亿元 float -> CNY_10000
conversion (through `repr`, quantized to cents) is verified against the vendor's yuan values; 0 rows exist in only one source.

Table counts after the runs: `community_observation` 56, `funding_observation` 200 (2 instruments x 10 days x 10 metrics), `seat_report` 60,023 (59,886 legacy + 137 live; re-runs idempotent),
`seat_report_row` 599,820, `seat_entity_version` 10,821, news `claim_revision` for 178 claims (max revision 3), `event_revision` 63 (impact mapping), `analysis.model_run` 4.
(`document_revision` also holds cninfo rows written concurrently by another package; not counted here.)

## 3. Spec items / acceptance cases -> tests

All in `tests/intel/`. `pg` = real PostgreSQL.

| Item | Proven by |
|---|---|
| FINAL 5.1 claim, not page, is the verification unit; reposts add no vote; unknown origin adds none | `test_domain_claims::test_fixture_origin_evidence_counts_two_independent_origins` (uses spec `fixtures.json`: expected 2), `::test_three_reposts_of_one_wire_item_add_no_vote_and_unknown_adds_none`, `::test_reposts_cannot_buy_support_and_withdrawal_revokes_it`; pg `test_pg_news::test_reposts_join_one_claim_and_add_no_independent_origin`, `::test_two_dispatches_of_one_outlet_on_the_same_fact_are_one_origin` |
| 5.1 supported / contradicted / insufficient (+disputed, not_falsifiable_yet) | `::test_verification_states`; pg `::test_news_about_an_issuer_is_supported_by_its_own_official_disclosure_only`, `::test_without_an_official_document_the_same_news_stays_insufficient` |
| 5.1 document revisions with original ids; correction appends; withdrawal revokes | pg `::test_recorded_feeds_become_raw_objects_and_document_revisions_with_original_ids` (EvidenceDocument contract), `::test_same_delivery_twice_adds_no_document_and_a_changed_item_appends_a_revision`, `::test_correction_replaces_the_old_revision_in_the_claim_without_double_counting`, `::test_withdrawal_recomputes_the_claim_and_its_event_without_the_withdrawn_evidence` (SS-G12) |
| 5 impact: fact -> variable -> instruments -> horizon -> falsifier; "+/-" only an uncalibrated prior; sector keyword names no company | `::test_impact_mapping_named_subject_has_horizon_falsifier_and_uncalibrated_prior`, `::test_impact_mapping_sector_keyword_has_no_instruments`, `::test_impact_schema_rejects_a_probability`, `::test_sector_keyword_alone_never_creates_a_subject`; pg `::test_claims_from_recorded_feeds_have_evidence_events_and_honest_status` |
| Categories are taxonomy ids | `::test_categories_are_taxonomy_ids` (reads `taxonomy.json`) |
| SS-A01 valid empty response | `::test_valid_empty_list_is_success_empty_not_an_error`, `::test_lhb_no_data_body_is_success_empty_but_vendor_error_is_not`, pg `::test_success_empty_page_is_healthy_and_adds_nothing`, `test_pg_flows::test_second_run_adds_nothing_and_an_empty_day_is_success_empty` |
| SS-A02 login page is not data; SS-A04 block stops at once | `::test_login_page_and_schema_drift_are_not_data`, pg `::test_login_page_and_block_do_not_produce_documents` (1 request on 429, circuit OPEN) |
| SS-A05/A06 duplicate delivery, late correction | pg `::test_same_delivery_twice_adds_no_document_and_a_changed_item_appends_a_revision` |
| SS-A10 origin independence | tests above (fixtures.json) |
| Community: declared sampling window, coverage, dedup, independent authors, growth, never "retail buying" | `test_domain_flows::test_coverage_is_complete_only_when_the_window_start_is_reached_in_publish_order`, `::test_summary_dedups_counts_authors_and_withholds_growth_without_coverage`; pg `test_pg_flows::test_guba_sample_is_stored_with_declared_scope_and_never_calls_itself_buying` (CommunityObservation contract), `::test_wide_window_is_partial_and_resampling_adds_rows_not_edits` |
| SS-A23 future engagement | `::test_engagement_seen_three_days_later_is_not_usable_ten_minutes_after_posting` (fixtures.json `community`) |
| SS-A24 no default posting | `::test_unregistered_instrument_is_skipped_and_there_is_no_posting_code` (no POST in the guba adapter) |
| FINAL 5.2 seat entities keep full names, aliases; unresolved stay unresolved | `::test_seat_name_resolution_rules`; pg `::test_live_lhb_reports_keep_names_verbatim_resolve_only_on_evidence_and_respect_availability` (SeatReport contract) |
| SS-A11 unit | `::test_amount_units_convert_exactly` (fixtures.json), pg `::test_legacy_lhb_raw_objects_normalize_with_exact_units_and_their_own_availability`; plus the 493/493 live-vs-legacy check above |
| SS-A14 availability | `::test_after_close_report_is_not_usable_intraday_but_is_next_day`; pg live test (`seat_reports_for` before/after) and legacy test (legacy ingest time kept, not back-dated) |
| SS-A15/G09 overlapping windows | `::test_overlapping_windows_are_not_additive` |
| SS-A16 same seat both sides | `::test_same_seat_on_both_sides_keeps_both_and_derives_net_inside_the_report` (net 100000 from fixtures.json), pg live test |
| SS-A17 "前东路" | `::test_seat_name_resolution_rules` |
| SS-A18 "机构专用1" | same, plus pg `seat_id IS NULL` assertions |
| SS-G08 seat identity available by snapshot | pg legacy test: `seat_rows_as_of` shows the same row `unresolved` for an earlier snapshot and `resolved` later |
| SeatStyleHypothesis, conditional stats only after availability | `::test_style_hypothesis_uses_only_information_available_at_as_of`, `::test_style_observation_entering_before_disclosure_is_rejected_as_lookahead`, `::test_hypothesis_id_is_deterministic_and_condition_sensitive`; pg `::test_seat_style_hypothesis_is_registered_once_and_evaluated_with_pit_gates` (`research.hypothesis`) |
| Fund flow: method dependency, not summed across providers | SS-A12 `::test_different_providers_or_methods_are_never_combined`, pg `::test_fundflow_revision_when_the_vendor_changes_a_value_and_no_cross_namespace_sum`; SS-A27 `::test_missing_value_is_not_zero_and_gross_turnover_has_no_net`; `::test_recorded_fundflow_becomes_provider_derived_series_with_finality_rule`; pg `::test_fundflow_rows_are_provider_derived_series_with_exact_values_and_partial_current_day` (FundFlowObservation contract) |
| Transport failure | pg `::test_transport_failure_fails_the_batch_records_health_and_lets_the_next_code_run` |
| FINAL 7 / 12 model run binding | `test_models`: manifest after `analysis_as_of` refused; run row has policy version, prompt digest, retrieval policy, manifest + context hash, start/finish, `output_ref` + `output_hash`; replay reads stored output and verifies the hash (pg); same manifest reuses the run (provider call count stays 1); tampered output fails; invalid output = failed run with no stored output; ModelRun contract |
| LLM output = enums + evidence refs only | `test_models::test_guard_rejects_floats_unknown_refs_and_free_text` |
| Optional OpenAI-compatible provider | `test_models::test_openai_provider_is_disabled_unless_the_key_file_is_readable`, `::test_openai_provider_sends_key_only_in_header_and_returns_structured_output`, `::test_openai_http_errors_never_leak_the_key[401/429/500]`, pg `::test_provider_failure_records_code_without_secrets` (fake transport; key text asserted absent from every `model_run`/`model_policy` row) |
| Community forecast extension | `test_domain_flows::test_forecast_payload_passes_contract_and_semantic_rules` (`contracts.check('CommunityForecastPayload', ..., SUPPLEMENTAL_ID)`), `::test_late_discovered_forecast_is_known_only_from_actual_availability` |
| CLI | `test_worker_cli` (parser, weekday walk, provider gating) + the real runs in section 2 |

## 4. Design decisions worth reviewing

* **Origin rule** (`origin-rules-v1`): only an outlet's *own dispatch* ("财联社X月X日电") is `confirmed_independent_report`; an item naming an upstream ("据新华社", "公司公告称") is a `republication` keyed by the named publisher; an item naming nobody is `unknown`. Several own dispatches of one outlet on one claim are **one** origin (found by the real run: two CLS items on the same A50 close had briefly produced a false `supported`; fixed and covered by a test).
* **Confirmation**: `supported` needs an official disclosure by the subject issuer (linked by issuer + day +- 1 + shared topic keyword to the cninfo documents already ingested) or >= 2 independent confirmed origins; `contradicted` = confirmed refutation without confirmed support; both = `disputed`; forecasts = `not_falsifiable_yet`; `insufficient` is stored as `unverified`. An official announcement supports the *announcement being made*, not the truth of its content.
* **Model boundary**: the rules provider is the default; any provider must return only `category / assertion_type / polarity_hint / attribution_kind` enums, `subject_refs` copied from the record's own vendor stock refs, and `evidence_spans` offsets (strict JSON schema, no floats, refs checked against the manifest). Numbers, publisher, cluster, status and impact are derived by code.
* **Legacy LHB**: amounts are float 亿元; each is converted via its shortest decimal representation quantized to cents (float noise below 0.01 CNY discarded) into `CNY_10000`, so `canonical = raw x 10000` holds in the DDL check. Reports keep the legacy `ingested_at` as `available_at` (not back-dated). Legacy ranks are 0- or 1-based; stored rank is the position, the source rank is kept in `resolution_evidence.source_amounts.source_rank`. Window start of multi-session reasons ("连续三/五/10个交易日") comes from the observed legacy trading dates.
* **Seat resolution** needs broker legal name + a city found by prefix in a versioned gazetteer (`cn-city-gazetteer-v1`) + a branch suffix. 分公司 named only by province, 深/沪股通专用 (no broker legal name in the DDL), and any fragment stay `unresolved`; "机构专用N" is `report_local_label`; "拉萨天团"-type nicknames are `ambiguous`. 37,624 legacy rows are unresolved only because the city is not determinable (most are province-level 分公司), 35,803 are stock-connect units.

## 5. Not done, limits, synthetic parts

* **No licensed or official feeds**: all five live endpoints are public web endpoints with unconfirmed entitlement (permitted_uses / retention not confirmed); CLS is explicitly not an official API. Nothing was activated in the catalog.
* **Fund flow**: `push2his.eastmoney.com` is intermittently unreachable from this host (plain http worked once, https another time; both dropped later). 600000 never succeeded in the real run, so live fund flow exists for 000001 and 002415 only. Vendor publishes no methodology version: `methodology_version = observed-20261111` is the observation epoch of our mapping, thresholds are unknown; `session_scope` is stored as `vendor_day_bar_scope_unspecified`.
* **Real-data confirmation is thin by construction**: in the live run no claim reached `supported` (no matching cninfo disclosure; EM items are aggregator rewrites with unknown origin). The `supported` paths are proven on recorded real EM text paired with a **synthetic** cninfo announcement (`tests/intel/helpers.cninfo_page`) and on synthetic CLS dispatches; revision, correction, repost and cluster scenarios use pages synthesized in the recorded shape (marked in the helpers).
* The rules provider is keyword based: one headline claim per document, no coreference, Chinese only, subject only from vendor stock refs. Community forecast *extraction* from free text is not implemented (needs a language model); only the validated claim builder/PIT check exists.
* `SeatStyleHypothesis` is registered (`research.hypothesis`) and the evaluator is tested with synthetic observations; **no real conditional statistics were computed** (no price/market-state join was wired) and no table stores evaluation results.
* Guba list: no likes, no participant counts (stay NULL); only the instrument's own stock bar is stored (the list also mixes other bars); author = one-way digest of the platform user id. `like_count`/`unique_participant_count` unavailable by source.
* Gazetteer is a hand-written list (cities + a few county-level cities); misses (e.g. 东阳) fail safe to `unresolved`.
* LHB `trade_scope`/`rule_version` are `unknown` (the vendor states neither); report quality for legacy is `partial` (units, top-N only, derived window start).
* Withdrawal of news documents is never inferred (the feeds expose no deletion signal); `withdraw_document` appends it only on explicit source evidence.
* Isolation level: all writes use READ COMMITTED short transactions; no concurrent-writer test of these tables was run.

## 6. Proposed schema/contract changes (nothing applied)

1. A table (or `ops`/`research` extension) for **seat-style evaluation results** keyed by `(hypothesis_id, hypothesis_version, as_of)`; today the evaluation is a pure function and `research.metric_value` requires a `strategy.instance`.
2. `intelligence.seat_report.notes jsonb` (top-N-only, derived window start, skipped rows without amount) - currently only counted in the run summary.
3. `intelligence.community_observation.instrument_id uuid` + index; today the instrument is `analysis_payload->>'instrument_code'`.
4. `seat_entity_version.entity_kind='trading_unit'` currently requires `broker_legal_name NOT NULL`, which blocks modelling 深/沪股通专用; allow NULL for trading units.
5. Contract definition for `Funding`/`Seat` rows exists, but `analysis_payload` and `evidence_span` remain "seed-extension-point": the named schemas used here (`pulse.intel.*@1`, registered in `core.contract_version`) should be adopted as the registered versions.
