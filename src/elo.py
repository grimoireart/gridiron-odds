"""Elo ratings with home field, rest, margin of victory, preseason regression and a QB term.

Pure math (SPEC "Elo model" + D1 tie_multiplier + D2 QB keyed by id) and one causal replay.
No file or UI logic, no global state. Surface per docs/CONTRACT.md section 6.1:

    win_prob(d)                                     -> float
    mov_multiplier(margin, d_fav, tie_multiplier)   -> float
    rollover(rating, reversion)                     -> float
    qb_inactivity(q, days_since_last_start, penalty) -> float   (re-exported from src/qb.py)
    pregame_d(elo_home, elo_away, hfa_elo, rest_elo, q_home, q_away) -> float
    replay(games, params, initial=None, extra=None) -> ReplayResult (dict; see docstring)
                                                       extra = {game_id: (extra_home_elo, extra_away_elo)}
                                                       optional per-game rating terms (D18.6 injuries);
                                                       absent => byte-identical to the plain replay

Conventions: result s = 1 home win / 0 away win / 0.5 tie; d is from the home side; d_fav is d
from the winner's side (|d| for a tie); delta is added to home and subtracted from away.
Every number is kept at full float precision; nothing is rounded here.
"""
from __future__ import annotations

import math

from . import qb as _qb
from .qb import qb_inactivity  # noqa: F401  (contract name lives on elo too)
from .schema import TEAMS, canonical_code

INITIAL_RATING = 1500.0


def win_prob(d: float) -> float:
    """Home win probability from the rating difference d (Elo logistic, 400-point scale)."""
    return 1.0 / (1.0 + 10.0 ** (-d / 400.0))


def mov_multiplier(margin: int, d_fav: float, tie_multiplier: float) -> float:
    """Margin-of-victory multiplier: ln(m+1)*2.2/(0.001*d_fav+2.2); a tie (m == 0) returns tie_multiplier (D1)."""
    m = abs(margin)
    if m == 0:
        return float(tie_multiplier)
    return math.log(m + 1) * 2.2 / (0.001 * d_fav + 2.2)


def rollover(rating: float, reversion: float) -> float:
    """Preseason regression: 1500 + (r - 1500) * (1 - reversion). REV=0.4 takes 1600 to 1560."""
    return INITIAL_RATING + (rating - INITIAL_RATING) * (1.0 - reversion)


def pregame_d(elo_home: float, elo_away: float, hfa_elo: float, rest_elo: float, q_home: float, q_away: float) -> float:
    """The d fed to win_prob: (R_home + Q_home) - (R_away + Q_away) + HFA + REST*(rest diff)."""
    return (elo_home + q_home) - (elo_away + q_away) + hfa_elo + rest_elo


def game_result(home_score: int, away_score: int) -> float:
    """s for the home side: 1 win, 0 loss, 0.5 tie."""
    if home_score > away_score:
        return 1.0
    if home_score < away_score:
        return 0.0
    return 0.5


def rating_update(d: float, p: float, s: float, margin: int, k: float, tie_multiplier: float) -> tuple[float, float]:
    """Return (delta, mult) for one played game; delta is added to home and subtracted from away."""
    if s == 1.0:
        d_fav = d
    elif s == 0.0:
        d_fav = -d
    else:
        d_fav = abs(d)
    mult = mov_multiplier(margin, d_fav, tie_multiplier)
    return k * mult * (s - p), mult


def _rest_elo(game: dict, rest_per_day: float) -> float:
    """REST * (home_rest - away_rest); 0 when either rest field is null (D1 ground truth)."""
    hr, ar = game["home_rest"], game["away_rest"]
    if hr is None or ar is None:
        return 0.0
    return rest_per_day * (hr - ar)


def _rollover_state(ratings: dict, qbs: dict, reversion: float, annual_reversion: float) -> None:
    """Apply the between-season regression to every team and every QB, in place."""
    for code in ratings:
        ratings[code] = rollover(ratings[code], reversion)
    for st in qbs.values():
        st["q"] = _qb.season_regress(st["q"], annual_reversion)


def _pregame(game: dict, ratings: dict, qbs: dict, params: dict, extra: dict | None = None) -> dict:
    """Everything known before a game: ratings, terms, QB q's (lazily decayed) and d/p.

    ``extra`` (optional) maps game_id -> (extra_home_elo, extra_away_elo): per-game rating
    terms (the D18.6 injury term during tuning) added to d as extra_home - extra_away. When
    ``extra`` is None nothing changes and the record keeps its original keys."""
    elo_p = params["elo"]
    h, a = game["home"], game["away"]
    elo_home = ratings.get(h, INITIAL_RATING)
    elo_away = ratings.get(a, INITIAL_RATING)
    hfa_elo = 0.0 if game["neutral"] else float(elo_p["hfa"])
    rest_elo = _rest_elo(game, float(elo_p["rest_per_day"]))
    q_home = q_away = None
    starts_home = starts_away = None
    if params["qb"]["enabled"]:
        hid, aid = game["home_qb_id"], game["away_qb_id"]
        if hid:
            st = qbs.get(hid)
            q_home = _qb.effective_q(st, game["gameday"], params)
            starts_home = st["starts"] if st else 0
        if aid:
            st = qbs.get(aid)
            q_away = _qb.effective_q(st, game["gameday"], params)
            starts_away = st["starts"] if st else 0
    d = pregame_d(elo_home, elo_away, hfa_elo, rest_elo, q_home or 0.0, q_away or 0.0)
    extra_home = extra_away = 0.0
    if extra is not None:
        pair = extra.get(game["game_id"])
        if pair:
            extra_home, extra_away = float(pair[0]), float(pair[1])
        d += extra_home - extra_away
    rec = {
        "p_home": win_prob(d),
        "d": d,
        "elo_home": elo_home,
        "elo_away": elo_away,
        "hfa_elo": hfa_elo,
        "rest_elo": rest_elo,
        "q_home": q_home,
        "q_away": q_away,
        "qb_home_starts": starts_home,
        "qb_away_starts": starts_away,
    }
    if extra is not None:
        rec["extra_home"] = extra_home
        rec["extra_away"] = extra_away
    return rec


def _apply_result(game: dict, pre: dict, ratings: dict, qbs: dict, params: dict) -> float:
    """Update team ratings and QB state for a played game. Returns delta (home side)."""
    s = game_result(game["home_score"], game["away_score"])
    margin = abs(game["home_score"] - game["away_score"])
    delta, mult = rating_update(pre["d"], pre["p_home"], s, margin, float(params["elo"]["k"]), float(params["elo"]["tie_multiplier"]))
    h, a = game["home"], game["away"]
    ratings[h] = ratings.get(h, INITIAL_RATING) + delta
    ratings[a] = ratings.get(a, INITIAL_RATING) - delta
    if params["qb"]["enabled"]:
        kq = float(params["qb"]["k"])
        for side, sign in (("home", 1.0), ("away", -1.0)):
            qid = game[side + "_qb_id"]
            if not qid:
                continue
            q_pre = pre["q_" + side]  # decayed-before-game value
            st = qbs.get(qid)
            if st is None:
                st = {"q": q_pre, "name": game[side + "_qb_name"] or qid, "team": game[side], "starts": 0, "last_start_date": game["gameday"]}
                qbs[qid] = st
            st["q"] = q_pre + sign * kq * mult * (s - pre["p_home"])
            st["name"] = game[side + "_qb_name"] or st["name"]
            st["team"] = game[side]
            st["starts"] += 1
            st["last_start_date"] = game["gameday"]
    return delta


def replay(games: list[dict], params: dict, initial: dict | None = None, extra: dict | None = None) -> dict:
    """Causal replay over normalized games (CONTRACT 6.1).

    Played games are processed in ``sort_key`` order; the season rollover (team Elo by
    ``params.elo.reversion``, Q by ``params.qb.annual_reversion``) is applied exactly once
    immediately before the first game of each season. Every game, played or not, gets a
    ``pregame`` record computed BEFORE its update. Unplayed games are evaluated against the
    final state (rolled over once more if their season is later than the last played season),
    with the QB term 0 for a blank starter.

    ``initial`` is an optional ``{"ratings": {code: float}, "qb": {id: state}, "season": int}``
    starting point (tests and continuation); omitted = every team at 1500, no QB history.

    ``extra`` is an optional ``{game_id: (extra_home_elo, extra_away_elo)}`` hook (D18.6): the
    pair is added to that game's d as ``extra_home - extra_away`` BEFORE p and the rating
    update, so the term shapes both the forecast and the post-game delta. Games absent from
    the map get 0. With ``extra=None`` the replay is byte-identical to the plain one and the
    pregame records keep their original keys; with a map each record also carries
    ``extra_home`` / ``extra_away``.

    Returns::

        {"ratings": {code: elo_after_last_played_game},
         "qb": {qb_id: {"q", "name", "team", "starts", "last_start_date"}},
         "pregame": {game_id: {"p_home","d","elo_home","elo_away","hfa_elo","rest_elo",
                               "q_home","q_away","qb_home_starts","qb_away_starts"}},
         "history": {code: [[season, week, game_id, elo_after], ...]},
         "usual_starter": {code: {"id","name"} | None},
         "records": {code: {"w","l","t"}}}
    """
    ratings = {code: INITIAL_RATING for code in TEAMS}
    qbs: dict = {}
    season = None
    if initial:
        ratings.update({k: float(v) for k, v in (initial.get("ratings") or {}).items()})
        qbs = {k: dict(v) for k, v in (initial.get("qb") or {}).items()}
        season = initial.get("season")
    reversion = float(params["elo"]["reversion"])
    annual = float(params["qb"]["annual_reversion"])

    ordered = sorted(games, key=lambda g: g["sort_key"])
    pregame: dict = {}
    history: dict = {code: [] for code in TEAMS}
    starters: dict = {code: [] for code in TEAMS}
    unplayed: list = []
    for g in ordered:
        if not g["played"]:
            unplayed.append(g)
            continue
        if g["season"] != season:
            if season is not None or initial:
                _rollover_state(ratings, qbs, reversion, annual)
            season = g["season"]
        pre = _pregame(g, ratings, qbs, params, extra)
        pregame[g["game_id"]] = pre
        _apply_result(g, pre, ratings, qbs, params)
        for side in ("home", "away"):
            code = g[side]
            history.setdefault(code, []).append([g["season"], g["week"], g["game_id"], ratings[code]])
            qid = g[side + "_qb_id"]
            if qid:
                starters.setdefault(code, []).append({"id": qid, "name": g[side + "_qb_name"] or qid})

    final_ratings = dict(ratings)
    final_qbs = {k: dict(v) for k, v in qbs.items()}
    # Unplayed games: evaluate against the final state, rolled over once if a later season.
    if unplayed:
        eval_ratings, eval_qbs = ratings, qbs
        later = [g for g in unplayed if season is None or g["season"] > season]
        if later and season is not None:
            eval_ratings = dict(ratings)
            eval_qbs = {k: dict(v) for k, v in qbs.items()}
            _rollover_state(eval_ratings, eval_qbs, reversion, annual)
        for g in unplayed:
            use_rolled = season is not None and g["season"] > season
            pregame[g["game_id"]] = _pregame(g, eval_ratings if use_rolled else ratings, eval_qbs if use_rolled else qbs, params, extra)

    window = int(params["qb"]["usual_starter_window"])
    usual = {code: _qb.usual_starter(starters.get(code, []), window) for code in TEAMS}
    latest_season = max((g["season"] for g in ordered), default=season)
    records = {code: {"w": 0, "l": 0, "t": 0} for code in TEAMS}
    for g in ordered:
        if g["season"] != latest_season or g["game_type"] != "REG" or not g["played"]:
            continue
        s = game_result(g["home_score"], g["away_score"])
        h, a = g["home"], g["away"]
        if s == 1.0:
            records[h]["w"] += 1
            records[a]["l"] += 1
        elif s == 0.0:
            records[h]["l"] += 1
            records[a]["w"] += 1
        else:
            records[h]["t"] += 1
            records[a]["t"] += 1
    return {
        "ratings": final_ratings,
        "qb": final_qbs,
        "pregame": pregame,
        "history": history,
        "usual_starter": usual,
        "records": records,
    }


def ranks(ratings: dict) -> dict:
    """Rank 1..n by rating descending, ties broken by code (CONTRACT 2.9)."""
    order = sorted(ratings, key=lambda c: (-ratings[c], c))
    return {code: i + 1 for i, code in enumerate(order)}


def form_delta(history: dict, pregame: dict, code: str, n: int = 3) -> float | None:
    """Elo change over the team's last ``n`` played games (fewer if fewer; None if none played).

    Measured from the PREGAME rating of the first game in the window (recorded in
    ``pregame``, so a season rollover inside the window is handled correctly) to the rating
    after the last game. The team's side in a game is read off the game_id suffix
    (``season_week_away_home``), mapped through the code aliases.
    """
    hist = history.get(code) or []
    if not hist:
        return None
    window = hist[-n:]
    first_gid = window[0][2]
    home_raw = first_gid.rsplit("_", 1)[-1]
    is_home = canonical_code(home_raw) == code
    pre = pregame[first_gid]["elo_home" if is_home else "elo_away"]
    return window[-1][3] - pre
