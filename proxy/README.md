# Gridiron Odds proxy (Cloudflare Worker)

Optional live-price overlay for the page (DECISIONS D6, CONTRACT section 3). The daily build is
the official snapshot; this Worker only lets the page refresh **best price / live EV / "price
moved"** under a LIVE label. It never changes a pick, probability, tier, rationale or record.

## What it does

| Rule | Implementation |
|---|---|
| One fixed upstream URL | `/v4/sports/americanfootball_nfl/odds?regions=us&markets=h2h,spreads,totals&oddsFormat=american` (3 credits/call). Caller query params are ignored. |
| One canonical cache key | `caches.default` keyed by a fixed internal URL, never by the caller's URL. |
| 15-minute TTL | `Cache-Control: max-age=900` on the stored copy; a 7-hour "stale" copy is kept for degraded service. |
| Miss coalescing | A module-level in-flight promise: concurrent misses share one upstream call. |
| CORS allowlist | `env.ALLOWED_ORIGINS` (comma list, exact match). No/unknown `Origin` → 403. `OPTIONS` → 204. |
| Hard reserve | If upstream `x-requests-remaining` < 60: a cutoff is stored in the cache until the first of the next UTC month (the free tier's monthly reset; at least 60 s), upstream is not called again in that window, and the Worker serves the stale copy (7 h) with `degraded: true`, then an empty `{"degraded": true}` 503. |
| Errors never cached | Upstream non-2xx / bad JSON → stale copy (`degraded: true`) or 503; nothing is written to the fresh key. |
| Key never echoed | The key is only ever in the upstream URL; responses, headers and logs never contain it. |

Response shape (names verbatim, `canonical: false`, the page maps names to codes):

```json
{"schema_version":"1.0.0","fetched_at":"2026-10-11T12:40:07Z","degraded":false,"canonical":false,"source":"worker",
 "games":[{"home":"Jacksonville Jaguars","away":"Philadelphia Eagles","commence_time":"2026-10-11T13:30:00Z",
           "books":[{"key":"draftkings","home_ml":-380,"away_ml":300,"home_spread":-7.5,"total":41.5}]}]}
```

Offshore books (`bovada`, `betus`, `betonlineag`, `lowvig`, `mybookieag`) are dropped; every other
book the feed returns is passed through so the page can honour the user's book toggles.

Diagnostic header `x-gridiron-cache`: `HIT` (fresh cache), `MISS` (called upstream), `RESERVE`
(upstream called, reserve tripped on this call), `STALE` (served the stale copy, see
`x-gridiron-reason`), `NONE` (nothing to serve, 503).

## Deploy (Lauren, by hand)

```sh
cd proxy
npx wrangler login
npx wrangler secret put ODDS_API_KEY        # paste the key; it is never written to this repo
# edit ALLOWED_ORIGINS in wrangler.toml to the real GitHub Pages origin
npx wrangler deploy
```

Smoke test (must send an allowed Origin; curl without one gets 403 by design):

```sh
curl -i -H 'Origin: http://localhost:8787' https://gridiron-odds-proxy.<account>.workers.dev/
```

## Budget

Free tier: 500 credits/month, 3 per call → ~166 calls. With a 15-minute TTL and round-the-clock
traffic from ONE location the Worker could make up to 96 calls/day; if the page is only open on
game days this is far below the budget.

The cache and the in-flight promise are **per Cloudflare data center** (the Cache API does not
replicate between PoPs) and per isolate, so the 15-minute window, the miss coalescing and the
reserve cutoff each hold per PoP: viewers in N regions can cost N upstream calls per 15 minutes,
and a forged `Origin` header from any region counts too (CORS is a browser control, not
authentication). What bounds total spend is the reserve floor: every PoP stops calling upstream
the first time IT sees `x-requests-remaining` < 60, so the worst case after the floor is reached
is one call (3 credits) per PoP that has not yet observed it. The floor of 60 is DECISIONS D6; if
the page is ever opened from many regions, raise `RESERVE_FLOOR` (e.g. to 120) so the daily
build's own call always survives, or move the FRESH/CUTOFF state to a Workers KV namespace
(global, eventually consistent) and keep `caches.default` as a per-PoP accelerator.

## Test

```sh
node proxy/test_odds.mjs
```

No npm packages: the test stubs `globalThis.fetch` and `caches.default` and checks the canonical
key, ignored params, TTL boundary, coalescing, reserve cutoff (to the monthly reset), CORS
allow/deny, key never in body/headers, and that upstream errors are not cached. The fake cache is
one global Map, so the per-PoP behaviour above is not exercised by the test.
