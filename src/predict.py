"""Official per-game prediction: pick, probabilities, tier, scores, factors, rationale.

Implements docs/CONTRACT.md section 2.6 ("official") and 2.9 ("factors") plus
DECISIONS D2 (QB wording), D3 (blend), D4 (tiers), D5 (scores and total
source), D8 (EV label threshold lives in schema), D17 (edge chip).

Public surface (CONTRACT 6.3):
    blend(p_model, p_market, w) -> float
    expected_scores(total, model_spread) -> (expected_home, expected_away)
    display_scores(expected_home, expected_away, favorite_is_home) -> (int, int)
    choose_total(market, game, params) -> (total_used, total_source)
    rationale(factors, official_partial, game, names) -> (keys, sentences)
    edge_chip(factors, names) -> dict | None
    predict_game(game, pregame, market, params, context) -> {"official", "availability"}

Everything is pure. Every number that appears in a rationale sentence is a
value in `factors` or `official` (or a rounding / absolute value / percent /
difference of two such values); tests/test_predict.py enforces that.

The small American-odds helpers here (`decimal_profit`, `estimated_ev`) use the
same formulas as src/market.py; they are duplicated so this module has no
import-order dependency on it.
"""
from __future__ import annotations

import math

from . import qb, schema

# Rationale triggers (CONTRACT 2.6); the values live in schema so validate_model can enforce
# the "emitted iff the trigger fires" rule with the same numbers.
QB_GAP_THRESHOLD = schema.RATIONALE_QB_GAP  # Elo points: QB sentence when |qb_elo| >= this
REST_GAP_THRESHOLD = schema.RATIONALE_REST_GAP  # days: rest sentence / edge reason when |rest_diff| >= this
FORM_THRESHOLD = schema.RATIONALE_FORM_MOVE  # Elo points over last 3 games: form sentence / edge reason
MARKET_SENTENCE_THRESHOLD = schema.RATIONALE_MARKET_GAP  # |p_model - p_market| on the pick side


# ----------------------------------------------------------------------------
# Rounding and formatting (display only; model.json keeps full precision)
# ----------------------------------------------------------------------------


def round_half_away(x: float) -> int:
    """Round half away from zero (CONTRACT 0): 2.5 -> 3, -2.5 -> -3, 2.4 -> 2."""
    if x >= 0:
        return int(math.floor(x + 0.5))
    return -int(math.floor(-x + 0.5))


def pct(p: float) -> int:
    """Whole-number percent of a probability, round half away from zero."""
    return round_half_away(100.0 * p)


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11 -> '11th', 21 -> '21st'."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


def fmt_num(x: float, decimals: int = 1) -> str:
    """Format a number for prose: integers without a decimal point, else up to
    `decimals` places with trailing zeros stripped (1.60 -> '1.6', 40.0 -> '40')."""
    s = ("%." + str(decimals) + "f") % x
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


# ----------------------------------------------------------------------------
# Team display names
# ----------------------------------------------------------------------------


def team_label(code: str, other: str | None, names: dict) -> str:
    """Display name for a team: its city ("Kansas City"); the full name when the
    opponent shares the city ("New York Jets" vs "New York Giants")."""
    meta = names.get(code) or {}
    city = meta.get("city") or code
    if other is not None:
        other_meta = names.get(other) or {}
        if other_meta.get("city") == city and meta.get("name"):
            return "%s %s" % (city, meta["name"])
    return city


def _labels(game: dict | None, names: dict) -> dict:
    """{"home": label, "away": label}. `names` is either {code: team_meta}
    (needs `game` for the codes) or already {"home": str, "away": str}."""
    if "home" in names and "away" in names and isinstance(names["home"], str):
        return {"home": names["home"], "away": names["away"]}
    if game is None:
        raise ValueError("names must be {'home': label, 'away': label} when no game is given")
    return {
        "home": team_label(game["home"], game["away"], names),
        "away": team_label(game["away"], game["home"], names),
    }


# ----------------------------------------------------------------------------
# Pure math
# ----------------------------------------------------------------------------


def blend(p_model: float, p_market: float | None, w: float) -> float:
    """D3: w*p_model + (1-w)*p_market; the model alone when there is no market."""
    if p_market is None:
        return p_model
    return w * p_model + (1.0 - w) * p_market


def expected_scores(total: float, spread: float) -> tuple[float, float]:
    """D5: (expected_home, expected_away), unrounded; they sum to `total` and
    differ by `spread` (home-positive). The build passes the BLENDED spread so the
    score always agrees with the pick."""
    return (total + spread) / 2.0, (total - spread) / 2.0


def spread_from_prob(p_home: float, points_per_elo: float) -> float:
    """D5 (amended 2026-10-09): invert the Elo curve so a home win probability maps
    back to a home-positive point spread: d = 400*log10(p/(1-p)), spread = d / points_per_elo.
    Clamped so p in {0, 1} cannot overflow."""
    p = min(max(float(p_home), 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(p / (1.0 - p)) / float(points_per_elo)


def display_scores(expected_home: float, expected_away: float, favorite_is_home: bool) -> tuple[int, int]:
    """D5 display integers: round half away from zero; if equal, +1 to the favorite.
    Never below 0."""
    home = max(0, round_half_away(expected_home))
    away = max(0, round_half_away(expected_away))
    if home == away:
        if favorite_is_home:
            home += 1
        else:
            away += 1
    return home, away


def choose_total(market: dict | None, game: dict, params: dict) -> tuple[float, str]:
    """D5 priority: market consensus median total -> CSV total_line -> league average.

    A csv-sourced market's consensus_total IS the CSV total_line, so its source
    is reported as "csv"; only an odds_api market yields "odds_api"."""
    if market is not None and market.get("consensus_total") is not None:
        source = "odds_api" if market.get("source") == "odds_api" else "csv"
        return float(market["consensus_total"]), source
    if game.get("total_line") is not None:
        return float(game["total_line"]), "csv"
    return float(params["score"]["league_average_total"]), "league_average"


def decimal_profit(american: int) -> float:
    """Decimal profit per $1 of an American price: +150 -> 1.5, -120 -> 0.8333."""
    if abs(american) < 100:
        raise ValueError("American odds need |x| >= 100, got %r" % (american,))
    if american > 0:
        return american / 100.0
    return 100.0 / (-american)


def estimated_ev(p: float, american: int) -> float:
    """Model-estimated EV per $1 at this price: p*payout - (1-p)."""
    return p * decimal_profit(american) - (1.0 - p)


# ----------------------------------------------------------------------------
# Prices
# ----------------------------------------------------------------------------


def prices_for_pick(market: dict | None, pick_is_home: bool, pick: str, books: list) -> dict:
    """{book: price-or-None} keyed by exactly `books` (CONTRACT 2.6 best_prices).
    All None when there is no market or the market is CSV-sourced."""
    out = {b: None for b in books}
    if market is None or market.get("source") != "odds_api":
        return out
    by_key = {b["key"]: b for b in market.get("books", [])}
    ml_key = "home_ml" if pick_is_home else "away_ml"
    for book in books:
        entry = by_key.get(book)
        if entry is not None and entry.get(ml_key) is not None:
            out[book] = {"book": book, "side": pick, "american": int(entry[ml_key])}
    return out


def best_of_prices(prices: dict, books: list) -> dict | None:
    """Highest decimal profit among the pick's prices; ties go to the first in `books`."""
    best = None
    best_profit = None
    for book in books:
        price = prices.get(book)
        if price is None:
            continue
        profit = decimal_profit(price["american"])
        if best is None or profit > best_profit:
            best, best_profit = price, profit
    return best


def csv_best_price(market: dict, pick_is_home: bool, pick: str) -> dict | None:
    """The CSV closing moneyline of the pick as a price object (source "csv")."""
    ml_key = "home_ml" if pick_is_home else "away_ml"
    for entry in market.get("books", []):
        if entry.get("key") == schema.CSV_BOOK_KEY and entry.get(ml_key) is not None:
            return {"book": schema.CSV_BOOK_KEY, "side": pick, "american": int(entry[ml_key])}
    return None


# ----------------------------------------------------------------------------
# Factors
# ----------------------------------------------------------------------------


def _qb_factor(qb_id, qb_name, q, starts, team: str, context: dict, today: str | None = None,
               params: dict | None = None) -> dict | None:
    """CONTRACT 2.9 qb_home/qb_away. None when no starter is listed (D2).

    ``usual_starter_q`` is the usual starter's Q as it would enter a game dated ``today``
    (lazy inactivity decay via qb.effective_q, the same rule the listed starter gets in
    elo._pregame) so the "rates N points above/below their usual starter" comparison is
    like for like. Without ``today``/``params`` the stored Q is used as is."""
    if not qb_id:
        return None
    qb_state = (context.get("qb") or {}).get(qb_id) or {}
    if q is None:  # QB term disabled or unknown: contributes 0 (D2)
        q = 0.0
    if starts is None:
        starts = int(qb_state.get("starts", 0))
    usual = (context.get("usual_starter") or {}).get(team) or None
    usual_id = usual.get("id") if usual else None
    usual_name = usual.get("name") if usual else None
    usual_q = None
    if usual_id is not None:
        if usual_id == qb_id:
            usual_q = float(q)
        else:
            usual_state = (context.get("qb") or {}).get(usual_id)
            if usual_state is not None and usual_state.get("q") is not None:
                if today is not None and params is not None:
                    usual_q = float(qb.effective_q(usual_state, today, params))
                else:
                    usual_q = float(usual_state["q"])
    return {
        "id": qb_id,
        "name": qb_name or qb_state.get("name") or qb_id,
        "q": float(q),
        "starts": int(starts),
        "first_start": int(starts) == 0,
        "usual_starter_id": usual_id,
        "usual_starter_name": usual_name,
        "usual_starter_q": usual_q,
        "differs_from_usual": usual_id is not None and usual_id != qb_id,
        "source": "nflverse_listed",
    }


def build_factors(game: dict, pregame: dict, params: dict, context: dict,
                  p_home_model: float, p_home_market: float | None, pick_is_home: bool) -> dict:
    """CONTRACT 2.9: exactly the 19 factor keys, computed from the replay's pregame
    numbers, the game row and the build context."""
    ppe = float(params["score"]["points_per_elo"])
    elo_home = float(pregame["elo_home"])
    elo_away = float(pregame["elo_away"])
    hfa_elo = 0.0 if game.get("neutral") else float(pregame.get("hfa_elo", params["elo"]["hfa"]))
    rest_home = game.get("home_rest")
    rest_away = game.get("away_rest")
    rest_diff = None if rest_home is None or rest_away is None else int(rest_home) - int(rest_away)
    rest_elo = 0.0 if rest_diff is None else float(params["elo"]["rest_per_day"]) * rest_diff
    today = game.get("league_date")
    qb_home = _qb_factor(game.get("home_qb_id"), game.get("home_qb_name"), pregame.get("q_home"),
                         pregame.get("qb_home_starts"), game["home"], context, today, params)
    qb_away = _qb_factor(game.get("away_qb_id"), game.get("away_qb_name"), pregame.get("q_away"),
                         pregame.get("qb_away_starts"), game["away"], context, today, params)
    q_h = qb_home["q"] if qb_home else 0.0
    q_a = qb_away["q"] if qb_away else 0.0
    gap_home = None if p_home_market is None else p_home_model - p_home_market
    form = context.get("form") or {}
    ranks = context["ranks"]
    return {
        "team_elo_home": elo_home,
        "team_elo_away": elo_away,
        "elo_gap_home": elo_home - elo_away,
        "home_rank": int(ranks[game["home"]]),
        "away_rank": int(ranks[game["away"]]),
        "hfa_elo": hfa_elo,
        "hfa_spread_pts": hfa_elo / ppe,
        "rest_home": None if rest_home is None else int(rest_home),
        "rest_away": None if rest_away is None else int(rest_away),
        "rest_diff": rest_diff,
        "rest_elo": rest_elo,
        "qb_home": qb_home,
        "qb_away": qb_away,
        "qb_elo": q_h - q_a,
        "form_home_3": form.get(game["home"]),
        "form_away_3": form.get(game["away"]),
        "d_total": (elo_home - elo_away) + hfa_elo + rest_elo + (q_h - q_a),
        "model_market_gap_home": gap_home,
        "model_market_gap_pick": None if gap_home is None else (gap_home if pick_is_home else -gap_home),
    }


# ----------------------------------------------------------------------------
# Rationale (the Why? button)
# ----------------------------------------------------------------------------


def _rating_sentence(f: dict, lab: dict) -> str:
    gap = f["elo_gap_home"]
    n = round_half_away(abs(gap))
    if n == 0:
        return "%s and %s rate even on Elo, %s in the league against %s." % (
            lab["home"], lab["away"], ordinal(f["home_rank"]), ordinal(f["away_rank"]))
    if gap > 0:
        lead, trail, r1, r2 = lab["home"], lab["away"], f["home_rank"], f["away_rank"]
    else:
        lead, trail, r1, r2 = lab["away"], lab["home"], f["away_rank"], f["home_rank"]
    return "%s rates %d Elo points above %s, %s in the league against %s." % (
        lead, n, trail, ordinal(r1), ordinal(r2))


def _home_field_sentence(f: dict) -> str:
    return "Home field is worth about %s points, or %s points on the spread." % (
        fmt_num(f["hfa_elo"], 1), fmt_num(f["hfa_spread_pts"], 1))


def _qb_side_clause(qb: dict, label: str) -> str | None:
    """Per-side QB clause: first start (D2 wording) or a change from the usual starter."""
    if qb["first_start"]:
        n = round_half_away(abs(qb["q"]))
        rel = "below" if qb["q"] < 0 else "above"
        return "%s's listed starter, %s, is making their first start on record, rated %d points %s an average starter" % (
            label, qb["name"], n, rel)
    if qb["differs_from_usual"]:
        if qb["usual_starter_q"] is not None and qb["usual_starter_name"]:
            diff = qb["q"] - qb["usual_starter_q"]
            n = round_half_away(abs(diff))
            rel = "below" if diff < 0 else "above"
            return "%s's listed starter, %s, rates %d points %s their usual starter, %s" % (
                label, qb["name"], n, rel, qb["usual_starter_name"])
        return "%s's listed starter, %s, differs from the usual starter" % (label, qb["name"])
    return None


def qb_sentence(f: dict, lab: dict) -> str | None:
    """None unless both starters are listed (D2) and a trigger fires: a starter
    change, a first start, or a listed-starter gap of 30+ Elo points."""
    qh, qa = f["qb_home"], f["qb_away"]
    if qh is None or qa is None:
        return None
    clauses = [c for c in (_qb_side_clause(qh, lab["home"]), _qb_side_clause(qa, lab["away"])) if c]
    if clauses:
        return "; ".join(clauses) + "."
    gap = f["qb_elo"]
    if abs(gap) >= QB_GAP_THRESHOLD:
        side, qb = ("home", qh) if gap > 0 else ("away", qa)
        return "%s's listed starter, %s, rates %d points above the other listed starter." % (
            lab[side], qb["name"], round_half_away(abs(gap)))
    return None


def rest_sentence(f: dict, lab: dict) -> str | None:
    if f["rest_diff"] is None or abs(f["rest_diff"]) < REST_GAP_THRESHOLD:
        return None
    if f["rest_diff"] > 0:
        more, less, dm, dl = lab["home"], lab["away"], f["rest_home"], f["rest_away"]
    else:
        more, less, dm, dl = lab["away"], lab["home"], f["rest_away"], f["rest_home"]
    return "%s has %d days of rest to %s's %d, worth %s Elo points to %s." % (
        more, dm, less, dl, fmt_num(abs(f["rest_elo"]), 1), more)


def _form_clause(move: float | None, label: str) -> str | None:
    if move is None or abs(move) < FORM_THRESHOLD:
        return None
    verb = "lost" if move < 0 else "gained"
    return "%s has %s %d points of rating over its last three games" % (label, verb, round_half_away(abs(move)))


def form_sentence(f: dict, lab: dict) -> str | None:
    clauses = [c for c in (_form_clause(f["form_home_3"], lab["home"]), _form_clause(f["form_away_3"], lab["away"])) if c]
    return "; ".join(clauses) + "." if clauses else None


def market_sentence(f: dict, official: dict, lab: dict, pick_is_home: bool) -> str | None:
    gap = f["model_market_gap_pick"]
    if gap is None or abs(gap) < MARKET_SENTENCE_THRESHOLD:
        return None
    p_model = official["p_home_model"] if pick_is_home else 1.0 - official["p_home_model"]
    p_market = official["p_home_market"] if pick_is_home else 1.0 - official["p_home_market"]
    relation = "closer than" if p_market < p_model else "further than"
    return "The books have %s at %d%%, %s the model's %d%%; the gap may reflect information this model lacks." % (
        lab["home" if pick_is_home else "away"], pct(p_market), relation, pct(p_model))


def verdict_sentence(official: dict, lab: dict, pick_is_home: bool) -> str:
    """The closing sentence. Both the pick and the displayed score follow the blend
    (D3/D5 as amended), so the pick is always the higher score; the else-branch is a
    guard that can only fire if that invariant is ever broken upstream."""
    side = "home" if pick_is_home else "away"
    other = "away" if pick_is_home else "home"
    x = official["display_home"] if pick_is_home else official["display_away"]
    y = official["display_away"] if pick_is_home else official["display_home"]
    if x > y:
        tail = "a %s pick, predicted %d to %d." % (official["tier"], x, y)
    else:
        tail = "a %s pick, though the model alone scores it %s %d to %d." % (official["tier"], lab[other], y, x)
    p = pct(official["p_pick_blend"])
    if official.get("p_home_market") is None:
        return "No market line yet, so this is the model alone: %s at %d%%, %s" % (lab[side], p, tail)
    return "Blended, that comes to %s at %d%%: %s" % (lab[side], p, tail)


def rationale(factors: dict, official_partial: dict, game: dict, names: dict) -> tuple[list[str], list[str]]:
    """(keys, sentences) for the Why? button (CONTRACT 2.6).

    `official_partial` needs pick, p_home_model, p_home_market, p_pick_blend, tier,
    display_home, display_away. `names` is {code: team_meta} (or {"home","away"} labels).
    Every key is emitted iff its trigger fires (no budget: a factor the model used is
    never dropped), in the fixed contract order rating, home_field, quarterback, rest,
    form, market, verdict; 2 (neutral site, nothing else) to 7 sentences."""
    lab = _labels(game, names)
    f = factors
    pick_is_home = official_partial["pick"] == game["home"]
    middle = []
    if not game.get("neutral"):
        middle.append(("home_field", _home_field_sentence(f)))
    for key, text in (("quarterback", qb_sentence(f, lab)), ("rest", rest_sentence(f, lab)), ("form", form_sentence(f, lab)),
                      ("market", market_sentence(f, official_partial, lab, pick_is_home))):
        if text is not None:
            middle.append((key, text))
    keys = ["rating"] + [k for k, _ in middle] + ["verdict"]
    sentences = [_rating_sentence(f, lab)] + [t for _, t in middle] + [verdict_sentence(official_partial, lab, pick_is_home)]
    return keys, sentences


# ----------------------------------------------------------------------------
# Edge chip (D17)
# ----------------------------------------------------------------------------


def edge_chip(factors: dict, names: dict) -> dict | None:
    """Amber chip when |model - market| >= EDGE_THRESHOLD on the pick side, naming
    only a measured factor. `names` = {"home": label, "away": label}."""
    gap = factors["model_market_gap_pick"]
    if gap is None or abs(gap) < schema.EDGE_THRESHOLD:
        return None
    lab = _labels(None, names)
    f = factors
    for side in ("home", "away"):
        qb = f["qb_" + side]
        if qb is not None and qb["differs_from_usual"]:
            return {"flag": True, "reason_key": "starter_change",
                    "reason_text": "%s's listed starter differs from the usual starter" % lab[side]}
    if f["rest_diff"] is not None and abs(f["rest_diff"]) >= REST_GAP_THRESHOLD:
        side = "home" if f["rest_diff"] > 0 else "away"
        return {"flag": True, "reason_key": "rest_gap",
                "reason_text": "%s has %d more days of rest" % (lab[side], abs(f["rest_diff"]))}
    moves = [(abs(f["form_%s_3" % s] or 0.0), s) for s in ("home", "away")]
    size, side = max(moves)
    if size >= FORM_THRESHOLD:
        return {"flag": True, "reason_key": "form_move",
                "reason_text": "%s's rating moved %d points in the last three games" % (lab[side], round_half_away(size))}
    return {"flag": True, "reason_key": "unknown", "reason_text": "may reflect information this model lacks"}


# ----------------------------------------------------------------------------
# The official object
# ----------------------------------------------------------------------------


def predict_game(game: dict, pregame: dict, market: dict | None, params: dict, context: dict) -> dict:
    """Build {"official": CONTRACT 2.6, "availability": CONTRACT 2.5} for one game.

    game     normalized game (CONTRACT 1)
    pregame  replay["pregame"][game_id]
    market   CONTRACT 2.8 object or None
    params   CONTRACT 2.3
    context  {"ranks", "names", "form", "usual_starter", "qb", "books"}
    """
    books = list(context.get("books") or params["books"])
    p_home_model = float(pregame["p_home"])
    p_home_market = None if market is None else market.get("consensus_p_home")
    p_home_blend = blend(p_home_model, p_home_market, float(params["blend"]["model_weight"]))
    pick_is_home = p_home_blend >= 0.5
    pick = game["home"] if pick_is_home else game["away"]
    p_pick_blend = p_home_blend if pick_is_home else 1.0 - p_home_blend
    tier = schema.tier_for(p_pick_blend, params["tier_bounds"])

    factors = build_factors(game, pregame, params, context, p_home_model, p_home_market, pick_is_home)
    model_spread = factors["d_total"] / float(params["score"]["points_per_elo"])
    blend_spread = spread_from_prob(p_home_blend, params["score"]["points_per_elo"])
    total_used, total_source = choose_total(market, game, params)
    expected_home, expected_away = expected_scores(total_used, blend_spread)
    display_home, display_away = display_scores(expected_home, expected_away, pick_is_home)

    best_prices = prices_for_pick(market, pick_is_home, pick, books)
    if market is None:
        best_price = None
    elif market.get("source") == "odds_api":
        best_price = best_of_prices(best_prices, books)
    else:
        best_price = csv_best_price(market, pick_is_home, pick)
    ev = None if best_price is None else estimated_ev(p_pick_blend, best_price["american"])

    official = {
        "pick": pick,
        "p_home_model": p_home_model,
        "p_home_market": p_home_market,
        "p_home_blend": p_home_blend,
        "p_pick_blend": p_pick_blend,
        "tier": tier,
        "model_spread": model_spread,
        "blend_spread": blend_spread,
        "expected_home": expected_home,
        "expected_away": expected_away,
        "display_home": display_home,
        "display_away": display_away,
        "total_used": total_used,
        "total_source": total_source,
        "market": market,
        "best_prices": best_prices,
        "best_price": best_price,
        "estimated_ev_per_dollar": ev,
        "factors": factors,
    }
    names = context["names"]
    keys, sentences = rationale(factors, official, game, names)
    official["rationale"] = keys
    official["rationale_sentences"] = sentences
    official["edge"] = None if market is None else edge_chip(factors, _labels(game, names))
    official["parlay_eligible"] = market is not None and p_pick_blend >= schema.PARLAY_MIN_P and best_price is not None

    availability = {
        "starter_home": bool(game.get("home_qb_id")),
        "starter_away": bool(game.get("away_qb_id")),
        "market": market is not None,
        "csv_lines": game.get("home_moneyline") is not None and game.get("away_moneyline") is not None,
    }
    return {"official": official, "availability": availability}
