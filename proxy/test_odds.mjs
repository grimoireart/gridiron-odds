// node proxy/test_odds.mjs — no npm packages. Stubs fetch + caches.default and drives the Worker.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const sample = JSON.parse(readFileSync(join(here, "..", "tests", "fixtures", "odds_api_sample.json"), "utf8"));
const KEY = "sk-test-SECRET-9f8e7d6c";
const ORIGIN = "https://grimoireart.github.io";
const env = { ODDS_API_KEY: KEY, ALLOWED_ORIGINS: `${ORIGIN}, http://localhost:8787` };

// ---- fake clock + Cache API honouring max-age -------------------------------------------
let now = Date.parse("2026-10-11T12:00:00Z");
const realNow = Date.now;
Date.now = () => now;
const advance = (seconds) => { now += seconds * 1000; };

const makeCache = () => {
  const store = new Map();
  return {
    puts: [],
    async put(req, res) {
      const url = typeof req === "string" ? req : req.url;
      const m = /max-age=(\d+)/.exec(res.headers.get("cache-control") || "");
      this.puts.push(url);
      store.set(url, { body: await res.text(), expires: Date.now() + (m ? Number(m[1]) : 0) * 1000 });
    },
    async match(req) {
      const url = typeof req === "string" ? req : req.url;
      const e = store.get(url);
      if (!e || Date.now() >= e.expires) return undefined;
      return new Response(e.body, { headers: { "content-type": "application/json" } });
    },
    keys: () => [...store.keys()],
  };
};

// ---- fake upstream ---------------------------------------------------------------------
const upstream = { calls: [], status: 200, remaining: "400", body: sample, deferred: null };
globalThis.fetch = async (url, init) => {
  upstream.calls.push({ url: String(url), init });
  if (upstream.deferred) await upstream.deferred;
  return new Response(JSON.stringify(upstream.body), {
    status: upstream.status,
    headers: { "content-type": "application/json", "x-requests-remaining": upstream.remaining, "x-requests-used": "100" },
  });
};

let cache = makeCache();
globalThis.caches = { get default() { return cache; } };

const { default: worker } = await import("./odds.js");
const call = (path = "/", headers = { Origin: ORIGIN }, method = "GET") =>
  worker.fetch(new Request(`https://proxy.example${path}`, { method, headers }), env);

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const reset = () => { cache = makeCache(); upstream.calls = []; upstream.status = 200; upstream.remaining = "400"; upstream.body = sample; upstream.deferred = null; };

test("overlay shape: names verbatim, canonical false, offshore dropped, fields trimmed", async () => {
  reset();
  upstream.body = [...sample, { ...sample[0], bookmakers: [{ key: "bovada", markets: [] }, ...sample[0].bookmakers] }];
  const res = await call();
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.schema_version, "1.0.0");
  assert.equal(body.canonical, false);
  assert.equal(body.source, "worker");
  assert.equal(body.degraded, false);
  assert.match(body.fetched_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.equal(body.games.length, 3);
  const g = body.games[0];
  assert.deepEqual(Object.keys(g), ["home", "away", "commence_time", "books"]);
  assert.equal(g.home, "Jacksonville Jaguars");
  assert.deepEqual(g.books[0], { key: "draftkings", home_ml: -375, away_ml: 295, home_spread: -7.5, total: 41.5 });
  assert.deepEqual(body.games[1].books[0], { key: "draftkings", home_ml: -178, away_ml: 150, home_spread: 3.0, total: 54.5 });
  assert.ok(!body.games[2].books.some((b) => b.key === "bovada"), "offshore book dropped");
  assert.equal(res.headers.get("x-gridiron-cache"), "MISS");
});

test("fixed upstream URL and caller params ignored", async () => {
  reset();
  await call("/?markets=player_props&regions=eu&bookmakers=pinnacle&apiKey=other");
  assert.equal(upstream.calls.length, 1);
  const u = new URL(upstream.calls[0].url);
  assert.equal(u.origin + u.pathname, "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds");
  assert.equal(u.searchParams.get("markets"), "h2h,spreads,totals");
  assert.equal(u.searchParams.get("regions"), "us");
  assert.equal(u.searchParams.get("oddsFormat"), "american");
  assert.equal(u.searchParams.get("apiKey"), KEY);
  assert.equal(u.searchParams.has("bookmakers"), false);
});

test("canonical cache key: different caller URLs share one entry; TTL boundary at 15 min", async () => {
  reset();
  await call("/a?x=1");
  const res2 = await call("/b?y=2");
  assert.equal(res2.headers.get("x-gridiron-cache"), "HIT");
  assert.equal(upstream.calls.length, 1, "second call served from cache");
  assert.ok(cache.keys().every((k) => k.startsWith("https://gridiron-odds.cache/")), "keys are internal, not caller URLs");
  advance(15 * 60 - 1);
  assert.equal((await call()).headers.get("x-gridiron-cache"), "HIT");
  assert.equal(upstream.calls.length, 1);
  advance(1);
  assert.equal((await call()).headers.get("x-gridiron-cache"), "MISS");
  assert.equal(upstream.calls.length, 2, "expired at exactly 900 s");
});

test("concurrent misses coalesce into one upstream call", async () => {
  reset();
  let release;
  upstream.deferred = new Promise((r) => { release = r; });
  const p1 = call("/", { Origin: ORIGIN });
  const p2 = call("/", { Origin: "http://localhost:8787" });
  const p3 = call();
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(upstream.calls.length, 1);
  release();
  const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
  assert.equal(upstream.calls.length, 1);
  assert.equal(r1.status, 200);
  assert.equal(r2.headers.get("access-control-allow-origin"), "http://localhost:8787", "each caller gets its own origin");
  assert.equal(r3.headers.get("access-control-allow-origin"), ORIGIN);
  assert.equal((await r2.json()).games.length, 2);
  assert.equal((await r3.json()).games.length, 2);
});

test("reserve: remaining < 60 stops upstream until the monthly reset; stale served degraded; empty => 503", async () => {
  reset();
  upstream.remaining = "59";
  const r1 = await call();
  assert.equal(r1.status, 200);
  assert.equal((await r1.json()).degraded, true);
  assert.equal(r1.headers.get("x-gridiron-cache"), "RESERVE");
  advance(16 * 60); // fresh TTL would have expired; cutoff still active
  const r2 = await call();
  assert.equal(upstream.calls.length, 1, "no upstream call during the cutoff");
  assert.equal(r2.status, 200);
  assert.equal(r2.headers.get("x-gridiron-cache"), "STALE");
  const b2 = await r2.json();
  assert.equal(b2.degraded, true);
  assert.equal(b2.games.length, 2, "stale copy carries the last prices");
  advance(6 * 60 * 60);
  const r2b = await call();
  assert.equal(upstream.calls.length, 1, "no 6-hour resume: still no upstream call");
  assert.equal(r2b.headers.get("x-gridiron-cache"), "STALE");
  advance(2 * 60 * 60); // past the 7 h stale window: empty degraded 503, still no upstream call
  const r2c = await call();
  assert.equal(r2c.status, 503);
  assert.equal(upstream.calls.length, 1, "stale expired; still inside the cutoff");
  const resetAt = Date.UTC(2026, 10, 1); // 2026-11-01T00:00Z, the first of the next UTC month
  now = resetAt - 1000;
  await call();
  assert.equal(upstream.calls.length, 1, "one second before the monthly reset: still cut off");
  now = resetAt;
  upstream.remaining = "400";
  const r3 = await call();
  assert.equal(upstream.calls.length, 2, "upstream resumes at the monthly reset");
  assert.equal((await r3.json()).degraded, false);

  // cutoff with nothing cached at all => empty degraded 503
  reset();
  upstream.remaining = "10";
  await call();
  cache = (() => { const c = makeCache(); return c; })(); // simulate stale eviction: drop everything but re-create the cutoff
  await cache.put(new Request("https://gridiron-odds.cache/overlay/v1/cutoff"), new Response("{}", { headers: { "cache-control": "max-age=1800000" } }));
  const r4 = await call();
  assert.equal(r4.status, 503);
  const b4 = await r4.json();
  assert.equal(b4.degraded, true);
  assert.deepEqual(b4.games, []);
  assert.equal(b4.canonical, false);
  assert.equal(upstream.calls.length, 1);
});

test("CORS: exact-origin allow, deny unknown/missing origin with 403, OPTIONS preflight", async () => {
  reset();
  const ok = await call("/", { Origin: ORIGIN });
  assert.equal(ok.headers.get("access-control-allow-origin"), ORIGIN);
  assert.equal(ok.headers.get("vary"), "Origin");
  for (const bad of [{ Origin: "https://evil.example" }, { Origin: "https://grimoireart.github.io.evil.example" }, { Origin: "https://GRIMOIREART.github.io" }, {}]) {
    const r = await call("/", bad);
    assert.equal(r.status, 403, `denied ${JSON.stringify(bad)}`);
    assert.equal(r.headers.get("access-control-allow-origin"), null);
  }
  assert.equal(upstream.calls.length, 1, "denied requests never reach upstream");
  const pre = await call("/", { Origin: "http://localhost:8787" }, "OPTIONS");
  assert.equal(pre.status, 204);
  assert.equal(pre.headers.get("access-control-allow-origin"), "http://localhost:8787");
  assert.match(pre.headers.get("access-control-allow-methods"), /GET/);
  assert.equal((await call("/", { Origin: ORIGIN }, "POST")).status, 405);
});

test("key never appears in any response body or header, in any state", async () => {
  reset();
  const check = async (res, label) => {
    const text = await res.text();
    assert.ok(!text.includes(KEY), `${label}: key in body`);
    assert.ok(!/apiKey/i.test(text), `${label}: apiKey param in body`);
    for (const [k, v] of res.headers) assert.ok(!v.includes(KEY) && !k.includes(KEY), `${label}: key in header ${k}`);
  };
  await check(await call(), "miss");
  await check(await call(), "hit");
  await check(await call("/", { Origin: "https://nope.example" }), "denied");
  reset();
  upstream.status = 500;
  await check(await call(), "upstream error");
  reset();
  upstream.remaining = "1";
  await check(await call(), "reserve");
  await check(await call(), "cutoff");
});

test("cutoff length: to the first of the next UTC month, never under 60 s", async () => {
  reset();
  now = Date.UTC(2026, 9, 31, 23, 59, 30); // 30 s before November
  upstream.remaining = "5";
  await call();
  const cutoffBody = async () => JSON.parse(await (await cache.match(new Request("https://gridiron-odds.cache/overlay/v1/cutoff"))).text());
  assert.equal((await cutoffBody()).seconds, 60, "floored at 60 s");
  advance(59);
  await call();
  assert.equal(upstream.calls.length, 1);
  advance(1);
  upstream.remaining = "400";
  await call();
  assert.equal(upstream.calls.length, 2, "resumes after the floor");
  reset();
  now = Date.UTC(2026, 9, 11, 12, 0, 0);
  upstream.remaining = "5";
  await call();
  assert.equal((await cutoffBody()).seconds, (Date.UTC(2026, 10, 1) - now) / 1000, "exactly the seconds to 2026-11-01T00:00Z");
});

test("upstream errors are never cached and serve stale-or-503 degraded", async () => {
  reset();
  upstream.status = 500;
  const r1 = await call();
  assert.equal(r1.status, 503);
  assert.equal((await r1.json()).degraded, true);
  assert.ok(!cache.puts.length, "nothing written to the cache on error");
  upstream.status = 200;
  const r2 = await call();
  assert.equal(r2.status, 200);
  assert.equal(upstream.calls.length, 2, "retried on the next request");
  assert.equal((await r2.json()).degraded, false);
  // a later failure falls back to the stale copy, still marked degraded, still not cached as fresh
  advance(16 * 60);
  upstream.status = 502;
  const putsBefore = cache.puts.length;
  const r3 = await call();
  assert.equal(r3.status, 200);
  assert.equal(r3.headers.get("x-gridiron-cache"), "STALE");
  assert.equal((await r3.json()).degraded, true);
  assert.equal(cache.puts.length, putsBefore);
  const r4 = await call();
  assert.equal(upstream.calls.length, 4, "error not cached: upstream tried again");
  assert.equal(r4.headers.get("x-gridiron-cache"), "STALE");
  // malformed JSON is an error too
  reset();
  globalThis.fetch = async () => new Response("<html>oops</html>", { status: 200, headers: { "x-requests-remaining": "400" } });
  const r5 = await call();
  assert.equal(r5.status, 503);
  assert.ok(!cache.puts.length);
});

let failed = 0;
for (const t of tests) {
  try {
    await t.fn();
    console.log(`ok   ${t.name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL ${t.name}\n     ${err && err.stack ? err.stack.split("\n").slice(0, 3).join("\n     ") : err}`);
  }
}
Date.now = realNow;
console.log(failed ? `${failed} of ${tests.length} failed` : `${tests.length} passed`);
process.exit(failed ? 1 : 0);
