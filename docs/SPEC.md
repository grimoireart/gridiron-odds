# Gridiron Odds Build Spec (Lauren C, 2026-10-09)

Build a daily NFL cross-check for recreational betting: for every game, a predicted winner, predicted score and confidence, with a button that explains the reasoning and a button that builds the best parlay from the day's slate. Under the hood: an Elo model with a quarterback adjustment, a live odds feed from the major sportsbooks, and a blended model-plus-market probability. The prototype (model, tuning, page) already exists and is the reference for every number below.

## Scope
v1 is a page you open on game day. It answers, for each game: who wins, by what score, how sure the model is, why, and which combination of today's picks is the best parlay. It runs from one command plus one API key.

In scope:
- Elo ratings with a quarterback adjustment, updated after every game since 1999
- Live moneylines, spreads and totals from the major US books, refreshed when the page loads
- Per game: predicted winner, predicted score, confidence tier, model vs market gap, best available price and which book has it
- A Why? button per game with a plain-language rationale built from the model's own inputs
- A Build a parlay button that ranks the day's parlays by expected value
- Expected value per bet after vig, so the page shows most bets as slightly negative and a few worth a look
- Power rankings and season simulation on a second tab
- A holdout evaluation report (model vs Vegas) that runs with every build

Out of scope for v1: injuries other than the starting quarterback, weather, player props; in-game win probability; placing bets, tracking a bankroll, or logging results; any claim of a long-run edge. The page's copy says what it is: a cross-check that steers toward the best price and away from bad spots.

## Architecture
Everything is a batch job. The page is static.

### Data
One file: the nflverse schedules dataset (games.csv, public, no auth), downloaded fresh on every build. ~7,500 games, 1999 to end of current season (future games have empty scores).
Fields used: game_id season game_type week gameday gametime home_team away_team home_score away_score location home_rest away_rest home_moneyline away_moneyline spread_line total_line home_qb_name away_qb_name.
Team aliases: STL->LA, SD->LAC, OAK->LV. Played game = home_score non-empty. Neutral site = location == "Neutral" (no home-field term).
Cache the download to data/games.csv; fail the build loudly if the fetch fails or the row count drops below the cached copy. Never commit the CSV.

### Elo model
FiveThirtyEight-style Elo with home field, rest, margin of victory, preseason regression. Every team starts at 1500. Process games in gameday order.
d = R_home - R_away + HFA*[not neutral] + REST*(rest_home - rest_away)
p = 1 / (1 + 10^(-d/400))
Update with result s (1 home win, 0 away win, 0.5 tie) and margin m:
delta = K * (ln(m+1)*2.2 / (0.001*d_fav + 2.2)) * (s - p), d_fav = d from the winner's side. Add delta to home, subtract from away.
Season rollover: R = 1500 + (R - 1500)*(1 - REV).
Tuned (grid search 2010-2024, Brier): K=20 (16,20,24,28); HFA=40 (30,40,48,55,65); REV=0.40 (0.25,0.33,0.40); REST=2 (0,1,2). Keep as defaults in params.json.
Reference 2025 holdout (285 games): Elo acc 64.4% Brier 0.2209 logloss 0.634; Vegas closing ML vig-removed acc 66.2% Brier 0.2104 logloss 0.607. A rebuild must reproduce within +/-0.003 Brier.
Vig removal: implied prob per side, home / sum. For played games the page shows the pre-game probability (record p before update).

### Quarterback adjustment
Each QB carries Q in Elo points (value over an average starter):
d = (R_home + Q_homeQB) - (R_away + Q_awayQB) + HFA + REST*(rest diff)
Starter names from home_qb_name/away_qb_name in the games file. No second data source.
New QB starts at -50. After each game: Q += KQ * mult * (s - p), same mult and (s-p) as the team update, KQ tuned (grid 4,6,8,12). Between seasons regress Q 20% toward 0. A QB who has not started in 365 days decays 25 points.
Tune KQ on 2010-2024 with the other four held; keep QB on only if 2025 holdout Brier <= no-QB Brier (0.2209). Record both in params.json.
When a team's announced starter for the upcoming game differs from its last game, the Why? rationale says so and the edge chip names it as the likeliest reason for a model-market gap.

### Predicted score and confidence
model_spread = d / 25 (positive = home favored).
Score: total from the market when available, else team season averages. home_pts = (total + model_spread)/2, away_pts = (total - model_spread)/2, rounded; if rounding ties, nudge the favorite up one. Display "KC 27, DEN 20".
Blended probability (headline): p_blend = w*p_model + (1-w)*p_market, w=0.35 default; fit w on the 2025 holdout against closing lines, store in params.json. No market line -> p_blend = p_model, card says so.
Tiers from the favorite's p_blend: Toss-up <55%; Lean 55-65%; Strong 65-78%; Lock-ish >=78%. Scorecard tracks pick accuracy by tier so tiers can be recalibrated in week 10.

### Odds feed
The Odds API free tier (500 requests/month). Endpoint /v4/sports/americanfootball_nfl/odds, markets=h2h,spreads,totals, regions=us, oddsFormat=american.
Proxy: Cloudflare Worker holds the key, caches JSON 10 min, returns with CORS. The page never sees the key. If the Worker is unreachable the page falls back to model.json lines (from CSV) with a "lines as of build time" notice.
Per game from the feed: consensus market probability (vig-free, averaged across books); best price (highest ML for the predicted winner across the user's selected books, and which book); market spread and total (consensus median).
EV per $1 at best price: EV = p_blend*payout - (1 - p_blend), payout = decimal profit (+150 -> 1.5, -120 -> 0.833). Show as cents per dollar.
Match feed games to CSV games by kickoff date + home/away team with a name map.

### Why? button
Rationale generated at build time in predict.py from the model's inputs; 3-5 sentences, factors in order of size:
- Rating gap (always): "Kansas City rates 72 Elo points above Denver, 4th in the league against 19th."
- Home field (not neutral): "Home field is worth about 40 points, or 1.5 points on the spread."
- Quarterback (either starter new or Q differs by 30+): "Denver's listed starter, Jarrett Stidham, rates 48 points below their usual starter."
- Rest (diff 3+ days): "Kansas City is coming off a bye; Denver played Monday."
- Form (Elo moved 40+ in last 3 games): "Denver has lost 55 points of rating over its last three games."
- Market (model and market differ by 5+ pts): "The books have this at 71%, closer than the model's 78%; the gap is most likely injury news the model does not see."
- Verdict (always): "Blended, that comes to 74%: a Strong pick, predicted 27 to 20."
Rules: every number in the text must exist in model.json; no adjectives about teams; no mention of an absent factor; the market sentence attributes the gap to information the model lacks, never to an edge.

### Parlay builder
Today's games (or the week's, on a toggle), moneyline picks only. Legs independent; same-game legs excluded. For every combination of 2-5 legs from favorites with p_blend >= 0.55: hit = product of p_blend; payout = product of decimal odds at best price, minus 1; fair odds = 1/hit in American; EV = hit*payout - (1-hit). Rank by EV, top five, hit probability beside each; best 2-, 3-, 4-leg pinned at top. Rows expand to legs; user can toggle legs and rebuild. ~5,000 combos for 15 games, computed in the browser.
Copy, always visible: "Parlays multiply the house edge. Most of these have negative expected value; the ranking shows which lose least."

### Track record
Every pick frozen when made, graded when the game ends. picks.jsonl committed by the daily build, one line per game per build:
game_id, build_at; pick, p_model, p_market, p_blend, tier; pred_home, pred_away; best_price, best_book, ev; qb_home, qb_away; result, margin, hit, close_price (filled by the first build after the game).
A game can appear more than once if the pick changed; grade the last pick before kickoff, keep earlier lines.
Track record tab (computed from the log at build time): season record with the market's accuracy beside it; by tier; by week (cumulative accuracy line chart, model vs market); calibration (10 bins, hollow + excluded if <10 games); score accuracy (MAE of margin and total vs market); closing-line value (how often the price moved toward the pick, average move in cents); units (1 unit per pick at best price, all picks and EV-positive only, with the vig-adjusted break-even rate).
Scorecard on the main page shows season record and model-vs-market accuracy; each finished game's card shows whether the pick hit and by how many points the score missed.

### Season simulation
Secondary tab. Simulate remaining regular season 10,000 times, fixed seed, current Elo frozen.
1. Draw each unplayed game from its win probability; add to real wins.
2. Per conference: 4 division winners then 3 wild cards by record; ties by Elo + small jitter (approximation, noted on page).
3. Seeds 1-4 division winners by record, 5-7 wild cards.
4. Bracket 2v7, 3v6, 4v5; 1 rests. Reseed, two divisional games, conference final. HFA to higher seed. Super Bowl neutral.
5. Count per team: playoffs, division, 1 seed, Super Bowl, total wins.
Output per team: proj_wins, playoff, div_win, seed1, sb. Must finish under 5 s in pure Python; NumPy only if it does not.

### Front end
One static index.html with model.json inlined at build. Odds fetched on load from the proxy. Works at 400px wide, light and dark. Default view = today's games, rest of the week collapsed below.
- Game card: away at home, kickoff, predicted winner, predicted score, tier, model % vs market %, best price + book; finished games show final score and whether the pick hit.
- Why? button: expands 3-5 sentence rationale.
- Build a parlay button: panel ranking today's parlays by EV with combined odds, hit probability, fair payout; toggle legs.
- Edge chips: gold chip when model and market disagree by 8+ pts, with one line on the likeliest reason (QB change, rest, model lag after a blowout).
- Scorecard: today's game count, season pick record, holdout accuracy vs Vegas. From JSON, never hardcoded.
- Rankings tab: Elo, trend sparkline, record, playoff/div/1 seed/SB odds; All/AFC/NFC filter.
- How it works tab; Track record tab.
Design: condensed display face, mono for data, one accent. (OVERRIDDEN by Lauren 2026-10-09: use the Eyrie look - white background, techy mono text, brighter colors.)

### Repo layout and commands
gridiron-odds/ data/games.csv (gitignored); src/elo.py, tune.py, simulate.py, predict.py, parlay.py, build.py, page_template.html; proxy/odds.js (Cloudflare Worker); params.json; model.json; index.html; tests/; .github/workflows/daily.yml
make tune (grid search -> params.json, prints holdout table); make build (<30 s); make test (unit + reference check); make serve.
Python 3.11+, stdlib only for the model. Worker ~40 lines, ODDS_API_KEY secret. GitHub Pages + Actions cron 10:00 UTC daily in season.

### Acceptance tests
- win_prob(0)==0.5, win_prob(400)~0.909, symmetric
- ml_prob(-150,+130) home prob in 0.40-0.45, sides sum to 1
- one-game fixture, 10-pt home win moves home up and away down by the same amount
- rollover REV=0.4 takes 1600 to 1560
- aliases: STL feeds LA; OAK feeds LV
- QB: swapping a starter for a backup 60 pts lower lowers win prob; holdout Brier with QB <= without
- holdout: 2025 Brier within +/-0.003 of params.json value
- predicted score: Elo gap 0, neutral, total 44 -> 22-22; predicted margin == model spread
- blend: equal weights, model 60% market 50% -> 55%
- EV: +100 at 50% -> 0; -110 at 50% negative
- parlay: two independent 60% legs -> hit 0.36, fair odds +178
- rationale: every game's text names the winner, Elo gap, home field, QB or rest when present; no number not in model.json
- proxy: cached within 10 min, never includes key, page degrades to CSV lines with notice
- sim: playoff odds sum to 7.0 per conference; SB odds sum to 1.0
- page renders at 400px with no horizontal body scroll
- pick log: never modifies pick fields once written; grading only fills result fields
- pick log: a game with two picks graded on the last before kickoff
- track record: fixture of 10 picks, 6 hits at -110 shows 6-4, 60%, negative units; break-even 52.4%
- calibration: bins <10 games hollow and excluded

### Later (priority order): feature model (EPA etc.); full injury report; AI rationale; log real bets.
### Open questions: which books; Gryfin org or personal repo; rank parlays by EV or hit prob (default EV); real NFL tiebreakers or approximation.
