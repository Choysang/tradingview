# M1 data plane evidence (W03 sources/evidence + W04 market/features)

Date of runs: 2026-11-11. Working tree, not committed. Database: PostgreSQL 18 `postgresql://postgres@127.0.0.1:55432/pulse` (migrations 0001-0010).
Lake root: `pulse3/var/lake` (git-ignored). Tests: `uv run pytest tests/data -q` -> **97 passed, exit 0** (85 s); `tests/test_foundation.py` 3 passed.

## 1. What the legacy warehouse really contains (inspection, spot-checked against live sources)

| Dataset | Finding | Evidence |
|---|---|---|
| `bars` | Float64 OHLC, **forward adjusted with an unknown, drifting factor series**; `volume` = **shares** (integral float); `amount` = **NULL in every row**; every full re-pull stores a new version (`ingested_at`). | 000001 close vs Tencent raw close: 2025-11-03 10.828324/11.430 = 0.94736; 2026-03-02 ratio 0.94736; 2026-06-15 ratio 0.978534; 2026-09-30 / 10-30 / 11-06 ratio 1.000 (piecewise constant = ex-rights steps). Volume vs Tencent lots x100: diffs +43, -45, -5, +45, +7, +39 shares (< one lot, Tencent rounds to lots). Sina daily volume for 2026-11-10 = 74,888,996 shares = legacy 74,889,000 within lot rounding. |
| revisions | 15,584 (code,date) keys in the window changed values between ingests (adjustment drift after new dividends, volume precision refinements). Each distinct value set is kept as an observation revision. | `legacy_revised_keys` in the import summary below. |
| `securities` | name with full-width blanks / ST prefixes, `board` = **price-limit class** (10cm/20cm/30cm), not the exchange board. | Board is derived from the code ranges; ST from the dated name only. |
| `index_bars` | index points, volume unit not verified. | Published as a separate lake dataset (see proposal P1). |
| `boards`, `lhb_seats`, `lhb_stocks` | kept as raw objects (bytes + sha256) for the sector / seat packages; not normalized here. | 92 raw objects. |

Consequence: legacy daily history is imported as `adjustment_mode=forward_adjusted`, `adjustment_version=legacy-qfq-unversioned`, turnover NULL, never as raw prices. Raw (unadjusted) daily bars come from the live Sina/Tencent adapters. Capability `bars_legacy_fwd_adjusted_1d` therefore **rejects** execution/raw requirements (test below).

## 2. Real runs and exit codes

| Command | Exit | Result |
|---|---|---|
| `uv run python -m pulse.adapters.postgres.migrate --create-db` | 0 | migrations up to date (0001-0010) |
| `uv run python -m pulse.workers.ingest legacy-import --since 2025-11-01` | 0 | below |
| `... live-daily --codes 000001 600000 002415 --source both --count 320` | 0 | 6 HTTP requests, 960 raw bars per source (320 sessions x 3), each a READY snapshot |
| `... minute --codes 000001 --source tencent --period 1 --count 240` | 0 | 240 one-minute bars, READY `bars_raw_1m` |
| `... minute --codes 000001 --source sina --period 5 --count 96` | 0 | 96 five-minute bars, READY `bars_raw_5m` |
| `... quote --codes 000001 600000 002415 --source sina` | 0 | 3 quote observations |
| `... announcements --days 1` | 0 | 38 pages (38 requests at 1 req/s), 1,139 documents, 213 classified events |
| `... reconcile --codes 000001 600000 002415 --last 5` | 0 | close 27 matched / 12 unverifiable; volume 27 matched / 12 unverifiable |

`legacy-import` summary (real, full main-board universe, 248 sessions 2025-11-03..2026-11-10):

```
instruments_seen 5935 (all boards registered), legacy_instruments (main-board with bars) 3214, legacy_sessions 248
legacy_rows_all_versions 2,254,379   legacy_distinct_value_versions 805,610   legacy_revised_keys 15,584
observations_created 805,610   lake_rows_bars (latest revision per key) 790,005   manifest_version_bars 1
legacy_files_kept_raw 30 (129,208,169 bytes)   legacy_evidence_files_kept_raw 92   index_bars_rows 1,487
state_versions_appended 5,985   unexplained missing session rows 3,718 across 307 instruments (suspension not distinguishable, see 5)
```

Row counts in the dev DB afterwards: `core.instrument` 5,941 (5,935 equity + 6 index), `core.instrument_alias` 23,746, `core.instrument_state` 5,991 (3,530 main / 1,456 chinext / 632 star / 367 bse equities; 318 risk-warning states), `ingest.raw_object` 175, `ingest.observation` 807,869, `market.bar_projection` 2,256 (raw live bars only), `market.dataset_manifest` 7 (all READY), `intelligence.document_revision` 1,139, `claim_revision` 1,199, `event_revision` 213, `ops.reconciliation_result` 78 (54 matched, 24 unverifiable, 0 disputed), `ops.outbox` 7 (one `market.dataset_snapshot_ready` per snapshot).
Bulk load: 805k observations in ~5 minutes after switching to COPY into a staging table + `INSERT ... ORDER BY observation_id` with entity-clustered ids (the first attempt with random-UUID row inserts fell to ~70 rows/s once the index outgrew `shared_buffers` and was aborted; the dev DB was dropped and rebuilt).

## 3. Exit evidence

| ID | Satisfied by | Tests (all in `tests/data/`) |
|---|---|---|
| **M1-E01** two raw-price routes + one official event chain | Sina (`hq_quote`, `kline_daily`, `kline_minute`) and Tencent (`qt_quote`, `kline_day`, `kline_minute`, `minute_query`) adapters; cninfo `his_announcement_query` with original id/title/time/url, origin group and revision/withdrawal relation. Real captures recorded in `fixtures/data/recorded/` (CAPTURE.json). Interface success, new content, canonical ready and freshness are separate health columns. Same-origin mirrors share one `origin_group`; an official announcement supports only its own publication. | `test_normalization::test_sina_daily_and_tencent_daily_agree_on_shares...`, `::test_tencent_quote_and_sina_quote_agree...`; `test_health_evidence_identity::test_recorded_cninfo_pages_normalize...`, `::test_same_announcement_on_other_portals_is_one_origin...`; `test_pg_live::test_live_daily_sina_stores_raw...`, `::test_cross_source_reconciliation_is_stored_and_classified`, `::test_announcements_preserve_original_ids...` |
| **M1-E02** raw, mapping, revisions, availability | `ingest.raw_object` keeps bytes + sha256 (verified byte-exact in tests); profile-driven units per exact endpoint (`domain/market/units.py`); drift (`missing_mapped_keys`), additive fields, fractional shares, NaN/negative/float inputs, excess precision, OHLC contradiction all detected; missing stays NULL with field status; revisions append (`observation_revision`) with their own `available_at`; legacy `available_at` is the legacy ingest time (back-filled history is not back-dated). | `test_normalization` (19), `test_pg_legacy::test_raw_objects_keep_bytes...`, `::test_revisions_are_appended_not_overwritten...`, `test_pg_live::test_second_fetch_of_same_data_adds_no_facts_and_changed_data_appends_a_revision`, `::test_login_page_and_schema_drift_are_not_data` |
| **M1-E03** snapshot/publish/lifecycle recovery | write temp -> fsync -> sha256 -> atomic rename to content-addressed path -> immutable member manifest -> PG `COMMITTED_UNPUBLISHED` + dependencies -> validation -> `READY` + outbox in one transaction -> release stage pin. Pins/leases and GC share one catalog lock; GC reads pins then asks PG for READY/COMMITTED_UNPUBLISHED manifests under that lock. Historical snapshot replays its frozen member list after compaction. | `test_lake` (10, incl. `test_publish_gc_race_never_loses_a_member` 25 publishes vs a spinning GC thread, `test_historical_snapshot_replays_frozen_members_after_compaction`, `test_releasing_the_pin_before_the_reference_exists_is_unsafe` negative control), `test_pg_legacy::test_manifest_lifecycle_dependency_closure_outbox_and_replay`, `::test_incomplete_publication_is_refused_and_leaves_no_ready_manifest`, `::test_gc_through_pg_reference_query_keeps_committed_unpublished_and_ready` |
| **M1-E04** health, duplicates, revisions, gaps | pure state machine (`domain/evidence/health.py`) -> `ingest.source_health`; HTTP 200 with stale content leaves an impaired source `degraded`; quiet/closed is `not_expected`; login page = `auth_required`; first 403/429/456/501 stops the run (circuit OPEN, cooldown in `next_attempt_at`); cursor advances only by CAS after RAW_COMMITTED; repeated polls add no business facts; withdrawal is inferred only from a fully paged window. | `test_health_evidence_identity` health tests, `test_pg_live::test_block_stops_the_run_immediately_and_opens_health`, `::test_announcement_correction_appends_a_revision_and_withdrawal_is_inferred_only_from_a_fully_paged_window`, `::test_announcement_login_page_and_block_do_not_produce_documents`, `test_pg_legacy::test_cursor_cas_and_checkpoint_requires_raw_commit` |
| **M1-E05** capability matching | `domain/market/capability.py` registry (also stored in `core.source_endpoint.policy_document` and each manifest `coverage_document`): legacy adjusted daily, raw daily (Sina/Tencent), true-OHLC minutes, close-only sampled minute (`estimated=True`), L1 quotes, official events. Sampled series are `Bar.estimated`, partial, never final; requirements that need true OHLC / raw / turnover are rejected or explicitly degraded with reasons. Interval (not cumulative) volume, suspension vs missing vs true zero, W/M/Q/Y aggregation over the calendar with `is_final=false` for incomplete periods, adjustment factors kept separate. | `test_market_domain` (10), `test_pg_live::test_minute_bars_are_interval_volume_on_the_regular_grid...`, `::test_minute_bar_just_captured_is_not_final`, `::test_todays_bar_before_16_cst_is_not_final` |

Shared features (spec 4.1/8): `domain/features` (MA/EMA/MACD/RSI/ATR/volume ratio/relative strength, fixed windows, warm-up, null policy that resets recursions after a gap, `sma_seed`/`first_value` EMA init), 19-dimension `FeatureKey` validated by `contracts.check("FeatureKey")`, single-flight store. `DataService.compute_features` filters inputs by `available_at <= decision_as_of`, writes the result as a lake object and a `market.feature_result` row (input snapshot, calendar, adjustment version in the key), reuses an existing READY key and is fenced by `lease_epoch`. Tests: `test_features` (7), `test_pg_legacy::test_shared_features_use_pit_inputs_dedupe_by_full_key_and_survive_gc`, `::test_feature_result_is_fenced_by_lease_epoch`.

Unit-change detection: `detect_unit_change` (implied VWAP vs price) -> `test_normalization::test_unit_change_detection_flags_lots_read_as_shares`; wired into quote ingestion -> `test_pg_live::test_quote_unit_mismatch_is_detected_and_recorded` (`ops.data_issue unit_mismatch`, observation `disputed`).

Contract checks: CanonicalBar / AdjustedBar / FeatureKey / EvidenceDocument payloads pass `contracts.check` (`test_market_domain::test_wire_payload_passes_the_v3_contract`, `test_pg_live`, `test_features`).

## 4. Findings that change the picture

* Tencent `qfq` is **additive** (`adj = raw - offset`); a multiplicative factor series cannot be read off it. `derive_events_from_pair` therefore recovers ex-dates and cash amounts from offset steps (stock dividends/splits are not recoverable this way) and labels them `derived_provider_pair_cash_equivalent`.
* Sina `kline_daily` does not return the current session until after the vendor completes it; Tencent does (earlier). Neither daily endpoint carries turnover amount, so every raw daily/minute bar is `quality_status=partial` with NULL turnover (never 0); the quote endpoints do carry cumulative amount.
* Tencent minute volume is lots with fractional lots (`92710.27`), converted exactly; Sina 5-minute and Tencent 5-minute volumes differ slightly at the closing auction (different session scope), so minute cross-source checks are `unverifiable_until_scope_aligned` rather than disputes.
* `minute/query` includes after-hours fixed-price trades after 15:05: it is cumulative, close-only and registered as an estimated capability.

## 5. Not done / synthetic / limits

* No Tushare or other paid source; cninfo entitlement is "public query, unconfirmed" (endpoint policy says so). No full-market live crawl was run (explicit codes only); raw daily bars exist only for the 3 live-fetched codes, not for the 3,214-stock universe (a full backfill is ~3,200 requests, about 55 minutes at 1 request/s: `live-daily --codes ...`).
* Legacy history is forward adjusted with unknown factors and has no turnover; it cannot be converted to raw. 3,718 session-rows (307 instruments) are missing without explanation because no suspension feed is registered; suspension vs gap is distinguished in the domain (`classify_sessions`) but cannot be populated.
* Trading calendar is observed from bar dates (`known_through` = last observed day); the exchange holiday calendar is not ingested, so future periods are never final.
* `market.quote_projection` and the `bars.window` completeness feature are not populated; quotes are stored as observations only.
* Lake pins/leases live in a SQLite catalog inside the lake root (no PG table exists for them): correct for one host owning the lake root, not for a shared lake.
* Bulk observation payloads store explicit field-quality entries only for non-observed fields plus one profile-level entry under key `*` (declared in `builders.FQ_SUMMARY_KEY`) to keep 800k rows near 2 KB each.
* `ingest.observation` rows were never validated against the AdjustedBar contract at bulk scale (payload is snapshot-independent); contract validation runs on live bars on every row and on sampled legacy payloads in tests.

## 6. Proposals (nothing applied; no `migrations/proposals` file needed to run)

* P1: an `IndexBar` contract (index points, original unit/multiplier). `CanonicalBar`/`AdjustedBar` require `currency=CNY`; `index_bars_legacy_1d` is currently published against `AdjustedBar` only to satisfy the manifest FK and says so in its `coverage_document`.
* P2: index `market.bar_projection(observation_id)`. Deleting observations scans every partition per row for the FK check (a 386k-row delete ran >10 minutes).
* P3 (optional): `ingest.source_health` has no content-clock or circuit column; circuit OPEN is encoded as `runtime_state=unavailable` + `last_error_code='blocked:...'` + future `next_attempt_at`, and the content watermark lives in `ingest.source_cursor.source_event_watermark`.
