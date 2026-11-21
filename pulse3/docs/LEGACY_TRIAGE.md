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

## Batch 3: research tools (user decision 2026-11-17: recommended plan plus the PC fusion main book)

Source: the old lab scripts (`scripts/lab/`, about 70) and the experiment ledger (`docs/research/ledger/experiments.jsonl`,
92 entries: 16 works, 21 mixed, 27 inconclusive, 28 fails). A tool enters pulse3 only with its pre-registration and a test.
A forward experiment that is still recording moves only after pulse3 and the old system ran in parallel and agreed day by
day (the reconciliation is the acceptance test).

| Lane | What comes back | Old rows / ledger | Acceptance |
|---|---|---|---|
| lab | the whole ledger imported as read-only history (verdict, reason, when to reactivate, artifacts) and linked from the experiment archive | #177, #178 | 92 of 92 entries, byte-exact source hash |
| lab | tear sheet: Sortino, Calmar, longest drawdown in sessions, profit factor, win rate, average win and loss, exposure | #160, #161 | hand-computed fixtures |
| lab | batch comparison: one playbook replayed on the top 10 of a screen run, through the job queue | #36, #77 | job test |
| fwdlog | tail-of-day forward log (E077) and index first/last half hour (E078) from the imported 60 s snapshots | #173 | same rows as the old log for every common day |
| fwdlog | market-state forward log with recorder health (E089) | #174 | same rows as the old log |
| fwdlog | intraday paper replay, triggers v2.1: B1 and B3 shadow, S1/S3/M3 sells (E053-E059) | #163, #165, #166, #171, #164 (box5 as a shadow, E059) | same fills as the old paper log |
| fusion | the round-5 static fusion scorer (`static40_safe`, main board): factor expression engine, zoo + mined pool + AlphaGen expressions as frozen data, level-safety filter, expanding-window IC weights | #168, family B (engine only) | top-40 overlap >= 95% against the PC scores for the last 60 sessions (the PC's own reproduction gate) |
| fusion | frozen main book (5-day hold, 20 positions), 10-day shadow (E065), curve gate 20 d / 0.3 (E036/E040), OS20 (E081), X20 basket (E085), and the fusion forward monitor (E068) | #169, #170, #172 | same decisions as the PC logs for every common day |

Deferred, not retired: factor mining and model training (GP E007, AlphaGen E009, LLM mining E008, LightGBM). The mined
pool is frozen data for the scorer; new mining stays in the old lab on the PC until a mining run is pre-registered in
pulse3. E092 (web-search LLM) stays switched off; spending needs the user's approval.

Retired research tools (the code is not ported; the finding stays in the imported ledger):

| Family | Ledger | Reason |
|---|---|---|
| traditional rule batteries, Sequoia audit, exit-only tests (trad_battery, trad_exits, sequoia_audit, exit_tails, trullas_div) | E003, E033, E058, E060 | fail against the same-day matched baseline (E062) |
| playbook rotation and selection (strategy_panel, style_switch, analog_select; old rows #22, #152 rotation part) | E005, E006, E035, E042 | fail; the regime timeline itself is ported (`domain/market_state/regime.py`) |
| leader-cycle simulations (replay, cycles, claims_check, b1_regime) | E027, E030, E037, E038, E054 | fail on daily bars; reactivation needs intraday matching, which the paper replay covers |
| market-wide gates and calendar clustering (exposure breadth gate, regime.py clustering, r7_2 scaling) | E018, E020, E044 | fail |
| US overnight (overnight_us, hist_index_events US drop) | E072, E084 | fail |
| stock-level RL (rl_fqi) | E015 | fail |
| short-window mining, factor registry rerun, Chan pattern fix (gp_mine window, registry, p0_patterns PIT) | E010, E047, E061 | fail |
| vetoes and turnover variants (overbought_veto, neg_veto_bite, rank_buffer, random_controls, llm_reverse) | E063, E064, E066, E079, E080 | fail |
| one-off data and ops scripts (backfill_index_day, ann_fetch, ext_daily, export_*) | E069, E087, E090 | done once; the data now comes through pulse3 sources |
| descriptive studies (calendar_effects, macro_events, etf_surge, zodiac, demon, theme_check, cninfo_industry) | E021, E026, E028, E052, E073, E074, E083 | descriptive only; the forward rules they suggest are E089 (ported) or wait for a pre-registration |

## Audit 2026-11-18 and dispositions

An independent row-by-row audit of all 206 inventory rows against the code (not the docs) found 116 ported, 69 partial,
13 retired with a reason, 1 deferred, 3 missing, 4 in progress (fusion lane); 25 rows depended on a producer that was never
scheduled and 26 rows read the old system at runtime. The per-row table is kept privately (`.work/audit/`). User decision
(2026-11-18): fix step 1 now; step 2 (cutover readiness) writes and tests pulse3's own recorders but switches them on only
at the cutover, so the old and new systems never double the requests from one IP.

Numbering: batch 1 and 2 use the inventory order. Batch 3 above numbered sections 5-6 one higher: read "#168 fusion" as
row 167, "#173 tail" as 172, "#174 state" as 173, "#177/#178 ledger" as 176/177, and the lab families as rows 178-183.

Step 1, scheduling (producers that existed but never ran on the server):

| Rows | Producer | Schedule |
|---|---|---|
| 15, 92, 19, 75, 84, 85, 87, 36 | `alerts drain`, `research run`, `screen-run`, `tactic-run`, `lab run-batch` | task Pulse3Jobs, every 5 minutes |
| 9, 69, 127, 39 | `analysis actionability` + `analysis tape` | task Pulse3Tape, weekdays 09:30-15:05 every 5 minutes; **disabled 2026-11-19 until the cutover**: actionability fetches live Sina quotes from the server IP while the old recorder polls the same host (its first run got HTTP 403 at 09:30 and the circuit opened; the old recorder kept recording). The cutover script enables it again when the old recorder stops |
| 12, 94 | `analysis outcomes` | Pulse3Daily, after the build |
| 114, 115 | `alerts calendar-sync`, `calendar-derive`, `calendar-expiries` (this month and next) | Pulse3Daily |
| 37, 80, 16, 82 | `research ext-b-run` (52 playbooks + 6 factors) | task Pulse3Weekly, Saturday 09:00 |
| 32 | `intel fundflow --focus 60` (the old system never crawled it) | Pulse3Daily |
| 136 | `intel community` (guba) | not before the cutover: the old recorder samples the same pages from the same IP |

Step 1, ports in progress: #16/#82/#150 fitness and verdict, #21/#87 custom run form, #35 monthly heatmap, #37/#80 IC and
long-short series, #42/#68/#158 analogs on demand, the matched baseline and block bootstrap (#182: E062, E082); #32/#90/#155
industry diffusion, #43 stats fields, #142/#144/#146 cap facts for six playbooks, #39 watchlist tape, #13 bear banner from
the regime trend, #121 forward exchange holidays, the old calendar seed as a pulse3 source.

Step 1, retired or deferred with a reason (rows that had none):

| Rows | Feature | Disposition |
|---|---|---|
| 10, 11, 154 | playbook-vote ensemble, its weights and the 总榜 tab | retired: replaced by the release ranking (one ranking model with explanations); selecting or weighting playbooks by recent fitness failed in the ledger (E005, E006, E042) |
| 44, 123 | factor breakdown, four-dimension scoring engine | retired: replaced by the ranking's per-instrument explanation bodies, which carry their inputs |
| 45 | plain-language observations | retired: generated sentences without an evidence trail; the evidence tabs show the same facts with their source |
| 51 | 1-9 view keys | retired: Ctrl+K and `/` cover navigation; bare number keys collide with typing in forms |
| 53 | top ribbon vitals | retired: the same vitals are on /market |
| 164 | closing market state rule (E054) | retired: ledger E054 fails |
| 165 | board rules M2 and M4 | retired: candidate thresholds never validated (E055 kept M3 only; M3 is ported in the paper replay) |
| 196, 197 | single command entry and launchers | retired: replaced by `pulse.workers.*` and the scheduled tasks in `deploy/windows/` |
| 134 | LLM fallback for news alerts | deferred: it spends API money; enabled only with the user's approval, like E092 |
| 175 | holdings book | the PC fusion book's holdings come with the fusion lane; the light daily book is superseded by the forward books |
| 177 | pre-registrations | kept as documents; each ported forward experiment records its pre-registration's sha256 in the research registry |
| 178 (family A) | book, official_eval, cap_filter, lhasa_book, rolling_break, score_baskets, cond_reversal | retired: evaluation is the pulse3 reconcile and registry; findings stay in the ledger (E041, E051, E067, E070, E071); the X20 basket (E085) and OS20 (E081) are ported |
| 179 (family B) | rl_alloc, lockbox, tengine_check, verify_all, jobq, harness, wctc_overlay | retired: RL failed (E015); lockbox is the pulse3 blind-final budget; engine checks and the job queue are replaced by the fusion engine tests and the pulse3 queue; the curve gate (E036/E040) is ported |
| 180 (family C) | regime_eval, style_test | retired: E020 fails; E023 mixed and E024 needs a futures account (reactivation condition kept in the ledger) |
| 181 (family D) | sectors, sw_industry | merged: pulse3 sector domain and the frozen 申万 map of the fwdlog lane |
| 182 (family E) | trad_export, tech_signals, box5_scenarios, lhasa; sameday_base, matched_reeval, reeval_block | trad_export retired (E003); tech_signals merged into the server indicators (batch 1); box5 kept as the S_Box5m shadow (E059); lhasa deferred (E048 needs the user's approval for a portfolio test); the matched baseline and block bootstrap are being ported (E062, E082) |
| 183 (family F) | llm, llm_ask, llm_eval, llm_live, llm_judge, llm_judge_pit, llm_judge_topo, llm_probe, llm_select, llm_track, pit_news | deferred with E092: every one spends API money (findings E001, E025, E046 inconclusive, E064 fails) |

Step 2 (cutover readiness, recorders written and tested, switched on only at the cutover): #103/#185 intraday recorder with
auction, order book and limit queues; #104/#186 extended recorder; #131/#187 news recorder; #108 adjusted and index bars and
#112/#113 boards and listing dates from native sources; #49/#89 sync triggers; #119 a second quote source with fallback;
#199/#203 keys, tools and paths out of `C:\pulse`; the fwdlog M3 catalyst read from the pulse3 calendar.

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

## Step 2 status (2026-11-19): written and tested, not switched on

| Rows | Native producer | Command (not scheduled) | Output |
|---|---|---|---|
| 103, 185, 119 | intraday recorder, Tencent primary (the old recorder's host, the only one with every legacy column), Sina fallback per session on a block | `ingest intraday-record --universe` / `run_intraday_record.ps1` | `quote_snapshots_60s`, same frame as the legacy import, lineage `pulse_intraday_recorder` |
| 104, 186 | extended intraday recorder (ETF, index, CFFEX, A50, CNH ...) | `rec_ext record` / `run_ext_record.ps1` | `ext_quote_snapshots`, lineage `pulse_native_ext` |
| 131, 187 | 7x24 news recorder (Tonghuashun 10 s, Eastmoney and CLS 30 s, 08:00-22:00) | `rec_news record` / `run_news_record.ps1` | the same document store and claims as `legacy-news`, source `native_news_recorder` |
| 108, 112, 113 | adjusted bars (Sina raw / Sina factor, the old method), index bars, boards, listing dates | `refdata ...` / `run_refdata.ps1` | NEW datasets `*_native_*` beside the legacy ones; `refdata reconcile` compares them; consumers not switched |
| 49, 89 | data sync jobs queued from the health page | `sync_jobs run --max 2` (in `run_jobs.ps1`, only after the cutover) | `ops.sync_job_log` |
| 199, 203 | key and tools under `C:\pulse3` (`set_openai_key.ps1`; deploy copies uv and Python once) | - | `C:\pulse` copies only as a fallback |
| fwdlog M3 | catalyst events from pulse3's calendar dataset (imported before each forward-log run) | - | old file only until the first import |

Cutover (`deploy/windows/cutover.ps1`, dry run by default, `-Apply` only with the user's approval). Preflight: the four old
recording tasks exist (PulseIntraday, PulseIntradayExt, PulseNews, PulseNewsAlerts), the native scripts are deployed,
`C:\pulse3\secrets\push_webhook.url` exists (else `-NoPush`: phone pushes stop with PulseNewsAlerts), and it is not a weekday
07:30-22:10 Asia/Shanghai. Then: state `switching`, old tasks disabled (not deleted) and their python processes stopped,
Pulse3IntradayRecord 09:10, Pulse3ExtRecord 08:55, Pulse3NewsRecord 07:55 (weekdays) and Pulse3SyncJobs (every 10 minutes,
5 h limit) registered, Pulse3Tape enabled, end state verified, state `cutover`; any error rolls back and writes `failed`.
With `cutover` the import scripts skip the old files (one recorder per session day), daily `intel-news` is skipped (the news
recorder covers it), sync runs execute, and pulse3 pushes to the phone. The native `run_*_record.ps1` refuse to start before.
Every pulse3 process paces per host across processes (`PULSE_HOST_PACING_DIR`), so two recorders sharing a host stay at
1 request/s. `-Rollback -Apply` reverses it.

Known gaps: the intraday recorder at 1 request/s needs about 39 s per full board and cuts hot polls (reported as
`truncated_hot`); Eastmoney limit pools, minute bars, convertible bonds and guba follow-ups of the old recorders are not
ported; the news recorder polls weekday holidays; native boards are current membership only (no history).

## Re-audit 2026-11-21 (independent, read-only, against the code at f050170)

Of the 77 rows that were not ported on 2026-11-18: 37 are now ported, 16 retired with a written reason, 0 missing;
14 partial, 6 wait for the refdata switch, 4 deferred. Totals over 206 rows: ported 153, partial 14, retired 29,
deferred 4, waits-for-switch 6. The per-row table is kept privately (`.work/audit/`).

Dispositions added now:

| Rows | Feature | Disposition |
|---|---|---|
| 32, 90, 155 (name part), 145 (three name/sector playbooks) | linkage by shared characters of short names ("名称同字联动") | retired: it needs point-in-time short-name history, and the industry/seat linkage (E093/E094, batch 6) is the tested form of the same idea; the two seat playbooks of #145 move to `intelligence.seat_report` |
| 171 | gate_grid (grid search of curve-gate parameters) | retired: the curve gate (E036/E040) is frozen in the fusion books; a parameter grid on the same data is a new multiple look and would need its own pre-registration |
| 84 | exit summary across all playbooks | partial by design: per playbook only; a cross-playbook summary adds no decision (exits are compared within a playbook) |
| 103 (limit pools) | Eastmoney limit pools in the native recorder | written, off by default (`--limit-pools`); switched on by the user after the first native week |

Work started from the re-audit (code only, no user decision, no new live requests): native instrument register, ST and
caps plus alias-independent code lookup (rows 108/113, prerequisite of the refdata switch); market tape from the native
snapshots (9/69/127/39); E089 health from the native ext coverage (173); M3 catalyst from pulse3's own calendar behind
`PULSE_CATALYST_SOURCE` (174); the two seat playbooks (145); `sync_refdata` job kind (49/89); guba sampling after the
cutover (136, run_daily); a failed legacy import no longer stops the build when the native reference data is on.
