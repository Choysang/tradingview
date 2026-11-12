# Package compliance status (2026-11-13)

Checklist against `spec_package/traceability/milestones_v32.json` (M0-M6) and the 16 product extension cards.
"Done" means the evidence file exists and its tests or drill ran. "Waiting" means the item needs future trading
sessions or a decision by the user. Nothing below is claimed from a plan alone.

## Milestones

| Milestone | Status | Evidence |
|---|---|---|
| M0 database, migrations, contract registry | done | `docs/db/M0_evidence.md` |
| M1 sources, lake, health, capability | done | `docs/data/M1_evidence.md` |
| M2 one ledger, costs, concurrency | done | `docs/execution/M2_evidence.md` |
| M3 rankings, plans, research registry | done | `docs/decision/M3_evidence.md` |
| M4 web pages on the one contract | done | `docs/web/M4_evidence.md`, `docs/web/I2_evidence.md` |
| I1-I3 integration, web, intelligence | done | `docs/integration/I1_evidence.md`, `docs/web/I2_evidence.md`, `docs/intel/I3_evidence.md` |
| PEX01-PEX16 product extensions | done | `docs/ext/A_evidence.md`, `B_evidence.md`, `C_evidence.md` |
| M5-E01 same-cutoff reconciliation | done | `docs/m5/m5_e01_2026-11-09.md`: 0 unexplained differences in securities and prices |
| M5-E02 recovery on the target host | done | `docs/m5/E02_recovery_drill.md`: 3 findings fixed, rerun passed |
| M5-E03 continuity over 10 sessions | **waiting** | needs 10 sessions of the daily pipeline after 2026-11-13; check `C:\pulse3\logs\daily_<date>.log` |
| M5-E04 shadow forward record | **recording** | pre-registered in `docs/m5/E04_preregistration.md`, first decisions 2026-11-12; nodes N1/N2/N3 after 5/10/20 sessions |
| M5-E05 candidate and baseline snapshot | done | `docs/m5/m5_e05_2026-11-12.md` |
| M6 cutover, scope exclusions, rollback | **waiting** | starts after M5 passes; the cutover is irreversible and needs the user's decision |

## Items kept exactly as the package defines them

These look like gaps but follow the package's own type definitions. Changing them would depart from the package,
so they stay as proposals (the package says a contract change goes through one contract owner with schema, SQL
mapping, client compatibility and verification).

| Item | Package definition | What the implementation does | Proposal |
|---|---|---|---|
| `TypedFeature` has no inline value | card type `TypedFeature` (`status: proposed_type_not_public_v3`), value behind `result.artifact` | values are in the artifact ref (`screen-value://...`); `readArtifact` resolves them | add an inline `value` field |
| `StreamEnvelope.notification` is the four-type `UpdateNotification` | card type `StreamEnvelope` | tape events use their own log (`queryMarketTape`, `streamMarketTape`) | register `market.tape.event` and merge the streams |
| `League` pairing outcome enum lacks `insufficient_evidence` / `execution_failed` | public v3 `League` enum; FINAL_SYSTEM_SPEC §5 allows `insufficient_evidence` | stored exactly in the database; the wire maps to the nearest enum value and returns warning `LEAGUE_OUTCOME_MAPPED` | add the two values to the v3 enum |
| List entries carry only `instrument_id` (M4 G1) and similar G2-G8 | public v3 schemas | labels come from extension reads (`/v1/x/b/instrument-labels` and others) | see `docs/web/M4_evidence.md` section 2 |

## Fixed in this round (2026-11-13)

- The server build failed with an empty universe. Three causes: no raw closes for the session, a 10-day research
  window, and delisted aliases in the raw refresh. All three are fixed, with tests.
- Cap facts keep every dated version, so a history rebuild uses the cap known for its session.
- The user plan compile step runs in one transaction with a per-plan lock (`tests/ext_c/test_pg_plans.py::test_a_failed_compile_leaves_no_partial_state`).
- Recovery: pool connection check, API supervisor loop, exit code 3 for an unreachable source.

## Known limits (not package items)

- No intraday first-seal time (no tick data source). Turnover is estimated where the source has no amount.
- pulse3 has no intraday recorder or 7x24 news schedule yet; the old system still records both.
