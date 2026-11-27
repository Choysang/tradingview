# Retiring the rest of the old system after the recorder cutover (draft 2026-11-20; full retirement procedure added 2026-11-24)

The cutover (`deploy/windows/cutover.ps1`, 2026-11-20 22:15) stops only the four old recording tasks. The old tasks below
keep running. Each one stays until its condition holds; each retirement disables the task (never deletes it, never
touches `C:\pulse` data) and is a separate decision of the user. **Superseded on 2026-11-24** by the full retirement below (all nine tasks unregistered, `C:\pulse` archived and deleted). Task actions were read on the server on 2026-11-20.

| Old task (action, trigger) | What it does | Does pulse3 still read its output? | Same-IP double requests today | Retire when | Proposed |
|---|---|---|---|---|---|
| PulseLhbBackfill (`run_lhb.cmd --years 3`, one-off 2026-10-31 23:59) | 3-year LHB backfill, already done | yes, once: the 3 years of seats came from the legacy import | no (finished) | already: one-off time trigger, ran 2026-10-31 23:59 with result 0, no next run | nothing to do |
| PulseLhbDaily (`run_lhb.cmd --days 5`, 18:40) | daily LHB seats from Eastmoney into the old warehouse | no for new days: pulse3 `intel seats --days 2` (Pulse3Daily ~19:15) fetches its own | **yes**: both crawl the Eastmoney LHB pages every evening | pulse3 seats for the last 10 sessions equal the old warehouse's (row count and seat names per stock) | after a 10-session comparison |
| PulsePaper (`run_paper.cmd`, 17:20) | old daily paper book | no: pulse3 paper replay (fwdlog, triggers v2.1) and the forward books replace it | no | the fwdlog paper replay has run 10 native sessions without a failed day | after the native week + 5 |
| PulseForwardLab (`run_forward_lab.cmd`, 17:45) | old forward lab experiments | no: the fusion lane (batch 3) runs the frozen score, books, X20, OS20 on the server | no | fusion-daily produces 10 consecutive sessions and `fusion reconcile` against the PC stays >= 95% top-40 overlap | after 10 fusion sessions |
| PulseWeb (`run_web.cmd`, at start; site :8100 + in-process scheduler) | the old site, and after 15:35 on trading days: daily warehouse sync (Sina), report, push (push not configured) | **yes**: `legacy-import` reads the warehouse it syncs (adjusted bars, index, boards, listings); the calendar store `events.jsonl` feeds `fillm import-calendar` and the M3 catalyst | **yes**: its Sina daily sync and pulse3 `live-daily --universe` (and later `refdata daily`) fetch the same klines | `PULSE_REFDATA_SOURCE=native` has run 10 sessions with `refdata daily` scheduled, reconcile clean, and the M3 catalyst reads events pulse3 produces itself (`alerts calendar-sync`/`calendar-derive`) or the old store is frozen deliberately | last; the site itself goes when the user no longer opens :8100 |
| (PC) PulseDailyForward 20:30 | the PC fusion pipeline (scores, books, monitor) | no: server fusion replaces it; reconcile reads the PC files | no (PC IP) | same as PulseForwardLab | with PulseForwardLab |

Order: PulseLhbDaily after its comparison; PulsePaper and PulseForwardLab (+ the PC task) after
10 clean sessions; PulseWeb last, after the refdata switch. Each step: disable, keep 5 sessions of overlap in the logs,
then update `docs/LEGACY_TRIAGE.md` and this table.

Pulse3 settings that pointed into `C:\pulse` (`PULSE_LEGACY_WAREHOUSE`, `PULSE_LEGACY_DATA`, the `PULSE_FWDLOG_EVENTS` default, the OpenAI key and tools fallbacks) are handled by the procedure below: gated by `legacy_state.json` or removed.

## Retirement procedure (2026-11-24: the user keeps only pulse3)

Decision 2026-11-24: the old system on the server (`C:\pulse`, about 7 GB, and its nine scheduled tasks) is archived into one
compressed backup and deleted. The script is `deploy/windows/retire_legacy.ps1` (deployed to `C:\pulse3` by `deploy_app.ps1`).
It is a dry run by default; `-Apply` only with the user's approval.

Order of the whole move: `refdata_switch.ps1 -Apply` (native reference data; the pending task `Pulse3RefdataSwitchOnce` does
this) -> `refdata daily` and the LHB comparison clean for the sessions that matter -> `retire_legacy.ps1` (dry run, then `-Apply`).

```
powershell -File C:\pulse3\retire_legacy.ps1                    # preflight + every change printed
powershell -File C:\pulse3\retire_legacy.ps1 -Apply -Detached    # does it, in a one-off SYSTEM task (RetireLegacyOnce); read the log
powershell -File C:\pulse3\retire_legacy.ps1 -Rollback -Apply   # tasks back, only before the rename
```

Preflight (refuses with one reason per failed check, changes nothing):

| Check | Why |
|---|---|
| `refdata_source.txt` = `native` (an empty or missing file is refused, not crashed on) | otherwise pulse3 still reads the old warehouse |
| `secrets\openai.key` exists, `tools\uv.exe` and the interpreter exist under `C:\pulse3` | `pulse3_env.ps1` no longer falls back to `C:\pulse` |
| cutover state = `cutover` | the old recorders are off |
| no pulse3 run is in progress (Pulse3Daily, Pulse3ForwardLog, Pulse3IntradayImport, Pulse3NewsImport) | they may still read `C:\pulse` |
| nothing else resolves into `C:\pulse\`: the venv `pyvenv.cfg` (home), every scheduled task that is not one of the nine (Execute, Arguments, WorkingDirectory) and every service ImagePath (`pulse3-pg` is printed) | all of these would break when the tree goes (server on 2026-11-24: venv home is `C:\pulse3\tools\python\...`, the service is `C:\pulse3\pg18\...`) |
| no process runs from `C:\pulse\` except the PulseWeb tree (cmd `run_web.cmd`, uvicorn, children) | PulseWeb is stopped by the script, anything else is refused |
| old calendar store copied to `C:\pulse3\var\legacy_calendar\events.jsonl`, sha256 equal | done by `-Apply` (again after PulseWeb stopped, so the copy is final); the dry run reports the hash |
| free disk >= size of the archive set + 0.5 GB (compression is assumed to save nothing: the data is parquet) | a full C: would hurt PostgreSQL; read on 2026-11-24: set 6.45 GB, free 7.75 GB |
| `tar` self-test on a tiny synthetic tree | the exclusion arguments must drop exactly `.venv`, `node_modules`, `__pycache__` and `pulse/tools` |
| LHB comparison (`lhb_compare --json`): newest consecutive SAME sessions >= 10 | the old PulseLhbDaily goes only after 10 equal sessions; `-Force` relaxes only this check, and the number is always printed |
| native calendar holds events (`fwdlog catalyst-compare`) | after the retirement `PULSE_CATALYST_SOURCE=native` never falls back to the old store |
| archive and its folder do not exist (a folder left by a `failed` / `rolled_back` run is reused), no `C:\pulse.removing`, state is not `retired` / `deleting` / `switching` | nothing is overwritten |

Apply (every step is logged to `C:\pulse3\logs\retire_legacy_<date>.log`):

0. Run it detached: `-Apply` alone dies with a dropped ssh session half-way. `-Apply -Detached` does the preflight in the foreground, registers the one-off SYSTEM task `RetireLegacyOnce`, starts it and exits; follow `C:\pulse3\logs\retire_legacy_<date>.log` and `legacy_state.json`, and `Unregister-ScheduledTask RetireLegacyOnce` afterwards.
1. Export each task's XML to `C:\pulse3\backup\legacy_pulse_<date>\<task>.xml` (and its enabled / running state to the state file); the folder and the archive get an explicit ACL like the secrets folder (inheritance removed, SYSTEM and Administrators only, set with icacls on the empty archive file before tar writes into it, again after); state `switching`. Then **Pulse3Daily, Pulse3ForwardLog, Pulse3IntradayImport and Pulse3NewsImport are disabled, and Pulse3IntradayRecord, Pulse3ExtRecord and Pulse3NewsRecord too unless one is running at that moment** (a running recorder is left alone); all of them are enabled again in a `finally` block, on success and on failure (chosen instead of refusing near their next run time).
2. Stop and DISABLE (not yet unregister) the nine tasks (PulseWeb, PulseLhbDaily, PulseLhbBackfill, PulsePaper, PulseForwardLab, PulseIntraday, PulseIntradayExt, PulseNews, PulseNewsAlerts; absent ones are skipped), stop the PulseWeb processes, verify nothing runs from `C:\pulse\`.
3. Final copy of the calendar store, sha256 verified.
4. `tar -czf C:\pulse3\backup\legacy_pulse_<date>.tgz` of `C:\pulse`, excluding `.venv`, `node_modules`, `__pycache__` (any depth) and `C:\pulse\tools` (uv and the interpreter; pulse3 has its own copies).
5. Verify: the sorted `tar -tzf` file list must equal the sorted source list line by line (not just the count); sha256 and size are recorded; the listing is kept as `tar_list.txt`; the state records `archive_verified`.
6. Rename `C:\pulse` to `C:\pulse.removing` (a directory that is somebody's working directory or has open handles cannot be renamed, so a busy tree stops here with everything intact), write state `deleting`.
7. Only now unregister the nine tasks (a failure to unregister is a warning, listed in the state as `tasks_not_unregistered`; the tasks stay disabled), delete the tree, write state `retired`, restart `Pulse3Api` (pulse3's own port 8200 only).

Any failure up to the rename: the old tasks are only enabled again (they were never unregistered; started as before), `C:\pulse` is untouched, state `failed` with the error; a retry reuses the folder. A partial archive is removed; a **verified** archive (failure at the rename) is never deleted: the state records it as `archive_kept` and the retry writes `legacy_pulse_<date>_2.tgz` next to it (delete the kept one by hand if the disk is short). A delete that stops half-way after the rename leaves `C:\pulse.removing` and state `retired` with `leftover`; remove it with `cmd /c rmdir /s /q C:\pulse.removing`.
Other apps (OTN-AIOps :8000, :80, :8080) are never touched: processes are selected only by an image path or command line under `C:\pulse\`. `C:\pulse3` receives only the backup folder and archive, `legacy_state.json`, `var\legacy_calendar\events.jsonl` and the log.

State file `C:\pulse3\legacy_state.json`: `{state, at, by, archive, folder, sha256, counts{files_source, files_archived, bytes_source, archive_bytes, excluded}, tasks[{name, enabled, running, xml}], calendar_sha256, lhb_same_tail}`; states `switching`, `deleting`, `retired`, `failed`, `rolled_back`. `pulse3_env.ps1` reads it: `deleting` and `retired` mean retired.

Rollback: `-Rollback -Apply` works until the rename (it enables the old tasks and the paused pulse3 tasks and removes a partial archive of a crashed run; a verified archive stays). After the delete the old system comes back only by hand (the script prints the commands): `tar -xzf <archive> -C C:/`, copy `C:\pulse3\tools` to `C:\pulse\tools`, `uv sync` in `C:\pulse\app\backend`, `Register-ScheduledTask -Xml` per task from the folder, delete `legacy_state.json`, restart `Pulse3Api`. `refdata_switch.ps1 -Rollback` refuses after the retirement; `cutover.ps1 -Rollback` refuses too (checked at its start from `legacy_state.json`).

Tested on 2026-11-24 in a local sandbox only (nothing was run on the server): dry run, full apply, second run refused, rollback after the delete refused with the manual commands, failure in `tar` (tasks restored, partial archive removed, state `failed`), rename blocked by a process whose working directory is inside the tree (tasks re-registered from the XML, tree intact), and a successful retry afterwards.

### What changes in pulse3 once retired (`pulse3_env.ps1` is the single switch)

Chosen: the fallbacks to `C:\pulse\secrets\openai.key` and `C:\pulse\tools` are removed outright (the `C:\pulse3` copies exist: key, `tools\uv.exe`, interpreter), not made conditional; `deploy_app.ps1` now throws when the tools are missing instead of copying them from the old install.

| Item | Before | Retired |
|---|---|---|
| `PULSE_LEGACY_WAREHOUSE`, `PULSE_LEGACY_DATA`, `PULSE_FWDLOG_EVENTS` | set to `C:\pulse\...` (only inside the `LEGACY-BEGIN/END` block of `pulse3_env.ps1`) | unset; `PULSE_FWDLOG_EVENTS` = `C:\pulse3\var\legacy_calendar\events.jsonl` |
| `PULSE_CATALYST_SOURCE` | unset (legacy) | `native` (the old calendar stopped growing) |
| `PULSE_LEGACY_RETIRED` | unset | `1` |
| Python defaults | `LegacyRecordings` root and the calendar path pointed into `C:\pulse` | `C:\pulse3\var\legacy_data` (absent: reads as no recordings) and `C:\pulse3\var\legacy_calendar\events.jsonl` (the frozen copy) |
| `run_daily.ps1` `legacy-import` | runs (a failure is tolerated with native refdata) | skipped and logged; stops with `!!` if `PULSE_REFDATA_SOURCE` is not native; warns when today's `refdata daily` log has no `adjusted-refresh exit 0` |
| `run_daily.ps1` `calendar-legacy` | imports the live old store | skipped and logged; `run_fwdlog.ps1` imports the frozen copy |
| `$imported` gate (outcomes, fundflow, fusion-daily, llm-live) | result of `legacy-import` | true: none of these reads the warehouse. Fusion daily and the release build read the datasets chosen by `PULSE_REFDATA_SOURCE` (`bars_native_*`, `index_bars_native_1d`, native membership and caps); outcomes read the raw bars (`live-daily`); fundflow reads the database and Eastmoney |
| `fusion reconcile --pc` | compares with the PC files | exits 3 with "skipped" (those files stopped growing); not scheduled anywhere |
| `lhb_compare` | reads the old warehouse | exits 2 with "no old warehouse" (variable unset); the old seats stay in the archive |
| `run_fwdlog.ps1` `calendar-legacy` | imports the live old store | imports the frozen copy (same bytes: nothing new is published) |

Remaining references to `C:\pulse\`, all deliberate: the `LEGACY-BEGIN/END` block of `pulse3_env.ps1` (only while not retired); `retire_legacy.ps1` itself and `cutover.ps1` (history and process matching; `cutover.ps1` cannot run once the old tasks are gone); comments in `deploy_app.ps1`, `setup_server.ps1`, `set_openai_key.ps1`; the `--legacy-data` help text in `src/pulse/workers/ingest.py` (a file under edit by another change; the default it describes is already the pulse3 path); documentation under `docs/`. `tests/legacy_retirement` scans for anything else.

What is lost, by design: the old recordings and warehouse exist only in the archive (what pulse3 imported stays in the lake); E089's "unlisted holiday" check reads `summary.json` of old recorder days, which matters only when a pre-cutover day is decided again (decided sessions are never rewritten).

## Log

- 2026-11-26 00:30: retirement applied on the server (`retire_legacy.ps1 -Apply`, task Pulse3RetireLegacyOnce, result 0). The first attempt on 2026-11-25 22:30 stopped at preflight (LHB comparison 0 SAME sessions: the 11-25 seats were missing on the pulse3 side because `live-daily` failed with WinError 10054 and `run_daily.ps1` skipped `intel-seats`; fixed in e16dbb3, seats rerun by hand, 12 SAME). Archive `C:\pulse3\backup\legacy_pulse_2026-11-26.tgz` (6,624,910,677 bytes, 5672/5672 files, `archive_verified: true`, task XML exports beside it); `C:\pulse` deleted, the nine old tasks unregistered, `legacy_state.json` = retired, Pulse3Api restarted and answering 200, paused pulse3 tasks resumed. Log `C:\pulse3\logs\retire_legacy_2026-11-26.log`.
- 2026-11-24: the user decided to keep only pulse3. The conditions in the table above no longer gate the retirement (only the LHB check, which `-Force` can relax); the nine tasks and `C:\pulse` go together through `retire_legacy.ps1` (procedure above). Written and tested in a local sandbox; nothing was run on the server.
- 2026-11-21: PulseWeb died at 02:46 when an RDP logoff ended the session it ran in; the user chose to keep it down and its
  task is Disabled. From now on the legacy warehouse no longer grows (last session 2026-11-20): pulse3 needs
  `PULSE_REFDATA_SOURCE=native` plus a scheduled `run_refdata.ps1 -Mode daily` to keep its research data current.
- 2026-11-21 14:50: user asked to bring PulseWeb back without its duplicate requests. `C:\pulseun_web.cmd` now sets
  `PULSE_MODE=SIM` (backup `run_web.cmd.bak-2026-11-21`): the 24/7 live snapshot loop (akshare spot + qt.gtimg.cn enrich,
  the same hosts as pulse3's intraday recorder) no longer runs; 0 qt.gtimg / Eastmoney requests in the first 90 s after the
  restart. The site and its scheduler (warehouse sync after 15:35, report) keep running as SYSTEM in session 0, so an RDP
  logoff no longer stops it. The remaining overlap: its daily Sina kline sync (15:35) and pulse3 `live-daily` (18:22) fetch
  the same bars at different times; it goes with the refdata switch.
