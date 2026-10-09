"""Quarterback adjustment helpers (D2). Pure functions; state lives in elo.replay.

Every QB is keyed by the nflverse ``*_qb_id`` — names are display only (the CSV maps
one display name to several ids, e.g. Kyler Murray). Each QB carries ``q`` in Elo points
(value over an average starter).

    new_q(params)                                   -> float   Q0 for a first start on record
    qb_inactivity(q, days_since_last_start, penalty) -> float   lazy decay, pure in elapsed days
    season_regress(q, annual_reversion)             -> float   between-season regression toward 0
    days_between(iso_a, iso_b)                      -> int     calendar days from a to b
    effective_q(state, today, params)               -> float   q after lazy inactivity decay
    usual_starter(starts, window)                   -> dict|None  most frequent starter id, ties -> most recent

``starts`` for usual_starter is the team's chronological list of ``{"id", "name"}`` per played
game (blank-starter games are skipped by the caller).
"""
from __future__ import annotations

from datetime import date


def new_q(params: dict) -> float:
    """Q for a starter with no history (``params.qb.new_qb``, -50 by default)."""
    return float(params["qb"]["new_qb"])


def qb_inactivity(q: float, days_since_last_start: int, penalty: float) -> float:
    """D2 inactivity decay: ``penalty`` points per COMPLETED 365-day gap since the last start.

    Pure in the elapsed days, so applying it lazily before the next game yields the same
    value no matter how many builds ran in between. Never compounds with season regression
    (that is applied separately at rollover).
    """
    if days_since_last_start is None or days_since_last_start < 365:
        return q
    return q - penalty * (days_since_last_start // 365)


def season_regress(q: float, annual_reversion: float) -> float:
    """Between seasons Q moves ``annual_reversion`` of the way toward 0 (0.2 -> Q *= 0.8)."""
    return q * (1.0 - annual_reversion)


def days_between(iso_a: str, iso_b: str) -> int:
    """Calendar days from date ``iso_a`` to ``iso_b`` (both 'YYYY-MM-DD'); negative if b < a."""
    return date.fromisoformat(iso_b).toordinal() - date.fromisoformat(iso_a).toordinal()


def effective_q(state: dict | None, today: str, params: dict) -> float:
    """The Q a listed starter brings to a game dated ``today``.

    ``state`` is the replay's per-id record ``{"q", "last_start_date", ...}`` or None for a
    first start on record (-> ``new_q``). Lazy inactivity decay is applied here, before the
    game, and nowhere else.
    """
    if state is None:
        return new_q(params)
    gap = days_between(state["last_start_date"], today) if state.get("last_start_date") else 0
    return qb_inactivity(state["q"], gap, float(params["qb"]["inactivity_penalty"]))


def usual_starter(starts: list[dict], window: int) -> dict | None:
    """D2 usual starter: most frequent starter id over the last ``window`` entries of ``starts``.

    Ties go to the id that started most recently. Returns ``{"id", "name"}`` (the name as
    listed at that id's most recent start) or None when there are no starts.
    """
    recent = starts[-window:] if window > 0 else []
    if not recent:
        return None
    counts: dict[str, int] = {}
    last_index: dict[str, int] = {}
    names: dict[str, str] = {}
    for i, s in enumerate(recent):
        counts[s["id"]] = counts.get(s["id"], 0) + 1
        last_index[s["id"]] = i
        names[s["id"]] = s["name"]
    best = max(counts, key=lambda k: (counts[k], last_index[k]))
    return {"id": best, "name": names[best]}
