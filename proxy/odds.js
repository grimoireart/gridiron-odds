/**
 * Gridiron Odds — optional live-price overlay Worker (DECISIONS D6, CONTRACT section 3).
 *
 * One fixed upstream request (caller query params are ignored), one canonical Cache API key,
 * 15-minute TTL, miss coalescing, strict CORS allowlist, and a hard credit reserve: when the
 * upstream reports fewer than 60 requests remaining the Worker stops calling upstream until the
 * free tier's monthly reset (the first of the next UTC month) and serves the last copy
 * (degraded: true, for up to 7 hours) or an empty degraded 503.
 *
 * Scope of the state: caches.default and the in-flight promise are per Cloudflare data center
 * (and per isolate), so the TTL, the coalescing and the cutoff each hold per PoP; the reserve
 * floor is what bounds total spend (proxy/README.md "Budget").
 *
 * Output is the overlay contract with team names VERBATIM (canonical: false); the page maps
 * them to codes. The API key lives only in env.ODDS_API_KEY and only in the upstream URL.
 */
const UPSTREAM = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds?regions=us&markets=h2h,spreads,totals&oddsFormat=american";
const FRESH_KEY = "https://gridiron-odds.cache/overlay/v1/fresh";
const STALE_KEY = "https://gridiron-odds.cache/overlay/v1/stale";
const CUTOFF_KEY = "https://gridiron-odds.cache/overlay/v1/cutoff";
const TTL_SECONDS = 15 * 60;
const STALE_SECONDS = 7 * 60 * 60;
const CUTOFF_MIN_SECONDS = 60; // floor for the cutoff when the reset is imminent
const RESERVE_FLOOR = 60; // D6: below this many remaining requests, stop calling upstream
const SCHEMA_VERSION = "1.0.0";
const OFFSHORE = new Set(["bovada", "betus", "betonlineag", "lowvig", "mybookieag"]);
const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };

let inflight = null; // module-level: concurrent misses share one upstream call

const allowedOrigin = (request, env) => {
  const origin = request.headers.get("Origin");
  const list = String(env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return origin && list.includes(origin) ? origin : null;
};

const reply = (body, status, origin, extra = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, "access-control-allow-origin": origin, vary: "Origin", ...extra },
  });

/** Seconds from `nowMs` to the first instant of the next UTC month (the Odds API's monthly reset). */
const secondsToMonthEnd = (nowMs) => {
  const d = new Date(nowMs);
  const reset = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
  return Math.max(CUTOFF_MIN_SECONDS, Math.ceil((reset - nowMs) / 1000));
};

const price = (v) => (Number.isInteger(v) && Math.abs(v) >= 100 ? v : null);
const point = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Trim a raw Odds API game to the overlay shape; names stay verbatim (no canonicalization). */
const trimGame = (g) => ({
  home: g.home_team,
  away: g.away_team,
  commence_time: g.commence_time,
  books: (g.bookmakers || []).filter((b) => b && b.key && !OFFSHORE.has(b.key)).map((b) => {
    const m = Object.fromEntries((b.markets || []).map((x) => [x.key, x.outcomes || []]));
    const h2h = (name) => (m.h2h || []).find((o) => o.name === name);
    const spread = (m.spreads || []).find((o) => o.name === g.home_team);
    const total = (m.totals || []).find((o) => o.name === "Over") || (m.totals || []).find((o) => o.name === "Under");
    return {
      key: b.key,
      home_ml: price(h2h(g.home_team)?.price),
      away_ml: price(h2h(g.away_team)?.price),
      home_spread: point(spread?.point),
      total: point(total?.point),
    };
  }),
});

const overlay = (games, degraded, fetchedAt) =>
  ({ schema_version: SCHEMA_VERSION, fetched_at: fetchedAt, degraded, canonical: false, source: "worker", games });

const store = (cache, key, body, maxAge) =>
  cache.put(new Request(key), new Response(JSON.stringify(body), { headers: { "cache-control": `public, max-age=${maxAge}` } }));

const load = async (cache, key) => {
  const hit = await cache.match(new Request(key));
  return hit ? hit.json() : null;
};

/** Upstream call; the only place the key is used. Returns {games, remaining} or throws without echoing the URL. */
const callUpstream = async (env) => {
  const res = await fetch(`${UPSTREAM}&apiKey=${encodeURIComponent(env.ODDS_API_KEY || "")}`, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`upstream status ${res.status}`);
  const raw = await res.json();
  if (!Array.isArray(raw)) throw new Error("upstream payload is not a list");
  const remaining = Number.parseFloat(res.headers.get("x-requests-remaining"));
  return { games: raw.map(trimGame), remaining: Number.isFinite(remaining) ? remaining : null };
};

/** Serve the stale copy (degraded) if any, else an empty degraded 503. Never caches errors. */
const degradedResponse = async (cache, origin, reason) => {
  const stale = await load(cache, STALE_KEY);
  if (stale) return reply({ ...stale, degraded: true }, 200, origin, { "x-gridiron-cache": "STALE", "x-gridiron-reason": reason });
  return reply(overlay([], true, null), 503, origin, { "x-gridiron-cache": "NONE", "x-gridiron-reason": reason });
};

const refresh = async (cache, env, origin) => {
  const fetchedAt = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  let result;
  try {
    result = await callUpstream(env);
  } catch (err) {
    console.error("odds upstream failed:", err.message);
    return degradedResponse(cache, origin, "upstream_error");
  }
  const body = overlay(result.games, false, fetchedAt);
  await store(cache, STALE_KEY, body, STALE_SECONDS);
  if (result.remaining !== null && result.remaining < RESERVE_FLOOR) {
    const cutoff = secondsToMonthEnd(Date.now());
    console.error(`odds reserve hit: x-requests-remaining=${result.remaining}; no upstream calls for ${cutoff}s (until the monthly reset)`);
    await store(cache, CUTOFF_KEY, { since: fetchedAt, remaining: result.remaining, seconds: cutoff }, cutoff);
    return reply({ ...body, degraded: true }, 200, origin, { "x-gridiron-cache": "RESERVE" });
  }
  await store(cache, FRESH_KEY, body, TTL_SECONDS);
  return reply(body, 200, origin, { "x-gridiron-cache": "MISS" });
};

export default {
  async fetch(request, env) {
    const origin = allowedOrigin(request, env);
    if (!origin) return new Response(JSON.stringify({ error: "origin not allowed" }), { status: 403, headers: JSON_HEADERS });
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: { "access-control-allow-origin": origin, "access-control-allow-methods": "GET, OPTIONS", "access-control-allow-headers": "content-type", "access-control-max-age": "86400", vary: "Origin" },
      });
    }
    if (request.method !== "GET" && request.method !== "HEAD") return reply({ error: "method not allowed" }, 405, origin);
    const cache = caches.default; // caller URL/params are never part of any key
    const fresh = await load(cache, FRESH_KEY);
    if (fresh) return reply(fresh, 200, origin, { "x-gridiron-cache": "HIT" });
    if (await load(cache, CUTOFF_KEY)) return degradedResponse(cache, origin, "reserve");
    if (!inflight) inflight = refresh(cache, env, origin).finally(() => { inflight = null; });
    const shared = await inflight; // a coalesced caller gets a clone with its own CORS origin
    return new Response(shared.clone().body, { status: shared.status, headers: { ...Object.fromEntries(shared.headers), "access-control-allow-origin": origin } });
  },
};
