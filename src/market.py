"""Market math and odds-feed handling (docs/CONTRACT.md section 6.2, DECISIONS D5/D6/D8/D16).

Pure functions throughout, except `fetch_official_odds` (network). Every function
takes and returns plain dicts/lists/numbers. Nothing here rounds: callers round
for display only.

Conventions (CONTRACT section 0):
  * American odds are ints with |x| >= 100; absent = None.
  * `home_spread` / `consensus_home_spread` use the BOOK convention: the point
    attached to the home team's spread outcome (negative = home favored).
  * Probabilities are floats in [0, 1].
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import schema

ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
ODDS_API_PARAMS = {"regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american"}
ODDS_API_TIMEOUT = 10.0

# Offshore books dropped from the official snapshot (CONTRACT 6.2).
OFFSHORE_BOOKS = frozenset({"bovada", "betus", "betonlineag", "lowvig", "mybookieag"})

OVERLAY_SCHEMA_VERSION = schema.OVERLAY_SCHEMA_VERSION
MATCH_WINDOW = timedelta(hours=schema.OVERLAY_MATCH_WINDOW_HOURS)


# ----------------------------------------------------------------------------
# Price arithmetic
# ----------------------------------------------------------------------------


def _check_american(american) -> int:
    """Return `american` as an int, or raise ValueError when it is not a valid price."""
    if isinstance(american, bool) or american is None:
        raise ValueError("American odds must be an int, got %r" % (american,))
    if isinstance(american, float):
        if american != american or american != int(american):
            raise ValueError("American odds must be integral, got %r" % american)
        american = int(american)
    if not isinstance(american, int):
        raise ValueError("American odds must be an int, got %r" % (american,))
    if abs(american) < 100:
        raise ValueError("American odds must satisfy |x| >= 100, got %d" % american)
    return american


def american_to_decimal_profit(american: int) -> float:
    """Profit per 1 unit staked: +150 -> 1.5, -120 -> 0.8333...; ValueError if |x| < 100."""
    a = _check_american(american)
    return a / 100.0 if a > 0 else 100.0 / -a


def implied_prob(american: int) -> float:
    """Raw (vig-included) implied probability: +150 -> 0.4, -120 -> 0.5454..."""
    a = _check_american(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def no_vig_pair(home_american: int, away_american: int) -> tuple[float, float]:
    """Two-way no-vig probabilities (p_home, p_away), normalised to sum to 1.

    Rejects 0 and any price with |x| < 100 (ValueError), per the contract.
    """
    ph = implied_prob(home_american)
    pa = implied_prob(away_american)
    total = ph + pa
    return ph / total, pa / total


def break_even(payout: float) -> float:
    """Win rate needed to break even at a given decimal profit: -110 -> 0.5238..."""
    return 1.0 / (1.0 + payout)


def estimated_ev(p: float, american: int) -> float:
    """Model-estimated EV per $1 at this price: p*payout - (1-p). +100 @ 0.5 -> 0."""
    payout = american_to_decimal_profit(american)
    return p * payout - (1.0 - p)


def ev_per_dollar(p: float, american: int) -> float:
    """Alias of `estimated_ev` (name used in the build brief)."""
    return estimated_ev(p, american)


def clv_pp(close_p_pick: float | None, consensus_p_pick: float | None) -> float | None:
    """Closing-line value in percentage points (D8): 100*(close - pick-time consensus).

    Same side, same no-vig basis at both timestamps. None when either input is None.
    """
    if close_p_pick is None or consensus_p_pick is None:
        return None
    return 100.0 * (close_p_pick - consensus_p_pick)


# ----------------------------------------------------------------------------
# Consensus and best price over books
# ----------------------------------------------------------------------------


def _has_both_ml(book: dict) -> bool:
    return book.get("home_ml") is not None and book.get("away_ml") is not None


def consensus(books: list[dict]) -> dict:
    """Consensus over books (CONTRACT 6.2): MEAN no-vig p_home, MEDIAN total and home spread.

    Returns {"p_home", "n_ml", "total", "n_total", "home_spread", "n_spread"}; the
    value keys are None when no book contributes.
    """
    probs = [no_vig_pair(b["home_ml"], b["away_ml"])[0] for b in books if _has_both_ml(b)]
    totals = [b["total"] for b in books if b.get("total") is not None]
    spreads = [b["home_spread"] for b in books if b.get("home_spread") is not None]
    return {
        "p_home": (sum(probs) / len(probs)) if probs else None,
        "n_ml": len(probs),
        "total": statistics.median(totals) if totals else None,
        "n_total": len(totals),
        "home_spread": statistics.median(spreads) if spreads else None,
        "n_spread": len(spreads),
    }


def _side_key(side: str) -> str:
    if side not in ("home", "away"):
        raise ValueError("side must be 'home' or 'away', got %r" % (side,))
    return side + "_ml"


def prices_by_book(books: list[dict], side: str, pick_code: str, book_keys: list[str]) -> dict:
    """{book_key: price-or-None} for every key in `book_keys` (the pick side's h2h price)."""
    ml_key = _side_key(side)
    by_key = {}
    for b in books:
        if b.get("key") in book_keys and b.get("key") not in by_key:
            by_key[b["key"]] = b
    out = {}
    for key in book_keys:
        b = by_key.get(key)
        price = b.get(ml_key) if b else None
        out[key] = {"book": key, "side": pick_code, "american": price} if price is not None else None
    return out


def best_price(books: list[dict], side: str, pick_code: str, book_keys: list[str]) -> dict | None:
    """Highest decimal profit for the pick among `book_keys`; tie -> first in `book_keys` order."""
    best = None
    best_payout = None
    for key, price in prices_by_book(books, side, pick_code, book_keys).items():
        if price is None:
            continue
        payout = american_to_decimal_profit(price["american"])
        if best is None or payout > best_payout:
            best, best_payout = price, payout
    return best


def market_from_books(books: list[dict], source: str, as_of: str) -> dict:
    """CONTRACT 2.8 market object from a list of normalised book dicts."""
    c = consensus(books)
    return {
        "source": source,
        "as_of": as_of,
        "consensus_p_home": c["p_home"],
        "consensus_total": c["total"],
        "consensus_home_spread": c["home_spread"],
        "n_books": len(books),
        "books": [
            {
                "key": b["key"],
                "home_ml": b.get("home_ml"),
                "away_ml": b.get("away_ml"),
                "home_spread": b.get("home_spread"),
                "total": b.get("total"),
            }
            for b in books
        ],
    }


def market_from_csv(game: dict, as_of: str) -> dict | None:
    """CONTRACT 2.8 market with source "csv" from a normalised game; None if a CSV ML is missing.

    The CSV spread is home-positive, so the book-convention home spread is its negation.
    """
    home_ml = game.get("home_moneyline")
    away_ml = game.get("away_moneyline")
    if home_ml is None or away_ml is None:
        return None
    spread = game.get("spread_line")
    book = {
        "key": schema.CSV_BOOK_KEY,
        "home_ml": home_ml,
        "away_ml": away_ml,
        "home_spread": -spread if spread is not None else None,
        "total": game.get("total_line"),
    }
    return market_from_books([book], "csv", as_of)


# ----------------------------------------------------------------------------
# Odds API normalisation (overlay, CONTRACT section 3)
# ----------------------------------------------------------------------------


def _parse_utc(value) -> datetime | None:
    """Parse an ISO-8601 timestamp (Z or offset; optional fractional seconds) to an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_price(value) -> int | None:
    """An American price from the feed as an int with |x| >= 100, else None."""
    try:
        return _check_american(value)
    except ValueError:
        return None


def _clean_point(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def name_map(teams: list[dict]) -> dict:
    """lowercased full name / alias / code -> canonical code from teams.json-style entries.

    Accepts both `src/teams.json` entries (`code`) and model.json `teams[]` entries (`team`).
    """
    out = {}
    for t in teams:
        code = t.get("code") or t.get("team")
        if not code:
            continue
        out[code.lower()] = code
        full = t.get("full_name")
        if full:
            out[" ".join(full.split()).lower()] = code
        for alias in t.get("aliases") or []:
            out[" ".join(alias.split()).lower()] = code
    return out


def _lookup(names: dict, raw) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return names.get(" ".join(raw.split()).lower())


def _normalize_book(bm: dict, home_name: str, away_name: str, home_code: str, away_code: str, names: dict) -> dict:
    """Flatten one bookmaker entry into {key, home_ml, away_ml, home_spread, total}."""
    markets = {}
    for m in bm.get("markets") or []:
        if isinstance(m, dict) and m.get("key") not in markets:
            markets[m.get("key")] = m.get("outcomes") or []

    def side_of(outcome) -> str | None:
        name = outcome.get("name") if isinstance(outcome, dict) else None
        if name == home_name:
            return "home"
        if name == away_name:
            return "away"
        code = _lookup(names, name)
        if code == home_code:
            return "home"
        if code == away_code:
            return "away"
        return None

    home_ml = away_ml = None
    for o in markets.get("h2h", []):
        side = side_of(o)
        if side == "home" and home_ml is None:
            home_ml = _clean_price(o.get("price"))
        elif side == "away" and away_ml is None:
            away_ml = _clean_price(o.get("price"))

    home_spread = None
    for o in markets.get("spreads", []):
        if side_of(o) == "home":
            home_spread = _clean_point(o.get("point"))
            break

    total = None
    over = [o for o in markets.get("totals", []) if isinstance(o, dict) and str(o.get("name", "")).lower() == "over"]
    under = [o for o in markets.get("totals", []) if isinstance(o, dict) and str(o.get("name", "")).lower() == "under"]
    for o in over + under:
        total = _clean_point(o.get("point"))
        if total is not None:
            break

    return {"key": bm["key"], "home_ml": home_ml, "away_ml": away_ml, "home_spread": home_spread, "total": total}


def normalize_odds_api(payload: list, teams: list[dict], fetched_at: str | None = None) -> dict:
    """Raw Odds API list -> overlay (CONTRACT section 3) with canonical: true, source: "build".

    Drops games whose team names do not map through the full-name alias map (or map to the
    same team), books in OFFSHORE_BOOKS, and duplicate book keys within a game. Prices that
    are not ints with |x| >= 100 become None. `home_spread` is the home outcome's point and
    `total` the Over point (Under's point when Over is missing).
    """
    names = name_map(teams)
    games = []
    for g in payload if isinstance(payload, list) else []:
        if not isinstance(g, dict):
            continue
        home_name, away_name = g.get("home_team"), g.get("away_team")
        home, away = _lookup(names, home_name), _lookup(names, away_name)
        kick = _parse_utc(g.get("commence_time"))
        if home is None or away is None or home == away or kick is None:
            continue
        books = []
        seen = set()
        for bm in g.get("bookmakers") or []:
            key = bm.get("key") if isinstance(bm, dict) else None
            if not isinstance(key, str) or not key or key in OFFSHORE_BOOKS or key in seen:
                continue
            seen.add(key)
            books.append(_normalize_book(bm, home_name, away_name, home, away, names))
        games.append({"home": home, "away": away, "commence_time": _iso_utc(kick), "books": books})
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "fetched_at": fetched_at,
        "degraded": False,
        "canonical": True,
        "source": "build",
        "games": games,
    }


def canonicalize_overlay(overlay: dict) -> dict:
    """Copy of a Worker overlay with team codes and canonical: true; unmapped games are dropped."""
    games = []
    for g in overlay.get("games") or []:
        if not isinstance(g, dict):
            continue
        home = g.get("home") if g.get("home") in schema.TEAM_SET else schema.code_from_name(g.get("home"))
        away = g.get("away") if g.get("away") in schema.TEAM_SET else schema.code_from_name(g.get("away"))
        if home is None or away is None or home == away:
            continue
        games.append(
            {
                "home": home,
                "away": away,
                "commence_time": g.get("commence_time"),
                "books": [dict(b) for b in (g.get("books") or []) if isinstance(b, dict)],
            }
        )
    out = {k: v for k, v in overlay.items() if k != "games"}
    out["canonical"] = True
    out["games"] = games
    return out


def _is_final(game: dict) -> bool:
    return game.get("status") == "final" or game.get("played") is True


def match_overlay(games: list[dict], overlay: dict) -> dict:
    """{game_id: overlay_game} per CONTRACT section 3 / D6.

    Requires `overlay.canonical == true` (else nothing matches). For each overlay game the
    candidates are non-final games with the same home AND away codes (orientation matters),
    narrowed to |commence_time - kickoff_utc| <= 36h. Exactly one candidate -> match; zero or
    more than one -> no match. If two overlay games claim the same game it is also dropped.
    """
    if overlay.get("canonical") is not True:
        return {}
    index: dict[tuple, list] = {}
    for g in games:
        if _is_final(g):
            continue
        kick = _parse_utc(g.get("kickoff_utc"))
        if kick is None:
            continue
        index.setdefault((g["home"], g["away"]), []).append((g["game_id"], kick))

    matched: dict = {}
    contested: set = set()
    for og in overlay.get("games") or []:
        start = _parse_utc(og.get("commence_time"))
        if start is None:
            continue
        cands = [gid for gid, kick in index.get((og.get("home"), og.get("away")), []) if abs(start - kick) <= MATCH_WINDOW]
        if len(cands) != 1:
            continue
        gid = cands[0]
        if gid in matched:
            contested.add(gid)
        else:
            matched[gid] = og
    for gid in contested:
        matched.pop(gid, None)
    return matched


# ----------------------------------------------------------------------------
# The single official Odds API call (D6)
# ----------------------------------------------------------------------------


def _header_int(headers, name: str) -> int | None:
    value = headers.get(name) if headers is not None else None
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "***") if api_key else text


def fetch_official_odds(api_key: str, url: str = ODDS_API_URL, timeout: float = ODDS_API_TIMEOUT, _urlopen=None, _now=None) -> dict | None:
    """Call The Odds API ONCE (3 credits). Returns None on any failure so the build falls back to CSV.

    Returns {"payload": <raw list>, "fetched_at": ISO UTC, "requests_remaining": int|None,
    "requests_used": int|None}. The key appears only in the request URL: it is never logged,
    never stored in the returned dict, and is redacted from any error text. The
    x-requests-remaining header is logged to stderr. `_urlopen`/`_now` are test injection points.
    """
    if not api_key:
        return None
    opener = _urlopen or urllib.request.urlopen
    now = _now or (lambda: datetime.now(timezone.utc))
    params = dict(ODDS_API_PARAMS)
    params["apiKey"] = api_key
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": "gridiron-odds/1.0 (stdlib urllib)", "Accept": "application/json"})
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            headers = resp.headers
            raw = resp.read()
        if status != 200:
            print("odds api: upstream status %s; falling back to CSV lines" % status, file=sys.stderr)
            return None
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # network, timeout, HTTP error, bad JSON
        print("odds api: call failed (%s: %s); falling back to CSV lines" % (type(exc).__name__, _redact(str(exc), api_key)), file=sys.stderr)
        return None
    if not isinstance(payload, list):
        print("odds api: unexpected payload type %s; falling back to CSV lines" % type(payload).__name__, file=sys.stderr)
        return None
    remaining = _header_int(headers, "x-requests-remaining")
    used = _header_int(headers, "x-requests-used")
    print("odds api: ok, %d games, x-requests-remaining=%s x-requests-used=%s" % (len(payload), remaining, used), file=sys.stderr)
    return {"payload": payload, "fetched_at": _iso_utc(now()), "requests_remaining": remaining, "requests_used": used}
