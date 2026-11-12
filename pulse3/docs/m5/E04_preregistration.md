# M5-E04 pre-registration: shadow forward record

Written 2026-11-13 (Asia/Shanghai), before any session after 2026-11-12 is observed. This file is the
pre-registration the package's M5-E04 asks for; it must not be edited after the first observation node.
Corrections go in a new dated section below the original text.

## What is recorded

Two forward books run in the new ledger (`pulse.domain.portfolio.forward_book`, rules frozen on 2026-11-01):

| Book | Rule |
|---|---|
| `前向主书（持有5日）` | from the current main release's `5td` market ranking, offer the top 40; buy 4 new names per session at the next raw open, 50,000 CNY each, 20 slots, hold 5 sessions |
| `前向影子（持有10日）` | same ranking, 2 new names per session, hold 10 sessions |

- Costs: the user account profile (commission 0.05% with a 5 CNY minimum, sell stamp duty 0.05%, transfer fee 0.001%).
- Execution: raw open of the next session; no fill when the open is limit-up; T+1; 100-share lots.
- Each session the daily pipeline runs `pulse.workers.forward daily` after the release build. The decision uses
  only the release published before that open. The ledger events are append-only, so later data revisions
  cannot rewrite what was decided.
- The books are labelled 检验中. They carry no ranking weight and do not change any production permission.

## Observation nodes and verdict

| Node | Sessions after the first fill | Recorded |
|---|---|---|
| N1 | 5 | NAV, excess over the main-board equal-weight median, fills, costs, skipped orders |
| N2 | 10 | same, plus the M5-E03 10-session continuity check |
| N3 | 20 | same; verdict |

- Verdict at N3: **pass** if the main book's net excess (after declared costs) over the main-board median is > 0
  and no session is missing from the record; **fail** if it is <= 0; **insufficient evidence** if a session is
  missing or fewer than 20 sessions were recorded.
- A failure or insufficient evidence is a valid result and stays in the record. No parameter is tuned between nodes.
- Reaching N3 does not by itself give any strategy a production permission (PromotionPolicy still applies).
