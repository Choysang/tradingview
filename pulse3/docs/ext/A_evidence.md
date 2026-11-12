# Extension group A evidence: PEX01 / PEX02 / PEX03 / PEX09 / PEX16 and the old-site market overview

Run 2026-11-12, Windows 11, `uv run` from `pulse3/`, PostgreSQL 18.3 at `127.0.0.1:55432`. Not committed.
Real data: the dev DB `pulse` (raw daily bars of 3,214 main-board instruments to 2026-11-11, legacy index bars to 2026-11-10).
Synthetic data: only `tests/ext_a` (the I1 world plus exact limit-up / broken-board paths for five codes, and six later sessions published after the rankings under test).

## 1. Files

New: `src/pulse/domain/market_state/{__init__,daily,mood,regime,panels,tape,outcomes}.py`, `src/pulse/application/market_today_service.py`, `src/pulse/api/routes_ext_a.py`, `migrations/0020_ext_a_registry.sql`,
`tests/ext_a/{__init__,conftest,support,test_domain,test_pg_state,test_pg_rankings,test_pg_stream}.py`,
`apps/web/src/pages/ext/a/{types,api,hooks,charts,TapePanel,MarketPage,RegimePage,TrackRecordPage,SectorDetailPage}.ts(x)`, `ext-a.css`, `apps/web/tests/ext_a.test.tsx` (outside the allowed web path only because vitest collects `tests/**`), this file.
Changed (marked `EXT-A`): `src/pulse/application/release_service.py` (optional component), `src/pulse/workers/analysis.py` (`tape`, `outcomes`), `apps/web/src/routes/extA.tsx` (four routes).
No contract data file was edited.

## 2. Design

* **Market state** is computed once per release from the raw daily bars (point in time, observed or reconstructed policy), persisted as D01 `pulse.market_today` (7 panels), D11 `pulse.market_sentiment` (3 features x 49 scopes: market + 48 sectors) and registered as `market.feature_result` rows (FeatureKey adjustment_mode `raw`; one scope uuid as the single "instrument id").
  Wired into `build_release` as the optional component `market_state` (ALLOW_MISSING): a failure records MISSING and never blocks the required components.
* Panel bodies are fetched through `typed feature -> artifact ref feature:<hash>`; the content hash is verified on read. Pages never recompute.
* **Sentiment thermometer**: the old weights (0.40 / 0.25 / 0.20 / 0.15) kept as a versioned, uncalibrated indicator (`mood-v1-old-site-weights-uncalibrated`, `calibrated: false`); score is `null` when any component is unknown.
* **Regime timeline**: old four-dimension rules, trailing labels (a day's label never changes when later days arrive, tested), each dimension carries `basis` (`ok` / `insufficient_history` / `missing_index`) so default labels are shown faded and named, style `unknown` instead of silently `balanced`.
* **Ranking outcomes** (D02): separate immutable dataset `ranking_outcome`; entry = next-session raw open, exit = raw close at horizon, net of the declared round-trip cost of the user's cost profile, excess vs the median of all evaluable ranked names; pending rankings have no row; avoid group is descriptive long-only only.
* **PEX16**: SSE over the same `ops.outbox` log as `pollUpdates`; event id = outbox event id, `id:` = opaque cursor (epoch + position), `Last-Event-ID` resumes, missing / expired / other-epoch cursor sends one `reset` event (`resync_required: true`), only the four registered 3.0.0 event types and schema major 3 are sent, same-id-different-payload ends the stream with an `error` event.
* **Event tape**: derived from consecutive same-day quote snapshots (`analysis tape`, idempotent, deterministic event ids), stored in the outbox aggregate `market_tape`, delivered by `/v1/x/a/market-tape` (poll) and `/v1/x/a/market-tape/stream` (SSE).

## 3. Card acceptance -> proof

| Card item | Proof |
|---|---|
| PEX01 four baseline scenarios (valid / expired / PC offline / key field missing) have deterministic results; page never labels an old package as new analysis | `test_pg_state.py::test_four_scenarios_of_the_baseline_have_deterministic_results`, `::test_old_package_is_labelled_stale_and_missing_component_is_a_gap_not_a_panel` (STALE_BASELINE warning + freshness `stale`; missing component = `COMPONENT_NOT_IN_RELEASE` gap, quality `partial`, no panels) |
| PEX01 failed stage does not publish new READY / history intact | `::test_failed_market_state_degrades_to_missing_and_the_release_still_publishes` (optional failure never blocks; the build's own required-component refusal is I1 `test_a_missing_or_failed_required_component_blocks_ready`) |
| PEX01 different accounts' overlays do not hit one private cache | `::test_account_overlays_are_private_and_never_shared_between_accounts` (`Cache-Control: private, no-store`, `Vary: Cookie`, distinct artifact refs, frozen content identical) |
| PEX01 query is not a synchronous computation or publish command | `market_today()` only reads; `test_datasets_features_and_lineage_are_registered` shows everything was written at build time |
| PEX02 SHORT/SWING/LONG are display labels; real filter is target/horizon/scope | `test_pg_rankings.py::test_history_lists_each_published_ranking_newest_first_with_real_filters`, `::test_display_labels_are_not_filters_and_bad_requests_are_typed` |
| PEX02 cursor fixes the publication-history watermark; new publications do not change paged results; expired cursor is `CURSOR_EXPIRED` | `::test_paging_is_pinned_to_the_publication_watermark` |
| PEX02 original forward ranking and later outcome readable separately; replayable | `::test_resolved_outcome_is_a_separate_immutable_live_forward_artifact` (ranking wire unchanged before/after, re-run publishes nothing), `::test_outcome_is_invisible_to_a_cutoff_before_it_existed` |
| PEX02 unplaced / unfilled opportunities stay in the denominator; avoid list is not a short return | same test: `top_hit_rate_over_opportunities` sample = opportunity count, `*_evaluable_count` separate, no metric named short; unit `test_domain.py::test_outcome_pending_until_the_horizon_is_observed_then_resolved_with_denominators` |
| PEX02 unmatured = pending, not fetched early | `test_unmatured_rankings_are_pending_and_nothing_is_fetched_early`, domain pending test |
| PEX03 same membership for ranking, ladder, candidate pools; versions addressable | `test_sector_details_use_one_membership_for_members_ranking_and_pool`, `test_other_taxonomy_versions_are_addressed_separately` |
| PEX03 insufficient members carry coverage, never widen the universe | `test_sector_without_a_ranking_is_reported_with_coverage_not_widened` |
| PEX09 limit-up / broken / promotion denominators equal research and product | `test_sentiment_denominators_match_the_overview` (sentiment = vitals = ladder facts), domain promotion tests |
| PEX09 promotion denominator = yesterday's frozen set, missing is not 0 | `test_domain.py::test_promotion_uses_yesterdays_frozen_set_and_premium`, `::test_missing_member_leaves_the_evaluable_denominator` |
| PEX09 thresholds / versions bound to the artifact | `test_thermometer_is_a_declared_uncalibrated_gauge` |
| PEX16 repeated / reordered / reconnect still switches a complete release; SSE is the pollUpdates log, no second business calculation | `test_pg_stream.py::test_first_connection_resets_then_resumes_from_last_event_id_without_duplicates`, `::test_stream_and_poll_share_one_log` |
| PEX16 expired cursor / epoch change => explicit resync | `::test_expired_cursor_and_epoch_change_reset_instead_of_guessing` |
| PEX16 unregistered types not sent; schema major not mixed; cursor of another scope refused | `::test_unregistered_event_types_never_ride_the_3_0_0_stream`, `::test_stream_request_validation` |
| Event tape (old 盘口异动) | `::test_tape_derives_every_event_kind_from_consecutive_snapshots`, `::test_tape_is_idempotent_and_never_repeats_inside_the_cooldown`, `::test_tape_poll_and_stream_serve_the_same_validated_events`; rules in `test_domain.py::test_tape_*` |
| Registry gate | `test_extension_registry_has_every_served_path` |
| Contract-checked responses | every `/v1/x/query-*` answer leaves through `respond_ext` (validates `<op>Response`); the tests ran with validation on; request parameters are checked against `<op>Request` (unknown / invalid -> 400 `INVALID_CONTRACT`) |
| Frontend | `apps/web/tests/ext_a.test.tsx` (request fields match the extension schema, JSON encoding, tape merge/dedupe, outcome state, gauge/bars/timeline rendering) |

## 4. Commands and results

| Command | Exit | Result |
|---|---|---|
| `uv run pytest tests/ext_a -q` | 0 | 51 passed (17 domain, 34 PostgreSQL) |
| `uv run pytest -q` (whole repository, other groups' tests included) | 1 | 672 passed, 3 failed (all outside group A, see section 7), 48 subtests |
| `cd apps/web && npx tsc --noEmit` | 0 | clean |
| `cd apps/web && pnpm test` | 0 | 8 files, 90 tests passed |
| `cd apps/web && pnpm build` | 0 | built |
| `uv run python -m pulse.adapters.postgres.migrate` (dev DB) | 0 | applied `0020_ext_a_registry.sql` |
| `uv run python -m pulse.workers.analysis build --as-of 2026-11-12T14:25:06Z --legacy-warehouse ...` (dev DB) | 0 | release `b5af7f68-93bf-5324-b41f-4f33815e3404` READY, pointer generation 5, 85 s; `market_state` READY: 7 panels, 147 sentiment features over 49 scopes |
| `analysis tape` / `analysis outcomes` (dev DB) | 0 / 0 | tape: 0 pairs (only one real quote snapshot exists); outcomes: 15 rankings evaluated, all pending |

Real-data endpoint check on the new release (in-process client against the dev DB, plus the built web app in the browser pane: `/market`, `/regime`, `/track-record` rendered, no console errors):
queryMarketToday 200 (7 features, freshness `current`, quality `valid`, quote overlay present); session 2026-11-11: 76 limit-ups, 25 broken, 2 limit-downs, max height 4, seal rate 75.2%, yesterday-limit-up premium +2.98% (85 samples), mood 50.2 (中性), ladder 4连板 2 / 3连板 5 / 2连板 12 / 首板 57 with promotion 50% / 33.3% / 18.5%, regime 熊市 · 缩量 · 高潮 · 风格均衡 on 000985 through 2026-11-10;
queryMarketSentiment 200 (coverage 3,192 of 3,495 main-board instruments, 73 without a verified limit rule); querySectorDetails 200 (new_dzxx: 247 members, 62 ranked, coverage shows 185 not in the universe); queryRankingHistory 200 (5 releases); queryRankingTrackRecord 200 (all 5 pending: the 5-session horizon of the 2026-11-10 session ends 2026-11-17; nothing is backfilled).

## 5. Gaps and honest limits

* **Seal clock** has no data: first-seal times need intraday quotes or minute bars. It is a declared capability gap (`NO_INTRADAY_SEAL_TIMES`) and the page says so; nothing is invented.
* **Turnover amount** is an estimate (shares x close) because raw daily bars carry no amount (`TURNOVER_AMOUNT_ESTIMATED`, flagged in the body).
* Limit statistics exclude instruments without a verified price-limit rule (SZSE risk-warning shares and others: 73 on 2026-11-11). Risk-warning status is the state visible at the decision time applied to the whole window. Streaks are truncated at the loaded window start (about 250 sessions).
* Regime is dated at the last index session (index bars end 2026-11-10 while stock bars reach 2026-11-11): `INDEX_BARS_BEHIND_SESSION` gap; the theme/vitals panels use the later stock session.
* Real outcomes are all pending until 2026-11-17 (5td). The resolved path is proven on the synthetic world only (six later sessions published after the rankings). A reconstructed rebuild of an earlier real session is impossible today: the legacy boards evidence is ingested after any earlier cut, so `sector_membership` is invisible and the build correctly refuses.
* Outcome prices are raw (no dividend adjustment); a price path with an impossible move is `UNVERIFIABLE_PRICE_PATH`. Cost is the user profile's round trip on 50,000 CNY. Both are declared in every record.
* Event tape covers the quoted set only (the focus set), not the market; surge/plunge use a per-instrument cooldown (15 minutes) instead of the old hysteresis. No real tape exists yet (one quote snapshot).
* Sector `role_feature` (龙头/中军) is null: no algorithm output exists. `related_event_artifacts` is empty: no event artifact contract is registered.
* The thermometer and theme strength are uncalibrated engineering parameters from the old site; the page labels them so. The session mood curve of the old site (intraday) is replaced by a daily history.
* The extension request schema has object-valued fields; they travel as JSON text in the query string (`scope`, `window`, `page`, `sector`, arrays).
* `tests/db/test_roles.py::test_every_table_is_readable_by_runtime_roles` fails in the whole run because other groups' tables (`ui.saved_screen`, `research.screen_run`, ...) from migrations 0030/0040 have no runtime-role grants; not touched by group A (group A adds no table).

## 6. Proposed contract / schema changes (not applied)

1. `FeatureKey.instrument_ids` has no scope form: market / sector features use a deterministic scope uuid as the single "instrument id". Proposal: a `scope` slot or `scope_ids`.
2. `TypedFeature.result.artifact` carries only a reference; there is no operation that resolves it. Proposal: register `readArtifact` (`GET /v1/x/a/artifact?ref=`, served here and bound as `readArtifactA`) and a typed body contract per `feature_id`.
3. `StreamEnvelope.notification` is the strict four-type `UpdateNotification`, so tape events cannot ride `streamRegisteredUpdates`. Served on a separate log (`queryMarketTape`, `streamMarketTape`, payload contract `pulse.market_tape`); proposal: register `market.tape.event` under D26 and merge the streams.
4. `streamRegisteredUpdates` reset: the strict data object has no reason field; the reason (`NO_CURSOR` / `CURSOR_EXPIRED`) is sent as an SSE comment line.
5. `ranking_run` carries no information-session date; the outcome job recovers it from the release's `universe` summary body. Proposal: a column or a registered release component.
6. `evaluation_context_id` of `queryRankingTrackRecord` must be a registered context; only the fixed forward-outcome method id (`3dffbb0a-c3ae-5dac-a527-c8f81b80ef54`, uuid5 of the method version) is accepted, others return 422 `UNSUPPORTED_CONTEXT_SHAPE`.

## 7. Whole-repository run

`uv run pytest -q` -> 672 passed, 3 failed. The failures are not in group A and none touches a file of group A: `tests/db/test_roles.py::test_every_table_is_readable_by_runtime_roles` (tables of migrations 0030/0040 from groups B/C lack runtime-role grants: 104 tables vs 87 expected) and two `tests/ext_c/test_domain.py` tests of group C. All 548 earlier tests still pass; `tests/ext_a` adds 51.
