# Gridiron Odds — data contract v1.0.0 (NORMATIVE)

Every builder codes against this file literally. `src/schema.py` is the executable form
(`validate_model`, `validate_overlay`, constants); `tests/fixtures/model_fixture.json` is a complete
valid example; `tests/fixtures/overlay_fixture.json` and `tests/fixtures/odds_api_sample.json` are the
overlay and raw-feed examples. Where this file and DECISIONS.md disagree, DECISIONS.md wins and this
file has a bug — say so in your report. Source: REVIEW_codex.md "Recommended v1 architecture"
amended by DECISIONS.md D1–D17 (D16 as amended 2026-10-09: default books draftkings, fanduel,
betmgm, betrivers; Caesars is absent from the feed).

## 0. Global rules

- JSON only. Team identity = canonical nflverse code (`KC`, `LA`, `LAC`, `LV`, `WAS`, ... the 32 in
  `src/teams.json`). Never `STL`/`SD`/`OAK`/`LAR` in output; `data.py` aliases them on load.
- Timestamps: ISO-8601 UTC with a trailing `Z`, second precision: `2026-10-11T13:30:00Z`. Dates:
  `YYYY-MM-DD`. `league_date` is the nflverse `gameday` (an America/New_York date).
- Probabilities are floats in [0, 1]; American odds are ints with |x| >= 100; absent = `null`,
  never `""`, never `NaN`, never `0` as a stand-in.
- Full precision in model.json (no rounding of floats); round only for display in the page.
- Home-positive spread convention for MODEL fields (`model_spread`, `expected_home - expected_away`,
  CSV `spread_line`): positive = home favored. BOOK convention for market fields
  (`books[].home_spread`, `consensus_home_spread`): the point attached to the home team's spread
  outcome, negative = home favored. `market_from_csv` therefore sets `consensus_home_spread = -spread_line`.
- Official fields never change in the browser. The overlay (section 6) may only change what the
  page shows under a LIVE label: best price, live EV, "price moved".
- Nullable means the key is always present and may be `null`. Keys are never omitted: every
  object has exactly the keys listed here (`validate_model` rejects missing AND unexpected keys).
- Display rounding of points (`display_home/away`, text numbers): round half away from zero,
  `int(math.floor(x + 0.5))` for x >= 0 — NOT Python's banker's `round()`. Percentages in prose:
  same rule on `100*p`.
- Pure functions for all math, dicts and lists only at module boundaries, no global state, no
  third-party packages, Python 3.11+ syntax.

## 1. Normalized game (output of `data.load_games`, input to everything else)

```
{
  "game_id": "2026_05_PHI_JAX",      str   nflverse id (season_week_away_home)
  "season": 2026, "game_type": "REG"|"WC"|"DIV"|"CON"|"SB", "week": 5,
  "gameday": "2026-10-11",           str   as in CSV
  "gametime": "09:30" | null,        str   HH:MM ET as in CSV, null when blank
  "weekday": "Sunday" | null,
  "league_date": "2026-10-11",       == gameday (D7)
  "kickoff_utc": "2026-10-11T13:30:00Z",   gameday+gametime in America/New_York -> UTC (zoneinfo);
                                     blank time => 13:00 ET
  "time_known": true|false,          false when gametime was blank
  "home": "JAX", "away": "PHI",      canonical codes
  "home_raw": "JAX", "away_raw": "PHI",    codes as written in the CSV (STL etc. survive here)
  "location": "Home"|"Neutral"|null, "neutral": bool (location == "Neutral", case-insensitive)
  "home_score": int|null, "away_score": int|null, "played": bool (both scores present),
  "result": int|null (home - away), "total": int|null (home + away), "overtime": bool|null,
  "home_rest": int|null, "away_rest": int|null,
  "home_moneyline": int|null, "away_moneyline": int|null,   CSV CLOSING moneylines
  "spread_line": float|null (home-positive), "total_line": float|null,
  "home_qb_id": str|null, "away_qb_id": str|null, "home_qb_name": str|null, "away_qb_name": str|null,
  "div_game": bool|null, "roof": str|null, "surface": str|null, "stadium": str|null,
  "sort_key": (season, gameday, 1 if gametime is null else 0, gametime or "", game_id)   tuple, D1
}
```
`load_games` returns the list already sorted by `sort_key`. Rest difference rule (D1 ground truth):
when EITHER rest field is null the rest term is 0 and `factors.rest_diff` is null.

## 2. model.json (top level: exactly these 8 keys)

### 2.1 `schema_version`: `"1.0.0"`. Consumers reject a different MAJOR version.

### 2.2 `build` {}
| key | type | meaning |
|---|---|---|
| id | str | `built_at` compact + "-" + first 7 hex of `data.nflverse_sha256`, e.g. `20261010T140312Z-3f2a9c1`. Used in ledger snapshot ids. |
| built_at | ISO UTC | wall clock at build start |
| official_odds_at | ISO UTC or null | when the single Odds API call returned; null when `odds_source == "csv"` |
| league_date | date | the America/New_York date at `built_at` |
| next_league_date | date or null | first `league_date` >= `build.league_date` that has a scheduled (not final) game in `games[]`; null if none |
| odds_source | "odds_api" or "csv" | whether ODDS_API_KEY was set and the call succeeded |
| data | {} | `source_url` str, `nflverse_sha256` 64-hex, `rows` int, `max_result_date` date or null (latest league_date with a final score), `fetched` bool (false under GRIDIRON_OFFLINE) |

### 2.3 `params` {} (mirrors params.json; every key required)
- `elo`: `k` 20, `hfa` 40, `rest_per_day` 2, `reversion` 0.4, `tie_multiplier` 1.0 (numbers)
- `qb`: `enabled` bool, `k` (KQ), `new_qb` -50, `annual_reversion` 0.2, `inactivity_penalty` 25, `usual_starter_window` 8
- `blend`: `model_weight` w in [0,1], `fit_seasons` [2015, 2024], `fit_metric` "brier"
- `tier_bounds`: `lean` 0.55, `strong` 0.65, `lockish` 0.78 (strictly increasing)
- `score`: `points_per_elo` 25, `league_average_total` float (computed mean total of `league_average_season`), `league_average_season` int (previous full season)
- `sim`: `runs` 10000, `seed` 2026
- `books`: list of default book keys, `["draftkings","fanduel","betmgm","betrivers"]` (D16). `official.best_prices` is keyed by exactly this list.

### 2.4 `evaluation` {} (2025 holdout; produced by `evaluate.holdout`, RUN BY EVERY BUILD — SPEC "runs with every build"; params.json keeps tune's copy for reference only)
`season` int; `samples` {`model` int, `market_comparable` int}; `model`, `model_no_qb`, `market`,
`blend` each {`brier`, `logloss`, `accuracy`} floats >= 0 or null (null only when the sample is 0).
Same eligible rows for `model` and `model_no_qb`; `market`/`blend` use the `market_comparable` rows.

### 2.5 `games[]` — one entry per game of the CURRENT season in `sort_key` order (all REG rows; played
rows carry `result`). Each game has exactly these keys:

| key | type | meaning |
|---|---|---|
| game_id, season, game_type, week | as section 1 | |
| league_date, kickoff_utc, time_known | as section 1 | |
| away, home | code | |
| neutral | bool | |
| status | "scheduled" or "final" | final iff played |
| availability | {} | `starter_home` bool, `starter_away` bool (qb id listed), `market` bool (official.market != null), `csv_lines` bool (CSV moneylines present) |
| official | {} | section 2.6 |
| result | {} or null | null unless status == "final"; section 2.7 |

### 2.6 `official` {} — immutable snapshot, exactly these 23 keys

| key | type | meaning |
|---|---|---|
| pick | code | `home` if `p_home_blend >= 0.5` else `away` |
| p_home_model | prob | pregame model probability (team Elo + HFA + rest + QB) |
| p_home_market | prob or null | `market.consensus_p_home`; null without a market |
| p_home_blend | prob | `w*p_home_model + (1-w)*p_home_market`; == p_home_model without a market |
| p_pick_blend | prob | p_home_blend from the pick's side (>= 0.5) |
| tier | "Toss-up"/"Lean"/"Strong"/"Lock-ish" | `schema.tier_for(p_pick_blend, params.tier_bounds)` |
| model_spread | float | `d_total / points_per_elo`, home-positive (model alone; informational) |
| blend_spread | float | `400*log10(p/(1-p)) / points_per_elo` from `p_home_blend`, home-positive; drives the score |
| expected_home | float | `(total_used + blend_spread)/2`, unrounded |
| expected_away | float | `(total_used - blend_spread)/2` |
| display_home, display_away | int >= 0 | round-half-up of expected; if equal, +1 to the favorite (favorite = home when p_home_model >= 0.5). Each within 1.5 of its expected value (0.5 rounding + the 1-point nudge; `schema.DISPLAY_TOLERANCE`). NOTE: `pick` follows the blend (D3) while the score follows the model spread (D5), so with a market the pick's display score can be the LOWER one (14/272 games with w = 0.05); the verdict sentence and the card say so — a product decision for Lauren if the score should follow the blend instead. |
| total_used | float | the total actually used |
| total_source | "odds_api"/"csv"/"league_average" | D5 priority: consensus median total -> CSV total_line -> params.score.league_average_total |
| market | {} or null | section 2.8; null when neither odds nor CSV moneylines exist |
| best_prices | {book: price-or-null} | keyed by EXACTLY `params.books`; price = `{book, side: pick, american}` = the pick side's h2h price at that book, null if that book has none (always all-null when `market.source == "csv"`) |
| best_price | price or null | highest decimal profit for the pick among `best_prices` (tie -> first in `params.books` order); when `market.source == "csv"` it is `{book:"csv", side: pick, american: CSV ML of the pick}`; null without a market |
| estimated_ev_per_dollar | float or null | `market.estimated_ev(p_pick_blend, best_price.american)`; null iff best_price is null. Label "Model-estimated EV at this price"; positive styling only when >= 0.02 (D8) |
| factors | {} | section 2.9 — the single source of every number in the rationale |
| rationale | list of keys | subset of `["rating","home_field","quarterback","rest","form","market","verdict"]` in EXACTLY that order; always starts with "rating" and ends with "verdict"; each key present iff its trigger below fires (no budget/cap: 2 to 7 sentences; `validate_model` enforces order and triggers via `schema.expected_rationale_keys`). SPEC's "3-5 sentences, in order of size" is superseded. |
| rationale_sentences | list of str | one sentence per key, same length and order as `rationale` |
| edge | {} or null | null unless market present and `abs(model_market_gap_pick) >= 0.08` (D17); else `{flag: true, reason_key, reason_text}` |
| parlay_eligible | bool | `market != null and p_pick_blend >= 0.55 and best_price != null` (a book-level check happens in the parlay builder) |

Rationale rules (predict.py): sentence keys are emitted when
- `rating` always: "{City} rates {N} Elo points above {City}, {rank}th in the league against {rank}th." (N = round(abs(elo_gap_home)))
- `home_field` iff not neutral: "Home field is worth about {hfa_elo} points, or {hfa_spread_pts} points on the spread."
- `quarterback` iff a listed starter `differs_from_usual`, or `first_start`, or `abs(qb_elo) >= 30`. A first start is said as "first start on record", never "rates X below the usual starter". Always "listed starter".
- `rest` iff `abs(rest_diff) >= 3`.
- `form` iff `abs(form_home_3) >= 40 or abs(form_away_3) >= 40`.
- `market` iff market present and `abs(model_market_gap_pick) >= 0.05`; the gap "may reflect information this model lacks" — never "injury news", never "edge".
- `verdict` always: "Blended, that comes to {City} at {P}%: a {tier} pick, predicted {X} to {Y}." or, without a market, "No market line yet, so this is the model alone: {City} at {P}%, ...". When the pick's display score is not the larger one (market overrules the model) the tail reads "a {tier} pick, though the model alone scores it {OtherCity} {Y} to {X}." — never "predicted" for a score the pick loses.
Every number in a sentence must be derivable from `factors`/`official` by the rules above (tests check this, not prose scraping). No adjectives about teams; no sentence about an absent factor.

Edge chip `reason_key` priority (D17, measured factors only): `starter_change` (either side `differs_from_usual`), then `rest_gap` (`abs(rest_diff) >= 3`), then `form_move` (either `abs(form_*_3) >= 40`, text "{City}'s rating moved {N} points in the last three games"), else `unknown` ("may reflect information this model lacks").

### 2.7 `result` {}
`home_score` int, `away_score` int, `winner` code or `"tie"`, `margin` int (home - away),
`hit` bool or null (null for a tie), `pred_margin_error` float (`model_spread - margin`),
`pred_total_error` float (`total_used - (home_score + away_score)`).

### 2.8 `market` {} (official market snapshot)
| key | type | meaning |
|---|---|---|
| source | "odds_api" or "csv" | |
| as_of | ISO UTC | `build.official_odds_at` or, for csv, `build.built_at` |
| consensus_p_home | prob or null | MEAN of per-book no-vig home probabilities over books with both MLs |
| consensus_total | float or null | MEDIAN of book totals |
| consensus_home_spread | float or null | MEDIAN of book home spreads (book convention) |
| n_books | int | == len(books) |
| books | list | `{key, home_ml, away_ml, home_spread, total}` each nullable except key; ALL books the feed returned (not only defaults; offshore books are dropped by `normalize_odds_api` — see 6.2). For csv: exactly one book `{key:"csv", home_ml, away_ml, home_spread: -spread_line, total: total_line}` |

### 2.9 `factors` {} — exactly these 19 keys
| key | type | meaning |
|---|---|---|
| team_elo_home, team_elo_away | float | pregame TEAM Elo (no QB) |
| elo_gap_home | float | team_elo_home - team_elo_away |
| home_rank, away_rank | int 1..32 | rank by current team Elo (ties broken by code) |
| hfa_elo | float | params.elo.hfa, or 0 when neutral |
| hfa_spread_pts | float | hfa_elo / points_per_elo |
| rest_home, rest_away | int or null | days |
| rest_diff | int or null | rest_home - rest_away; null if either is null |
| rest_elo | float | rest_per_day * rest_diff, 0 when null |
| qb_home, qb_away | {} or null | null iff `availability.starter_*` is false. `{id, name, q (Elo pts), starts (int, prior starts on record), first_start (== starts == 0), usual_starter_id (str or null), usual_starter_name (str or null), usual_starter_q (float or null; the usual starter's Q as of `league_date`, i.e. inactivity-decayed by `qb.effective_q` exactly like the listed starter's `q`), differs_from_usual (bool), source: "nflverse_listed"}` |
| qb_elo | float | q_home - q_away, treating a null side as 0 |
| form_home_3, form_away_3 | float or null | Elo change over the team's last 3 played games (fewer if fewer played; null if none) |
| d_total | float | elo_gap_home + hfa_elo + rest_elo + qb_elo (the d in win_prob) |
| model_market_gap_home | float or null | p_home_model - p_home_market |
| model_market_gap_pick | float or null | same gap from the pick's side |

### 2.10 `teams[]` — exactly 32, sorted by code, exactly these keys
`team`, `name`, `city`, `conference`, `division`, `full_name`, `aliases` (list of str — the page's
name map for the overlay; copy from teams.json), `elo` float (current), `rank` int (permutation of
1..32), `record` {w,l,t} (current season REG), `trend` list of `[week, elo_after_that_game]` pairs
for the current season's played games, `qb` {id, name, q} or null (the usual starter and its Q as of `build.league_date`, inactivity-decayed like 2.9),
`simulation` {`runs` int, `projected_wins` float, `playoffs`, `division`, `seed1`, `super_bowl` probs}.
Validation: playoffs sum to 7 +/- 0.05 per conference (tests use +/- 0.01), super_bowl sums to 1 +/- 1e-6.

### 2.11 `scorecard` {}
`league_date` (the slate shown: build.league_date if it has games else next_league_date),
`games_on_slate` int, `season` {season, n, hits, losses, pushes, accuracy or null, market_n,
market_accuracy or null} (== track_record.overall for the current season), `holdout` {season, n,
model_accuracy, market_accuracy, model_brier, market_brier}.

### 2.12 `track_record` {} — from the ledger reducer; EVERY aggregate carries `n`
- `overall`: {n, hits, losses, pushes, accuracy (hits/(hits+losses) or null when 0), market_n, market_hits, market_accuracy or null}. n = graded picks; market_* over the same games that had a market pick (p_market != 0.5 exactly; ties excluded).
- `by_tier`: list of 4 in TIERS order, each {tier, n, hits, losses, pushes, accuracy, market_n, market_hits, market_accuracy}.
- `by_week`: list ordered by (season, week), each {season, week, n, hits, losses, pushes, cum_n, cum_hits, cum_accuracy, market_n, market_hits, market_cum_n, market_cum_hits, market_cum_accuracy}. Cumulative within the season.
- `calibration`: {n, bins (exactly 10), error, error_n}. Bin i covers `[i/10, (i+1)/10)` (last bin closed at 1.0) on the FROZEN `p_home_blend`, target = home win, ties excluded. Each bin {lo, hi, n, mean_p or null, hit_rate or null, low_sample (n < 10)}. `error` = n-weighted mean of |mean_p - hit_rate| over bins with n >= 10, `error_n` their total n; error null when error_n is 0. Bin counts sum to n.
- `score_error`: {n, margin_mae, total_mae, market_n, market_margin_mae, market_total_mae}. Model: |pred margin - margin| and |pred total - total| using the DISPLAYED integers frozen in the snapshot (`pred_home - pred_away`, `pred_home + pred_away`; the 4.1 snapshot carries nothing unrounded, and D10 fixes its fields), so the D5 favorite nudge can bias a predicted margin by up to 1 point; market: CSV close `spread_line`/`total_line` (market_n = games with both).
- `clv`: {n, mean_pp, share_toward_pick}. `clv_pp` per grade (see 4.2); share = fraction with clv_pp > 0. Null means when n = 0.
- `units`: {all: {n, hits, losses, pushes, units, roi, mean_break_even}, ev_positive: {same, over snapshots with ev >= 0}, break_even_minus_110: 110/210}. 1 unit per pick at `best_price`: hit +payout, loss -1, push 0; roi = units/n; `mean_break_even` = mean of `1/(1+payout)` over the picks. Picks without a best_price are excluded (n counts only priced picks).

## 3. Overlay (Worker output and `market.normalize_odds_api` output) — D6

```
{ "schema_version": "1.0.0", "fetched_at": ISO UTC or null, "degraded": bool,
  "canonical": bool, "source": "worker" | "build",
  "games": [ { "home": str, "away": str, "commence_time": ISO UTC,
               "books": [ {"key": str, "home_ml": int|null, "away_ml": int|null,
                           "home_spread": float|null, "total": float|null} ] } ] }
```
- The Worker does NOT canonicalize (D6): it emits `canonical: false`, `source: "worker"`, and
  `home`/`away` are the upstream full names verbatim ("Jacksonville Jaguars"). Consumers map names
  to codes with the alias map (`schema.code_from_name` in Python; `teams[].full_name`+`aliases` from
  model.json in the page). `market.canonicalize_overlay(overlay)` returns a copy with codes and
  `canonical: true`, dropping any game whose names do not map.
- `degraded: true` = served stale-or-empty because the upstream reserve (< 60 credits) was hit or
  the upstream failed; the page shows "live prices unavailable" and keeps embedded data.
- Matching to `games[]` (`market.match_overlay`): require `canonical == true`; candidates = games with
  the same home AND away codes (orientation matters; reversed = no match); then require
  `abs(commence_time - kickoff_utc) <= 36h`; exactly one candidate -> match, zero or two+ -> no
  match (fail closed to embedded data). Finals are never matched.
- The page may use the overlay ONLY for: best price among the user's selected books, live EV at
  that price, and a "price moved" note — all under a LIVE label with `fetched_at` shown.

## 4. Ledger `picks.jsonl` (append-only; one JSON object per line; D10)

Every event has `"type"` and `"schema_version": "1.0.0"`. Nothing is ever edited or deleted.

### 4.1 snapshot
```
{ "type":"snapshot", "schema_version":"1.0.0",
  "id": game_id + "@" + build.id, "game_id", "kickoff_utc", "build_at" (= build.built_at),
  "pick", "p_model" (p of the pick side), "p_market" (pick side, or null), "p_blend" (pick side), "tier",
  "pred_home", "pred_away" (display ints), "best_price" (int or null), "best_book" (str or null), "ev" (float or null),
  "market_source" ("odds_api"|"csv"|null), "qb_home_id", "qb_away_id", "qb_home", "qb_away" (names; nullable),
  "consensus_p_pick" (market no-vig consensus for the pick side, or null) }
```
Write rule: a build appends a snapshot for a game only if there is no existing snapshot for that
game with `build_at < kickoff_utc` OR the OFFICIAL FIELDS (`pick, p_model, p_market, p_blend, tier,
pred_home, pred_away, best_price, best_book, ev, market_source, qb_home_id, qb_away_id,
consensus_p_pick`) differ from the latest such snapshot. Reruns add nothing. Snapshots are only
written for games with `status == "scheduled"` and `build_at < kickoff_utc`.

### 4.2 grade
```
{ "type":"grade", "schema_version":"1.0.0",
  "snapshot_id", "game_id", "graded_at" (ISO UTC), "result" ("home"|"away"|"tie"), "margin" (home - away),
  "home_score", "away_score", "hit" (true|false|null for tie),
  "close_p_pick" (no-vig prob of the pick side from CSV closing MLs, or null),
  "close_price_pick" (CSV closing ML of the pick side, or null),
  "clv_pp" (100 * (close_p_pick - consensus_p_pick), or null when either is null) }
```
Write rule: one grade per game, written by the first build after the game is final, for the
LATEST snapshot with `build_at < kickoff_utc` (latest by `(build_at, id)`). Games with no pre-kick
snapshot get no grade. Ties: push (hit null), excluded from accuracy and units, included in n of
`overall` via `pushes`.

## 5. Raw Odds API response (ground truth from DECISIONS; `tests/fixtures/odds_api_sample.json`)

`GET /v4/sports/americanfootball_nfl/odds?regions=us&markets=h2h,spreads,totals&oddsFormat=american`
-> JSON list of
```
{ "id": str, "sport_key": "americanfootball_nfl", "sport_title": "NFL",
  "commence_time": "2026-10-11T13:30:00Z", "home_team": "Jacksonville Jaguars", "away_team": "Philadelphia Eagles",
  "bookmakers": [ { "key": "draftkings", "title": "DraftKings", "last_update": ISO,
      "markets": [ { "key": "h2h", "last_update": ISO, "outcomes": [ {"name": <team full name>, "price": -375}, ... ] },
                   { "key": "spreads", "outcomes": [ {"name": <team>, "price": -110, "point": -7.5}, ... ] },
                   { "key": "totals",  "outcomes": [ {"name": "Over"|"Under", "price": -110, "point": 41.5}, ... ] } ] } ] }
```
Headers: `x-requests-remaining`, `x-requests-used`, `x-requests-last`. 3 credits per request.
Book keys seen live 2026-10-09: draftkings, fanduel, betmgm, betrivers, bovada, betus,
betonlineag, lowvig, mybookieag (williamhill_us absent that day but may reappear; keep it mapped).
Any market/outcome may be missing for a book; never assume all three markets exist.

## 6. Module boundaries and exported signatures (builders implement EXACTLY these)

All functions are pure unless named `read_*`/`write_*`/`fetch_*`/`render_*`. Arguments and
returns are plain dicts/lists/tuples/numbers.

### 6.1 `src/elo.py` (Elo + QB state; may delegate QB helpers to `src/qb.py` but this is the surface)
```
win_prob(d: float) -> float                       1/(1+10**(-d/400))
mov_multiplier(margin: int, d_fav: float, tie_multiplier: float) -> float
                                                 ln(m+1)*2.2/(0.001*d_fav+2.2); a tie (m == 0) returns tie_multiplier
rollover(rating: float, reversion: float) -> float   1500 + (r-1500)*(1-reversion)
qb_inactivity(q: float, days_since_last_start: int, penalty: float) -> float
                                                 q - penalty * (days // 365)   (completed years only; pure)
pregame_d(elo_home, elo_away, hfa_elo, rest_elo, q_home, q_away) -> float
replay(games: list[dict], params: dict) -> dict  # ReplayResult
```
`replay` processes PLAYED games in `sort_key` order with rollover applied exactly once before the
first game of each season (team Elo by `params["elo"]["reversion"]`; Q *= (1 - annual_reversion)),
then evaluates every UNPLAYED game in the input against the final state (rolling over first if
the unplayed game's season is later than the last played season). `params` is the `params` object
of section 2.3. ReplayResult:
```
{ "ratings": {code: float},                                 # team Elo after the last played game
  "qb": {qb_id: {"q": float, "name": str, "team": code, "starts": int, "last_start_date": "YYYY-MM-DD"}},
  "pregame": {game_id: {"p_home": float, "d": float, "elo_home": float, "elo_away": float,
                        "hfa_elo": float, "rest_elo": float, "q_home": float|None, "q_away": float|None,
                        "qb_home_starts": int|None, "qb_away_starts": int|None}},   # for EVERY input game
  "history": {code: [[season, week, game_id, elo_after], ...]},  # per played game, chronological
  "usual_starter": {code: {"id": str, "name": str} | None},  # most frequent starter id over the team's last 8 played games (ties -> most recent)
  "records": {code: {"w": int, "l": int, "t": int}}          # REG games of the latest season present
}
```
`p_home` for a played game is its PREGAME probability (recorded before the update). With
`params.qb.enabled == false` the q terms are 0 and `q_home/q_away` are null.

### 6.2 `src/market.py`
```
american_to_decimal_profit(american: int) -> float    +150 -> 1.5, -120 -> 0.8333...; ValueError if |x| < 100
implied_prob(american: int) -> float                  +150 -> 0.4, -120 -> 0.5454...
no_vig_pair(home_american: int, away_american: int) -> tuple[float, float]   (p_home, p_away), sum 1
break_even(payout: float) -> float                    1/(1+payout); -110 -> 0.5238...
estimated_ev(p: float, american: int) -> float        p*payout - (1-p)
consensus(books: list[dict]) -> dict                  {"p_home": mean no-vig over books with both MLs or None, "n_ml": int,
                                                       "total": median or None, "n_total": int,
                                                       "home_spread": median or None, "n_spread": int}
best_price(books: list[dict], side: "home"|"away", pick_code: str, book_keys: list[str]) -> dict|None
                                                      {"book","side": pick_code,"american"}; highest decimal profit; tie -> first in book_keys
prices_by_book(books, side, pick_code, book_keys) -> dict   {key: price|None} for every key in book_keys
market_from_books(books: list[dict], source: str, as_of: str) -> dict        section 2.8 object
market_from_csv(game: dict, as_of: str) -> dict|None                        section 2.8 with source "csv"; None if a CSV ML is missing
normalize_odds_api(payload: list, teams: list[dict]) -> dict                 overlay (section 3) with canonical: true, source: "build";
                                                      drops games whose names do not map and books in OFFSHORE_BOOKS
                                                      = {"bovada","betus","betonlineag","lowvig","mybookieag"}; spread = home outcome's point; total = Over point
canonicalize_overlay(overlay: dict) -> dict           copy with codes, canonical: true (names via schema.code_from_name)
match_overlay(games: list[dict], overlay: dict) -> dict   {game_id: overlay_game} per section 3 rules
```

### 6.3 `src/predict.py`
```
blend(p_model: float, p_market: float|None, w: float) -> float
expected_scores(total: float, model_spread: float) -> tuple[float, float]       (expected_home, expected_away)
display_scores(expected_home: float, expected_away: float, favorite_is_home: bool) -> tuple[int, int]
choose_total(market: dict|None, game: dict, params: dict) -> tuple[float, str]  (total_used, total_source) per D5
rationale(factors: dict, official_partial: dict, game: dict, names: dict) -> tuple[list[str], list[str]]
edge_chip(factors: dict, names: dict) -> dict|None
predict_game(game: dict, pregame: dict, market: dict|None, params: dict, context: dict) -> dict
```
`context` = `{"ranks": {code: int}, "names": {code: team_meta}, "form": {code: float|None},
"usual_starter": replay["usual_starter"], "qb": replay["qb"], "books": params["books"]}`.
`predict_game` returns `{"official": <2.6>, "availability": <2.5 availability>}` for one game.

### 6.4 `src/parlay.py` (Python oracle for web/app.js; D9)
```
fair_american(p: float) -> int
combo_stats(legs: list[dict]) -> dict             legs = [{"game_id","side","p","american"}];
                                                  {"hit","decimal_odds","payout","ev","fair_american"}
eligible_legs(games: list[dict], book: str, min_p: float = 0.55) -> list[dict]
                                                  from official fields: pick with p_pick_blend >= min_p and best_prices[book] != null
rank_parlays(legs_by_book: dict[str, list[dict]], min_legs: int = 2, max_legs: int = 5, top: int|None = None) -> list[dict]
```
Each candidate: `{"book", "n_legs", "game_ids": [...], "legs": [...], "hit", "decimal_odds",
"payout", "ev", "fair_american"}`, one book per candidate, no two legs from one game, sorted by
`ev` desc then `hit` desc then `book` then `game_ids`. `top` limits the list after sorting; the
page pins the best 2/3/4-leg itself.

### 6.5 `src/simulate.py` (D11)
```
simulate(teams: list[dict], remaining_games: list[dict], ratings: dict, params: dict, runs: int, seed: int) -> dict
```
`teams` = teams.json entries each extended with `"record": {w,l,t}`; `remaining_games` = unplayed
REG games (section 1); `ratings` = `replay["ratings"]`; probability per game = `win_prob(elo_home -
elo_away + hfa(0 if neutral) + rest_elo)` — no QB term. Returns
`{"runs", "seed", "teams": {code: {"projected_wins","playoffs","division","seed1","super_bowl"}}, "notes": [str]}`
with the `random.Random(seed)` stream; ratings are not mutated.

### 6.6 `src/ledger.py` (D10)
```
read_events(path: str) -> list[dict]
snapshot_event(game: dict, build: dict) -> dict|None          None for finals or build_at >= kickoff_utc
append_snapshots(events: list[dict], games: list[dict], build: dict) -> list[dict]   NEW events only (pure)
append_grades(events: list[dict], games: list[dict], graded_at: str) -> list[dict]   NEW events only (pure)
write_events(path: str, new_events: list[dict]) -> None        append lines; never rewrites the file
reduce(events: list[dict], games: list[dict]) -> dict          {"track_record": <2.12>, "scorecard_season": <2.11 season>, "grades": {game_id: grade}}
```
`games` in `reduce` supplies CSV close lines and tiers for score error / market comparisons; every
aggregate is computed from frozen snapshot values, never from live data.

### 6.7 `src/evaluate.py`, `src/tune.py`, `src/build.py`, `src/data.py`, `src/schema.py`
- `data.load_games(path)`, `data.fetch_games(url, cache_path, offline=False, timeout=60)`,
  `data.validate_csv(path, min_rows=0)`, `data.sha256_file(path)`, `data.kickoff_utc(gameday, gametime)`,
  `data.sort_key(game)`, `data.max_result_date(games)` — implemented (this file's author).
- `schema.validate_model(model) -> list[str]`, `schema.validate_overlay(overlay) -> list[str]`,
  `schema.tier_for(p, bounds=None)`, `schema.canonical_code(code)`, `schema.code_from_name(name)`,
  constants `TEAMS, TEAM_META, DIVISIONS, TIERS, TIER_BOUNDS, DEFAULT_BOOKS, ALIAS, NAME_TO_CODE,
  SCHEMA_VERSION, NFLVERSE_URL, EDGE_THRESHOLD, EV_POSITIVE_THRESHOLD, PARLAY_MIN_P` — implemented.
- `evaluate.holdout(games, params, season=2025) -> evaluation` (section 2.4) and
  `python -m src.evaluate --reference-check` (exit non-zero if 2025 model Brier is outside
  +/- 0.003 of `params.json["reference"]["brier"]`; `make test` runs it with GRIDIRON_OFFLINE=1).
- `tune.main()` writes params.json: `{...section 2.3 keys..., "reference": {"season": 2025, "brier": float, "logloss": float, "accuracy": float, "n": int}}`.
- `build.main()` orchestration only: fetch -> load -> replay -> one odds call (or csv) -> predict ->
  simulate -> ledger -> assemble -> `validate_model` (fail loudly on any problem) -> write model.json
  and index.html atomically (temp + os.replace). Exit non-zero on any failure; never write a partial file.
- Build uses `GRIDIRON_OFFLINE=1` (no fetch), `ODDS_API_KEY` (official odds call), nothing else.

## 7. Open points for other builders (not contract breaks)
- `teams[].qb` is the usual starter (D2 definition); if a team has no played games it is null.
- `form_*_3` counts only played games of the current replay (season-agnostic): the last 3 entries of `history[code]`.
- `evaluation.model_no_qb` requires a second replay with `qb.enabled=false` over the same rows.
