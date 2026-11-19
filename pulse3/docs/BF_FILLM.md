# Fill-market lane (legacy backfill, audit of 2026-11-18)

Contract `backfill_fillm.schema.json`, reads under `/v1/x/bf/fillm`, tests `tests/bf_fillm/`, web `apps/web/src/pages/bf/fillm/`.

| Row | What | Where | Status |
|---|---|---|---|
| #32 / #90 / #155 | industry diffusion (limit-ups, share up, leader, followers, 5-session spread) and institutional net buy | `GET /v1/x/bf/fillm/crowd`, `application/bf_fillm_crowd.py`, `CrowdPanel` on the market page | done |
| #32 / #90 / #155 | name clusters | same read, field `name_clusters` | **unavailable, recommended for retirement**: pulse3 keeps no security-name history, the old rule used the current names for every past day |
| #43 | volume ratio, circulating cap, seal success rate | `GET /v1/x/bf/fillm/stock-stats-plus`, `application/bf_fillm_stock.py`, `StatsPlusRows` inside `StockStatsCard` | done; the seal success rate covers only the sessions with seal facts (policy `seal-facts-v2`) and prints how many |
| #142 / #144 / #146 | float-cap playbooks | `domain/playbooks/capseries.py`, `application/bf_fillm_caps.py`, `playbook_service.evaluate_playbook` | 5 of 6 now evaluate on cap versions known at each session; `value_pe_pb` still cannot (no PIT fundamentals) |
| #39 | watchlist tape | `WatchTape` on the home page (ext A tape filtered by instrument id) | done |
| #13 | bear-mode banner | `BearBanner` on the market page | done (state label, ledger E016) |
| #121 | forward holidays | `domain/market_rules/holidays.py`, `forward_days`, `HybridCalendar` | mechanism done; **2026 and 2027 are MISSING**: no official notice text is stored in the repository |
| #114 / #115 / #174 | calendar seed | source `legacy_calendar_store`, dataset `legacy_calendar_events`, `python -m pulse.workers.fillm import-calendar` | done; reader for the forward log: `bf_fillm_calendar.read_calendar_events(conn, lake, cutoff)` |

## Float-cap playbooks (point in time)

A session sees only the cap versions known then (`as_of <= session`, recorded by the session's usable time under the observed policy), scaled by the
adjusted-close ratio from the version's as-of date; a version older than 400 days is not used; before the first usable version there is no cap and the
playbook has no signal. The legacy securities table holds three cap versions (2026-10-30, 2026-10-31, 2026-11-09), so the evaluation of these playbooks
has signals only from 2026-11-02 on: results are valid but thin until the daily import collects more versions. `small_cap` also needs 250 bars.

## Holiday schedule

`OFFICIAL_SCHEDULES` accepts a year only with the notice title, issuer, publication date, source and the closed weekday dates (`YearSchedule` refuses less).
The old calendar seed links SSE notice `c_20251222_10802507` (2025-12-22) but its text is not in the repository, so 2026 is MISSING; 2027 is not published
in anything the repository holds. Future counting keeps the Monday-Friday rule for a MISSING year and reports `assumed`.

## Failure codes of a float-cap playbook trial

`data_not_available:float_cap:no_cap_dataset_visible` (no cap dataset READY at the experiment cutoff) and
`data_not_available:float_cap:no_instrument_has_a_cap_version_usable_in_the_panel` (a dataset exists but no version is usable by any session of the
evaluated slice, e.g. its only version is dated at the last session). `tests/ext_b/test_pg_api.py` asserts the second one for the synthetic world; `tests/bf_fillm/test_caps.py` asserts the same failure for all five cap playbooks and then that they evaluate once an early version exists; the `no_cap_dataset_visible` branch has no test of its own.
