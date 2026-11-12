# Old site feature inventory (2026-11-12)

Retention checklist: every old-site feature and whether pulse3 has it. The old code stays untouched and keeps running; features marked no/partial are ported into pulse3 through the contract-extension process.

Resumed and finished the inventory. The full table is below. Legend: **yes** means the same behaviour exists in pulse3, **partial** means some of it does, **no** means none found in `pulse3\src` or `pulse3\apps\web\src`. Every path is absolute. The working-directory change does not affect any result.

**Notes that change how to read the table**

- The `ledger/README.md` in your list is at `docs\research\ledger\README.md`. There is no top-level `ledger` folder.
- `PulseLhbDaily` has zero references anywhere in the repo. `PulseNews` and `PulsePaper` appear only in docs. Their task definitions live only on the server, per `docs\rebuild\01-audit.md` (finding A4).
- PulseIntraday's exit time conflicts: `round7-plan` says 15:30 (line 81), `intraday.py` says 17:00 (line 7).
- I read "holdings book" as the holdings file that `news_alerts` reads, plus the frozen main book in `forward_book.py`. Tell me if you meant something else.
- The pulse3 strategy catalog (61 candidates, 105 factors) comes from package catalogs outside `pulse3\src`. I did not open it. The seed marks every entry unimplemented (`pulse3\src\pulse\domain\strategy\seed.py`).
- pulse3 depends on the old system at runtime: `Pulse3Daily` step one runs `legacy-import` against the old warehouse output (`pulse3\deploy\windows\run_daily.ps1:2-3,16`).
- Scope: I did not open `node_modules`, `.venv`, `data` or `.work`. One earlier unscoped glob under `pulse3` printed file names inside `node_modules` and `.venv`. No contents were read.

### 1. Pages, tabs and panels (`frontend\src`)

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| Market overview `#/overview` (default view) | Landing dashboard: sentiment, vitals, ladder, seal clock, breadth, themes, tape, growth | `frontend\src\features\overview\OverviewView.tsx`; `frontend\src\app\views.ts:23` | no. Home has four entries and no market dashboard (`pulse3\apps\web\src\pages\HomePage.tsx:24-29`) |
| Sentiment thermometer (MoodHero) | 0–100 score with six bands (冰点 to 高潮) and a session curve. Backend weights: limit-up ratio 0.40, breadth 0.25, height 0.20, seal quality 0.15 | `frontend\src\features\overview\OverviewView.tsx:51`; formula in `docs\ARCHITECTURE.md` §4 | no |
| Market vitals tiles | Limit-ups, broken boards, max height, yesterday's limit-up premium, turnover, up/down split; click to screen | `frontend\src\features\overview\OverviewView.tsx:105`; top bar in `frontend\src\features\shell\Shell.tsx:101` | no |
| Limit ladder (连板天梯) | Rungs by consecutive-limit count, promotion rate, sealed vs broken members | `frontend\src\features\overview\Ladder.tsx:53` | no |
| Seal clock (封板时钟) | Limit-ups placed by first-seal time, with cumulative curve | `frontend\src\features\overview\SealClock.tsx:23` | no |
| Breadth histogram (涨跌分布) | 11 bins by percent change | `frontend\src\features\overview\OverviewView.tsx:151` | no |
| Theme resonance (题材共振) | Top 8 themes; strength = limit-ups×2 + growth streak + average change×0.5 | `frontend\src\features\overview\OverviewView.tsx:170` | partial: sector opportunity entry (`pulse3\apps\web\src\pages\HomePage.tsx:91`); sector ranking in `pulse3\src\pulse\domain\sectors\ranking.py` |
| Growth streak strip (连阳梯度) | Counts for 1–7+ consecutive up days; click opens the pool | `frontend\src\features\overview\OverviewView.tsx:204` | no |
| Event tape (盘口异动) | Seal, reseal, break, surge, plunge, new-high events from snapshot diffs; filterable | `frontend\src\features\overview\EventTape.tsx:87`; events in `backend\app\core\events.py` | no. pulse3 polling only reports release changes (`pulse3\src\pulse\api\routes_ops.py:64`) |
| Today's picks `#/picks` | Tabs 总榜 / 短线 / 波段 / 长线; podium cards plus expandable rows (entry note, stop, target, holding days, exit plan, risk flags) | `frontend\src\features\picks\PicksView.tsx:263` | partial: "全市场精选" ranked by horizon (5, 20, 120 trading days) (`pulse3\apps\web\src\config.ts:59-63`, `…\pulse3\apps\web\src\pages\HomePage.tsx:73`). No ensemble, no 总榜 |
| Ensemble weights (参与加权的战法) | Playbooks in the ensemble, with horizon, verdict and weight | `frontend\src\features\picks\PicksView.tsx:189` | no |
| Track record (以史为鉴) | Out-of-sample replay vs recorded picks; per-horizon table; excess-return bars | `frontend\src\features\picks\PicksView.tsx:219` | no |
| Bear-mode banner (熊市模式) | Shown when the full-market index closes below its 60-day mean | `frontend\src\features\picks\PicksView.tsx:331-336`; logic in `backend\app\playbooks\picks.py` | no |
| Exit plan and entry note | Per-pick sell plan list and buy hint | `frontend\src\features\picks\PicksView.tsx:62-71` | partial: plan card with conditions and price bands, frozen per release (`pulse3\apps\web\src\components\PlanCard.tsx`) |
| Recompute and push buttons (重新计算, 推送) | Recompute picks; send a test push | `frontend\src\features\picks\PicksView.tsx:307-312` | no UI trigger. Analysis runs only via worker CLI (`pulse3\src\pulse\workers\analysis.py`) |
| Playbook library list `#/playbooks` | 52 strategies; filter by horizon and verdict; fitness ring; out-of-sample average; equity sparkline | `frontend\src\features\playbooks\PlaybooksView.tsx:46` | no. Strategy page lists unimplemented candidates only (`pulse3\apps\web\src\pages\StrategiesPage.tsx:46`) |
| Playbook evaluation | Equity curve vs 中证全指 with drawdown; stats table for full, in-sample, out-of-sample, last 60 days | `frontend\src\features\playbooks\PlaybookDetail.tsx:172` (StatsTable at 35) | partial: experiment metrics with windows and sample counts (`pulse3\apps\web\src\pages\StrategiesPage.tsx:146`); no equity curve |
| Performance by regime (RegimeAttribution) | Average per trade by entry regime; cells under 10 trades faded | `frontend\src\features\playbooks\PlaybookDetail.tsx:67` | no |
| Exit study (买卖法对比) | 16 exit reasons; buy and sell variants chosen in-sample, checked out-of-sample | `frontend\src\features\playbooks\PlaybookDetail.tsx:128` | no. Intraday-T and grid bodies exist (`pulse3\src\pulse\domain\plans\bodies.py`) but no exit lab |
| Recent trades | Code, entry, exit, return, holding days, exit reason | `frontend\src\features\playbooks\PlaybookDetail.tsx:102` | no |
| Custom run form (自定义回测) | Signal parameters, holding days, stop/target/trailing, sizing, max positions, daily new entries; not saved | `frontend\src\features\playbooks\RunForm.tsx:23` | partial: experiment form with strategy instance, hypothesis, purpose, seed (`pulse3\apps\web\src\pages\StrategiesPage.tsx:180`); no parameter or exit controls |
| Playbook rotation (战法轮换) | Re-election every 20 trading days; walk-forward tests; configuration table | `frontend\src\features\playbooks\Rotation.tsx:111` | no walk-forward election found |
| Regime timeline (市场周期) | Index line with four ribbons (trend, liquidity, sentiment, style); shared crosshair; 1–6 year windows | `frontend\src\features\regime\RegimeView.tsx:19` and `:199` | no |
| Playbook × regime heatmap | Average per trade by regime for each playbook | `frontend\src\features\regime\RegimeView.tsx:141` | no |
| Regime banner and rules | Current four-dimension state with evidence; rule text | `frontend\src\features\regime\RegimeBanner.tsx:48`; rules `…\regime\RegimeView.tsx:229`; labels `frontend\src\features\regime\labels.ts` | no |
| Ask bar (问句选股) | Chinese sentence becomes removable condition chips; unmatched fragments flagged; rotating examples | `frontend\src\features\screener\ScreenerView.tsx:33`; parser `backend\app\query\nlq.py` | no. Condition tree is for plan logic only (`pulse3\apps\web\src\components\ConditionTree.tsx`) |
| Presets (策略预设) | Five built-in presets plus user-saved presets in browser storage | `frontend\src\features\screener\ScreenerView.tsx:114`; presets in `backend\app\research\strategies.py` | partial: strategy catalog of unimplemented candidates (`pulse3\apps\web\src\pages\StrategiesPage.tsx:46`); no screening presets |
| Filter rail | Pool type, streak height, growth streak, cap / change / turnover / price / amount ranges, turnover structure, seal time, seal ratio, hard board, 1-year limit-ups, 60-day high, board / ST / new-stock toggles, themes | `frontend\src\features\screener\FilterRail.tsx:46` | no |
| Results table | Virtualized, sortable, keyboard navigation, Enter to open, CSV export, density toggle | `frontend\src\features\screener\ResultTable.tsx:111` (CSV in `ScreenerView.tsx:182`) | partial: paged ranking table (`pulse3\apps\web\src\components\PagedRanking.tsx`); no CSV, no sort controls |
| Theme treemap (题材图谱) | Area = turnover, colour = average change; click to select | `frontend\src\features\themes\ThemesView.tsx:76` | no. Sector lists only (`pulse3\apps\web\src\pages\HomePage.tsx:118`) |
| Theme constituents (Members) | Top 40 members of the selected theme; button into screener | `frontend\src\features\themes\ThemesView.tsx:98` | partial: sector page with in-sector ranking and heating state (`pulse3\apps\web\src\pages\SectorPage.tsx:17`) |
| Crowd and linkage (抱团与联动) | Industry-diffusion leaders and followers; same-character name clusters; seat table; institutional net buy | `frontend\src\features\themes\Crowd.tsx:102` | partial: seat evidence tab (`pulse3\apps\web\src\components\EvidenceTabs.tsx`); no name clusters, no diffusion snapshot |
| Pattern radar (形态雷达) | 12 rule cards; family filter; today's hit counts | `frontend\src\features\patterns\PatternsView.tsx:71` | no |
| Pattern hits | Ranked hit list per pattern; opens stock | `frontend\src\features\patterns\PatternsView.tsx:46` | no |
| Strategy backtest (策略回测) | Strategy, parameters, exits, KPIs, equity and drawdown, monthly heatmap, trades, assumptions, recent-run archive | `frontend\src\features\lab\BacktestPanel.tsx:181` (Result at 60) | partial: experiments are queued and evaluated asynchronously (`pulse3\apps\web\src\pages\StrategiesPage.tsx:146,180`); no instant backtest |
| Batch comparison (批量对比) | Same strategy on the top 10 screener results | `frontend\src\features\lab\BacktestPanel.tsx:130` | no |
| Factor test (因子检验) | Six factors; rank IC, ICIR, t, IC>0 share, top-group turnover; daily IC, quantile returns, long-short curve | `frontend\src\features\lab\FactorPanel.tsx:21` | no factor-IC analysis found |
| Watchlist (自选观察) | Cards with live price, change, sparkline, amount, composite score; remove | `frontend\src\features\watchlist\WatchlistView.tsx:53` | partial: server-side watchlist with star toggle (`pulse3\apps\web\src\pages\HomePage.tsx:195`); no live cards |
| Watchlist tape and suggestions | Events for watched codes; empty state offers top-8 by score | `frontend\src\features\watchlist\WatchlistView.tsx:13` and `:24` | no |
| Stock drawer (个股详情) | Right-side sheet; Esc closes; focus trap; deep link `#/view?s=code` | `frontend\src\features\stock\StockSheet.tsx:117` | partial: full stock page at `/stock/:instrumentId` (`pulse3\apps\web\src\App.tsx:47`) |
| Daily chart with overlays | MA or BOLL on price; MACD, KDJ or RSI sub-pane | `frontend\src\features\stock\KChart.tsx:12-13`; `…\stock\StockSheet.tsx:87` | partial: MA/EMA and MACD panes, minute to yearly periods, adjustment modes (`pulse3\apps\web\src\pages\StockPage.tsx:323`). RSI and ATR kernels exist (`pulse3\src\pulse\domain\features\indicators.py`) but no BOLL or KDJ |
| Analogs (相似情景) | Nearest historical setups; 5/10/20/40-day horizon; up-probability; percentile fan; closest examples | `frontend\src\features\stock\Analogs.tsx:12`; backend `backend\app\research\analogs.py` | no |
| Stock stats (盘面) | Turnover, volume ratio, free float, first/last seal, seal amount, seal ratio, broken count, yearly limit-ups, seal success rate, distance to 60-day high | `frontend\src\features\stock\StockSheet.tsx:41` | partial: quote card (`pulse3\apps\web\src\pages\StockPage.tsx:281`) without limit-up fields |
| Factor breakdown (因子拆解) | Four weighted scores with explanations | `frontend\src\features\stock\StockSheet.tsx:70`; weights `backend\app\factors\engine.py` | partial: strategy contributions, relative and not additive (`pulse3\apps\web\src\pages\StockPage.tsx:231`) |
| Facts and observations (事实观察) | Plain-language observations for the stock | `frontend\src\features\stock\StockSheet.tsx:208-215`; data `backend\app\api\market.py:17` | partial: evidence tabs for news and claims, sentiment and community, fund flow, LHB seats (`pulse3\apps\web\src\components\EvidenceTabs.tsx`) |
| Data sources dialog (数据来源) | Provenance text, provider list, field coverage, warehouse panel, disclaimer | `frontend\src\features\settings\SourcesDialog.tsx:12` | partial: data status page with per-source enablement, runtime state, freshness (`pulse3\apps\web\src\pages\SmallPages.tsx:65`) |
| SIM / LIVE switch | Switch data mode from the top badge or the dialog | `frontend\src\features\shell\Shell.tsx:83`; `…\settings\SourcesDialog.tsx:25` | no. Synthetic banner only in mock builds (`pulse3\apps\web\src\components\Shell.tsx:68-74`) |
| Field coverage bars | Per-field coverage with low / mid / high colouring | `frontend\src\features\settings\SourcesDialog.tsx:88-100` | partial: coverage in response metadata shown by MetaStrip (`pulse3\apps\web\src\components\MetaStrip.tsx`) |
| Warehouse panel (历史数据仓) | Active history set, table rows and date ranges, incremental sync, backfill 1/3/5 years, job progress | `frontend\src\features\settings\WarehousePanel.tsx:33` | no UI trigger. CLI only (`pulse3\src\pulse\workers\ingest.py`) |
| Command palette (⌘K / Ctrl+K) | Stocks, questions, navigation, mode switch, density, sources | `frontend\src\features\command\CommandPalette.tsx:29` | no |
| Keyboard shortcuts | `/` focuses ask bar; 1–9 switch views; Esc; arrow keys in table | `frontend\src\app\router.ts:73-94`; `…\screener\ResultTable.tsx:136` | no |
| Left rail (导航栏) | Nine views plus data button | `frontend\src\features\shell\Shell.tsx:36`; `…\app\views.ts:22-32` | partial: five entries (`pulse3\apps\web\src\components\Shell.tsx:15-21`) |
| Top ribbon | Market vitals, stream state, mode badge, search | `frontend\src\features\shell\Shell.tsx:71-141` | partial: data-source health badges and update age (`pulse3\apps\web\src\components\Shell.tsx:41-57,88-92`) |
| Live stream (SSE client) | Single EventSource; refetch per snapshot; exponential backoff; stall watchdog | `frontend\src\core\live.ts:13` | partial: polling update channel (`pulse3\apps\web\src\hooks\useUpdates.tsx`) |
| Toasts (通知) | Success and error messages; auto-dismiss after 4.2 s | `frontend\src\features\shell\Shell.tsx:147` | yes: `pulse3\apps\web\src\components\Toast.tsx` |
| Deep links | Hash routes such as `#/view?s=code&p=playbook`, restorable from a shared link | `frontend\src\app\router.ts:14-46` | partial: routes and query parameters (`pulse3\apps\web\src\App.tsx:44-53`; home `?entry=&h=`) |
| Persisted workspace | Browser storage key `pulse.workspace.v3`: view, filters, presets, density, factor settings, horizon | `frontend\src\core\store.ts:126` | partial: server-side layouts and annotations (`pulse3\src\pulse\api\routes_user.py:40-84`) |
| Aurora background (极光背景) | Background hue follows sentiment | `frontend\src\features\shell\Shell.tsx:11`; `…\shell\mood.ts` | no. Theme toggle only (`pulse3\apps\web\src\hooks\useTheme.ts`) |
| Design system (kit) | OKLCH tokens, canvas charts, primitives | `frontend\src\kit\tokens.css`; `…\kit\charts.tsx`; `…\kit\primitives.tsx` | partial: `pulse3\apps\web\src\styles.css`, `…\components\States.tsx` |
| Research disclaimer | Footer and sources dialog: research only, not investment advice | `frontend\src\features\settings\SourcesDialog.tsx:101` | yes: `pulse3\apps\web\src\components\Shell.tsx:95-97` |

### 2. API routes (`backend\app\api`, prefix `/api/v1` unless noted)

| Route | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| GET /api/v1/health | Health check; not called by the UI | `backend\app\api\admin.py:21` | no |
| POST /api/v1/admin/mode | Switch SIM / LIVE | `backend\app\api\admin.py:26` | no |
| GET /api/v1/agent/manifest | Capability manifest for agents; not called by the UI | `backend\app\api\admin.py:31` | no |
| GET /llms.txt (no `/api` prefix) | Plain-text guide for LLM agents | `backend\app\main.py:103` | no |
| GET, POST, DELETE /mcp | MCP server; tools generated from the capability registry | `backend\app\main.py:100`; `backend\app\agent\mcp_server.py:25` | no |
| GET /api/v1/market/overview | Market dashboard aggregate | `backend\app\api\market.py:12` | no |
| GET /api/v1/stocks/{code} | Stock detail: bars, factors, observations | `backend\app\api\market.py:17` | partial: workspace and quote routes (`pulse3\src\pulse\api\routes_market.py:260` and `:239`) |
| GET /api/v1/stocks/{code}/analogs | Analog forecast | `backend\app\api\market.py:48` | no |
| GET /api/v1/market/events | Event tape with `since` cursor | `backend\app\api\events.py:12` | no |
| POST /api/v1/screener/query | Filtered screen | `backend\app\api\screener.py:16` | no |
| POST /api/v1/screener/parse | Parse a Chinese question into filters | `backend\app\api\screener.py:21` | no |
| GET /api/v1/strategies | Strategy catalogue: presets and signal families | `backend\app\api\strategies.py:10` | partial: candidate catalogue, all unimplemented (`pulse3\src\pulse\api\routes_research.py:29`) |
| GET /api/v1/watchlist | Watchlist | `backend\app\api\watchlist.py:10` | partial: list of watchlists (`pulse3\src\pulse\api\routes_user.py:59`) |
| PUT /api/v1/watchlist | Save ordered watchlist | `backend\app\api\watchlist.py:16` | partial: PUT `/v1/watchlists/{id}` (`routes_user.py:64`) |
| POST /api/v1/research/backtests | Run and archive one backtest | `backend\app\api\research.py:22` | partial: enqueue experiment, returns 202 (`pulse3\src\pulse\api\routes_research.py:80`) |
| GET /api/v1/research/backtests | Archive list | `backend\app\api\research.py:36` | no |
| POST /api/v1/research/backtests/batch | One strategy over many codes | `backend\app\api\research.py:41` | no |
| GET /api/v1/research/backtests/{run_id} | Archived result | `backend\app\api\research.py:96` | partial: experiment read (`routes_research.py:117`) |
| GET /api/v1/lab/patterns | Pattern scan | `backend\app\api\lab.py:52` | no |
| POST /api/v1/lab/factors | Factor IC test | `backend\app\api\lab.py:70` | no |
| GET /api/v1/regime | Regime history (`days`) | `backend\app\api\regime.py:11` | no |
| GET /api/v1/playbooks | Playbook board | `backend\app\api\playbooks.py:12` | no |
| GET /api/v1/playbooks/rotation | Rotation report | `backend\app\api\playbooks.py:17` | no |
| GET /api/v1/playbooks/exits | Exit lab across playbooks; agents only | `backend\app\api\playbooks.py:23` | no |
| GET /api/v1/playbooks/{playbook_id}/exits | Exit study for one playbook | `backend\app\api\playbooks.py:29` | no |
| GET /api/v1/playbooks/{playbook_id} | Evaluation detail | `backend\app\api\playbooks.py:39` | no |
| POST /api/v1/playbooks/{playbook_id}/run | Custom-parameter rerun; not stored | `backend\app\api\playbooks.py:47` | no |
| GET /api/v1/data/warehouse | Warehouse status and job progress | `backend\app\api\data.py:29` | partial: data status (`routes_market.py:643`) |
| POST /api/v1/data/sync | Start incremental sync or backfill | `backend\app\api\data.py:70` | no. Worker CLI only |
| GET /api/v1/crowd | Crowd snapshot | `backend\app\api\crowd.py:10` | partial: sector rankings (`pulse3\src\pulse\api\routes_decision.py:88`) and seats (`routes_market.py:616`) |
| GET /api/v1/picks/today | Today's report | `backend\app\api\picks.py:10` | partial: rankings (`routes_decision.py:78`) |
| POST /api/v1/picks/generate | Recompute picks | `backend\app\api\picks.py:15` | no |
| GET /api/v1/picks/history | Report dates | `backend\app\api\picks.py:21` | no |
| GET /api/v1/picks/track-record | Live and replay track record | `backend\app\api\picks.py:26` | no |
| GET /api/v1/picks/{trade_date} | Report by date | `backend\app\api\picks.py:31` | partial: release by id, not by date (`pulse3\src\pulse\api\routes_ops.py:50`) |
| GET /api/v1/notify/status | Push configuration status | `backend\app\api\notify.py:22` | no |
| POST /api/v1/notify/test | Send test push | `backend\app\api\notify.py:27` | no |
| GET /api/v1/stream/sse | Server-sent events; snapshot notices | `backend\app\api\stream.py:21` | partial: polling (`pulse3\src\pulse\api\routes_ops.py:64`, GET /v1/updates) |
| GET /api/v1/providers | Data source list | `backend\app\api\ops.py:10` | partial: data status (`routes_market.py:643`) |
| GET /api/v1/quality | Field coverage | `backend\app\api\ops.py:15` | partial: coverage in response metadata (`pulse3\src\pulse\api\envelope.py`) |
| GET /openapi.json, /docs, /redoc | FastAPI defaults; no decorator | `backend\app\main.py:79-84` | partial: committed OpenAPI contract (`pulse3\src\pulse\contracts\data\openapi.json`) |
| GET / (static) | Serves `frontend\dist` when built | `backend\app\main.py:110-112` | partial: SPA catch-all (`pulse3\src\pulse\api\app.py:66`) |

### 3. Data recorders and warehouse

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| Main-board intraday recorder (intraday) | Auction, level-1 book, limit-board queues, minute bars, broken-board pools; one process per trading day | `backend\app\warehouse\intraday.py:1-7` | partial: quote and minute pollers (`pulse3\src\pulse\workers\ingest.py`), not scheduled, no auction or queue data |
| Extended intraday recorder (intraday_ext, named) | Broad ETFs, indices, IF/IH/IC/IM contracts, A50, offshore RMB, gold, oil; every 60 s | `backend\app\warehouse\intraday_ext.py:1-67` | no |
| Time-locked day view (intraday_view) | What the system could have known at a given clock time | `backend\app\warehouse\intraday_view.py:1` | partial: point-in-time discipline with `available_at` and `known_time` across `pulse3\src\pulse` (about 330 references) |
| Minute bars from snapshots (minute_synth) | 1-minute bars built from recorder snapshots | `backend\app\warehouse\minute_synth.py:1` | partial: sampled minute bars with an estimated flag (`pulse3\src\pulse\adapters\sources\registry.py:89-90`) |
| Historical bar store (store) | Append-only Parquet per dataset; read-time dedup; retired files; version hash | `backend\app\warehouse\store.py:1` | partial: content-addressed lake (`pulse3\src\pulse\adapters\lake\store.py`) and PostgreSQL bar partitions (`pulse3\migrations\0005_bar_partitions.sql`) |
| Sync jobs (sync) | Backfill with resume and rate-limit waits; daily incremental; stops on refusal | `backend\app\warehouse\sync.py:1`; CLI via `scripts\pulse.py` (sync) | partial: legacy-import and live-daily workers (`pulse3\src\pulse\workers\ingest.py`) |
| Sina daily bars (sina) | Raw price divided by adjustment factor (proportional forward adjustment) | `backend\app\warehouse\sina.py:1` | partial: raw daily adapter (`pulse3\src\pulse\adapters\sources\sina.py`); adjustment kept separate (`pulse3\src\pulse\domain\market\adjustment.py`) |
| Tencent quotes and bars (tencent) | Quotes, forward-adjusted daily bars, index bars | `backend\app\warehouse\tencent.py:1` | yes: `pulse3\src\pulse\adapters\sources\tencent.py` |
| Dragon-Tiger list (lhb) | Stock rows and top-five seat rows; resumable; current day not pulled before 18:00 | `backend\app\warehouse\lhb.py:1`; manual CLI in `README.md:80` | partial: LHB adapter and seat reports (`pulse3\src\pulse\adapters\sources\lhb.py`; `…\pulse3\src\pulse\domain\intelligence\seats.py`) |
| Industry and concept membership (boards) | Sina industry (48) and concept (213) snapshot | `backend\app\warehouse\boards.py:1` | partial: legacy import of boards (`pulse3\src\pulse\adapters\sources\legacy.py`); sector domain (`…\pulse3\src\pulse\domain\sectors\pool.py`) |
| Listing dates (listing) | Listing dates from exchange stock lists | `backend\app\warehouse\listing.py:1` | partial: instrument identity with board and risk-warning state (`pulse3\src\pulse\domain\instruments\identity.py`) |
| Corporate calendar (corp_events) | Lock-up expiries and suspensions, main board, from Eastmoney | `backend\app\warehouse\corp_events.py:1` | no |
| Event calendar store (calendar_events, named) | Research seed import; versioned events; rule-computed expiries; CLI import / sync / expiries; no API or UI | `backend\app\warehouse\calendar_events.py:1`; seed `A股事件日历_候选种子_2026-11-02.json` | no |
| Daily derived features (features) | Prior close, change, limit flags, consecutive-limit streak, amount estimate | `backend\app\warehouse\features.py:1` | partial: indicator kernels (`pulse3\src\pulse\domain\features\indicators.py`); price-limit registry (`…\pulse3\src\pulse\domain\market_rules\price_limits.py`) |
| Synthetic history (synthetic) | Deterministic synthetic daily bars for the SIM dataset | `backend\app\warehouse\synthetic.py:1` | partial: fixtures and mock data (`pulse3\fixtures\`, `pulse3\apps\web\mock\`) |
| Coverage gate (domain/coverage_gate) | Blocks daily sync when coverage is insufficient (E058) | `backend\app\domain\coverage_gate.py:1` | partial: session classes observed, true_zero, suspended, missing (`pulse3\src\pulse\domain\market\bars.py`) |
| Live snapshot chain (market/live) | Eastmoney, then Sina, then Tencent; timeouts, fallback, stale marking | `backend\app\market\live.py:1` | partial: Sina and Tencent quote adapters; no Eastmoney snapshot |
| Daily bars for charts and research (market/history, research/tencent_history) | Bars for detail charts and cross-sectional research | `backend\app\market\history.py:1`; `backend\app\research\tencent_history.py:1` | yes: `GET /v1/stocks/{id}/bars` (`pulse3\src\pulse\api\routes_market.py:458`) |
| Trading calendar and session clock (market/calendar) | Trading days and session times; holiday file maintained by operators | `backend\app\market\calendar.py:1` | yes: `pulse3\src\pulse\domain\market\calendar.py`; `…\pulse3\src\pulse\domain\market_rules\calendar.py` |
| Curated universe (market/universe) | Curated identifiers; themes are simulation labels | `backend\app\market\universe.py:1` | partial: universe rules and exclusion reasons (`pulse3\src\pulse\domain\ranking\universe.py`) |
| Four-dimension factor engine (factors/engine, registry) | Trend 30, volume 20, seal quality 25, position 25; dependency-graph registry and plugin discovery | `backend\app\factors\engine.py`; `backend\app\factors\registry.py:1` | partial: explainable fixed-weight baseline (`pulse3\src\pulse\domain\ranking\baseline.py`); feature registry (`…\pulse3\src\pulse\domain\features\definitions.py`) |
| Seal-span plugin (factors/plugins/seal_span) | First-to-last seal gap and early-seal flag, outside the total score | `backend\app\factors\plugins\seal_span.py:1` | no |
| Local storage (core/storage) | SQLite WAL for watchlist and archive; read-only DuckDB; rest in memory | `backend\app\core\storage.py:1` | partial: PostgreSQL schema (`pulse3\migrations\0001_core_schema.sql`) |
| In-memory market state and ticker (core/engine; main ticker) | One frame, every read is a projection; refresh loop every few seconds, then SSE | `backend\app\core\engine.py:1`; `backend\app\main.py:28-48` | partial: frozen analysis releases (`pulse3\src\pulse\application\release_service.py`) |
| Snapshot-diff events (core/events) | Seal, reseal, break, surge, plunge, new-high from consecutive frames | `backend\app\core\events.py:1` | no |
| SSE fan-out (core/bus) | One publisher, many listeners | `backend\app\core\bus.py:1` | partial: outbox and update delivery (`pulse3\src\pulse\adapters\postgres\outbox.py`) |
| Data-version cache key | File-set hash keys all research caches; sync invalidates them | `docs\ARCHITECTURE.md` §5.5 | yes: frozen releases (`pulse3\src\pulse\application\release_service.py`) |
| Limit-up queue fill model (domain/queue_fill) | Three fill scenarios for limit-up queues | `backend\app\domain\queue_fill.py:1` | partial: limit and suspension no-fill rules (`pulse3\src\pulse\domain\execution\matcher.py`); no queue model |

### 4. News, alerts and push

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| 7×24 news recorder (news) | Tonghuashun about every 10 s; Eastmoney and CLS about every 30 s; guba follow-ups at +1, +3, +5 min; 10-second watch list; 8 s timeout per source; a source stops for the day after 3 bad answers | `backend\app\warehouse\news.py:1-20` (sources at `:108-142`) | partial: Eastmoney, CLS and guba adapters (`pulse3\src\pulse\adapters\sources\eastmoney_news.py`, `cls_news.py`, `guba.py`); no Tonghuashun; intel news runs once daily, 2 pages (`pulse3\deploy\windows\run_daily.ps1:17`) |
| News alerts (news_alerts, E075) | Rules judge each new message, then match holdings and watch list. P0: holding negative. P1: policy negative in a holding's industry. P2: watch-list news or positive news not held. Optional LLM fallback | `backend\app\warehouse\news_alerts.py:1-24` | no. Push channels explicitly out of scope (`pulse3\src\pulse\application\intelligence_service.py:13`) |
| News rules (news_rules, named) | Keyword rules: 12 company-negative, 7 positive, 19 macro-policy classes; mapped to Shenwan level-2 industries | `backend\app\domain\news_rules.py:1-23` | partial: deterministic claim extraction (`pulse3\src\pulse\domain\intelligence\claims.py`); no P0/P1/P2 tiers |
| LLM fallback for news | Messages rules cannot judge go to an LLM; key from environment or protected file; rules only without a key | `backend\app\warehouse\news_alerts.py:13`; `scripts\set_openai_key.ps1` | partial: OpenAI-compatible provider enabled by key file (`pulse3\src\pulse\adapters\models\openai_compat.py`); rules provider (`…\models\rules.py`); run log (`…\models\runner.py`) |
| Report push (notify/push) | WeCom, Feishu, DingTalk or generic webhook; summary of regimes, top 5, top 3 per horizon, stops, disclaimer; sends nothing unless configured | `backend\app\notify\push.py:1` | no |
| Guba community sample (guba table in news) | Stock-bar posts and comment counts stored; no page | `backend\app\warehouse\news.py` | partial: community sampling and independent-author counts (`pulse3\src\pulse\domain\intelligence\community.py`; `…\pulse3\src\pulse\adapters\sources\guba.py`); stock-page tab |
| Official announcements (scripts/lab/ann_fetch; research only) | Monthly cninfo pulls by title keyword (E070); not a product feature | `scripts\lab\ann_fetch.py:1` | partial: cninfo adapter (`pulse3\src\pulse\adapters\sources\cninfo.py`); normalisation (`pulse3\src\pulse\domain\evidence\announcements.py`) |
| Event-to-stock linking (domain/event_links) | Rule table maps event names to Shenwan level-2 industries, then to members, point in time | `backend\app\domain\event_links.py:1` | no |

### 5. Strategies and playbooks

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| Playbook interface and panel (playbooks/base, panel) | One playbook = rule text plus pure signal function over a research panel; no-look-ahead test | `backend\app\playbooks\base.py:1`; `…\playbooks\panel.py:1` | partial: strategy specification and canonical hashing (`pulse3\src\pulse\domain\strategy\canonical.py`; `…\strategy\wire.py`) |
| Limit-up relay family (library.py, 5) | First-board relay, second-to-third board, broken-board rebound, limit pullback, dragon pullback | `backend\app\playbooks\library.py:123-194` | no |
| Trend and breakout family (library.py, 7) | Turtle 55-day breakout, platform breakout, MA bullish pullback, 20/60 golden cross, MACD above-zero golden cross, Bollinger squeeze breakout, one-year high | `backend\app\playbooks\library.py:205-304` | no |
| Academic factor family (library.py, 5) | 12-1 momentum, low volatility, short-term reversal, small cap, low PE+PB | `backend\app\playbooks\library.py:316-393` | no |
| Oversold and oscillator family (library.py, 5) | RSI oversold rebound, KDJ low golden cross, Bollinger lower rebound, dry-volume pullback, low-base volume surge | `backend\app\playbooks\library.py:406-475` | no |
| Sentiment-cycle and timing family (library_sentiment.py, 9) | Ice-point rebound, divergence to consensus, first-negative rebound, high-low switch, volume-price divergence reversal, low amplitude, overnight gap reversal, low turnover low volatility, micro-cap timing | `backend\app\playbooks\library_sentiment.py:97-276` | no |
| Crowd and linkage family (library_crowd.py, 5) | Sector follower, sector first board, name-linkage follower, strong-seat alignment, institutional net buy | `backend\app\playbooks\library_crowd.py:35-101` | no |
| Factor composite family (library_factor.py, 2) | Low-risk composite (20-day hold); shrink-volume oversold (10-day hold) | `backend\app\playbooks\library_factor.py:113,130` | no |
| Technical-analysis family (library_tech.py, 14) | Candlestick 6 (hammer, engulfing, morning star, three soldiers, low doji, low-base long lower shadow); indicators 4 (MACD bottom divergence, MACD below-zero golden cross, KDJ J turn, lower-band oversold); weekly, monthly and multi-timeframe 4 | `backend\app\playbooks\library_tech.py:119-190` | no |
| Research presets (research/strategies, 5) | First-board acceleration, ladder leader, breakout volume, shrink pullback, growth climb; each is a screen preset and a backtest signal | `backend\app\research\strategies.py:27-110` | no. pulse3 seeds 61 unimplemented candidates (`pulse3\src\pulse\domain\strategy\seed.py:1-13`) |
| Portfolio backtester (playbooks/backtest) | Next-open fills; one-word limit skip; T+1; stop before target; limit-down carry-over; A-share costs; lots; equal or volatility sizing; 中证全指 benchmark | `backend\app\playbooks\backtest.py:1` | partial: execution kernel and ledger (`pulse3\src\pulse\domain\execution\kernel.py`); cost profile (`…\pulse3\src\pulse\domain\market_rules\costs.py`); lot and T+1 rules (`…\pulse3\src\pulse\domain\market_rules\lots.py`) |
| Evaluation (playbooks/evaluate) | First 70% of dates in-sample, last 30% out-of-sample; attribution to four regime dimensions; fitness 0–100; six verdicts | `backend\app\playbooks\evaluate.py:1` | partial: fixed evaluation context with train and validation windows (`pulse3\src\pulse\domain\research\context.py`; `…\research\experiments.py`) |
| Market regime (playbooks/regime) | Trend, liquidity, sentiment and style, by deterministic rules | `backend\app\playbooks\regime.py:1` | no. Regime routing explicitly not covered (`pulse3\src\pulse\domain\ranking\baseline.py:13`) |
| Rotation (playbooks/rotation) | Every 20 trading days, re-elect from closed trades; Newey-West t; select on first 70%, test on last 30% | `backend\app\playbooks\rotation.py:1` | no |
| Exit lab (playbooks/exits) | 10 exit rules, 2 entry rules, scale-out, intraday T (reverse and forward) | `backend\app\playbooks\exits.py:1` | partial: intraday-T and grid plan bodies (`pulse3\src\pulse\domain\plans\bodies.py`; `…\pulse3\src\pulse\domain\execution\legs.py`); no exit comparison |
| Daily ensemble (playbooks/picks) | Playbooks with fitness ≥ 40 vote, weight = fitness; bear mode; sub-scores; stop = tighter of percent and 2×ATR; archived track record | `backend\app\playbooks\picks.py:1` | partial: frozen ranking with trial fusion (`pulse3\src\pulse\domain\ranking\run.py`) |
| Crowd features and snapshot (playbooks/crowd, crowd_view) | Industry diffusion, name linkage, seat track records, point-in-time joins | `backend\app\playbooks\crowd.py:1`; `…\playbooks\crowd_view.py:1` | partial: sector pool with breadth and leader confirmation (`pulse3\src\pulse\domain\sectors\pool.py`); seat reports (`…\pulse3\src\pulse\domain\intelligence\seats.py`) |
| Pattern rules (research/patterns, 12) | MA alignment, volume surge, volume limit-down, parking apron, year-line pullback, platform breakout, no large drawdown, turtle breakout, high tight flag, low-ATR growth, MACD below-zero cross, KDJ oversold cross | `backend\app\research\patterns.py:38-69` | no |
| Factor lab (research/factor_lab, 6 factors) | 20-day momentum, 5-day reversal, 20-day low volatility, volume surge, distance to 60-day high, MA20 gap; rank IC, quantile returns, long-short | `backend\app\research\factor_lab.py:29-34` | no. 105 unimplemented factor candidates seeded only (`pulse3\src\pulse\domain\strategy\seed.py`) |
| Analogs (research/analogs) | Nearest neighbours on six features, K = 200, de-overlapped, forward-return distribution | `backend\app\research\analogs.py:1` | no |
| Technical indicators (research/indicators) | MA 5/10/20/60, BOLL, volume MA, MACD, KDJ, RSI 6/14 (Wilder) | `backend\app\research\indicators.py:1` | partial: MA, EMA, MACD, RSI, ATR kernels (`pulse3\src\pulse\domain\features\indicators.py`); no BOLL or KDJ |
| Single-stock backtest and tear sheet (research/backtest, tearsheet) | Event-driven, deterministic; stop, target, trailing stop; Sortino, Calmar, drawdown days, profit factor | `backend\app\research\backtest.py:1`; `…\research\tearsheet.py:1` | partial: execution replay (`pulse3\src\pulse\domain\execution\entrypoints.py`); no tear sheet |
| Derived daily metrics (research/metrics) | Derived daily fields for detail views and research | `backend\app\research\metrics.py:1` | partial: feature definitions (`pulse3\src\pulse\domain\features\definitions.py`) |
| Buy and sell triggers (domain/triggers) | B1 and B3 buys, S1 and S3 sells, read one snapshot row at a time for paper replay | `backend\app\domain\triggers.py:1` | partial: trigger state machine (`pulse3\src\pulse\domain\plans\trigger.py`); three-valued condition tree (`…\pulse3\src\pulse\domain\plans\conditions.py`) |
| Five-minute box breakout (domain/box5) | Box and breakout state machine on 1-minute bars (E059); pure functions | `backend\app\domain\box5.py:1` | no |
| Closing market state (domain/market_state) | Short-term state at the close from main-board daily bars (E054) | `backend\app\domain\market_state.py:1` | no |
| Board rules (domain/board_rules) | Pure rules M2–M4; catalyst-pending check; thresholds are candidate parameters | `backend\app\domain\board_rules.py:1` | no |
| T+1 cash and inventory ledger (domain/inventory) | One account ledger as pure functions, shared by research and live runs | `backend\app\domain\inventory.py:1` | partial: one economic ledger (`pulse3\src\pulse\domain\execution\ledger.py`); sellability dates (`…\pulse3\src\pulse\domain\market_rules\lots.py`) |

### 6. Research and forward tracking

Named tools:

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| fusion | Round-5 walk-forward scorer over all factor sources; input to daily forward and the frozen book | `scripts\lab\fusion.py` | partial: trial fusion (`pulse3\src\pulse\domain\ranking\run.py`) and continuity check (`…\pulse3\src\pulse\domain\ranking\continuity.py`) |
| forward_book (and forward_book_h10) | Frozen main book, rules frozen 2026-11-01: 5-day hold, 20 positions, 4 new names a day from top-40 scores, 50k per name. h10 is the 10-day shadow | `scripts\lab\forward_book.py`; `…\scripts\lab\forward_book_h10.py` | partial: experiment purposes `shadow` and `blind_final` (`pulse3\src\pulse\domain\research\experiments.py:26`); paper accounts (`pulse3\src\pulse\application\account_service.py`); no frozen forward book |
| forward_monitor | E068: rank ability (IC) and excess return of the frozen score in later data | `scripts\lab\forward_monitor.py` | no |
| paper_replay | One recorded day: B1 and B3 buys, S1, S3 and M3 sells; nothing traded | `scripts\lab\paper_replay.py` | partial: online paper and replay entry points (`pulse3\src\pulse\domain\execution\entrypoints.py`) |
| curve_gate (and gate_grid) | Trade-level check of an equity-curve gate; parameter robustness grid (R7-4) | `scripts\lab\curve_gate.py`; `…\scripts\lab\gate_grid.py` | no |
| tail_forward | E077/E078: tail-of-day forward log from 60-second snapshots; up to five names a day by volume ratio; 0.252% round-trip cost | `scripts\lab\tail_forward.py` | no |
| state_forward | E089: market-state forward log (ETF surge, late-session share) and recorder health each day | `scripts\lab\state_forward.py` | no |
| Calendar events (named) | Event calendar store; rule-based expiries; event-to-industry-to-stock links; studies of calendar effects, macro events, historical index events | `backend\app\warehouse\calendar_events.py`; `backend\app\domain\event_links.py`; `scripts\lab\calendar_effects.py`; `…\scripts\lab\macro_events.py`; `…\scripts\lab\hist_index_events.py` | no. Trading calendar only (`pulse3\src\pulse\domain\market_rules\calendar.py`) |
| Holdings book (named) | Holdings file read by news alerts. Default location is under the backend data directory, overridable by `PULSE_HOLDINGS_FILE`; pushed daily by a local pipeline. Related: light daily book `scripts\lab\book.py`, Lhasa seat book `…\scripts\lab\lhasa_book.py`, T+1 ledger `backend\app\domain\inventory.py` | `backend\app\warehouse\news_alerts.py:22,75-77` | partial: paper account with actionability (`pulse3\src\pulse\application\account_service.py`); home "自选与持仓" entry (`pulse3\apps\web\src\pages\HomePage.tsx:195`) |
| Experiment ledger (ledger, docs) | Every method tried is kept with market and outcome; public ledger has 80+ entries | `scripts\lab\ledger.py`; `docs\research\ledger\README.md`; `docs\research\ledger\experiments.jsonl` | partial: every trial recorded, failures included (`pulse3\src\pulse\domain\research\experiments.py`) |
| Preregistrations (docs/research) | Each batch states metrics and pass thresholds before running | `docs\research\` (e.g. `2026-11-07-batch2-preregistration.md`) | partial: fixed, hashed evaluation context (`pulse3\src\pulse\domain\research\context.py`) |

Family rows. Directory for every file: `scripts\lab\`.

| Family | Files | What they do | pulse3 equivalent |
|---|---|---|---|
| A. Forward books, shadows, vetoes, exports (20) | book.py, exposure.py, official_eval.py, export_scores.py, export_panel.py, cap_filter.py, lhasa_book.py, rank_buffer.py, daily_forward.py, daily_forward.cmd (PulseDailyForward entry), random_controls.py, rolling_break.py, score_baskets.py, overbought_veto.py, neg_veto_bite.py, os20_forward.py, exit_tails.py, cond_reversal.py, shadow_x20.py, backfill_index_day.py | Shadow books, controls, vetoes, exports, the daily forward pipeline, index repair | no. Official evaluation maps loosely to `pulse3\src\pulse\domain\execution\kernel.py` |
| B. Fusion, factor mining, ML and RL (18) | dsl.py, baseline.py, ml_track.py, rl_alloc.py, lockbox.py, tengine_check.py, rl_fqi.py, gp_mine.py, select_pool.py, zoo.py, tengine.py, verify_all.py, jobq.py, rl_alphagen.py, registry.py, harness.py, analog_select.py, wctc_overlay.py | Factor DSL, tensor engine, genetic programming, ML and RL tracks, lockbox test, job queue, equity-curve overlay | no for mining, ML and RL. Partial for the baseline scorer (`pulse3\src\pulse\domain\ranking\baseline.py`) and factor candidate seeds (`…\pulse3\src\pulse\domain\strategy\seed.py`) |
| C. Market regime and style (7) | regime.py, regime_eval.py, cycles.py, r7_2_regime.py, style_switch.py, strategy_panel.py, style_test.py | Regime detection, cycle cutting, style switching | no |
| D. Macro, overnight, industry, theme (10) | overnight_us.py, ext_daily.py, etf_surge.py, zodiac.py, theme_check.py, demon.py, sectors.py, sw_industry.py, cninfo_industry.py, claims_check.py | US overnight, ETF surge, index and industry maps, theme and claim checks | partial for sector mapping (`pulse3\src\pulse\domain\sectors\`) and claim checks (`…\pulse3\src\pulse\domain\intelligence\claims.py`); no for the rest |
| E. Traditional rules, technical patterns, re-testing (14) | trad_battery.py, trad_export.py, trad_exits.py, tech_signals.py, p0_patterns.py, trullas_div.py, sequoia_audit.py, sameday_base.py, matched_reeval.py, reeval_block.py, b1_regime.py, replay.py, box5_scenarios.py, lhasa.py | Textbook rule batteries, exit tests, same-day baselines, bootstrap re-evaluation, day-by-day replay | no |
| F. LLM research tracks and news inputs (14) | llm.py, llm_ask.py, llm_eval.py, llm_live.py, llm_judge.py, llm_judge_pit.py, llm_judge_topo.py, llm_probe.py, llm_reverse.py, llm_mine.py, llm_select.py, llm_track.py, pit_news.py, ann_fetch.py | LLM judgement pilots and forward-only tests, LLM factor mining, point-in-time news | partial: model adapter and run log only (`pulse3\src\pulse\adapters\models\`); no research tracks |
| G. Data utilities (2) | tushare_limits.py, batch3_lib.py | Tushare limit-board history; batch-3 helpers | no |

Coverage check: 20 + 18 + 7 + 10 + 14 + 14 + 2 + 13 named files = 98 entries (97 `.py` and `daily_forward.cmd`).

Research documents: `docs\research\` and `docs\rebuild\` hold findings and plans. pulse3 equivalent: partial, `pulse3\docs\` (BUILD_BRIEF.md plus milestone evidence files).

### 7. Scheduled tasks, ops and workers

| Name | What it does | Old location | pulse3 equivalent |
|---|---|---|---|
| PulseIntraday | Weekdays 09:10, SYSTEM account, up to 9 h; records main-board intraday data to `C:\pulse\data\intraday\` on the server. Exit time conflicts (15:30 vs 17:00) | Code `backend\app\warehouse\intraday.py:7`; docs `docs\research\2026-11-01-round7-plan.md:81,137` | partial: not scheduled; quote and minute workers only (`pulse3\src\pulse\workers\ingest.py`). `pulse3\deploy\windows\deploy_app.ps1` registers only Pulse3Api and Pulse3Daily |
| PulseIntradayExt | Weekdays 08:58; 09:00–16:00 every 60 s plus a 15:01:30 close snapshot | Docs `docs\research\2026-11-08-batch4-preregistration.md:189`; ledger `pulse3\spec_package\review_v32\evidence\audit_ledger_evidence.json:3579` | no |
| PulseNews | Named only as a server process in docs; code starts the day at 08:00 and stops at 22:00 | `docs\research\2026-11-03-three-pillars-spec.md:3`; `backend\app\warehouse\news.py:54` | partial: Pulse3Daily runs intel news once at 18:10 (`pulse3\deploy\windows\run_daily.ps1:17`) |
| PulseNewsAlerts | Weekdays 08:05; incremental read of raw news every 30 s | Docs `docs\research\2026-11-08-batch4-preregistration.md:189`; ledger `pulse3\spec_package\review_v32\evidence\audit_ledger_evidence.json:3743` | no |
| PulseForwardLab | Weekdays 17:45; runs tail_forward and state_forward daily | Docs `docs\research\2026-11-08-batch4-preregistration.md:189`; `docs\research\2026-11-09-batch5-preregistration.md`; ledger lines 3626 and 3915 of the same evidence file | no |
| PulseDailyForward | Weekdays 20:30 on the local PC; catches up after boot if the PC was off; runs `daily_forward.cmd` then `daily_forward.py` | `docs\research\2026-11-07-batch2-preregistration.md:105`; `…\2026-11-07-batch1-preregistration.md:154`; `scripts\lab\daily_forward.cmd:2` | no |
| PulsePaper | Server-side paper-replay output; code and task definition not in repo; most likely `paper_replay.py` (unconfirmed) | `docs\research\2026-11-07-week1-review.md:76` | partial: paper account service, not scheduled (`pulse3\src\pulse\application\account_service.py`) |
| PulseLhbDaily | Zero references in the repo. LHB sync is a manual CLI | `README.md:80` | partial: `pulse3\deploy\windows\run_daily.ps1:19` runs intel seats daily |
| In-process after-close job (scheduler) | Daemon thread. Trading days after 15:35 CST: sync, regenerate picks, push. Three retries, 10 minutes apart. Catches up at startup. `PULSE_SCHEDULER=0` disables | `backend\app\scheduler.py:25,91-99,198`; `backend\app\main.py:68,73` | partial: Pulse3Daily Mon–Fri 18:10, 3-hour limit (`pulse3\deploy\windows\deploy_app.ps1:53-57`); no push |
| Startup warm-up and refresh ticker | Pattern-scan prewarm thread at startup; snapshot refresh every few seconds, then SSE | `backend\app\main.py:28-66` | partial: uvicorn starts at boot (`pulse3\deploy\windows\run_api.ps1`); no refresh loop |
| Warehouse CLIs | Backfill, daily sync, board and LHB jobs run by hand | `scripts\pulse.py` (sync); `backend\app\warehouse\*.py` | partial: ingest and intel workers (`pulse3\src\pulse\workers\ingest.py`, `…\workers\intel.py`) |
| Command entry (scripts/pulse.py) | install, contracts, check, test, e2e, build, dev, sync | `scripts\pulse.py:158-194` | partial: `pulse3\scripts\dev_api.cmd`; `pulse3\pyproject.toml` |
| Launchers (start.ps1, start.sh, Makefile) | Each delegates to `pulse.py dev` or the matching target | `start.ps1`; `start.sh`; `Makefile` | partial: `pulse3\deploy\windows\run_api.ps1` |
| Public snapshot publisher (publish_github.sh) | Publishes a sanitized one-commit snapshot after a secret scan and size check | `scripts\publish_github.sh:1-30` | no |
| API key entry (set_openai_key.ps1) | Hidden prompt; writes a protected key file | `scripts\set_openai_key.ps1`; `README.md:95` | partial: key-file setting `PULSE_OPENAI_KEY_FILE` (`pulse3\src\pulse\adapters\models\openai_compat.py`) |
| Contract generation and drift check | Pydantic contract generates TypeScript types and OpenAPI; `--check` blocks drift | `contracts\` (generate_types.py, export_openapi.py, per `docs\ARCHITECTURE.md` §1) | yes: `pulse3\src\pulse\contracts\data\v3.schema.json`; `…\contracts\data\openapi.json`; `pulse3\apps\web\src\api\schema.d.ts` |
| Tests | pytest on backend tests, tsc, Playwright end-to-end | `backend\tests\`; `frontend\e2e\`; `frontend\playwright.config.ts` | yes: `pulse3\tests\`; `pulse3\apps\web\tests\` |
| Data provenance on every response | Source, as-of time, coverage, fallback or synthetic flag; badges on pages | `docs\ARCHITECTURE.md` §3; `frontend\src\features\regime\RegimeBanner.tsx:86` | yes: response metadata (`pulse3\src\pulse\api\envelope.py`) and `MetaStrip` |
| Settings and environment variables | PULSE_MODE, PULSE_DATA_DIR, PULSE_PUSH_WEBHOOK, PULSE_PUSH_KIND, PULSE_SCHEDULER, PULSE_NEWS_DIR, PULSE_INTRADAY_EXT_DIR, PULSE_HOLDINGS_FILE, OPENAI_API_KEY, PULSE_CHROMIUM | `backend\app\config.py:1` | partial: `pulse3\src\pulse\config.py`; `pulse3\deploy\windows\pulse3_env.ps1` |
| Access model | Binds to localhost; reached through an SSH tunnel; no login | `README.md:128` | partial: session-cookie principal (`pulse3\src\pulse\api\auth.py`) |
| Storage and migrations | SQLite, DuckDB and Parquet; no migration step | `backend\app\core\storage.py:1` | partial: forward-only migrations, 11 files (`pulse3\migrations\`) |
| Documentation set | Architecture, execution log, research notes, rebuild plans | `docs\` | partial: `pulse3\docs\` (BUILD_BRIEF.md and milestone evidence) |