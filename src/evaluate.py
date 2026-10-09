"""Holdout evaluation and blend-weight fit (CONTRACT 2.4, 6.7; D3).

    metrics(pairs)                                   -> {"brier","logloss","accuracy","n"}
    no_vig_home(home_ml, away_ml)                    -> float   CSV closing lines, vig removed
    holdout_rows(games, replay_result, season)       -> list of per-game rows (p_model, p_market, s)
    holdout(games, params, season=2025)              -> evaluation object (section 2.4)
    fit_blend_weight(games, params, seasons=(2015, 2024), step=0.05) -> {"model_weight", "brier", "n", "grid"}
    season_brier(games, replay_result, seasons)      -> (brier, n)   tuning objective
    previous_full_season(games)                      -> int
    league_average_total(games, season)              -> float   mean REG total of that season

    python -m src.evaluate --reference-check   exit 1 if the 2025 model Brier is outside
                                               +/- 0.003 of params.json["reference"]["brier"]

Conventions: target s = 1 home win / 0 away / 0.5 tie for Brier and logloss (the prototype's
convention); accuracy counts decisive games only (ties excluded, declared tie policy).
"""
from __future__ import annotations

import copy
import json
import math
import os
import sys

from . import data, elo

REFERENCE_SEASON = 2025
REFERENCE_BAND = 0.003
PARAMS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "params.json")
GAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "games.csv")


def _implied(american: int) -> float:
    """Raw implied probability of an American price (+150 -> 0.4, -120 -> 0.5454...)."""
    x = float(american)
    return 100.0 / (x + 100.0) if x > 0 else -x / (-x + 100.0)


def no_vig_home(home_ml: int, away_ml: int) -> float:
    """Vig-free home probability from a two-way moneyline: implied home / sum of implieds."""
    h, a = _implied(home_ml), _implied(away_ml)
    return h / (h + a)


def metrics(pairs: list[tuple[float, float]]) -> dict:
    """Brier, logloss and accuracy over (p_home, s) pairs. Empty -> nulls with n = 0."""
    n = len(pairs)
    if n == 0:
        return {"brier": None, "logloss": None, "accuracy": None, "n": 0}
    brier = sum((p - s) ** 2 for p, s in pairs) / n
    eps = 1e-15
    logloss = -sum(s * math.log(max(p, eps)) + (1 - s) * math.log(max(1 - p, eps)) for p, s in pairs) / n
    decisive = [(p, s) for p, s in pairs if s != 0.5]
    accuracy = (sum(1 for p, s in decisive if (p >= 0.5) == (s == 1.0)) / len(decisive)) if decisive else None
    return {"brier": brier, "logloss": logloss, "accuracy": accuracy, "n": n}


def holdout_rows(games: list[dict], replay_result: dict, seasons) -> list[dict]:
    """Per played game of ``seasons``: {game_id, p_model, p_market (or None), s}."""
    if isinstance(seasons, int):
        seasons = (seasons,)
    lo, hi = min(seasons), max(seasons)
    pregame = replay_result["pregame"]
    rows = []
    for g in games:
        if not g["played"] or not lo <= g["season"] <= hi:
            continue
        pm = None
        if g["home_moneyline"] is not None and g["away_moneyline"] is not None:
            pm = no_vig_home(g["home_moneyline"], g["away_moneyline"])
        rows.append({"game_id": g["game_id"], "p_model": pregame[g["game_id"]]["p_home"], "p_market": pm, "s": elo.game_result(g["home_score"], g["away_score"])})
    return rows


def _blend(p_model: float, p_market: float, w: float) -> float:
    return w * p_model + (1.0 - w) * p_market


def _without_qb(params: dict) -> dict:
    p = copy.deepcopy(params)
    p["qb"]["enabled"] = False
    return p


def holdout(games: list[dict], params: dict, season: int = REFERENCE_SEASON, replays: dict | None = None) -> dict:
    """Section 2.4 evaluation on one holdout season.

    ``model`` uses ``params`` as given (QB on or off per ``params.qb.enabled``); ``model_no_qb``
    is a second replay with the QB term off over the SAME rows; ``market`` is the vig-free CSV
    closing moneyline and ``blend`` uses ``params.blend.model_weight``, both over the rows that
    have both closing moneylines. ``replays`` may pass precomputed {"model", "no_qb"} replay
    results to avoid recomputation.
    """
    replays = replays or {}
    rep = replays.get("model") or elo.replay(games, params)
    rep_no = replays.get("no_qb") or (rep if not params["qb"]["enabled"] else elo.replay(games, _without_qb(params)))
    rows = holdout_rows(games, rep, season)
    rows_no = holdout_rows(games, rep_no, season)
    w = float(params["blend"]["model_weight"])
    comparable = [r for r in rows if r["p_market"] is not None]
    return {
        "season": season,
        "samples": {"model": len(rows), "market_comparable": len(comparable)},
        "model": _strip_n(metrics([(r["p_model"], r["s"]) for r in rows])),
        "model_no_qb": _strip_n(metrics([(r["p_model"], r["s"]) for r in rows_no])),
        "market": _strip_n(metrics([(r["p_market"], r["s"]) for r in comparable])),
        "blend": _strip_n(metrics([(_blend(r["p_model"], r["p_market"], w), r["s"]) for r in comparable])),
    }


def _strip_n(m: dict) -> dict:
    return {"brier": m["brier"], "logloss": m["logloss"], "accuracy": m["accuracy"]}


def season_brier(games: list[dict], replay_result: dict, seasons) -> tuple[float | None, int]:
    """Brier of the model's pregame p_home over played games in ``seasons`` (tuning objective)."""
    rows = holdout_rows(games, replay_result, seasons)
    m = metrics([(r["p_model"], r["s"]) for r in rows])
    return m["brier"], m["n"]


def fit_blend_weight(games: list[dict], params: dict, seasons=(2015, 2024), step: float = 0.05, replay_result: dict | None = None) -> dict:
    """D3: grid w in 0..1 (step 0.05) minimizing Brier of w*p_model + (1-w)*p_market on the
    played games of ``seasons`` that have both closing moneylines. Ties -> the smaller w.
    Returns {"model_weight", "brier", "n", "grid": [[w, brier], ...]}.
    """
    rep = replay_result or elo.replay(games, params)
    rows = [r for r in holdout_rows(games, rep, seasons) if r["p_market"] is not None]
    steps = int(round(1.0 / step))
    grid = []
    best = None
    for i in range(steps + 1):
        w = i / steps
        b = metrics([(_blend(r["p_model"], r["p_market"], w), r["s"]) for r in rows])["brier"]
        grid.append([w, b])
        if b is not None and (best is None or b < best[1]):
            best = (w, b)
    return {"model_weight": best[0] if best else params["blend"]["model_weight"], "brier": best[1] if best else None, "n": len(rows), "grid": grid}


def previous_full_season(games: list[dict]) -> int:
    """Latest season whose REG games are all played (the 'previous full season' of D5)."""
    by_season: dict[int, list] = {}
    for g in games:
        if g["game_type"] == "REG":
            by_season.setdefault(g["season"], []).append(g["played"])
    full = [s for s, flags in by_season.items() if flags and all(flags)]
    return max(full)


def league_average_total(games: list[dict], season: int) -> float:
    """Mean points scored (home + away) over the played REG games of ``season``."""
    totals = [g["home_score"] + g["away_score"] for g in games if g["season"] == season and g["game_type"] == "REG" and g["played"]]
    return sum(totals) / len(totals)


# ----------------------------------------------------------------------------
# CLI: reference check (make test)
# ----------------------------------------------------------------------------


def _fmt(x, nd=4) -> str:
    return "-" if x is None else ("%.*f" % (nd, x))


def format_table(ev: dict) -> str:
    """Plain-text holdout table."""
    lines = ["%d holdout  n(model)=%d  n(market)=%d" % (ev["season"], ev["samples"]["model"], ev["samples"]["market_comparable"])]
    lines.append("%-12s %8s %8s %8s" % ("", "brier", "logloss", "acc"))
    for k in ("model", "model_no_qb", "market", "blend"):
        m = ev[k]
        lines.append("%-12s %8s %8s %8s" % (k, _fmt(m["brier"]), _fmt(m["logloss"], 3), _fmt(m["accuracy"], 3)))
    return "\n".join(lines)


def reference_check(params: dict, games: list[dict], band: float = REFERENCE_BAND) -> tuple[bool, str]:
    """Compare the 2025 model Brier with params['reference']['brier']; (ok, report text)."""
    ref = params["reference"]
    ev = holdout(games, params, ref["season"])
    got = ev["model"]["brier"]
    ok = got is not None and abs(got - ref["brier"]) <= band
    text = format_table(ev) + "\nreference brier %.4f, got %s, band +/-%.3f -> %s" % (ref["brier"], _fmt(got), band, "OK" if ok else "FAIL")
    return ok, text


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    with open(PARAMS_PATH, encoding="utf-8") as fh:
        params = json.load(fh)
    games = data.load_games(GAMES_PATH)
    if "--reference-check" in argv:
        ok, text = reference_check(params, games)
        print(text)
        return 0 if ok else 1
    print(format_table(holdout(games, params, params.get("reference", {}).get("season", REFERENCE_SEASON))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
