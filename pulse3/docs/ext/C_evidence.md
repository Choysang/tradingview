# Extension group C evidence (PEX05, PEX07, PEX08, PEX09, PEX12, PEX13, PEX14, PEX15 + kept old-site features)

Date: 2026-11-13. Working tree, not committed. Dev DB `postgresql://postgres@127.0.0.1:55432/pulse` (migrations 0020/0030/0040 applied; the first
version of 0040 had been applied there by an earlier run - all its tables were empty, so they were dropped and 0040 re-applied in its final form).
Tests: `uv run pytest tests/ext_c -q` (real PostgreSQL 18 per session, no network) -> see section 6 for the counts of the final run.

## 1. What was built

| Area | Files |
|---|---|
| Migration (D-ids) | `migrations/0040_ext_c_portfolio_alerts_calendar.sql` - D04 `decision.plan_revision_metadata`, D05 `decision.plan_proposal`, D08 `intelligence.cluster_revision`, D09 `calendar.{snapshot,event_revision,event_link}`, D10 `intelligence.seat_profile`, D15 `access.account_grant`, D16 `execution.{broker_import,broker_fill_fact}`, D17 `research.opportunity_event`, D18 `notification.{alert_rule,alert_rule_head}`, D19 `notification.alert_intent`, D20 `notification.delivery_attempt`, D21 `notification.destination`, D23 `ui.workspace_revision`, D24 `ops.export_request`, D25 `ops.job_run_argument`, D27 `execution.account_readmodel`, plus `ops.ext_artifact` (immutable store behind every `ArtifactRef`), `execution.tracked_{account,book_day}` (forward books), `notification.scan_cursor`; append-only triggers; runtime-role grants |
| Pure domain | `domain/alerts/tiers.py`, `domain/calendar/events.py`, `domain/portfolio/{forward_book,readmodels}.py` |
| Adapters | `adapters/notify/webhook.py`, `adapters/sources/{calendar_em,corp_events}.py` |
| Services | `application/{alert_service,calendar_service,portfolio_service,workspace_service,ops_service}.py` (calendar_service also holds clusters / seats / community because the allowed file list has no intelligence-read service) |
| API | `api/routes_ext_c.py` (25 contract operations + 7 supplementary reads) |
| Workers | `workers/alerts.py` (init-rules, scan, deliver, calendar-sync/derive/expiries, seal-workspace, drain), `workers/forward.py` (register, daily, grant, import-fills, readmodels, show) |
| Web | `apps/web/src/pages/ext/c/*`, `apps/web/src/routes/extC.tsx` (alerts, calendar, seats, books, health), tests `apps/web/tests/ext_c_*.test.*` |
| Tests | `tests/ext_c/*` |
| Outside my paths (necessary) | `tests/db/test_roles.py`: the table count is now derived from the migration files (every `CREATE TABLE` in the runtime schemas must exist) instead of a hard-coded total |

HTTP shape: GET queries take each `<op>Request` field as a query parameter (objects/arrays as JSON text, timestamps with `Z`); POST commands take the JSON body
and `Idempotency-Key` (via `PgCommandStore`, `response_contract_id = EXT_ID`); every response goes through `respond_ext`, every request through `contracts.check`.
Principal only from `auth.principal`. Cursors are opaque, bound to the query hash (`CURSOR_EXPIRED` when reused for another query).

## 2. Card acceptance -> tests (`tests/ext_c/`)

| Card / op | Acceptance | Test |
|---|---|---|
| PEX05 proposeTradePlan | duplicate command does not make a second plan; stale base/account = VERSION_CONFLICT; late events do not rewrite old release (pinned `based_on_release_id`); user edit and system plan stay separate | `test_pg_plans::test_propose_is_async_idempotent_and_compiles_into_a_separate_user_chain`, `::test_edit_of_an_edit_appends_to_the_users_chain_and_a_stale_base_is_a_version_conflict`, `::test_a_superseded_base_ends_superseded_not_as_a_stale_plan` |
| PEX05 queryPlanProposal | receipt reaches the final plan; stale account gives an explicit reason, no fake empty plan | `::test_account_version_is_checked_when_proposing_and_again_when_compiling`; `::test_plan_proposal_is_never_an_order` (no ledger event) |
| PEX05 queryPlanVersions / listTradePlans | unauthorized user cannot read someone else's edited plan by id; by stock both system and user versions with explicit origin | same first test (stranger -> 404, `origins` = system / user_edit) |
| PEX07 queryEventClusters | reposts of one upstream are one cluster (no extra vote); macro events without a stock are kept; membership change = new revision, old readable; post-release claims invisible | `test_pg_intel::test_clusters_reposts_are_one_cluster_membership_changes_append_revisions_and_macro_is_kept` |
| PEX07 queryCalendarEvents | postponed meeting, announcement correction, date-only schedule, macro expected vs actual revision; point-in-time; snapshot pinning | `test_pg_intel::test_calendar_revisions_postponement_correction_and_macro_actual_are_point_in_time`, `test_domain` calendar tests (7) |
| PEX08 querySeatDirectory | same names / relocated versions / anonymous pool labels are not merged | `test_pg_intel::test_seat_directory_never_merges_labels_or_versions_and_reports_respect_the_knowledge_cutoff` |
| PEX08 querySeatReports | after-close report is not intraday knowledge; later correction unseen before it exists; both sides of one seat stay | same test |
| PEX08 querySeatProfile | conditional on disclosure, sample floor (None, not 0), same seat both sides netted inside one report only, hand-checked mean | `::test_seat_profile_is_conditional_on_disclosure_has_a_sample_floor_and_nets_inside_a_report` |
| PEX09 queryCommunityOpinions | only claims bound to `pulse.community_forecast@1.0`; outcome is a separate artifact known only by `outcome_as_of`; empty when no forecast exists | `::test_community_opinions_only_bound_forecasts_and_outcome_is_a_separate_artifact` |
| PEX12 listAuthorizedAccounts | `account_head` has no owner: D15 grants; ungranted accounts invisible; revocation immediate | `test_pg_portfolio::test_grants_make_accounts_invisible_until_granted_and_revocable` |
| PEX12 importBrokerFills | duplicate does not double-book; changed redelivery = conflict for reconciliation; stale version keeps raw facts pending; unfillable fills stay pending; unknown fee never 0; cancel; API replay | `test_pg_portfolio::test_import_*`, `::test_changed_redelivery_*`, `::test_stale_account_version_*`, `::test_unknown_fee_*`, `::test_cancel_report_*`, `::test_api_import_*` |
| PEX12 queryAccountOrders / queryAccountPerformance | RESULT_NOT_READY until built; states from events; TWR from the ledger, cost x1/x3; missing marks = None; ownership | `::test_readmodels_orders_performance_and_not_ready_then_built`; `test_domain::test_order_views_*`, `::test_twr_cuts_at_flows_*` |
| PEX12 queryOpportunities | all denominators kept; sold-not-rebought inventory counted in the increment (`compare_with_hold`) | `::test_opportunities_keep_all_denominators_and_inventory_shortfall` |
| PEX13 saveAlertRule | CAS + owner, disabling stops new alerts and keeps history, foreign destination 403, annotation pin | `test_pg_alerts::test_rule_cas_owner_and_disable`, `::test_disabled_rule_and_older_claims_do_not_alert`, `test_pg_workspace_ops::test_deleting_an_annotation_stops_its_alert_rules_but_keeps_them`, `::test_api_save_rule_*` |
| PEX13 listUserAlerts | backfill never alerts; retry only adds attempts; delivery state overlay | `::test_scan_p0_p1_p2_dedup_supersede_and_backfill`, `::test_delivery_attempts_webhook_never_sent_unconfigured_and_retries_only_failed` |
| PEX14 deleteWorkspaceItem | VERSION_CONFLICT on stale, replay of repeated delete, id cannot be re-created, other owner 404 | `test_pg_workspace_ops::test_delete_tombstone_replay_conflict_owner_and_no_resurrection` |
| PEX14 queryWorkspaceRevision | old version readable, tombstone is not an empty body, unsealed history not faked | same test |
| PEX14 scheduleWorkspaceExport / queryExportArtifact | async; fixed inputs; spec stored before the job; csv/png; unfinished = null artifact; expiry; owner only; bad hash 409 | `::test_export_is_async_pinned_to_inputs_and_png_carries_its_signature` |
| PEX14 queryWorkspaceIndex | six kinds by owner | `::test_workspace_index_lists_the_six_kinds_by_owner` |
| PEX15 runRegisteredJob | ops permission, manual-trigger flag, exact definition hash, parameter hash, idempotent, stale epoch cannot commit | `::test_run_registered_job_needs_ops_permission_exact_definition_and_parameters` |
| PEX15 queryDatasetStatus / queryJobRuns | READY vs failed attempt vs source runtime vs freshness kept apart (market-closed is not a fault); run list scoped to the viewer | `::test_job_runs_are_scoped_to_the_viewer_and_dataset_status_keeps_states_apart`, `::test_source_runtime_and_freshness_are_reported_apart_from_snapshot_state` |
| All | mounted at the contract path, registered in `core.api_binding`, 403 without session, 400 on unknown/invalid input, response drift fails loudly, role grants | `test_contract_routes.py` (4) |

## 3. Old-site features brought back

| Old feature | New |
|---|---|
| News alerts P0 / P1 / P2 (E075) | `domain/alerts/tiers.py` on the I3 claims (`news-tier-v1`): P0 negative claim naming a holding, P1 negative policy/macro claim whose industry (versioned keyword table `alert-industry-map-v1` -> `legacy_boards_industry` boards) contains a holding and which names none, P2 watch-list or positive main-board claim not held. Unverified claims still alert and say so. Forward-book paper positions are not "holdings". `tests/ext_c/test_domain.py`, `test_pg_alerts.py` |
| User alert rules | `saveAlertRule` scopes `news_tier` / `instrument` / `sector` |
| Notification intent vs send attempt | `notification.alert_intent` (unique `(principal, dedup_key)`; key holds the claim CONTENT version, so a re-scan is a no-op and a status change supersedes) vs `delivery_attempt` (in_app confirmed immediately; webhook pending -> accepted/failed/unknown/expired; failed retried up to 3, unknown never auto-resent) |
| Webhook push (WeCom / Feishu / DingTalk / generic) | `adapters/notify/webhook.py`; enabled only by `PULSE_PUSH_WEBHOOK` (+`PULSE_PUSH_KIND`) or `PULSE_PUSH_WEBHOOK_FILE` / `<PULSE_SECRETS_DIR>/push_webhook.url`; the DB stores only the reference; the URL is in no repr / status / log; under pytest a send without an injected transport raises `SendDisabled` - no test sends |
| Financial / event calendar | `calendar.event_revision` (versioned, PIT `available_at`), Eastmoney 财经日历 sync, lock-up / suspension / meeting events only when the announcement TITLE states a date, rule-computed index-futures/ETF-option expiries (`rule_computed` until a notice), event -> industry links per revision (`event-links-v2`), stocks resolved point in time from stored memberships |
| Seat directory / profile | `querySeatDirectory/Profile/Reports`; conditional post-disclosure statistics with sample floor 10, entry = first session open after the report became available, raw (unadjusted) prices stated as a limitation |
| Portfolio | broker fill import into the unified ledger (raw object required first; facts stored before booking), account orders / performance read models (TWR cut at flows, cost x1/x2/x3 by re-costing fills with `scaled(user_account_2026, N)`), opportunities with hold comparison |
| Forward books | `LEGACY_FORWARD` tracked paper accounts `main_h5` (hold 5, 20 slots, 4 new/day from top 40, 50,000 CNY/name) and `shadow_h10` (hold 10, 2 new/day): registered on the ONE ledger, decided once per session from the current release's market ranking (5td), executed through the kernel at the next raw open (limit-up open = unfilled with reason), labelled 检验中, `ranking_weight = none` enforced by CHECK; the books start at the first session they see (no PIT ranking exists for earlier days) |
| Data health | `queryDatasetStatus`, `queryJobRuns`, `runRegisteredJob` (+ `workers/alerts.py drain`) |

## 4. Real runs on the dev DB (2026-11-12/13)

| Command | Exit | Result |
|---|---|---|
| `migrate` (after resetting the stale first 0040) | 0 | `applied 0040_ext_c_portfolio_alerts_calendar.sql` |
| `workers.forward register --principal 00000000-...-07` / `daily` | 0 / 0 | two books registered; both `decided` for 2026-11-11 (main 4 buys, shadow 2), NAV 1,000,000, 0 missing marks; books are executed when the 2026-11-12 bar exists |
| `workers.alerts init-rules` / `scan` | 0 / 0 | 3 default rules; scan read 0 claims after the cursor (no news since) |
| `workers.alerts calendar-expiries --year 2026 --month 12` | 0 | 2 rule-computed events |
| `workers.alerts calendar-derive` | 0 | 556 announcements matched by keyword, 537 have no date in the title (not turned into events), 0 dated events |
| `workers.alerts calendar-sync --days 30` (live, 1 req/s) | 0 | 2 HTTP requests, 380 rows -> 373 events, 17 industry links; second run: 373 `same`, 0 new/revised |
| API (TestClient on the dev DB, dev principal) | 200 | accounts, calendar (names via `/c/calendar-names`), dataset status, job runs, forward books (`检验中`), alerts, seat directory; `querySeatProfile` for the busiest resolved seat: 4,405 reports in a year, 1-day net-buy mean return +1.31% on 1,156 samples (raw prices; shown with its limitations - not a claim of edge) |

## 5. Not done / gaps / honest status

* **Contract gaps handled by supplementary reads** (proposed additions, not in the OpenAPI): `ArtifactRef` bodies (`/v1/x/c/artifact`), alert rules + destinations (`/c/alert-rules`), calendar names and links (`CalendarRevision` has no name field), forward books, job definitions (id + hash needed by `runRegisteredJob`), built read-model contexts. Also missing: an operation to upload a raw broker report (the API takes an existing `raw_object_id`; `workers/forward.py import-fills` stores the file first).
* `getTradePlan` (routes_decision.py, not my file) still returns every version; it must apply the new owner filter (`decision.plan_revision_metadata.origin = user_edit` -> owner only) like `queryPlanVersions` does.
* Runtime roles (`0007`): the API role has no INSERT on the ledger tables, so `importBrokerFills` needs the API process to run with a role that may write `execution.*` (as `ExecutionService` does); I did not widen 0007.
* Price-line `Condition` evaluation for alert rules is not implemented: only `news_tier` / `instrument` / `sector` scopes fire; the condition is stored and its annotation (id + version) pinned. No intraday "already reacted" magnitude (evidence field stays null). No LLM fallback judgement.
* `importBrokerFills`: fee correction reports (`correction_of_source_execution_id`) are stored as pending reconciliation, not applied (the kernel supports only `fee.cash_adjustment`; wiring it needs the original fill event id mapping). Fills dated before the account's ledger day are pending.
* Read models are built by `build_account_readmodel` job / `workers.forward readmodels`, never inside a GET; the account page needs a built context. Orders: a cancel request in flight leaves no event (stated in `coverage`).
* Forward books: no equity-curve gate (the pre-registered comparison is ungated vs ungated), execution at raw open without queue model, weekday-only calendar beyond the last observed bar (holidays unknown), calendar `assumed_calendar_after` is recorded per day; main/shadow use the release's market ranking, not the old fusion scores.
* Exports: parquet/csv only for raw daily bars; PNG is a code-drawn raster with metadata in text chunks (no font renderer); per-source export entitlement is unconfirmed for every public source and not enforced.
* Cluster = I3 claim (its documents); no cross-claim merging. Community forecasts: no extractor exists (I3), so the list is empty on real data.
* Eastmoney 财经日历 carries no expected / actual values; macro revisions are only fixture-tested.
* Tier P1 uses a hand-written keyword -> board table (the only industry map is the 48 Sina boards); coverage is limited and listed in `domain/alerts/tiers.py`.
* Webhook push was never sent to a real endpoint (by design); delivery paths are proven with `httpx.MockTransport`.

## 6. Final checks

| Command | Exit | Result |
|---|---|---|
| `uv run pytest -q` (whole repository, shared session DB) | 0 | **717 passed**, 48 subtests (338 s); the first full run showed one failure of `test_pg_plans` caused by `ops_service.run_next` claiming other packages' queued jobs - fixed: it now claims only this group's registered job ids, and the tests drain by job id |
| `uv run pytest tests/ext_c tests/db/test_roles.py -q` | 0 | 65 ext_c tests (24 domain, 41 real-PG/API) + roles |
| `cd apps/web && npx tsc --noEmit` / `pnpm test` / `pnpm build` | 0 / 0 / 0 | 9 test files, 109 tests (incl. other groups) |
