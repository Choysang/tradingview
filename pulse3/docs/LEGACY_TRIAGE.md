# Legacy feature triage (2026-11-13)

Principle (user decision): strict entry, clean exit. A legacy feature that is useful comes back into pulse3, but only
the pulse3 way: one contract (an extension read where the v3 contract has no slot), point-in-time data, one ledger,
tests. No legacy code is imported. A feature is retired only with a written reason. A retired feature is listed here;
nothing is dropped silently.

Source: re-audit of all 206 rows of `LEGACY_FEATURE_INVENTORY.md` against the code on 2026-11-13. The audit found 37
rows ported, 135 partial and 34 missing. Row numbers (#) refer to that inventory order.

## Batch 1: port now (useful, data exists, small or medium)

| # | Feature | What comes back | Data source |
|---|---|---|---|
| 41, 159 | Chart indicator overlays | MA, EMA, BOLL on price; MACD, KDJ, RSI panes, served by the server and drawn by the page | `domain/features/indicators.py` (already computed) |
| 10, 14 | Picks row details | entry zone, stop, target, holding days and exit plan in each ranking row | the release's `TradePlan` |
| 3, 8, 31 | Click-throughs | market tiles and the growth strip open the screen; theme members open the screen | existing screens |
| 17, 20 | Playbook equity and trades | equity curve with drawdown and the recent-trade list | `domain/playbooks/replay.py` (records exist) |
| 18, 24 | Regime attribution and heatmap | average trade by entry regime, per playbook and as a playbook x regime heatmap | replay trades x regime series |
| 33 | Pattern family filter | filter the 12 pattern cards by family | catalog field exists |
| 29 | Sortable results | sortable columns in the screen table | screen run rows |
| 38, 39 | Watchlist cards | last price, change, sparkline, amount, rank; top-8 suggestions when the list is empty | quotes and the release ranking |
| 43 | Stock stats | turnover estimate, distance to the 60-day high, 1-year limit-up count, listing date | features already registered |
| 30 | Theme treemap | area = amount, colour = average change | sector overview |
| 50, 51 | Command palette and shortcuts | Ctrl+K search over stocks and pages; `/` focuses the ask bar | client only |
| 15, 92 | Recompute trigger | the release build as a registered manual job (runs through the job queue) | `ops_service` manual jobs |
| 97 | Test push | send a test message to a configured alert destination | `adapters/notify/webhook.py` |
| 76 | Experiment archive | list past experiments | `research.experiment` |
| 63, 64 | Agent access | `llms.txt` and a capability manifest, both generated from the registered contracts | `core.api_binding` |
| 169 | Forward monitor | rank IC and excess return of each published ranking against later outcomes (also M5-E04 evidence) | releases + raw bars |
| 61 | Health probe | `/healthz` with a database check (done 2026-11-13; unknown `/v1` paths now return a JSON 404 instead of the web page) | - |

## Batch 2: port before the M6 cutover (the old system still does these daily)

| # | Feature | Why before cutover |
|---|---|---|
| 103, 185 | Intraday recorder (auction, book, limit queues) | the old server records it every session; losing it at cutover loses data |
| 131, 187 | 7x24 news recorder | pulse3 fetches news once a day; alerts need a continuous feed |
| 132, 188 | Scheduled news alerts (P0-P2 against holdings) | pulse3 alerts are manual only |
| 104, 186 | Extended intraday (ETF, index futures, A50, CNH) | recorded by the old system since 2026-11-08 |
| 5, 124 | Seal clock and seal span | need the intraday recorder's first-seal times; blocked until it exists |
| 135 | Daily report push | the old system pushes a summary after the close |

## Status (2026-11-16)

Batch 1 and batch 2 are ported. Reads are under `/v1/x/bf/<lane>` with one contract file per lane
(`src/pulse/contracts/data/backfill_<lane>.schema.json`); tests are in `tests/bf_<lane>/` and `tests/ext_c/test_pg_daily_report.py`.

| Lane | Rows | Notes |
|---|---|---|
| stock | 41, 159, 43 | indicators served by the server; turnover is unavailable (with a reason) when the float cap is not positive |
| board | 3, 8, 31, 30, 38, 39, 50, 51, 10, 14 | watch cards, theme treemap, command palette, click-throughs to the screen |
| research | 17, 20, 18, 24, 33, 29, 76 | replay trades for equity and regime attribution; `migrations/proposals/bf_research_trades.sql` is a proposal, not applied |
| ops | 15, 92, 97, 63, 64, 169 | the release build as a manual job (an `as_of` in the future is refused); test push; `llms.txt` and manifest; forward monitor |
| live | 103, 185, 131, 187, 132, 188, 104, 186, 5, 124 | during the parallel run pulse3 imports the old server's recordings as the declared source `legacy_recordings` (the previous day is re-imported to catch late files); the native recorder exists but is not scheduled, so the same IP does not send twice the requests; seal reads are pinned to the release cutoff |
| report | 135 | `alerts daily-report` after the daily build, once per (principal, release); in-app only on the server until the cutover, so the old system and pulse3 do not both push |

Forward monitor cost (2026-11-17): each snapshot lists its ranking ids in its coverage document, so a run reads the lake only
for snapshots that can hold a wanted id, and a ranking unresolved 150 days after its decision is reported as `expired`.

Server check (2026-11-16 and 2026-11-17 runs): the daily build, forward monitor and daily report steps ran unattended
(`done ok=True`). The daily report first sent nothing: the site's principal had no destination. The report now creates the default destinations (in-app always on) for the site's known principals first.

## Batch 3: research tools, ported one at a time through the research registry

Old lab scripts (#167-#184, #22/#152 rotation, #36/#77 batch comparison, #160 tear sheet, #171 curve gate,
#172/#173 tail and state forward logs). The old research lab keeps running unchanged until each tool has a
registered pulse3 equivalent; a tool enters pulse3 only with a pre-registration and a test, as the package requires
for strategy candidates.

## Retired (with reason)

| # | Feature | Reason |
|---|---|---|
| 47, 62, 117 | SIM/LIVE switch, synthetic history | the package forbids synthetic data in production views; mock data stays in the dev build only (`pnpm dev:mock`) |
| 58 | Aurora background | decoration that changes colour with sentiment; it carries no information the gauge does not, and it can hurt contrast |
| 125, 205 | SQLite/DuckDB storage | replaced by PostgreSQL + the content-addressed lake (package architecture) |
| 126, 194 | In-memory ticker refreshing every few seconds | replaced by frozen releases and versioned quotes (package rule: pages read a fixed release) |
| 198 | GitHub publish script inside the app | it stays at repository level (`scripts/publish_github.sh`); it is not an app feature |
| 184 | Tushare utilities | paid source, not in the declared source list |
| 65 | MCP server | deferred, not retired: `llms.txt` and the manifest (batch 1) come first; MCP follows when an agent needs write access |
