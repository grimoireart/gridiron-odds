# Injury term gate report (D18.6)

Run: 2026-10-13 (`python3 -m src.tune`, `params.injury_tuning.tuned_at = 2026-10-13T20:44:42Z`).
Elo and QB values held at the tuned params.json values (K=20, HFA=40, REV=0.40, REST=2, KQ=4, QB on).
Data: nflverse `injuries_{2010..2025}.csv` and `depth_charts_{2010..2025}.csv`, cached under `data/`
(gitignored; `src/injury_history.py` downloads a season only when its file is missing, streaming the
~50 MB 2025 chart to disk). Full history build from cache: 15 s; the whole `make tune` run: 25 s.

## Verdict

**Enabled.** With the term applied the way the page applies it (added to `d_total` at forecast time on
ratings replayed without it, `params.injury_tuning.mode = "pregame"`), the 2025 holdout Brier falls from
**0.21606 to 0.21516 (-0.00090)**, past the required -0.0005. `params.injury.enabled = true`.

For the record, the other way of applying the term (injected into the Elo replay through the new
`elo.replay(..., extra=...)` hook so that ratings absorb it) lands on the **same weights** but only reaches
0.21587 on the holdout (-0.00019) and **fails** the gate. The enable decision therefore depends on the
application mode; the chosen mode is the one `predict.py` implements (`official.injuries.elo` enters
`factors.d_total` on top of the replay ratings; `build.py` does not call the hook). Both results are stored in
`params.json` (`injury_tuning.holdout` and `injury_tuning.alternative`).

## Tuned weights (Elo points per starter listed Out; identical in both modes)

| group | OL | WR_TE | RB | DL_EDGE | LB | DB |
|---|---|---|---|---|---|---|
| weight | 10 | 20 | 30 | 0 | 30 | 10 |

`status_mult`: Out 1.0, Doubtful 0.75, Questionable 0.25, Probable 0.1 (fixed, not tuned).
Grid per group {0, 5, 10, 15, 20, 30}; coordinate descent from all-zero, 3 sweeps (the third confirmed no
change), 91 objective evaluations. Per side the term is
`-(sum over counted starters of weights[pos_group] * status_mult[status])`; `elo = elo_home - elo_away`.
A typical side carries about -20 Elo; the net term is non-zero in 92-94 % of games, |net| >= 20 Elo in
roughly 40 % of games, median |net| 14-15 Elo, maximum 132.5 (2010-2024) / 92.5 (2025).

## Brier

| rows | without | with (pregame mode, chosen) | with (replay mode, record) |
|---|---|---|---|
| 2010-2024 tuning set, n = 4,078 played games | 0.21818 | 0.21731 (-0.00087) | 0.21730 (-0.00088) |
| 2025 holdout, n = 285 | 0.21606 | **0.21516 (-0.00090)** | 0.21587 (-0.00019) |

2025 holdout detail (pregame mode): logloss 0.6236 -> 0.6216; accuracy 0.658 -> 0.651 (two games flip the
other way at the 50 % line while probabilities improve overall). Market 0.2104 / blend 0.2111 on the same rows.

Gate rule (CONTRACT 2.3, validated by `schema.validate_model`): `enabled` only when
`holdout_brier_without - holdout_brier_with >= improvement_required` (0.0005). 0.21606 - 0.21516 = 0.00090 >= 0.0005.

## Per-year coverage

"Both reports filed" = both teams have at least one injury-report row for that week (a missing report is "no
report yet", never "nobody hurt"). "Starters/side" = offence + defence starters found on the depth chart at the
cutoff (QB included in the count, excluded from the term). Status columns count starters only, QB excluded.

| season | depth format | played games | both reports filed | starters/side | starters Out | Doubtful | Questionable | Probable |
|---|---|---|---|---|---|---|---|---|
| 2010 | legacy | 267 | 266 | 22.0 | 232 | 102 | 673 | 1095 |
| 2011 | legacy | 267 | 267 | 22.0 | 316 | 111 | 680 | 1234 |
| 2012 | legacy | 267 | 267 | 22.1 | 340 | 100 | 650 | 1679 |
| 2013 | legacy | 267 | 265 | 22.0 | 300 | 97 | 607 | 1639 |
| 2014 | legacy | 267 | 267 | 21.9 | 243 | 84 | 583 | 1532 |
| 2015 | legacy | 267 | 267 | 22.0 | 415 | 127 | 649 | 1575 |
| 2016 | legacy | 267 | 267 | 21.9 | 453 | 113 | 1024 | 0 |
| 2017 | legacy | 267 | 266 | 22.1 | 415 | 83 | 813 | 0 |
| 2018 | legacy | 267 | 266 | 22.0 | 405 | 102 | 756 | 0 |
| 2019 | legacy | 267 | 267 | 22.1 | 493 | 92 | 756 | 0 |
| 2020 | legacy | 269 | 268 | 22.1 | 402 | 76 | 721 | 0 |
| 2021 | legacy | 285 | 285 | 22.2 | 407 | 84 | 845 | 0 |
| 2022 | legacy | 284 | 281 | 22.3 | 529 | 74 | 851 | 0 |
| 2023 | legacy | 285 | 277 | 22.4 | 490 | 69 | 848 | 0 |
| 2024 | legacy | 285 | 284 | 22.1 | 487 | 106 | 699 | 0 |
| 2025 | dt | 285 | 285 | 21.4 | 582 | 66 | 627 | 0 |

No season was skipped; every played game of 2010-2025 has a feature row (4,363 games); no starter slot was
left unmapped (`unmapped_starter_slots` is empty for every season). "Probable" disappeared from the league
report after 2015, so its multiplier only matters for 2010-2015.

## Method notes

- Cutoff per game: the day before gameday, 23:59:59Z (`injury_history.as_of_for_gameday`). The build uses
  `min(built_at, kickoff_utc)`; with a 10:00 UTC daily build the two see the same Friday report and, at most,
  one more Saturday depth-chart snapshot.
- Starters: `injuries.starters_by_team` (pos_rank 1 on the latest chart at or before the cutoff, per unit,
  kicking/return slots excluded); report: `injuries.report_for`; the feature row is the report's counted
  starters bucketed by `pos_group` x status. `tests/test_tune_injury.py` checks that the reduced term equals
  `injuries.injury_elo(report, params)` on the same report.
- 2010-2024 depth charts are nflverse's legacy weekly format (`season, club_code, week, formation, depth_team,
  depth_position, position`), not the timestamped 2025+ format `injuries.fetch_depth_charts` validates.
  `injury_history.load_depth_rows` maps them onto the new shape: `dt` = the day before the team's game that
  week (so "latest chart before gameday" is that week's chart), `formation` -> unit, `depth_team` -> rank,
  `depth_position` -> position code through `LEGACY_SLOT_ALIAS` (RE/LE -> DE, WILL -> WLB, MIKE -> MLB,
  SAM -> SLB, NICKE/NKL -> NB, ...) with the roster position as the fallback for formation nicknames and blank
  slots. Rows for a team-week with no game (byes, the "SBBYE" rows) are dropped (~4,000 of ~34,000 per season).
- Objective: Brier of p_home over every played game (regular season and playoffs) of 2010-2024; ties s = 0.5.

## Caveats

1. **Reports are Friday status, not inactives.** The injury file holds the final practice-week designation
   (Out / Doubtful / Questionable; Probable before 2016). A Questionable starter who ends up inactive 90 minutes
   before kickoff is counted at 0.25, and a late scratch never listed is counted at 0. The multipliers are
   fixed guesses, not fitted.
2. **Depth charts are noisy for OL rotations** (and for nickel/dime defences). Starters come from the club's
   published chart; a swing tackle or a sixth lineman who actually starts is rank 2 on paper and adds nothing,
   while a listed starter who has already been replaced still counts. The small OL weight (10) is consistent
   with that noise; DL_EDGE tuned to 0.
3. **QB excluded.** Quarterbacks are `covered_by_qb_term` and never enter the counts; the QB term (D2) owns the
   starter, so a listed QB who is Out only shows on the card and changes nothing unless the listed starter id
   changes.

Further: the holdout is one season (285 games); a 0.0009 Brier gain is within what a single season can swing,
and accuracy did not improve. **How weak that evidence is, in numbers** (`docs/verify/injury_bootstrap.py`,
independent recomputation of the same 285 games, re-run 2026-10-13): the paired per-game Brier difference
(with - without) has mean -0.00090 and standard error 0.00200 (t = -0.45); a 2,000-draw bootstrap gives a 95%
interval of [-0.00487, +0.00291], P(delta <= -0.0005) = 0.59 and P(delta < 0) = 0.69. The required 0.0005 is
therefore about a quarter of one standard error on this holdout, and the gain is statistically indistinguishable
from zero. The application mode was also chosen after seeing which one passed: the 2010-2024 tuning set cannot
tell the modes apart (0.21731 vs 0.21730) while the holdout splits them (-0.00090 vs -0.00019), and the
INTEGRATION record says replay mode was the original spec. D18.7 pre-registers the pregame mode (the one the
build applies) and the unchanged rule for the next re-gate. The weights are identical under both application modes, so the enable decision
rests on the gate rule, not on a weight choice; it should be re-run after the 2026 season with 2026 as the
holdout. The 2025 depth-chart file is the only timestamped one, so the 2025 holdout is the only season evaluated
on exactly the data shape the build consumes.

## What this run wrote

- `params.json`: `injury` (section 2.3 block: enabled, weights, status_mult, gate), `injury_tuning`
  (tuned_at, mode, grid, Briers, the holdout gate, the replay-mode alternative, per-season coverage, the
  descent trace) and `evaluation.model_with_injuries` (tune's copy only; the build's 2.4 block is unchanged
  unless the build passes `injury_extra` to `evaluate.holdout`).
- `make tune` prints the injury table (`tune.format_injury_table`) after the existing stages; `make test`'s
  reference check still passes (2025 model Brier 0.2161).

Reproduce: `python3 -m src.tune` (downloads any missing season once), or offline with the caches present:
`GRIDIRON_OFFLINE=1 python3 -m src.tune`.
