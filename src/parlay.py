"""Parlay ranking: the Python oracle for web/app.js (DECISIONS D9, CONTRACT 6.4).

Per-book enumeration: for one book, legs are official picks with
p_pick_blend >= 0.55 that have an h2h price AT THAT BOOK. Every 2-5 leg
combination (no two legs from one game) is a candidate priced with that book
only. hit = product of p; decimal_odds = product of (1 + profit); payout =
decimal_odds - 1; ev = hit*payout - (1-hit); fair_american from 1/hit. Legs are
treated as independent (the page says so).

Public surface:
    fair_american(p) -> int
    combo_stats(legs) -> {"hit","decimal_odds","payout","ev","fair_american"}
    eligible_legs(games, book, min_p=0.55) -> list[leg]
    rank_parlays(legs_by_book, min_legs=2, max_legs=5, top=None) -> list[candidate]
    pin_best(ranked, sizes=(2, 3, 4)) -> {n_legs: candidate | None}
    build_parlays(legs_by_book, min_legs=2, max_legs=5, top=5) -> {"pinned", "ranked", "n_candidates"}

leg = {"game_id", "side", "p", "american"}
candidate = {"book", "n_legs", "game_ids", "legs", "hit", "decimal_odds", "payout", "ev", "fair_american"}

tests/fixtures/parlay_fixture.json holds an input and this module's exact output
so the JavaScript port can be checked against it.
"""
from __future__ import annotations

import math
from itertools import combinations

from . import schema


def _round_half_away(x: float) -> int:
    if x >= 0:
        return int(math.floor(x + 0.5))
    return -int(math.floor(-x + 0.5))


def decimal_profit(american: int) -> float:
    """+150 -> 1.5, -120 -> 0.8333...; ValueError if |x| < 100."""
    if abs(american) < 100:
        raise ValueError("American odds need |x| >= 100, got %r" % (american,))
    return american / 100.0 if american > 0 else 100.0 / (-american)


def fair_american(p: float) -> int:
    """American odds equivalent to a probability p (no vig): 0.36 -> +178, 0.6 -> -150.
    The int is rounded half away from zero; |result| >= 100 always."""
    if not 0.0 < p < 1.0:
        raise ValueError("probability must be strictly between 0 and 1, got %r" % (p,))
    decimal = 1.0 / p
    if decimal >= 2.0:
        return max(100, _round_half_away((decimal - 1.0) * 100.0))
    return min(-100, -_round_half_away(100.0 / (decimal - 1.0)))


def combo_stats(legs: list[dict]) -> dict:
    """hit, decimal odds, payout (profit per $1), EV per $1 and fair American odds
    for a set of independent legs. Products are taken in list order."""
    hit = 1.0
    decimal_odds = 1.0
    for leg in legs:
        hit *= float(leg["p"])
        decimal_odds *= 1.0 + decimal_profit(int(leg["american"]))
    payout = decimal_odds - 1.0
    ev = hit * payout - (1.0 - hit)
    return {
        "hit": hit,
        "decimal_odds": decimal_odds,
        "payout": payout,
        "ev": ev,
        "fair_american": fair_american(hit),
    }


def eligible_legs(games: list[dict], book: str, min_p: float = schema.PARLAY_MIN_P) -> list[dict]:
    """Legs at `book` from model.json-style games: the official pick when
    p_pick_blend >= min_p and best_prices[book] is present. Input order is kept."""
    legs = []
    for g in games:
        o = g["official"]
        if o["p_pick_blend"] < min_p:
            continue
        price = (o.get("best_prices") or {}).get(book)
        if price is None:
            continue
        legs.append({
            "game_id": g["game_id"],
            "side": o["pick"],
            "p": o["p_pick_blend"],
            "american": int(price["american"]),
        })
    return legs


def _candidate(book: str, legs: list[dict]) -> dict:
    stats = combo_stats(legs)
    return {
        "book": book,
        "n_legs": len(legs),
        "game_ids": [leg["game_id"] for leg in legs],
        "legs": [dict(leg) for leg in legs],
        "hit": stats["hit"],
        "decimal_odds": stats["decimal_odds"],
        "payout": stats["payout"],
        "ev": stats["ev"],
        "fair_american": stats["fair_american"],
    }


def rank_parlays(legs_by_book: dict, min_legs: int = 2, max_legs: int = 5, top: int | None = None) -> list[dict]:
    """All (book, legs) candidates sorted by ev desc, hit desc, book, game_ids.

    One book per candidate; a leg missing at a book simply is not in that book's
    list, so no candidate can mix books. Combinations containing two legs from
    the same game are skipped. `top` truncates after sorting."""
    if min_legs < 1 or max_legs < min_legs:
        raise ValueError("need 1 <= min_legs <= max_legs")
    candidates = []
    for book in sorted(legs_by_book):
        legs = list(legs_by_book[book])
        for n in range(min_legs, min(max_legs, len(legs)) + 1):
            for combo in combinations(legs, n):
                ids = [leg["game_id"] for leg in combo]
                if len(set(ids)) != len(ids):
                    continue
                candidates.append(_candidate(book, list(combo)))
    candidates.sort(key=lambda c: (-c["ev"], -c["hit"], c["book"], c["game_ids"]))
    if top is not None:
        candidates = candidates[:top]
    return candidates


def pin_best(ranked: list[dict], sizes: tuple = (2, 3, 4)) -> dict:
    """{n_legs: best candidate of that size (first in `ranked`) or None}."""
    pinned = {n: None for n in sizes}
    for c in ranked:
        n = c["n_legs"]
        if n in pinned and pinned[n] is None:
            pinned[n] = c
    return pinned


def build_parlays(legs_by_book: dict, min_legs: int = 2, max_legs: int = 5, top: int | None = 5) -> dict:
    """The parlay panel's data: {"pinned": {2,3,4: candidate|None}, "ranked": top N
    candidates, "n_candidates": total enumerated}. Pins come from the FULL
    ranking, so a pinned 4-leg can sit outside the top-N list."""
    ranked_all = rank_parlays(legs_by_book, min_legs, max_legs, top=None)
    return {
        "pinned": pin_best(ranked_all),
        "ranked": ranked_all if top is None else ranked_all[:top],
        "n_candidates": len(ranked_all),
    }
