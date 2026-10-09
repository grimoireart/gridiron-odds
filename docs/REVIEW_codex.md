# Independent adversarial review — Gridiron Odds

Date: 2026-10-09

## Disagreements with prior review

1. **A 15-minute Worker cache does not make a public proxy safe or establish the stated budget.** An Origin/Referer allowlist is only a browser CORS control; either header can be forged by non-browser clients. “At most one request per 15 min” is true only if the Worker ignores all caller query parameters, constructs one fixed upstream URL, uses one canonical cache key, and coalesces concurrent misses. Even then, 24/7 traffic can cause 96 upstream calls/day, exhausting 500 credits quickly. Best v1: the daily build makes the sole upstream call and publishes its normalized snapshot; the Worker, if retained for live refresh, must have a hard monthly kill switch and canonical cache key. Do not expose `x-requests-remaining` through `model.json`: it is build metadata that becomes stale and creates needless commits. Log it in Actions and optionally return it in a Worker metadata response.

2. **The proposed build-time Odds API call does not by itself solve pick freezing.** A once-daily 10:00 UTC build freezes Thursday/Sunday/Monday games at materially different horizons, and a browser “live” update can change the displayed pick without ever entering the log. Define the product as one official daily snapshot (and label its age), or add a scheduled pre-kick snapshot workflow. For disciplined v1, use one daily official snapshot; live browser odds may update prices, but must not rewrite the official pick/probability/record.

3. **The prior review understates the QB-model problem.** Updating team Elo and QB Q from the same game residual is not merely acceptable double-counting. Q measures team outcomes, not quarterback performance; the team retains part of the QB's history after a transfer while the QB carries another part to the new team. The parameters are not identifiable, and using the realized starter in historical CSV rows gives the holdout information that may not have been known at prediction time. Gate the entire QB feature on a strictly causal replay and default it off if it fails. If retained, key by `*_qb_id`, not name: the CSV itself assigns the same displayed name to multiple IDs (Kyler Murray is `00-0035228` at CSV line 5328 but `00-0035752` at line 5838). Names are presentation only; anomalous ID changes should also be validated or explicitly reconciled.

4. **Training the blend on 2015–2024 is better than fitting on 2025, but still mismatches the product.** nflverse moneylines are closing lines; the page and frozen log use an earlier live snapshot. A weight optimized against closing probabilities and evaluated using realized starters overstates what the daily product knows. Use rolling/blocked validation and describe the result as “blend against closing market,” not a live-pick backtest. For v1, a documented fixed 0.35 is acceptable; do not claim it was fairly fitted until timestamped historical odds exist.

5. **The fallback-total recommendation has the source priority backwards.** Current Odds API consensus is fresher than the CSV closing/scheduled line. Use live/build odds consensus, then CSV `total_line`, then a preseason constant blended into season-to-date scoring. Record the chosen source.

6. **Allowing NumPy conflicts with the explicit “Python stdlib model” and deploy reproducibility.** The requested simulation is small enough for an optimized stdlib implementation after eliminating repeated object allocation; benchmark before adding a dependency. If 10,000 full brackets cannot meet 5 seconds, lower the v1 simulation count with a displayed Monte Carlo error or relax the speed test. Do not silently run fewer simulations depending on whether NumPy happens to be installed.

7. **“Today's games in the viewer's local date” is wrong for an NFL slate.** A London game and late game can fall on different dates for viewers. Store an authoritative kickoff instant and group by NFL league date (`America/New_York`), while rendering kickoff in the viewer's timezone. The dataset's `gameday` is the league date, not the viewer's date.

8. **The suggested CLV secondary metric remains ill-defined.** American-odds “cents” are nonlinear and discontinuous at ±100. Compare no-vig implied probability (percentage-point movement) for a consistent consensus/book at pick time and close. Best-price movement can be shown descriptively but should not be called CLV unless the same book and side are available at both timestamps.

9. **An Origin allowlist breaks `file://` and complicates localhost**, despite “one command” local use. Local builds should consume the embedded build snapshot; live refresh is an optional served-mode enhancement. CORS must allow only the deployed site, and the UI must work when it fails.

10. **I agree with findings 6, 10, 13–18 with refinements:** tier labels must be unified; independence must be disclosed; book selection and approximate tiebreakers need defaults; starters must be called “listed”; ranks and rationale inputs belong in structured data; missing markets require model-only display. Finding 12 (which GitHub account) is not a technical spec issue and should remain an owner decision. Finding 17 is arithmetically correct but does not test rollover timing or exactly-once application.

## New findings (ranked by severity)

### Critical

1. **The specified tie update is always zero.** For a tie, `m=0`, so `ln(m+1)=0`; therefore `delta=0` regardless of how surprising the tie was. The file contains 15 ties, including `2025_04_GB_DAL` at CSV line 7054. A tie between mismatched teams is evidence and should use `mult=1` (or a separately documented tie multiplier). This changes the reproduced reference slightly, so regenerate the reference and stored metrics rather than preserving a known defect.

2. **The parlay optimizer constructs prices that may not exist.** “Best price per leg across selected books” followed by multiplication can combine, for example, a FanDuel leg with a DraftKings leg; no sportsbook offers that ticket. Enumerate parlays separately per book using that book's price for every leg, then rank `(book, legs)` candidates. Exclude a candidate if any leg is absent at that book. The payout is a quoted-price approximation, not guaranteed accepted parlay odds.

3. **The frozen-log specification is internally contradictory and not idempotent.** It says one line per game per build, permits repeated picks, requires immutable pick fields, and also says grading “fills” fields in old JSONL records. Filling mutates a line; reruns can duplicate snapshots; “last before kickoff” is unstable if timestamps tie. Use an append-only event log with deterministic IDs: `snapshot_id = game_id + scheduled_kickoff + build_at`, and separate `grade` events keyed to the selected snapshot. A material correction is another event, never an edit. A reducer produces the track record.

4. **Live odds and build-time rationale can disagree on the same card.** If JavaScript replaces market probability, blend, tier, price, EV, or pick after load, the build-generated rationale and frozen record become false. Separate immutable `official` fields from optional `live_market` fields. Live refresh may update displayed price and a clearly labeled live cross-check; it must not silently alter the official headline or rationale.

### High

5. **Future availability is overstated.** The CSV has blank scores for 207 rows, but blank QB names/IDs, spread, and total for 179 rows. The missing future block begins at `2026_07_NE_CHI` on CSV line 7371. Conversely near-term rows are populated: `2026_05_PHI_JAX` has starters and lines at line 7343. Code must distinguish `scheduled`, `starter_known`, and each market's availability; it cannot assume all upcoming starters or lines exist.

6. **Use kickoff instants, not date strings.** `gameday` plus `gametime` is ET-like schedule data but has no offset or timezone field; 259 historical rows have blank `gametime` (for example line 180). Feed matching by “kickoff date + teams” fails around international/prime-time timezone conversion and schedule changes. Normalize Odds API `commence_time` to UTC, use `(season, game_id, canonical teams)` as the internal identity, and match within a bounded UTC window only after team matching. Persist both `kickoff_utc` and `league_date`.

7. **The score construction/test is incoherent after integer rounding.** Rounded home/away scores generally cannot preserve an arbitrary `model_spread`; “nudge the favorite” changes both the displayed margin and implied total. A neutral 50/50 game with total 45 also has no favorite to nudge. Store unrounded expected points and displayed integer scores separately; tests should allow rounding error, not assert exact equality of displayed margin and model spread.

8. **“Expected value after vig” is misleading and the probability used is not calibrated for ROI.** The EV formula correctly evaluates a offered price, including its vig, but nothing is “removed” from that payout. More importantly, a Brier-fitted blend is not proven calibrated in the tails where EV-positive labels arise, and using consensus probability against best price can manufacture apparent small edges. Label it “model-estimated EV at this price,” suppress positive-edge styling below a tolerance (for example 2 percentage points / 2 cents per dollar), and never treat it as validated profitability.

9. **Market accuracy needs a declared pick and tie policy.** A no-vig market probability of exactly 0.5 has no winner pick; NFL games can tie. The CSV contains 15 ties. Define accuracy as `hit=1/0` for decisive results and `push/null` for ties (excluded from accuracy and units unless the sportsbook settlement rule is explicitly modeled). Brier can score a tie as 0.5 only if that convention is disclosed; binary Brier otherwise has no valid target.

10. **Season simulation inputs are underspecified.** Division/conference mappings are not among the listed CSV fields, postseason matchups are generated rather than read, ties are never simulated, and “current Elo frozen” conflicts with applying future QB adjustments/HFA. Create a versioned 32-team metadata file and define each matchup probability from frozen team Elo plus HFA/rest only; omit QB from unknown future starters. Simulate only regular-season rows. State that playoff tie resolution and no regular-season ties are approximations.

11. **QB decay is nondeterministic as written.** “A QB who has not started in 365 days decays 25 points” could fire on every build after day 365, once per season, or once per elapsed year; season regression may also compound it. Define a pure function of elapsed days (for example 25 points toward zero per completed inactive year), apply it lazily before prediction/update, and store `last_start_date`. Also clarify that Q regresses toward zero, so a negative replacement improves toward average during inactivity—possibly the opposite of intent.

12. **The download safety rule can retain bad data or reject legitimate corrections.** A nondecreasing row count does not detect schema loss, duplicates, truncated columns, changed historical rows, or an HTML error page; it can also reject a legitimate upstream correction that removes rows. Validate required columns, unique IDs, parseability, score-pair completeness, allowed teams/types, and plausible season counts; download to a temporary file then atomically replace the cache. Pin/log the source URL and content hash.

### Medium

13. **Model chronology needs explicit rules.** Sort by `(gameday, gametime)` is insufficient when time is blank and multiple games share a timestamp. Rollover must occur exactly once immediately before a season's first game; do not regress teams again at calendar year boundaries. Define a stable final key `(season, gameday, missing-time-last, gametime, game_id)` and test it.

14. **GitHub Actions can race itself and write churn.** Cron, manual dispatch, and reruns may overlap; a push can also invalidate the checkout. Add a workflow `concurrency` group, least-privilege `contents: write`, fetch/rebase immediately before committing, and commit only changed generated artifacts/events. Prefer a single workflow owning `model.json`, `index.html`, and log events. A failed push must fail loudly rather than leave an apparently successful stale deployment.

15. **The data provenance does not support historical “announced starter” claims.** The CSV records the starter who played, not a timestamped announcement. A causal replay may use it as an oracle only if labeled as such; it cannot validate change-detection available to the real daily build. Store `starter_source`, `observed_at`, and `status`; say “listed starter” in current copy.

16. **The edge-reason priority can assert an invented cause.** “Model lag after a blowout” or injury news is not observed merely because probabilities differ. Rank only measured factors and say “may reflect information this model lacks.” Never emit “likeliest reason” without attributable data.

17. **Front-end accessibility and safe embedding are absent.** Requirements need keyboard-operable tabs/accordions, visible focus, `aria-expanded`/relationships, semantic tables, non-color-only status, reduced-motion behavior, and sufficient contrast. Escape embedded JSON (`</script>`, U+2028/U+2029) or load a separate `model.json`; otherwise upstream text can break the script context. Avoid rendering feed strings with `innerHTML`.

18. **Static page plus live fetch needs an explicit state machine.** Cards should expose `official snapshot`, `refreshing`, `live`, `stale`, and `unavailable` states without layout shift. Display timestamps with timezone, never replace values with zero/`NaN`, and preserve the complete embedded snapshot if fetch, parse, schema, or game matching fails.

19. **Track-record denominators can silently differ.** The market may lack a line, picks may be model-only, games may tie/cancel, and repeated snapshots exist. Every aggregate must publish `n`; model-vs-market comparisons must use the same eligible game set. Calibration must use frozen pre-kick probabilities only, never post-load live values.

20. **The spec says result logging is both out and in scope.** Scope says “not … logging results,” while the Track record section requires a committed graded log and units. Resolve in favor of the explicit Track record requirement, but call it a model pick ledger—not a bet log or bankroll record.

## Recommended v1 architecture + model.json contract

Keep one deterministic Python build and one optional thin Worker. The official product is the generated snapshot; browser code only renders it and optionally overlays normalized live prices.

### Module boundaries

- `src/schema.py`: schema version, enums, canonical teams, validation, JSON-safe serialization.
- `src/data.py`: atomic nflverse download/cache, CSV parsing, aliases, ordering, kickoff normalization, source hash.
- `src/elo.py`: pure probability/MOV/update/rollover functions and causal replay. No file or UI logic.
- `src/qb.py`: QB state keyed by nflverse ID, decay/regression, starter availability; feature flag/gate isolated from team Elo.
- `src/market.py`: American/decimal conversions, two-way no-vig probabilities, consensus/median rules, Odds API normalization, game matching. Shared JSON fixture contract with Worker.
- `src/predict.py`: creates immutable official game predictions, factors, expected/display scores, tier, and rationale tokens/text from structured inputs.
- `src/simulate.py`: deterministic seeded regular season + approximate playoffs using frozen team ratings and static `teams.json` metadata.
- `src/ledger.py`: append-only snapshot/grade events, deterministic IDs, reducer, calibration/units/CLV aggregates.
- `src/evaluate.py`: blocked tuning/holdout reports with explicit eligible samples; no tuning inside `build.py`.
- `src/build.py`: orchestration only—load, validate, replay, obtain one odds snapshot, predict, simulate, reduce ledger, validate output, render atomically.
- `src/page_template.html` + `web/app.js` + `web/styles.css`: rendering and optional live overlay; no model math except per-book parlay enumeration from validated fields.
- `proxy/odds.js`: fixed upstream request, normalized allowlisted books/markets, canonical cache key, cache lock, hard usage cutoff, strict CORS. It returns no secret or arbitrary upstream URL.

`parlay.py` is unnecessary for production if interaction is browser-only, but keep a pure Python mirror under tests as the oracle for JavaScript fixtures.

### Contract rules

- Version the contract (`schema_version`) and reject unknown major versions.
- JSON uses canonical team IDs and UTC ISO-8601 timestamps; probabilities are 0–1 numbers, American odds are integers, absent values are `null`—never empty strings.
- Every derived value declares source/as-of where ambiguity matters. Official values never change in the browser.
- Rationale is rendered from `factors`; do not scrape prose to test numbers.
- Keep full precision in JSON and round only for display.

### Proposed shape (abridged; structure and types are normative, sample metric values are illustrative)

```json
{
  "schema_version": "1.0.0",
  "build": {
    "id": "2026-10-09T10:03:12Z-a1b2c3d",
    "built_at": "2026-10-09T10:03:12Z",
    "official_odds_at": "2026-10-09T10:02:41Z",
    "league_date": "2026-10-09",
    "data": {"nflverse_sha256": "…", "rows": 7548, "max_result_date": "2026-10-08"}
  },
  "params": {
    "elo": {"k": 20, "hfa": 40, "rest_per_day": 2, "reversion": 0.4, "tie_multiplier": 1.0},
    "qb": {"enabled": false, "k": 6, "new_qb": -50, "annual_reversion": 0.2},
    "blend": {"model_weight": 0.35, "fit_window": "fixed-v1"},
    "tier_bounds": {"lean": 0.55, "strong": 0.65, "lockish": 0.78}
  },
  "evaluation": {
    "season": 2025,
    "samples": {"model": 285, "market_comparable": 280},
    "model": {"brier": 0.221, "logloss": 0.634, "accuracy": 0.646},
    "market": {"brier": 0.210, "logloss": 0.608, "accuracy": 0.660},
    "blend": {"brier": 0.209, "logloss": 0.604, "accuracy": 0.664}
  },
  "games": [{
    "game_id": "2026_05_PHI_JAX",
    "season": 2026, "game_type": "REG", "week": 5,
    "league_date": "2026-10-11", "kickoff_utc": "2026-10-11T13:30:00Z",
    "away": "PHI", "home": "JAX", "neutral": false, "status": "scheduled",
    "availability": {"starter_home": true, "starter_away": true, "market": true},
    "official": {
      "pick": "JAX", "p_home_model": 0.685, "p_home_market": 0.766,
      "p_home_blend": 0.738, "tier": "strong",
      "expected_home": 24.9, "expected_away": 16.6,
      "display_home": 25, "display_away": 17,
      "market": {
        "source": "odds_api", "as_of": "2026-10-09T10:02:41Z",
        "consensus_total": 41.5, "consensus_home_spread": -7.5,
        "books": [{"key": "draftkings", "home_ml": -390, "away_ml": 310}]
      },
      "best_single_price": {"book": "draftkings", "side": "JAX", "american": -390},
      "estimated_ev_per_dollar": -0.073,
      "factors": {
        "team_elo_home": 1610, "team_elo_away": 1514, "elo_gap_home": 96,
        "home_rank": 4, "away_rank": 13, "hfa_elo": 40,
        "rest_home": 7, "rest_away": 7, "rest_elo": 0,
        "qb_home": {"id": "00-0036971", "name": "Trevor Lawrence", "q": 0, "source": "nflverse_listed"},
        "qb_away": {"id": "00-0036389", "name": "Jalen Hurts", "q": 0, "source": "nflverse_listed"},
        "form_home_3": 47, "form_away_3": -31, "model_market_gap_home": -0.081
      },
      "rationale": ["rating", "home_field", "market", "verdict"]
    },
    "result": null
  }],
  "teams": [{
    "team": "JAX", "name": "Jaguars", "conference": "AFC", "division": "AFC South",
    "elo": 1610, "rank": 4, "record": {"w": 3, "l": 1, "t": 0},
    "trend": [1580, 1560, 1594, 1610],
    "simulation": {"runs": 10000, "projected_wins": 11.8, "playoffs": 0.970, "division": 0.920, "seed1": 0.343, "super_bowl": 0.123}
  }],
  "scorecard": {"official_picks": 0, "hits": 0, "losses": 0, "pushes": 0},
  "track_record": {"overall": {"n": 0, "hits": 0, "accuracy": null, "units": 0.0}, "by_tier": [], "calibration": []}
}
```

Do not put API quota, secrets, raw untrusted bookmaker labels, or a mutable `live_market` object into this file. The Worker response should use a smaller separately versioned `{schema_version, fetched_at, games:[{game_id, commence_time, books:…}]}` contract. JavaScript validates it, matches by canonical identity, and treats it as an overlay.

## Acceptance test corrections

Replace or add the following tests:

1. **Moneyline conversion:** the current `ml_prob(-150,+130) home prob in 0.40–0.45` is wrong. Raw implied probabilities are 0.600 and 0.435; normalized home probability is about `0.580`, away `0.420`, summing to 1. Test even money and reject zero odds.
2. **Tie update:** a tie at equal ratings changes neither team; a tie with a 200-point favorite moves ratings toward each other using the explicit tie multiplier. This catches the current zero-MOV defect.
3. **MOV symmetry/conservation:** test home and away wins with identical pregame absolute edge and margin; team deltas sum to zero and remain finite when the underdog wins.
4. **Chronology/rollover:** shuffled fixtures yield the same replay; rollover happens once before each season and turns 1600 into 1560—not merely that the arithmetic helper does.
5. **QB causality:** identify QBs by ID; an unseen listed starter uses `new_qb`; missing starter disables QB contribution without inventing a name. Holdout gating uses only information with `observed_at < kickoff` and the identical eligible rows for QB/no-QB.
6. **Score rounding:** assert unrounded expected margin equals `model_spread` and expected points sum to total. Displayed integers may differ by a documented tolerance (normally ≤1 point); define 50/50 odd-total behavior explicitly.
7. **Blend:** retain the arithmetic test, plus endpoints (`w=0/1`), missing-market fallback, and complementary home/away probabilities. Do not test a fitted weight on its fit set.
8. **EV:** test American-to-decimal conversion on both signs, `+100 @ 0.5 = 0`, `-110 @ 0.5 ≈ -0.04545`, missing price, and price/probability side alignment.
9. **Parlay:** retain `0.6 × 0.6 = 0.36` and fair decimal `2.777…` / American about `+178`; additionally prove every returned candidate uses one book, rejects a missing leg, excludes duplicate games, and uses 2–5 unique legs.
10. **Rationale:** the existing requirement that every rationale names “QB or rest when present” conflicts with the factor thresholds. Test that factor keys drive sentences, all formatted numbers map to structured values, absent factors are omitted, and text never asserts an unobserved cause.
11. **Proxy:** use a fake upstream. Verify canonical cache key, ignored caller query parameters, concurrent-miss coalescing, TTL boundary, hard quota cutoff, CORS allow/deny, upstream error passthrough without caching, schema validation, and absence of key in body/headers/loggable URL. “Never includes key” cannot be reliably tested against a real service.
12. **Feed matching:** aliases, reversed teams rejection, kickoff within tolerance, schedule change, international game, duplicate/no match, and ambiguous match must fail closed to embedded data.
13. **Simulation:** sums should be approximately 7 playoff berths per conference and 1 Super Bowl winner within Monte Carlo tolerance, not exactly `7.0/1.0` unless counts (not rounded probabilities) are checked. Also assert each simulation has 8 division winners, two No. 1 seeds, no duplicate qualifiers, reproducibility by seed, and no mutation of ratings.
14. **Ledger:** rerunning the same build produces no new snapshot; two distinct pre-kick snapshots select the latest by `(build_at, snapshot_id)`; post-kick snapshots are ineligible; grades are idempotent append-only events; postponed/cancelled/tied games follow explicit policy.
15. **Track-record fixture:** six wins and four losses at `-110` is **positive**, not negative: `6×0.9091 − 4 = +1.4545 units`; break-even is 52.38%. Correct the expected assertion. Add a 5–5 fixture for negative units.
16. **CLV:** compare same-side, same-source no-vig probability at two timestamps; test crossing even money and missing close. Do not measure “average American cents.”
17. **Calibration:** bins with `<10` are marked low-sample and excluded from summary calibration error, but still render; bin edges include exactly 0/1 deterministically and counts sum to eligible frozen picks.
18. **Data validation:** required schema, unique `game_id`, paired scores, allowed enums, valid numeric ranges, row-count plausibility, atomic cache preservation on invalid download, and the observed future missing-field pattern.
19. **Responsive/accessibility:** test at 400 CSS px with no body overflow, keyboard tabs/accordions, focus visibility, accessible names/states, non-color status text, dark/light contrast, and no uncaught errors when live odds fail.
20. **Embedded-data safety:** fixtures containing `</script>`, quotes, Unicode separators, and bookmaker HTML render as text and cannot execute markup.

The two incorrect acceptance expectations—moneyline probability and 6–4 units—would otherwise force a correct implementation to fail, so they should be fixed before parallel implementation begins.
