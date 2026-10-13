# Front end

One static page. `src/page_template.html` + `model.json` -> `index.html`, no build tooling,
no framework, no third-party JS. Everything the page shows comes from the embedded model;
nothing is hardcoded.

## Files

| File | What |
|---|---|
| `src/page_template.html` | The whole page: `<title>`, Google Fonts link, `<style>` (Eyrie tokens copied verbatim from `docs/eyrie-tokens.css` + page CSS), markup, `<script id="model" type="application/json">__MODEL_JSON__</script>`, `window.GRIDIRON_ODDS_URL = "__ODDS_URL__"`, and the page script (vanilla, sectioned, no `innerHTML`). |
| `src/render.py` | `render_page(template, model, odds_url)` / `write_index(model, out_path)`: serializes the model at full precision, escapes `</` -> `<\/` and U+2028/U+2029, fills the overlay URL from `GRIDIRON_ODDS_URL`, writes atomically. CLI: `python3 -m src.render model.json index.html`. |
| `tests/test_page.py` | unittest: escaping, round-trip, hostile strings, placeholders, token/a11y/copy requirements. |
| `tests/browser_check.mjs` | Playwright (`PLAYWRIGHT_MODULE=~/gryfin-eyrie/node_modules/playwright/index.mjs`): 400px and 1200px, no horizontal scroll, no console errors, tabs by click and arrow keys, Why?, the injuries block (keyboard expand, table in `.tablewrap`, hostile name as text, report states, stale eyebrow, dark-mode chip contrast >= 4.5), parlay panel vs the Python oracle fixture, dark mode. Screenshots to `docs/screens/`. `node tests/browser_check.mjs`. |

## How the page reads the model

`JSON.parse` of the embedded script, then four tabs (`role=tablist`, arrow keys, `aria-selected`,
hidden panels):

- **Today** (D7): the viewer's current date in `America/New_York` (via `Intl`), or the next league
  date with games, labelled "Next slate". The rest of that week sits under a `<details>`.
  `?date=YYYY-MM-DD` (or `window.GRIDIRON_TODAY`) overrides the viewer date for debugging and tests.
  Each card: away at home, kickoff in the viewer's zone with its abbreviation, pick, predicted score
  ("KC 27, DEN 20"), tier chip, model / market / blend, probability bar (cyan = blend for the
  pick, bracket = market), best price among the selected books (D16 defaults, persisted in
  `localStorage` under `gridiron.books`), "Model-estimated EV at this price" (positive styling
  only at >= +2c, D8), an Injuries block (D18.3, below), amber dashed Edge chip (D17), Why?
  disclosure with the official rationale, and for finals the score plus HIT / MISS / PUSH as text
  with the margin and total miss.
- **Injuries** (D18.3, `official.injuries`, CONTRACT 2.6b): under the spread line, one summary line
  per team, home then away: "DEN · 3 starters Out (WR Sutton, LT Bolles, CB Moss) · 1 Doubtful ·
  2 Questionable" (the counts are the report's `starters_*`, which exclude the quarterback; the
  names are the starters listed Out, `position` + surname). "no report filed yet" when
  `report_filed` is false, "injury reports unavailable this build" when `available` is false; a
  listed starting QB adds "QB {status} (covered by the QB term)". The eyebrow says "shown for
  context, not in the number yet" while `injuries.enabled` is false, or the signed Elo term toward
  the home side when true. "Full report (n)" (`aria-expanded` / `aria-controls`) reveals a compact
  table inside `.tablewrap`: team, status chip (Out = danger wash, Doubtful = hyp wash,
  Questionable / Probable = line; the word is always there), position, player (the QB row carries
  "(covered by the QB term)"), "Starter" mark as text, injury. Every string is a feed value and is
  rendered with `textContent` only. The eyebrow "latest report; the pick was frozen earlier" appears
  when `injuries.as_of_snapshot` is `false`, or, while the feed carries no such key, when the game's
  `frozen_snapshot` is set (CONTRACT 2.6a: the report is rebuilt every build while the pick is frozen).
- **Build a parlay** (D9): computed in the browser from `official` fields only, per selected book:
  legs = scheduled picks with `p_pick_blend >= 0.55` priced at that book; every 2-5 leg combination;
  hit = product of p, payout = product of decimal odds - 1, EV = hit * payout - (1 - hit), fair
  American from 1/hit; sorted by EV desc, hit desc, book, game_ids (same order as `src/parlay.py`).
  Best 2/3/4-leg pinned, then the top 5; rows expand to legs with checkboxes that rebuild; scope
  (slate / week) and sort (EV / hit %) toggles; mandated copy always visible.
- **Live overlay** (D6): if `GRIDIRON_ODDS_URL` is set at build time the page fetches it once with
  a 5 s timeout, validates the shape, maps full names through `teams[].full_name` + `aliases`,
  matches canonical codes + a 36 h window (exactly one candidate, finals excluded) and adds a LIVE
  price line + live EV + "price moved" per card and a "prices as of" note. Any failure -> "Lines as
  of build time"; the official pick, probabilities, tier and rationale never change.
- **Rankings**: Elo, change over the recorded trend, sparkline, record, projected wins,
  playoff / division / 1-seed / Super Bowl odds, All / AFC / NFC filter, table inside `.tablewrap`.
- **Track record**: every aggregate from `model.track_record` with its `n`; cumulative by-week line
  chart (model solid, market dashed) and calibration chart (hollow dots for `low_sample`) as inline
  SVG with data tables beside them; empty state when `overall.n` is 0. "By injury load" renders
  `track_record.by_injury_load` (rows with `n`, W-L, accuracy, market accuracy) when the reducer
  provides it and an empty-state line when it does not.
- **How it works**: prose that reads parameter values from `model.params` and the holdout numbers
  from `model.evaluation`; "The injury term" reads `params.injury` (enabled flag, the gate's
  Brier-improvement threshold and both holdout Briers or "not yet measured", the per-group weights
  and status multipliers) and states the three caveats (listed status is not severity, starters are
  the depth chart at build time so "no report filed yet" is not "nobody hurt", the QB is never
  counted here).

## Design (D13)

Eyrie "Signal Daylight": white paper with the dot-grid canvas, ink text, JetBrains Mono for every
label / number / button / eyebrow, Archivo display headings, Inter body. Cyan fills carry ink text;
`--cyan-ink` when cyan is text. Amber `--hyp` dashed chips for edges, `--signal` purple for the
parlay panel, `--qualified` for HIT and `--danger` for MISS, always with the word. Light and dark
(both guarded blocks), a Dark mode toggle (`data-theme`), reduced motion respected, visible focus,
no body horizontal scroll at 400px.

## Contract notes

- "Change vs preseason" in Rankings: `teams[]` carries no preseason Elo and `validate_model`
  rejects extra keys, so the column shows the change over the recorded `trend` (current Elo minus
  the Elo after the first recorded game) and is labelled that way.
- `official.estimated_ev_per_dollar` is for the official `best_price`; when the user's book filter
  changes the best price, the card recomputes EV with the same formula from `p_pick_blend`.
- The 2.6b player object carries no practice-status field, so the injuries table has no practice
  column; `injuries` has `as_of` but no `as_of_snapshot`, so the stale eyebrow falls back to
  `games[].frozen_snapshot` (see above). `track_record.by_injury_load` is not in the contract yet;
  the page renders it when present (any row shape with `n`, `hits`, `losses`, `accuracy`,
  `market_accuracy` and a `load`/`label` name) and otherwise says so.
