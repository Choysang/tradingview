# I1 evidence: analysis worker and AnalysisRelease pipeline (M3 chain, M4-E02 bundle, M5 inputs)

Run 2026-11-11 to 2026-11-12, Windows 11, Python via `uv run` from `pulse3/`, PostgreSQL 18.3 at `127.0.0.1:55432`. Not committed.
Real data: the dev DB `pulse` (legacy import 2025-11-03..2026-11-10, 3,214 main-board instruments) plus the raw backfill below.
Synthetic data: only the tests (`tests/integration/world.py`, random walks written in the legacy warehouse layout and imported by the real
`DataService.import_legacy`). Nothing here claims edge: the baseline is an explainable fixed-weight scorer, uncalibrated, no probability.

## 1. Files

Created: `src/pulse/application/bindings.py`, `release_service.py`, `account_service.py`, `src/pulse/workers/analysis.py`, `research.py`,
`tests/integration/{__init__,conftest,world,test_release,test_account_workers}.py`, this file.
Edits outside my allowed paths: **none** (no other package's file was touched; `git status` shows only the I2/other-package changes that were
already there). Two cross-package facts I worked around instead of editing: `DataService.ingest_quotes` takes at most 50 symbols per request
(I chunk), and `ingest_repo.ensure_endpoint` forces `schema_version 3.0.0` on the contract FK (my internal contracts register at 3.0.0).
Size note: `release_service.py` is ~920 lines and `bindings.py` ~680 (> the 800-line guideline for the first); the allowed-path list forbade splitting it.

## 2. Raw price backfill (item 1)

`pulse.workers.ingest live-daily --source sina --count 250` over the full main-board universe (3,214 codes from the legacy snapshot), run as 17
sequential chunks of 200 codes at 1 request/s, stop-on-nonzero (the adapter's breaker would have stopped on a block). Started 2026-11-11 20:31,
finished 22:24 (~5.3 min per 200-code chunk: 200 s of requests + ~2 min normalization). All 17 exit 0, **no block**, 3,214 / 3,214 codes.
Result: `market.dataset_manifest bars_raw_1d` 18 READY snapshots (versions 1-18), `market.bar_projection` 1d raw rows for 3,214 instruments,
794,881 rows 2025-11-03..2026-11-11 (turnover amount NULL, quality `partial`, as the data package documents). PostgreSQL stopped uncleanly after
the backfill (machine restart, "last known up 22:30"), crash-recovered on restart (`server.log`), all 18 manifests and rows intact.
Legacy forward-adjusted bars stay research-only: the universe price cap, plan zones, ATR/MA for conditions and quotes use raw bars; ranking
features use the adjusted series. The two bases are never mixed inside one series.

## 3. Design (what the code does)

* **bindings.py** - `PitFeatureMatrix` (`FeatureMatrixPort`): derived features from the registered `domain.features` kernels (`ma`, `relative_strength` vs
  000300, `volume_ratio`, `atr`, `macd`), each `(feature, params, instrument set)` registered in `market.feature_result` under the full 19-dimension
  key and reused when READY (re-run: `reused`). Visibility: observed policy = `available_at <= decision_as_of`; reconstructed policy
  (`reconstructed-session-close-plus-1h-v1`) = `policy_usable_at(trade_date) = 15:00 CST + 1h <= decision_as_of`, stored `available_at` untouched. The
  matrix refuses to answer for a cutoff different from the one its inputs were loaded for. Missing stays `None`. `PgReleaseComponentWriter`
  (`ReleaseComponentWriter` over `PgAnalysisRelease.mark_ready`), `LedgerAccountSnapshotPort` (`AccountSnapshotPort` over `LedgerRepo.load` replay), instrument
  facts, circulating-cap facts (legacy `securities.float_cap`, read through the legacy adapter, published as dataset `instrument_circulating_cap_legacy`),
  sector membership (taxonomy `legacy_boards_industry` v1, 48 nodes from the legacy boards evidence, `core.instrument_tag` rows + dataset snapshot
  `sector_membership_legacy_boards_industry`). Derived datasets use an internal source `pulse_analysis`/`release_artifacts` and contracts
  `pulse.sector_membership`, `pulse.instrument_cap_fact`, `pulse.analysis_artifact` (registered at 3.0.0).
* **release_service.build_release** - input context -> `decision_as_of` = latest READY source manifest time (explicit `--as-of` accepted) ->
  `release_id = uuid5(slot, context hash, attempt)`; a published release for the same context is returned (`replayed`). Horizons (explicit, versioned):
  短线 `5td`, 波段 `20td`, 长期 `60td`; targets `rel_return_<h>_vs_universe_median_after_declared_costs_v1`; policy `baseline-explainable-<h>-v1` (fixed weights,
  direction, source group per feature in `HORIZONS`); trial budget rules run with no approved candidate, so the baseline keeps weight 1. Universe: SH/SZ
  main board, ST out, new listing (< 60 sessions) out, raw price <= 100, circulating cap <= 100e8 CNY (unknown cap -> excluded with reason; if the
  cap is unknown for *every* instrument the bound is not applied and the universe policy version says `+circ-cap-unavailable`).
  Plans: top N (default 10) per horizon, state `watching`, validation `pending`, raw-price entry zone `[close - 0.5 ATR14, close]`, ATR invalidation / target,
  condition trees from the shared kernels, portfolio fraction 5%, strategy refs to the nearest catalog candidates (S001 entry, S056 exit, S054 holding,
  S060 allocation) - none implemented or admitted, so none can be `actionable`. T and grid sub-plans (`build_inventory_subplans`, bodies in
  `decision.plan_body`, `validation.result_ref = planbody:<kind>:<hash>`) exist only for held, sellable inventory. Fast-heating: four standardised surge components
  (relative-strength change, volume-ratio acceleration as the turnover proxy, breadth, leader confirmation); news/community/fund stay MISSING optional
  components (`ALLOW_MISSING`). Pool events through the domain state machine with the previous state loaded from the DB. Workspaces: contract `Workspace`
  (the `$defs` name behind `getStockWorkspaceResponse.data`), view `public_research`, scope key `public`, projection version 1, per-instrument FeatureKeys for
  MA20/MA60/EMA12/MACD/RSI/ATR. Explanations: all bodies (`explain:<sha>` and `summary:<component>:<digest>`) in READY dataset `analysis_explanations`, pinned
  by the `explanations` component. Derived `available_at` = `max(clock, decision_as_of)` for every component and event.
* **Publish**: `ensure_slot` -> `begin_stage` (captures the base pointer) -> components -> `publish` (slot lock, stale-stage check, `decision.published` outbox row
  in the same transaction). A required component that is missing/FAILED/expired raises `IncompleteRelease`; the build marks the release FAILED and the pointer
  does not move. Required: `bars_research`, `bars_raw`, `universe`, `sector_membership`, `explanations`, `workspace_projection`, `ranking:market:<h>` x3,
  `sector_overview:<tax>:<h>` x3, `sector_rising:<tax>`, `ranking:sector:<tax>:<h>` x3, `plans:<h>` x3, `candidate_pool:<id>`; optional `heating.*` x7.
* **account_service** - `open_paper_account` through `LedgerRepo.create_account` (account `98ef211e-7a35-5bc8-bc45-0ce9e3e367ba`, 1,000,000 CNY,
  event cost policy `user-account-2026-11-08-v1`); `publish_quote_snapshot` (Sina quotes -> observations -> READY `quotes_l1` dataset **and
  `market.quote_projection` rows**, G4); `build_actionability` (plan trigger evaluated on the last confirmed raw daily bar with Kleene logic, quote as the price;
  stored through `plan_service.store_actionability`).
* **workers/analysis.py** - `build [--as-of latest|ISO|date] [--availability observed|reconstructed]`, `daily` (cninfo announcements days=1, Sina raw daily
  increments for the focus set = plan instruments + holdings, then build), `open-account`, `actionability`, `show`; exit 0 / 1 not published / 2 blocked.
* **workers/research.py** - `seed` (contract registry, strategy registry 61+105 candidates, 46 papers, job definition), `enqueue`, `run`: claims through
  `PgJobStore.claim` (lease epoch = fencing token), seals ONE fixed `EvaluationContext` (research + benchmark snapshots, sessions split 60/25/15, policies fixed),
  registers hypothesis and experiment BEFORE running, heartbeats before writing the result, then completes (metrics) or fails with `error_code`.

## 4. Real run (dev DB)

| Command | Exit | Result |
|---|---|---|
| `research seed` | 0 | 61 strategy + 105 factor candidates (all `discovered`), 46 papers |
| `analysis open-account` | 0 | account opened, 1,000,000 CNY |
| `analysis build --as-of latest --top-n 10` | 0 | **READY** release `21a2f6dc-8e87-5e4e-bc64-f6a83e9c24f3`, wall **130.8 s** (CLI 2m15s), session 2026-11-10 |
| same command again | 0 | `replayed: true`, same release id, 2.3 s |
| `analysis actionability --horizon 20td --limit 30` | 0 | 1 Sina request, 10 plan entries: 2 eligible, 8 not_triggered; `account_version 0`; quote snapshot `667470fa-...` (10 `quote_projection` rows) |
| `research enqueue --catalog-id S001` + `research run` | 0 | experiment `failed / strategy_not_implemented` recorded (job failed) |
| `analysis daily --top-n 10` | 0 | 31 cninfo pages (925 documents), 19 focus codes (50 requests incl. announcements), then release `ac38b4fa-4988-50c6-a388-1cb9ab4b1684` **READY**, pointer generation 2, 50.3 s; release 1 became SUPERSEDED |

Counts of the current release (`ac38b4fa-...`, identical shape to the first): ranked instruments **1,971 per horizon** (5td / 20td / 60td; universe 1,971 included,
3,964 excluded: BSE/ChiNext/STAR boards 2,440, circulating cap above 100e8 CNY 1,510, ST 317, price above 100 28, new listing 14, unknown cap 14, unknown
listing age 1,331, no raw price for the rest); sectors **48** ranked in 4 overview runs (3 opportunity horizons + `sector_rising_rank`); in-sector ranking runs
**129** (43 sectors x 3 horizons; 5 sectors have < 5 included members and are listed as skipped in the component summary); pool items **18** (release 1: 18
`observing` seq 1; release 2: the same 18 `qualified` seq 2, legal transition); TradePlans **30** (10 x 3 horizons, all `watching`/`pending`; release 2 appended
version 2 with `supersedes` -> version 1 for each); workspaces **96** (88 market page-1 instruments of the 3 horizons + 8 pool instruments, all `Workspace`-valid);
explanation bodies 9,456; shared feature results 585 computed per release (1,175 READY in the table over both runs and the throwaway test keys); components: 20 required
READY, optional `heating.{relative_strength_change,turnover_acceleration,breadth,leader_confirmation}` READY, `heating.{news_novelty,community_growth,fund_proxy}` MISSING and
reported in `degraded_components`; outbox: 2 `decision.published`, 30 `market.dataset_snapshot_ready`.

## 5. Evidence IDs and the tests that prove them (all `tests/integration/`, real PG)

| Evidence | Proof |
|---|---|
| M3-E01 shared chain on real inputs | `test_release.py::test_three_horizons_with_explicit_versioned_targets_and_contract_valid_rankings`, `::test_default_universe_policy_excludes_with_reasons`, `::test_sector_overview_in_sector_rankings_and_pool` |
| M3-E02 baseline + trial budget, no probability | same ranking test (`calibrated_probability` null, unique 1..N alpha ranks); budget logic is the W05 domain exercised through `build_and_store_ranking` |
| M3-E03 plans / actionability | `::test_plans_are_watching_with_raw_price_zones_and_valid_conditions`; `test_account_workers.py::test_actionability_is_bound_to_release_account_version_and_quote_snapshot`, `::test_inventory_subplans_only_where_inventory_exists` |
| M4-E02 release bundle / workspace | `::test_release_is_published_with_every_required_component`, `::test_workspace_projections_are_contract_valid_and_cover_page_one`, `::test_explanations_are_stored_and_referenced` |
| M1-E03 missing required component blocks READY | `::test_a_missing_or_failed_required_component_blocks_ready` (skip and force-fail variants: release FAILED, slot pointer unchanged) |
| Idempotent re-run | `::test_rerun_for_same_inputs_returns_the_published_release`, `test_analysis_cli_build_is_idempotent_and_show_reports_the_pointer` |
| PIT / derived available_at | `::test_pit_loader_ignores_a_revision_ingested_after_the_cutoff`, `::test_matrix_refuses_a_different_cutoff`, `::test_derived_available_at_is_not_before_inputs`, `::test_history_release_uses_the_reconstructed_policy_and_records_it` |
| Pool history | `::test_pool_events_follow_the_state_machine_across_releases` |
| Ledger account (M2) | `test_account_workers.py::test_paper_account_is_opened_once_through_the_unified_ledger` |
| M3-E04 research worker, fencing | `::test_research_worker_records_unimplemented_trial_and_completed_trial_with_fencing` |

Commands: `uv run pytest tests/integration -q` -> 20 passed (about 31 s). Whole repository `uv run pytest tests -q` -> **508 passed**, 48 subtests, exit 0 (177 s).

## 6. Answers to the integrator

* **G3 (availability)**: implemented as asked. `BuildConfig.availability = "reconstructed-session-close-plus-1h-v1"` (CLI `--availability reconstructed`, explicit `--as-of`
  required) derives `policy_usable_at` per bar (and per index bar), uses the instrument state by `effective_from` only, never rewrites `available_at`, records the policy in
  `decision.release.availability_policy_version`, in every FeatureKey and in the release input-context hash. The live release uses the observed policy. In the
  real dev DB the live releases are observed: `decision_as_of` is the newest READY source manifest time, which is after every ingest time.
* **G1 (raw snapshots)**: each workspace `chart_window_refs` carries a raw-bar ArtifactRef `bars:<snapshot_id>:<instrument_id>:1d:raw` where `snapshot_id` is the manifest that
  holds that instrument's latest raw bar (so it covers the instrument), plus the research ref `bars:<research snapshot>:<instrument>:1d:forward_adjusted`. The release component
  `bars_raw` pins the last chunk snapshot as `snapshot_id` and lists the count/digest of all chunk snapshots in `artifact_ref`/`artifact_hash`; I did not publish a
  consolidated manifest (it would duplicate ~800k projection rows).
* **G4**: `market.quote_projection` is populated by `publish_quote_snapshot` (10 rows in the real run).
* **Workspace scope key**: I wrote only `view_kind=public_research`, `scope_key=public`, `projection_version=1`. I did **not** write account workspaces; for those I recommend
  `account:<account_id>` (a bare uuid could collide with future non-account scopes). The actionability id is carried by the overlay, not by the frozen projection.

## 7. Not done / limits / honest status

* Ranking features come from the **legacy adjusted snapshot, which ends 2026-11-10**; there is no continuing adjusted source, so a later `build` keeps session 2026-11-10 until
  one exists. Raw increments (to 2026-11-11 and daily) only feed prices, plans and quotes. Splicing raw returns onto the adjusted series was rejected (ex-rights risk).
* The lake snapshot keeps only the **latest revision per bar key**; a bar revised after the cutoff therefore shows as missing, not as its older revision (no leak, but data loss for
  that instrument; test documents it). Observations still hold every revision.
* The baseline weights, plan template (pullback entry, 0.5 / 1.5 / 2 ATR levels, 5% fraction) and pool thresholds (enter 0.75, exit 0.25, dwell 2, cooling 3) are **uncalibrated
  engineering parameters**, not validated. No plan is `actionable`, no strategy admitted, plan validation is "not run"; the plan refs name the nearest catalog candidates, which are `not_implemented`.
* Surge components: turnover acceleration is a **volume-ratio proxy** (the legacy bars have no turnover); news novelty, community growth and fund proxy have no source and are MISSING.
  Pool dwell is approximated by releases published since the previous event (no counter column).
* Circulating cap uses the legacy `float_cap` with an **assumed unit of 100M CNY** (magnitude check only); unknown caps are excluded.
* Workspaces cover market page 1 of the three horizons plus pool instruments, not the first page of each in-sector ranking; `evidence_revision_ids` and `strategy_contributions` are
  empty (no instrument-to-document link query; the baseline is not a registered strategy instance).
* Actionability triggers are evaluated on the last confirmed raw daily bar with the quote as price; intraday bar-close evaluation is not implemented. After hours quotes are old; freshness is derived later.
* The research worker ships no evaluator: every catalog candidate is `not_implemented`, so real experiments end as recorded failed trials. The success path is proven with an injected evaluator.
* The in-sector ranking of 5 small sectors is skipped (listed in the component summary); the analysis worker is not scheduled (no cron).

## 8. Proposed contract / schema changes (not applied)

* `FeatureOperand` has no parameter slot: a plan's `ma` operand cannot say which period without a registered FeatureKey. Interim: the period is encoded in
  `resolution_policy_version` as `raw-latest-completed-bar-v1;n=20` (read by `account_service.RawEvalContext`). Proposal: add `parameters` (object) to `FeatureOperand`.
* A `release_artifact` table (or `decision.release_component.artifact_body`) so explanation bodies need not live in a derived dataset; today they are a READY dataset snapshot.
* A `PromotionDecision`/availability-policy ArtifactRef contract (the context uses `pulse.availability_policy` 1.0 as an unregistered contract id).
* `ingest_repo.ensure_endpoint`/`publish_dataset` hard-code schema version `3.0.0`; internal derived contracts therefore register at 3.0.0.
