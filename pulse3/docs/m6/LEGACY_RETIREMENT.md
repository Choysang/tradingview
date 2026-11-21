# Retiring the rest of the old system after the recorder cutover (draft 2026-11-20)

The cutover (`deploy/windows/cutover.ps1`, 2026-11-20 22:15) stops only the four old recording tasks. The old tasks below
keep running. Each one stays until its condition holds; each retirement disables the task (never deletes it, never
touches `C:\pulse` data) and is a separate decision of the user. Task actions were read on the server on 2026-11-20.

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

Pulse3 settings that still point into `C:\pulse` and go with PulseWeb: `PULSE_LEGACY_WAREHOUSE` (legacy-import),
`PULSE_LEGACY_DATA` (old recorder files, already skipped after the cutover), `PULSE_FWDLOG_EVENTS` default
(`C:\pulse\app\backend\data\calendar\events.jsonl`), and the fallbacks for the OpenAI key and the tools in `pulse3_env.ps1`.

## Log

- 2026-11-21: PulseWeb died at 02:46 when an RDP logoff ended the session it ran in; the user chose to keep it down and its
  task is Disabled. From now on the legacy warehouse no longer grows (last session 2026-11-20): pulse3 needs
  `PULSE_REFDATA_SOURCE=native` plus a scheduled `run_refdata.ps1 -Mode daily` to keep its research data current.
