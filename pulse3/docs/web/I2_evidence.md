# I2 evidence: remaining /v1 reads, account projection, web switched to the real API

Date: 2026-11-12. Working tree, not committed. PostgreSQL 18.3 at `127.0.0.1:55432`. Nothing here claims alpha or performance.

## 1. What was built

| Operation | Code | Source tables / behaviour |
| --- | --- | --- |
| getStockWorkspace | `routes_market.get_stock_workspace` | `decision.workspace_projection` (latest `projection_version` of release + view_kind; scope `public` or `<account_id>` / `account:<account_id>`). Frozen keys only from the payload; `instrument` from core; `projection_hash` untouched. Overlays: `quote_overlay` (latest quote) and `actionability_id` (latest valid `decision.actionability_snapshot` of account+release) added per request. 400 bad view_kind / missing account_id, 404 unknown release/instrument/projection, 409 `RELEASE_COMPONENT_UNAVAILABLE` when a `workspace*` component is MISSING/FAILED/STALE, 403 without principal for the account view. SUPERSEDED release answers with `RELEASE_SUPERSEDED` warning and freshness stale. |
| getStockQuote | `get_stock_quote` | `market.quote_projection` (READY manifest check, 409 `QUOTE_SNAPSHOT_NOT_READY`) else latest `CanonicalQuote` observation (policy `quote-observation-latest-v1`, warning `QUOTE_PROJECTION_ABSENT`, derived `quote_snapshot_id` uuid5(observation, policy)). Zero bid/ask/last becomes null; daily close is never a quote (404). Freshness derived at read time from the endpoint's `stale_after_seconds` (`stale` / `unknown`). |
| getStockBars | `get_stock_bars` | One READY snapshot, `available_at <= decision_as_of` (actual available_at; unregistered policy strings only add `AVAILABILITY_POLICY_NOT_REGISTERED`). Capability check with `domain.market.capability.evaluate` against the manifest capability (409 `CAPABILITY_UNAVAILABLE`), adjustment mode/version (409 `ADJUSTMENT_MODE_UNAVAILABLE`, `ADJUSTMENT_VERSION_MISMATCH`, 400 version on raw), session scope (409 `SESSION_POLICY_UNAVAILABLE`), 409 `SNAPSHOT_NOT_READY`/`SNAPSHOT_NOT_BARS`, 404 snapshot. Raw: `market_repo.read_bars` + `ingest.observation.field_quality`. Forward-adjusted: read-only scan of the frozen lake manifest (hash verified, 503 on tamper). 1w/1mo/1q/1y: `domain.market.bars.aggregate` over a calendar observed from the data; not final until the calendar proves the period ended, and a first period that starts inside the data is `partial`. |
| getStockEvidence | `get_stock_evidence` | Latest claim revision per `claim_id` with `available_at <= release.decision_as_of`, subject `issuer`/`instrument` matched by instrument id or cninfo/legacy code alias; documents and evidence links PIT-filtered; keyset cursor pinned to the release (409 `CURSOR_RELEASE_MISMATCH`, 400 `CURSOR_INVALID`). |
| getStockCommunity / FundFlow / Seats | same module | `community_observation` (linked through `analysis_payload.instrument_ids` / `instrument_id`), `funding_observation` (latest revision per series), `seat_report` + `seat_report_row`. No rows: empty contract-valid list, `quality_status=partial`, coverage expected 1 / observed 0 / missing 1 with a reason. |
| getDataStatus | `get_data_status` | `core.source_endpoint` x `ingest.source_health` x `ingest.source_cursor`, one item per endpoint/partition. `enabled` (intent) is separate from `runtime_state`/`last_transport_ok_at` (transport) and `freshness_state` (content). An endpoint without health is `unconfigured` / `unknown`, partition `default`, null times. A `current` sample older than the endpoint's stale threshold is reported `unknown` + `HEALTH_SAMPLE_OLD`. |
| getAccountProjection | `routes_account.project_account` | Events up to `account_version` replayed through `LedgerReducer` (never the mutable projection); marks = latest raw 1d close inside the READY valuation snapshot. NAV identity asserted. Missing mark: NAV / unrealized / cost-origin bridge null, position `mark_quality_status=invalid`, meta partial + `MARK_MISSING`. `withdrawable_cash_cny` null. 409 `ACCOUNT_VERSION_UNKNOWN`, 404 account/snapshot, 409 `SNAPSHOT_NOT_READY`, 403. |
| getModelRun | `routes_account.get_model_run` | `analysis.model_run` joined to `analysis.model_policy` for the output `ArtifactRef` contract id/version. |
| `/app-config.json` | `routes_market.build_app_config` (+ `app.py` include) | Not a contract operation. See section 3. |

`app.py`: `routes_account` added to the router loop and `routes_market.config_router` included before the SPA catch-all. No other shared file touched.

## 2. Evidence (commands and results)

| Command | Result |
| --- | --- |
| `uv run pytest tests/api -q` | 24 passed, exit 0 (real PostgreSQL: 10 market, 6 intelligence/status/model/config, 3 account, plus the 5 existing) |
| `pnpm tsc --noEmit` (apps/web) | exit 0 |
| `pnpm test` | 5 files, 49 passed (2 new: snapshot selection, adjustment-version rule) |
| `pnpm build` | exit 0, `apps/web/dist` served by FastAPI |
| `pnpm mock:validate` | 77 responses, 0 invalid, 31/31 operations (mock mode still works) |
| pyflakes on the new modules | clean |

Behaviour proven by the tests: PIT cut-offs for bars and evidence (a later bar/claim/revision never appears), latest-revision selection, cursor pinning, capability refusal for adjusted-on-raw / minute-on-daily, adjusted bars from a real lake manifest (other instruments excluded, tampered manifest -> 503), weekly bar final only for a finished week, stale quote at read time, projection precedence over observations, empty-partial intelligence, account version replay (version 0 unchanged after a deposit) and NAV identity.

### Browser check (real API, not mock)

The dev database had **no published release** during this package (`select * from decision.release_slot` was empty on 2026-11-11 and 2026-11-12), so I could not view I1's data. What was checked:

1. API on :8200 against the dev DB, built `dist` served by FastAPI. Home: release bar shows the structured `NOT_FOUND` (slot `main` has no release) instead of a blank page. **Data status page** (dev DB, real health rows): 13 endpoint rows; enabled / runtime / freshness / four timestamps rendered, never-requested endpoints show 未配置 / 从未请求. Real calls: raw daily bars of 000001 (46 bars to 2026-11-11), forward-adjusted weekly from the legacy lake (44 bars, last one not final), 1m bars (240), quote (`stale`, `QUOTE_PROJECTION_ABSENT`).
2. A throw-away database `pulse_i2_demo` (dropped afterwards; the dev DB was not written) with one hand-made release (empty workspace projection, no ranking) plus real 000001 data (raw rows copied from the dev projection, real legacy lake manifest) and API on :8201. Stock workbench `/stock/<id>`: header, release bar, K-line chart rendered real raw daily candles; switching to 前复权 shows `前复权（legacy-qfq-unversioned）`; 周线 renders; quote card shows the structured `NOT_FOUND` (no quote in that DB) and the workspace says it only shows frozen analysis; MA/MACD read 缺失 (no indicator values exist, gap G2). Home and sector pages: release bar fine, rankings `NOT_FOUND` (no ranking in the demo release), sector heating components all 缺失. These are rendering checks of the real API path, not evidence about I1's content.

## 3. Web switch

* Real mode is the default (`vite.config.ts` proxies `/v1` and `/app-config.json` to `:8200`; the mock plugin loads only with `VITE_USE_MOCK=1`).
* `main.tsx` fetches `/app-config.json` before importing `App`, puts it on `window.__PULSE_CONFIG__`; `config.ts` precedence is `VITE_*` > served config > default. Default slot is `main`. Served keys: `slot_key`, `horizons`, `taxonomies` (read from `decision.ranking_run` of the slot's current release, defaults otherwise), `quote_policy_version`, `session_policy_version`, `availability_policy_version`. Env `PULSE_SLOT_KEY`, file `PULSE_APP_CONFIG_FILE`.
* `barsParams`: `pickBarsSnapshot` chooses the snapshot by timeframe and adjustment from `meta.input_snapshot_refs`; real mode sends no `adjustment_version` unless the frozen feature key declares one (the server fills the snapshot's own); default session scope `regular_continuous_v1`.
* StockPage: the "window not confirmed complete" note no longer claims missing bars when there are none.

## 4. Contract / data gaps found (no contract change made)

* **I2-G1 per-batch raw snapshots.** `bars_raw_1d` currently has ~20 READY snapshots (one per ingest batch), each holding only its own codes. `getStockBars` is per snapshot, so a release must pin a snapshot that covers the instrument; the client picks by dataset id only. Needs a merged universe snapshot or a "snapshot for instrument" lookup.
* **I2-G2 `window_complete` is derived**, not read from a persisted `bars.window` artifact; the exchange holiday calendar is not ingested, so every daily window carries `exchange_calendar_not_ingested`. Windows starting before dataset coverage are incomplete.
* **I2-G3 revision history in the lake.** The legacy lake keeps only the latest revision per bar, so the PIT filter on `available_at` drops a bar whose latest revision is later than `decision_as_of` even if an earlier revision was known. Backfilled history carries ingest-time `available_at`, so a release decided before that import sees no legacy bars (no `policy_usable_at` policy exists for history rebuild).
* **I2-G4 quote snapshots.** `market.quote_projection` is empty and the read-only API cannot fill it; the quote falls back to the observation with a derived id (flagged). A quote snapshot job must populate the projection.
* **I2-G5 community linkage.** `community_observation` has no instrument column; the API matches `analysis_payload.instrument_ids` / `instrument_id` (documented convention to register with the analysis schema). Claims are linked to instruments through issuer code aliases (cninfo claims have `subject_kind=issuer`, no instrument resolution).
* **I2-G6 workspace scope key / frozen payload shape** for account views (`<account_id>` or `account:<account_id>` accepted) and payload keys (`chart_window_refs, features, plan_refs, evidence_revision_ids, strategy_contributions`) are assumed from the contract; verify against I1's rows when they exist. Workspace `evidence_revision_ids` are not used to pin the evidence tab (evidence is selected by subject and release time).
* **I2-G7 ids in the demo.** Valuation marks come from the 1d bars of the named snapshot only (no quote marks); `valuation_policy_version` = `last-raw-close-in-snapshot-v1` is my own name.
* Carried from M4 and unchanged: G1 (names per row), G2 (no indicator series), G3-G8.

## 5. Not done / limits

* No view of I1's real release content (none was published); home ranking, sector ranking, plan card and account actionability were seen only as structured errors or empty states.
* Account ownership is single-tenant (any valid principal reads any account).
* Lake read is synchronous per request (13 parquet members, filter pushed down): fine at this scale, not measured under load.
