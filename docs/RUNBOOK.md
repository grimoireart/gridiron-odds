# Gridiron Odds — runbook

Operating notes for the daily build, the ledger, the Worker, and what to do when something breaks.
Design rules live in `DECISIONS.md`; the data shapes in `CONTRACT.md`.

## Daily cycle

| when (UTC) | what |
|---|---|
| 10:00 daily, Sep-Feb | `.github/workflows/daily.yml`: `make build` -> `make test` -> commit `model.json index.html picks.jsonl params.json` -> Pages deploy |
| any time | *Actions > daily build > Run workflow* for a manual build (same steps; `concurrency: gridiron-build` queues it behind a running one) |

One build = one official snapshot. Thursday, Sunday and Monday games are frozen at different
horizons (the 10:00 UTC build before each); the page labels the snapshot age. The ledger grades
whichever pre-kick snapshot was latest.

## Local build

```
GRIDIRON_OFFLINE=1 make build      # cached CSV, no network, ~0.5 s
make build                         # downloads games.csv first (needs network)
ODDS_API_KEY=... make build        # plus ONE Odds API call (3 credits)
make serve                         # http://localhost:8787/index.html
```

The build prints one line: `build <id>: 272 games (65 final), odds=csv, +207 snapshots +0 grades
(207 events), 0.4 s`. A second identical run prints `+0 snapshots +0 grades`.

`?date=YYYY-MM-DD` on the page URL pins the viewer date (useful to look at a future slate).

## Files the build owns

- `model.json` — the whole product state; validated by `schema.validate_model` before writing.
- `index.html` — `src/page_template.html` with model.json inlined; self-contained.
- `picks.jsonl` — APPEND-ONLY event ledger. Never edit, reorder or rewrite lines. A material
  correction is a new event, never an edit. If it is ever corrupted, the build fails with the line
  number (`read_events`): restore it from git, do not "fix" the line.
- `params.json` — written only by `make tune`; the build reads it. Re-tune deliberately (see below).
- `data/games.csv` — cache, gitignored. Safe to delete; the next online build re-downloads it.
- `data/injuries_{season}.csv`, `data/depth_charts_{season}.csv` — nflverse injury report + depth chart caches (D18), gitignored. The build refreshes the current season's pair each run (offline: used as-is); `make tune` keeps 2010-2025 for the injury-term fit. Safe to delete; a missing pair never fails the build — every card shows "injury reports unavailable this build" and `build.injuries.available` is false (with the term enabled that is a loud WARNING, see below). In Actions the pair is carried between runs by an `actions/cache` step in `daily.yml` (key `nflverse-injuries-<run id>`, restore-keys by prefix: every run saves its fresh files, the next run restores the newest earlier ones), so a nflverse outage in CI falls back to the last good files exactly as a local build does; a cache miss AND a download failure in the same run is the only way CI builds degraded. `make test` runs after `make build` in CI on the freshly downloaded files: `tests/test_injuries.py::RealDataSmokeTests` keeps only structural checks there (`GITHUB_ACTIONS` set) so a depth-chart quirk cannot block the publish; the tight bounds run locally.

## Failure modes and what they mean

| symptom | cause | action |
|---|---|---|
| `build failed: download of ... failed` | nflverse unreachable | the cache is untouched; rerun later, or `GRIDIRON_OFFLINE=1 make build` to republish from the cache |
| `downloaded games.csv failed validation (cache kept)` | nflverse published a broken/short file | cache kept; wait for upstream; inspect with `python3 -c "from src import data; print(data.validate_csv('data/games.csv'))"` |
| `model.json failed validation with N problem(s); nothing written` | a contract break between modules | the problems are printed; nothing was written; fix the module and rerun |
| `odds api: call failed ... falling back to CSV lines` (stderr) | key missing/invalid, quota, network | the build still succeeds with `odds_source: "csv"`; check the key and the credit headers logged on the previous successful call |
| `reference brier ... FAIL` in `make test` | the Elo replay drifted from `params.json["reference"]` by more than 0.003 | either the data changed materially (new corrections upstream) or someone changed the math; investigate before re-tuning |
| workflow fails at `git push` | the branch moved under us | the job fails loudly on purpose; rerun the workflow (it rebases before pushing) |
| `injuries: injuries_2026.csv, depth_charts_2026.csv unavailable; every report degrades to available=false` (stderr) | nflverse injuries/depth-chart release unreachable and no cache | the build still succeeds; cards say "injury reports unavailable this build"; the injury term is 0 for every game that build; rerun later |
| `injuries: WARNING the injury term is enabled but the season files are unavailable; ... N snapshot(s) were written without the reports` (stderr + build log) | the line above while `params.injury.enabled` is true | NOT cosmetic: every unfrozen model probability moved (term 0) and those N snapshots are picks made without the reports. They are superseded by the next build once the feed returns (the ledger's write rule fires on the changed `p_model`; the latest pre-kick snapshot is the graded one), and each carries `inj_home.available = false` so the track record buckets them as "unknown". In Actions this needs BOTH the download and the cache restore (below) to fail; locally, `GRIDIRON_OFFLINE=1` with no `data/` pair does it — put the files back (an online build, or copy from another checkout) and rerun |
| cards say "no report filed yet" for the next slate | the league has not published that week's report (Wednesday-ish) or it is a bye | expected; `build.injuries.teams_with_report_for_next_slate` counts the teams that have filed |
| page shows "Lines as of build time" | overlay off, Worker unreachable, CORS denied, degraded, or no game matched | expected whenever the Worker is not deployed; otherwise check the Worker (below) |

## The ledger (track record)

- A snapshot is appended for a scheduled game only when its official fields differ from the game's
  latest pre-kick snapshot, so reruns add nothing. A changed starter, line or price adds a line; the
  earlier line stays.
- A grade is appended once per game by the first build after it is final, against the latest
  snapshot with `build_at < kickoff_utc`. Games that were never snapshotted before kickoff get no
  grade and do not count.
- Ties are pushes (hit null), excluded from accuracy and units. Every aggregate carries `n`.
- `track_record.units` is a model pick ledger at the frozen best price, not a bankroll.

## Re-tuning (`make tune`)

Runs the Elo grid (2010-2024 Brier), the KQ grid, the QB gate (2025 holdout), the blend-weight fit
(2015-2024 against closing lines), the league-average total, and the 2025 holdout; writes
`params.json` with a new `reference`. Takes a few seconds. Do it when the model code changes or at
the start of a season; commit `params.json` with the reason. The blend weight the fit returns is
what the build uses (D3) — currently 0.05, i.e. the market dominates; a documented fixed weight is
a product decision, not a code change (set `blend.model_weight` by hand and say so).

## Odds API credits

Billing: credits = markets x regions per request; h2h+spreads+totals in `us` = 3 credits. Free
tier 500/month -> ~166 requests. The daily build uses 1 request (3 credits). The Worker serves one
cached copy to everyone for 15 minutes PER Cloudflare data center (the cache does not replicate),
so page loads cost at most 4 requests/hour per region at peak and nothing when nobody is looking;
each data center stops calling upstream until the first of the next month once it sees fewer
than 60 remaining credits and serves `degraded: true` (see proxy/README.md "Budget"). The remaining-credit header is logged to stderr by the build (visible in the
Action log) and never written into model.json.

## Worker (`proxy/`)

```
cd proxy
npx wrangler secret put ODDS_API_KEY
# set ALLOWED_ORIGINS in wrangler.toml to the Pages origin (comma list; exact match)
npx wrangler deploy
node test_odds.mjs            # offline tests against a fake upstream
curl -H "Origin: https://<user>.github.io" https://<worker>.workers.dev/   # 403 without an Origin header
```

Then set the repository variable `GRIDIRON_ODDS_URL` to the Worker URL and rebuild. The page
validates the overlay shape, maps names to codes, matches by canonical teams within 36 h, and
only ever changes best price / live EV / "price moved" under a LIVE label.

## Checks after a change

```
make test                     # 300+ unit tests + reference check
GRIDIRON_OFFLINE=1 make build # must finish well under 30 s and print +0/+0 on a rerun
node tests/browser_check.mjs  # page smoke test at 400 px and 1200 px (Playwright: `npm install playwright`
                              # in the repo root, or PLAYWRIGHT_MODULE=/path/to/node_modules/playwright/index.mjs;
                              # exits 2 with a message when it is not installed)
cd proxy && node test_odds.mjs
```

## Failure alerts (added 2026-10-23)

The `alert` job in `.github/workflows/daily.yml` runs after every build. On failure it opens (or updates) a GitHub issue labelled `build-failure`, assigned to the repository owner, with the run link; the next successful run comments "Recovered" and closes it. An assigned issue emails the assignee through GitHub's normal notification mail, which goes to the address and settings at https://github.com/settings/notifications (the "Participating" email toggle must be on; the "Actions" toggle is separate and only covers workflow-failure mail). Test the path any time with `gh workflow run daily.yml -f simulate_failure=true` (costs no Odds API credits: it fails before the fetch) and a normal run afterwards to close the issue. Verified 2026-10-23: issue #1 opened and auto-closed.
