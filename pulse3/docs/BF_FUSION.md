# Fusion lane (legacy triage batch 3): the frozen round-5 score, its books and forward experiments

What this is: the PC task `PulseDailyForward` (scores, frozen main book, 10-day shadow, forward monitor, X20 basket, OS20) moved into
pulse3 with the same frozen rules, so it survives the M6 cutover. Nothing here may depend on torch or lightgbm; numpy is the only
numeric addition (`uv add numpy`, in `pyproject.toml`).

## Frozen rules (unchanged; the pre-registration hashes are in `domain/fusion/data/manifest.json`)

* Score `fusion_static40_safe`: 392 level-safe factors of 428 (zoo 208, mined pool top 200 by |pre_ic|, AlphaGen 20), per-day float16
  percentile rank over the tradable main board, daily Spearman IC against the 5-day relative return (open t+1 to open t+6, minus the
  day's mean), expanding window of ICs from 2024-05-17 to six sessions back, the 40 factors with the largest |ICIR|, score =
  sum(ICIR x (rank - 0.5)) / sum|ICIR|. First score 2024-08-20.
* Main book: top 40, 20 slots, 4 new names a day, hold 5 sessions, T+1, costs 2.5/5/5 bp, curve gate (previous ungated NAV below the
  mean of the 20 before it: keep round(0.25 x 4) = 1 new name). 10-day shadow: 2 new names a day, gated day keeps 1.
* Monitor E068, X20 E085, OS20 E081: see `domain/fusion/forward.py` (each rule is in its docstring and in the pre-registration).

## Data (point in time, read from pulse3 only)

| Old field | pulse3 source |
|---|---|
| open high low close volume | `bars_legacy_fwd_adjusted_1d` (latest READY snapshot, latest revision with `available_at <= cutoff`), `bindings.load_research_inputs` |
| amount | typical price x volume / 1e8 (the source amount is null) |
| prev_close, pct_chg, limit flags, limit_streak | derived per stock row, main board 10% limit, price rounded to the cent, half-cent tolerance |
| float_cap, cap | `instrument_circulating_cap_legacy` (latest visible version, CNY / 1e8) x close / last close, only when the total cap is known |
| turn | amount / float_cap x 100 |
| days_listed | `core.instrument.listed_on` + the stock's rows |
| tradable | 20-row mean amount >= 0.5, listed >= 120, close >= 2, not price-ST (`st_like`), main board |
| industry (OS20, matched cells) | `sector_membership_legacy_boards_industry` |
| cgo, tk | derived from close, turn and ret (behavioural factors) |
| index close | not needed: the market-state columns are not factors of the static score |

## Datasets (derived, immutable snapshots; each publish is a new READY manifest)

`fusion_scores_current` (promoted scores), `fusion_scores_asof` (first computed score of a date, never rewritten), `fusion_ic_history`
(daily IC per factor, a stored IC is never replaced), `fusion_forward_log` (logs `main`, `h10`, `x20`, `monitor`, `os20`,
`os20_signals`, `run`). The five forward experiments are also registered in the research registry as `shadow` runs with their
pre-registration sha256 (`fusion register`).

## Tie order (the one unusual part)

The old engine ranked with an unstable `argsort` (Microsoft STL introsort). Factors with big tie groups are ordered by that sort,
so `domain/fusion/ranktie.py` is a port of it, verified against 12 fixtures generated with the old engine's torch. A stable sort
would rank tied stocks by code and move the top 40 by about a quarter.

## Daily step and commands

```
uv run python -m pulse.workers.fusion bootstrap          # one time: scores since 2024-08-20, IC history, as-of seed
uv run python -m pulse.workers.fusion register           # one time: registry rows of the five forward experiments
uv run python -m pulse.workers.fusion daily              # every session, after the daily analysis build
uv run python -m pulse.workers.fusion reconcile --pc <PC .work/lab>   # compare with the PC's files (read only)
uv run python -m pulse.workers.fusion audit-level-safety # re-run the level check against the frozen verdicts
```

A refused run (coverage < 95%, window too short, reproduction gate below 95% top-40 overlap on the last 60 sessions) writes a `run`
row with its code and changes nothing else; exit code 2.

## Reconcile against the PC (read only; run with the PC's `.work/lab`)

Result of the last run (panel through 2026-11-17, 738 sessions, 3282 main-board codes):

- Panel: no row only in pulse3; 3190 rows only on the PC, all of them the PC's 2026-11-18 session, which pulse3 had not imported. Price cells that differ by more than 1e-6
  relative: 1452 of 2.3 million (max 1.7%), a price revision between the two server snapshots. Flags: 3 limit-up and 2 limit-down rows differ,
  1 tradable row.
- Scores: 542 common sessions; top-40 overlap mean 0.9955 over all sessions, 0.9967 over the last 60 (acceptance 95%: PASS); rank
  correlation of the scores 0.99996. 19 sessions are below 95% (lowest 0.825).
- Books (main, h10), 5 logged days, with the PC's score file of each run: every decision (gate weight, number of buys, buy list, top 10
  offered, holdings) is identical. NAV and ungated NAV differ by at most 7 bp (price revisions); the reconcile prints each as explained.
  With pulse3's own scores (mode B) holdings differ on single names on 11-06 and 11-13: the PC's score history is the file as it was at
  each run.
- X20: 11 days, no difference. OS20: no difference in the rows; the frozen lists differ in one name on 2026-11-09. Monitor: 8 days; the
  5 differing fields on 10-30 and 11-02 are rows written on an earlier snapshot (the PC's current panel gives pulse3's value); with pulse3's
  scores (mode B) 20 fields differ because the PC's 2026-11-05 score file lacks 446 names of an incomplete server snapshot.
- Industry: three stocks (002217, 002617, 600714) sit in two industry boards of the legacy file. The PC takes the first board of the
  file, pulse3 the lowest node id, which is the same pick for all three.

## Cost of a daily run (measured on the development PC, 2 other Python processes competing)

Two consecutive incremental runs (2026-11-16, then 2026-11-17) on the 738-session panel: 753 s and 809 s of CPU, 1666 s and 1671 s of wall,
peak memory 2.18 GB, 61 rows ranked each (plus 1 new IC row on the second). The scores of 2026-11-17 are identical to the bootstrap's
(top 40 40/40, largest score difference 0). Most of the CPU goes into evaluating the 428 expressions over the whole window.
A run that finds nothing new takes about 45 s and 1.8 GB.
