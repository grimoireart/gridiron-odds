// Playwright smoke check for the rendered page (D13 acceptance).
// Run:  node tests/browser_check.mjs
// Playwright is resolved from (in order) $PLAYWRIGHT_MODULE, ./node_modules/playwright (an
// ordinary `npm install playwright` in the repo), or a bare `import('playwright')`; when none
// resolves the script prints how to install it and exits 2 (not a failure, not a pass).
// Builds index.html from tests/fixtures/model_fixture.json via src/render.py, then at
// 400px and 1200px checks: standards mode + charset + viewport, no body horizontal scroll, no
// console errors, every tab fully inside the strip, tabs switch (click + arrow keys), Why?
// expands, the parlay panel opens, the page's parlay engine reproduces
// tests/fixtures/parlay_fixture.json (the src/parlay.py oracle) candidate for candidate, the
// live overlay works and degrades, the empty track record shows its copy, dark mode paints the
// dark token background and the theme button follows the system scheme, a keyed slate with one
// CSV-fallback game still builds real-book parlays, and a keyless (csv) build shows nflverse
// copy with no dead book toggles. Screenshots land in docs/screens/.
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

async function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, path.join(ROOT, 'node_modules', 'playwright', 'index.mjs')].filter(Boolean);
  for (const c of candidates) {
    if (existsSync(c)) return import(pathToFileURL(c).href);
  }
  try { return await import('playwright'); } catch (e) { /* fall through */ }
  console.log('SKIP browser check: Playwright not found. Install it with `npm install playwright` in the repo root '
    + '(or set PLAYWRIGHT_MODULE=/path/to/node_modules/playwright/index.mjs), then run `npx playwright install chromium`.');
  process.exit(2);
}
const { chromium } = await loadPlaywright();

const SCREENS = path.join(ROOT, 'docs', 'screens');
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'model_fixture.json');
const PARLAY_FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'parlay_fixture.json');
const OUT_DIR = process.env.GRIDIRON_CHECK_DIR || tmpdir();
const OUT = path.join(OUT_DIR, 'gridiron-check-index.html');
const DATE = '2026-10-11'; // the fixture's Sunday slate (5 scheduled games); ?date= pins the viewer date
mkdirSync(SCREENS, { recursive: true });
execFileSync('python3', ['-m', 'src.render', FIXTURE, OUT], { cwd: ROOT, stdio: 'inherit' });
const url = pathToFileURL(OUT).href + '?date=' + DATE;

let failures = 0;
const ok = (cond, msg) => { console.log((cond ? 'PASS ' : 'FAIL ') + msg); if (!cond) failures++; };
const loadFixture = () => JSON.parse(readFileSync(FIXTURE, 'utf8'));
/** Render a mutated fixture to its own file and return its ?date= URL. */
function renderVariant(name, model, env) {
  const fx = path.join(OUT_DIR, 'gridiron-check-' + name + '.json'), out = path.join(OUT_DIR, 'gridiron-check-' + name + '.html');
  writeFileSync(fx, JSON.stringify(model));
  execFileSync('python3', ['-m', 'src.render', fx, out], { cwd: ROOT, env: { ...process.env, ...(env || {}) }, stdio: 'inherit' });
  return pathToFileURL(out).href + '?date=' + DATE;
}
/** Turn one game's official market into a CSV-line fallback (the keyed build's per-game fallback, or a keyless build). */
function toCsvMarket(g) {
  const off = g.official, pickHome = off.pick === g.home;
  const b = off.market && off.market.books.find((x) => x.home_ml != null && x.away_ml != null);
  if (!b) return false;
  off.market = { source: 'csv', as_of: off.market.as_of, consensus_p_home: off.market.consensus_p_home, consensus_total: off.market.consensus_total,
    consensus_home_spread: off.market.consensus_home_spread, n_books: 1, books: [{ key: 'csv', home_ml: b.home_ml, away_ml: b.away_ml, home_spread: b.home_spread, total: b.total }] };
  for (const k of Object.keys(off.best_prices)) off.best_prices[k] = null;
  off.best_price = { book: 'csv', side: off.pick, american: pickHome ? b.home_ml : b.away_ml };
  return true;
}
const choose = (n, k) => { let r = 1; for (let i = 1; i <= k; i++) r = r * (n - k + i) / i; return Math.round(r); };

const browser = await chromium.launch();
try {
  for (const width of [400, 1200]) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: 'light' });
    const page = await ctx.newPage();
    const errors = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(url);
    await page.waitForSelector('#slate article.game');

    const doc = await page.evaluate(() => ({ mode: document.compatMode, charset: document.characterSet, viewport: !!document.querySelector('meta[name="viewport"]'), lang: document.documentElement.lang }));
    ok(doc.mode === 'CSS1Compat' && doc.charset === 'UTF-8' && doc.viewport && doc.lang === 'en', `${width}px: standards mode, UTF-8, viewport meta, lang (${JSON.stringify(doc)})`);

    const widths = await page.evaluate(() => ({ sw: document.body.scrollWidth, dw: document.documentElement.scrollWidth, iw: window.innerWidth }));
    ok(widths.sw <= widths.iw && widths.dw <= widths.iw, `${width}px: no horizontal scroll (body ${widths.sw}, doc ${widths.dw}, inner ${widths.iw})`);

    const tabs = await page.evaluate(() => {
      const strip = document.getElementById('tabs').getBoundingClientRect();
      return Array.from(document.querySelectorAll('[role="tab"]')).map((t) => { const r = t.getBoundingClientRect(); return { text: t.textContent, inside: r.left >= strip.left - 0.5 && r.right <= strip.right + 0.5 && r.width > 0 }; });
    });
    ok(tabs.length === 4 && tabs.every((t) => t.inside), `${width}px: all four tabs fully inside the strip (${tabs.map((t) => t.text + (t.inside ? '' : ' CLIPPED')).join(', ')})`);

    const slateCount = await page.locator('#slate article.game').count();
    ok(slateCount === 5, `${width}px: Sunday slate shows 5 games (got ${slateCount})`);
    ok((await page.locator('#slate-label').textContent()).includes('Today'), `${width}px: slate labelled as today for ?date=${DATE}`);
    ok(await page.locator('#scorecard > div').count() >= 4, `${width}px: scorecard strip rendered from model`);
    ok((await page.locator('#books input[type="checkbox"]').count()) === 4, `${width}px: keyed build shows the four book toggles`);
    ok((await page.locator('#slate .chip.edge').allTextContents()).every((t) => /^Model − market [+−]\d+ pts$/.test(t)), `${width}px: amber chip reads "Model − market ±N pts", never "Edge"`);

    // Why? expands the rationale.
    const why = page.locator('#slate article.game').first().locator('button[aria-controls^="why-"]');
    await why.click();
    const expanded = await why.getAttribute('aria-expanded');
    const sentences = await page.locator('#slate article.game').first().locator('ol li').count();
    ok(expanded === 'true' && sentences >= 3, `${width}px: Why? expands (aria-expanded=${expanded}, ${sentences} sentences)`);
    await page.screenshot({ path: path.join(SCREENS, `today-${width}.png`), fullPage: true });

    // Parlay panel: opens, mandated copy visible, first row matches the oracle when present.
    await page.click('#parlay-btn');
    await page.waitForSelector('#parlay:not([hidden])');
    ok(await page.locator('#parlay .copy').isVisible(), `${width}px: parlay copy visible`);
    let rows = await page.locator('#parlay-rows .prow').count();
    ok(rows > 0, `${width}px: parlay rows rendered (${rows})`);
    const firstRow = page.locator('#parlay-rows .prow > button').first();
    const lbl = await firstRow.locator('.lbl').textContent();
    ok(/^Best \d-leg · [A-Za-z]/.test(lbl), `${width}px: pinned label does not repeat the leg count (${lbl})`);
    await firstRow.click();
    ok((await firstRow.getAttribute('aria-expanded')) === 'true', `${width}px: parlay row expands to legs`);
    await page.screenshot({ path: path.join(SCREENS, `parlay-${width}.png`), fullPage: true });

    // Tabs: click and arrow keys.
    await page.click('#tab-rankings');
    ok(await page.locator('#panel-rankings').isVisible() && !(await page.locator('#panel-today').isVisible()), `${width}px: Rankings tab switches by click`);
    ok((await page.locator('#rank-rows tr').count()) === 32, `${width}px: rankings table has 32 rows`);
    await page.screenshot({ path: path.join(SCREENS, `rankings-${width}.png`), fullPage: true });
    await page.focus('#tab-rankings');
    await page.keyboard.press('ArrowRight');
    ok(await page.locator('#panel-record').isVisible() && (await page.evaluate(() => document.activeElement.id)) === 'tab-record', `${width}px: ArrowRight moves to Track record`);
    ok((await page.locator('#record svg.chart').count()) === 2, `${width}px: track record draws both charts`);
    const chartPx = await page.evaluate(() => { const svg = document.querySelector('#record svg.chart'); const t = svg.querySelector('.axis'); return { w: svg.getBoundingClientRect().width, eff: 10 * svg.getBoundingClientRect().width / 640, wrap: !!svg.closest('.chartwrap') }; });
    ok(chartPx.wrap && chartPx.eff >= 8.7, `${width}px: chart axis text stays >= 8.7px effective (${chartPx.eff.toFixed(2)}px at ${chartPx.w}px wide, scrolls inside .chartwrap)`);
    await page.screenshot({ path: path.join(SCREENS, `record-${width}.png`), fullPage: true });
    await page.keyboard.press('ArrowRight');
    ok(await page.locator('#panel-how').isVisible(), `${width}px: ArrowRight moves to How it works`);
    await page.screenshot({ path: path.join(SCREENS, `how-${width}.png`), fullPage: true });
    const w2 = await page.evaluate(() => ({ sw: document.body.scrollWidth, iw: window.innerWidth }));
    ok(w2.sw <= w2.iw, `${width}px: still no horizontal scroll after visiting every tab`);

    ok(errors.length === 0, `${width}px: no console errors` + (errors.length ? ' -> ' + errors.join(' | ') : ''));
    await ctx.close();
  }

  // Parlay engine vs the Python oracle fixture (src/parlay.py -> tests/fixtures/parlay_fixture.json):
  // feed its input games through the page's own eligibleLegs/rankParlays and compare every candidate.
  if (existsSync(PARLAY_FIXTURE)) {
    const fx = JSON.parse(readFileSync(PARLAY_FIXTURE, 'utf8'));
    const ctxP = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    const pageP = await ctxP.newPage();
    await pageP.goto(url); await pageP.waitForSelector('#slate article.game');
    const got = await pageP.evaluate((input) => {
      const legsByBook = {};
      for (const b of input.books) legsByBook[b] = eligibleLegs(input.games, b, input.min_p).map(({ game_id, side, p, american }) => ({ game_id, side, p, american }));
      const ranked = rankParlays(legsByBook, input.min_legs, input.max_legs).map((c) => ({ book: c.book, n_legs: c.n_legs, game_ids: c.game_ids, hit: c.hit, decimal_odds: c.decimal_odds, payout: c.payout, ev: c.ev, fair_american: c.fair_american }));
      return { legsByBook, ranked };
    }, fx.input);
    const exp = fx.expected, near = (a, b) => Math.abs(a - b) <= 1e-12 * Math.max(1, Math.abs(b));
    ok(JSON.stringify(got.legsByBook) === JSON.stringify(exp.legs_by_book), 'oracle: eligible legs per book identical');
    ok(got.ranked.length === exp.ranked_all.length && got.ranked.length === exp.n_candidates, `oracle: candidate count ${got.ranked.length} == ${exp.n_candidates}`);
    let mismatches = 0;
    exp.ranked_all.forEach((e, i) => {
      const g = got.ranked[i];
      if (!g || g.book !== e.book || g.n_legs !== e.n_legs || g.game_ids.join() !== e.game_ids.join() || !near(g.hit, e.hit) || !near(g.decimal_odds, e.decimal_odds) || !near(g.payout, e.payout) || !near(g.ev, e.ev) || g.fair_american !== e.fair_american) { mismatches++; if (mismatches <= 3) console.log('   mismatch at', i, JSON.stringify(g), 'vs', JSON.stringify(e)); }
    });
    ok(mismatches === 0, `oracle: all ${exp.ranked_all.length} ranked candidates match (order, hit, odds, payout, EV, fair American)`);
    for (const [k, e] of Object.entries(exp.pinned || {})) {
      const g = got.ranked.find((c) => c.n_legs === Number(k));
      ok(g && g.book === e.book && g.game_ids.join() === e.game_ids.join() && near(g.ev, e.ev), `oracle: pinned best ${k}-leg matches`);
    }
    await ctxP.close();
  } else {
    console.log('NOTE tests/fixtures/parlay_fixture.json not present; only the internal 2-leg oracle below runs');
  }

  // Parlay math against an independent 2-leg oracle computed here from the model fixture (D9).
  {
    const fx = loadFixture();
    const dec = (a) => (a > 0 ? a / 100 : 100 / Math.abs(a));
    const legs = (book) => fx.games.filter((g) => g.status === 'scheduled' && g.league_date === DATE && g.official.market && g.official.p_pick_blend >= 0.55 && g.official.best_prices[book])
      .map((g) => ({ id: g.game_id, p: g.official.p_pick_blend, a: g.official.best_prices[book].american }));
    let best = null;
    for (const book of fx.params.books) {
      const L = legs(book);
      for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) {
        const hit = L[i].p * L[j].p, payout = (1 + dec(L[i].a)) * (1 + dec(L[j].a)) - 1, ev = hit * payout - (1 - hit);
        if (!best || ev > best.ev) best = { book, ids: [L[i].id, L[j].id].join(','), hit, ev };
      }
    }
    const ctx2 = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    const page2 = await ctx2.newPage();
    await page2.goto(url); await page2.click('#parlay-btn'); await page2.waitForSelector('#parlay-rows .prow');
    const row = page2.locator('#parlay-rows .prow').first();
    const got = { book: await row.getAttribute('data-book'), ids: await row.getAttribute('data-games'), hit: Number(await row.getAttribute('data-hit')), ev: Number(await row.getAttribute('data-ev')) };
    ok(best && got.book === best.book && got.ids === best.ids && Math.abs(got.hit - best.hit) < 1e-12 && Math.abs(got.ev - best.ev) < 1e-12,
      `parlay: first row equals the independent best 2-leg (${got.book} ${got.ids} hit ${got.hit} ev ${got.ev})`);
    const fair = Number(await page2.evaluate(() => fairAmerican(0.36)));
    ok(fair === 178, `parlay: fair American for 0.36 is +178 (got ${fair})`);
    await ctx2.close();
  }

  // Mixed slate in a KEYED build: one game falls back to CSV lines (build.game_market does this when the feed
  // lacks the game); the real-book parlays must survive and the CSV game is simply not a leg (D9).
  {
    const fx = loadFixture();
    const slate = fx.games.filter((g) => g.status === 'scheduled' && g.league_date === DATE);
    const victim = slate.find((g) => g.official.parlay_eligible && g.official.market && g.official.market.source === 'odds_api');
    ok(!!victim && toCsvMarket(victim), `mixed: fixture game ${victim && victim.game_id} converted to a CSV-line fallback`);
    let expected = 0;
    for (const book of fx.params.books) {
      const n = slate.filter((g) => g.official.market && g.official.market.source === 'odds_api' && g.official.p_pick_blend >= 0.55 && g.official.best_prices[book]).length;
      for (let k = 2; k <= Math.min(5, n); k++) expected += choose(n, k);
    }
    const mixedUrl = renderVariant('mixed', fx);
    const ctxM = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    const pageM = await ctxM.newPage();
    const errs = []; pageM.on('pageerror', (e) => errs.push(String(e)));
    await pageM.goto(mixedUrl); await pageM.waitForSelector('#slate article.game');
    await pageM.click('#parlay-btn'); await pageM.waitForSelector('#parlay:not([hidden])');
    const cands = await pageM.evaluate(() => parlayCandidates().map((c) => c.book));
    const books = new Set(cands);
    ok(cands.length === expected && expected > 0, `mixed: ${cands.length} real-book candidates (independent count ${expected}), not "No eligible legs"`);
    ok(!books.has('csv') && [...books].every((b) => fx.params.books.includes(b)), `mixed: books used ${JSON.stringify([...books])} are the selected real books only`);
    ok((await pageM.locator('#parlay-rows .prow').count()) > 0, 'mixed: parlay rows rendered');
    const csvCard = pageM.locator(`#slate article[data-game="${victim.game_id}"] .price`);
    ok((await csvCard.textContent()).includes('Line ') && (await csvCard.textContent()).includes('nflverse'), `mixed: CSV-fallback card prices as "Line … (nflverse, as of build)"`);
    ok(errs.length === 0, 'mixed: no page errors');
    await ctxM.close();
  }

  // KEYLESS build (odds_source csv): every market is an nflverse line; no dead book toggles; parlays at the csv pseudo-book.
  {
    const fx = loadFixture();
    fx.build.odds_source = 'csv'; fx.build.official_odds_at = null;
    let converted = 0;
    for (const g of fx.games) if (g.official.market) converted += toCsvMarket(g) ? 1 : 0;
    const csvUrl = renderVariant('csv', fx);
    const ctxC = await browser.newContext({ viewport: { width: 400, height: 900 } });
    const pageC = await ctxC.newPage();
    const errs = []; pageC.on('pageerror', (e) => errs.push(String(e)));
    await pageC.goto(csvUrl); await pageC.waitForSelector('#slate article.game');
    ok(converted > 0 && (await pageC.locator('#books input').count()) === 0 && (await pageC.locator('#books').textContent()).includes('No Odds API key'), 'keyless: no book checkboxes, one explanatory line instead');
    ok((await pageC.locator('#build-line').textContent()).includes('nflverse lines'), 'keyless: build line says nflverse lines');
    const priceTxt = await pageC.locator('#slate article.game .price').first().textContent();
    ok(priceTxt.startsWith('Line ') && priceTxt.includes('nflverse') && !priceTxt.includes('Best price'), `keyless: price line reads "${priceTxt.slice(0, 40)}…"`);
    await pageC.click('#parlay-btn'); await pageC.waitForSelector('#parlay-rows .prow');
    const rowsC = await pageC.locator('#parlay-rows .prow').all();
    const booksC = new Set(await Promise.all(rowsC.map((r) => r.getAttribute('data-book'))));
    ok(rowsC.length > 0 && booksC.size === 1 && booksC.has('csv'), `keyless: ${rowsC.length} parlay rows, all at the nflverse pseudo-book`);
    ok((await pageC.locator('#parlay').textContent()).includes('Priced at nflverse lines'), 'keyless: parlay footer names nflverse lines');
    ok((await pageC.locator('#parlay-rows .lbl').first().textContent()).includes('nflverse line'), 'keyless: parlay row label says nflverse line');
    await pageC.click('#tab-how');
    ok((await pageC.locator('#how').textContent()).includes('This build had no Odds API key'), 'keyless: How it works explains the mode');
    const w3 = await pageC.evaluate(() => ({ sw: document.body.scrollWidth, iw: window.innerWidth }));
    ok(w3.sw <= w3.iw && errs.length === 0, 'keyless: no horizontal scroll, no page errors at 400px');
    await pageC.screenshot({ path: path.join(SCREENS, 'today-keyless-400.png'), fullPage: true });
    await ctxC.close();
  }

  // Live overlay (D6): serve tests/fixtures/overlay_fixture.json at the overlay URL and check that
  // LIVE lines appear while the official pick/tier/probabilities are untouched.
  {
    const OVERLAY = path.join(ROOT, 'tests', 'fixtures', 'overlay_fixture.json');
    const LIVE_OUT = path.join(OUT_DIR, 'gridiron-check-live.html');
    execFileSync('python3', ['-m', 'src.render', FIXTURE, LIVE_OUT], { cwd: ROOT, env: { ...process.env, GRIDIRON_ODDS_URL: 'https://overlay.test/nfl' }, stdio: 'inherit' });
    const ctx3 = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    const page3 = await ctx3.newPage();
    const errs = []; page3.on('pageerror', (e) => errs.push(String(e)));
    await page3.route('https://overlay.test/nfl', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: readFileSync(OVERLAY, 'utf8') }));
    await page3.goto(pathToFileURL(LIVE_OUT).href + '?date=' + DATE);
    await page3.waitForFunction(() => document.getElementById('live-note').textContent.startsWith('LIVE'));
    const liveLines = await page3.locator('#slate .liveline:not([hidden])').count();
    ok(liveLines > 0, `overlay: LIVE price lines shown for matched games (${liveLines})`);
    const before = loadFixture().games.find((g) => g.game_id === '2026_05_PHI_JAX').official;
    const cardTxt = await page3.locator('#slate article[data-game="2026_05_PHI_JAX"] .pred').textContent();
    ok(cardTxt.includes(before.tier) && cardTxt.includes(before.pick), 'overlay: official pick and tier unchanged under live prices');
    await page3.screenshot({ path: path.join(SCREENS, 'today-live-1200.png'), fullPage: true });
    // Failure path: unreachable URL must degrade to the build-time note without throwing.
    await page3.unroute('https://overlay.test/nfl');
    await page3.route('https://overlay.test/nfl', (route) => route.abort());
    await page3.reload();
    await page3.waitForFunction(() => document.getElementById('live-note').textContent.includes('live prices unavailable'));
    ok(errs.length === 0, 'overlay: fetch failure degrades to "Lines as of build time" with no page errors');
    await ctx3.close();
  }

  // Empty track record: overall.n = 0 must show the empty-state copy, not NaN.
  {
    const fx = loadFixture();
    fx.track_record.overall = { n: 0, hits: 0, losses: 0, pushes: 0, accuracy: null, market_n: 0, market_hits: 0, market_accuracy: null };
    const emptyUrl = renderVariant('empty', fx);
    const ctx4 = await browser.newContext({ viewport: { width: 400, height: 900 } });
    const page4 = await ctx4.newPage();
    const errs = []; page4.on('pageerror', (e) => errs.push(String(e)));
    await page4.goto(emptyUrl);
    await page4.click('#tab-record');
    const txt = await page4.locator('#record').textContent();
    ok(txt.includes('No graded picks yet') && errs.length === 0, 'track record: empty state copy when n is 0');
    // Scorecard at 400px: five tiles, no grey unfilled cell (the strip's own background is paper).
    const sc = await page4.evaluate(() => { const el = document.getElementById('scorecard'); const cs = getComputedStyle(el); return { bg: cs.backgroundColor, tiles: el.children.length, cols: cs.gridTemplateColumns.split(' ').length }; });
    ok(sc.tiles === 5 && sc.bg === 'rgb(255, 255, 255)' && sc.cols === 2, `scorecard 400px: ${sc.tiles} tiles in ${sc.cols} columns on a paper background (${sc.bg})`);
    await ctx4.close();
  }

  // Dark theme via media emulation (no data-theme attribute): the dark token background must paint,
  // the toggle must describe the painted scheme, and one click must reach light.
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 }, colorScheme: 'dark' });
  const page = await ctx.newPage();
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto(url);
  await page.waitForSelector('#slate article.game');
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  ok(bg === 'rgb(11, 15, 26)', `dark: body background is the dark --paper token (${bg})`);
  const cardInk = await page.evaluate(() => getComputedStyle(document.querySelector('.team .code')).color);
  ok(cardInk === 'rgb(243, 245, 250)' || cardInk === 'rgb(79, 224, 255)', `dark: text uses dark ink tokens (${cardInk})`);
  const btnState = async () => page.evaluate(() => { const b = document.getElementById('theme-btn'); return { text: b.textContent, pressed: b.getAttribute('aria-pressed'), bg: getComputedStyle(document.body).backgroundColor, attr: document.documentElement.getAttribute('data-theme') }; });
  const s0 = await btnState();
  ok(s0.text === 'Light mode' && s0.pressed === 'true' && s0.attr === null, `dark (system): toggle reads "Light mode", aria-pressed=true before any click (${JSON.stringify(s0)})`);
  const signal = await page.evaluate(() => { const b = document.getElementById('parlay-btn'); const cs = getComputedStyle(b); return { color: cs.color, bg: cs.backgroundColor }; });
  ok(signal.color === 'rgb(11, 15, 26)' && signal.bg === 'rgb(167, 139, 250)', `dark: "Build a parlay" is ink-on-signal (${signal.color} on ${signal.bg}), not white`);
  await page.click('#theme-btn');
  const s1 = await btnState();
  ok(s1.bg === 'rgb(255, 255, 255)' && s1.attr === 'light' && s1.text === 'Dark mode' && s1.pressed === 'false', `dark (system): first click reaches light (${JSON.stringify(s1)})`);
  await page.screenshot({ path: path.join(SCREENS, 'today-dark-1200.png'), fullPage: true });
  await ctx.close();
} finally {
  await browser.close();
}
console.log(failures ? `${failures} check(s) FAILED` : 'All browser checks passed');
process.exit(failures ? 1 : 0);
