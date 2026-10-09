"""Grid search -> params.json (SPEC "Elo model", D1-D3). ``python -m src.tune``.

Steps (all offline, reads data/games.csv only):
  1. Elo grid on 2010-2024 Brier with the QB term OFF: K x HFA x REV x REST (180 replays).
  2. KQ grid {4,6,8,12} with the four Elo values held, QB ON, same objective.
  3. QB gate: enabled iff 2025 holdout Brier with QB <= without (same rows).
  4. Blend weight fit on 2015-2024 against CSV closing lines (D3) with the gated model.
  5. 2025 holdout evaluation (section 2.4) and the reference block.
Writes params.json = section 2.3 keys + reference + qb_gate + tuning + blend_fit + tuned_at.
Runs in well under two minutes (one full replay is ~0.1 s).
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import data, elo, evaluate, schema

GRID = {
    "k": (16, 20, 24, 28),
    "hfa": (30, 40, 48, 55, 65),
    "reversion": (0.25, 0.33, 0.40),
    "rest_per_day": (0, 1, 2),
    "kq": (4, 6, 8, 12),
}
TUNE_SEASONS = (2010, 2024)
PINNED_MODEL_WEIGHT = 0.35
BLEND_SEASONS = (2015, 2024)
HOLDOUT_SEASON = 2025

DEFAULT_PARAMS = {
    "elo": {"k": 20, "hfa": 40, "rest_per_day": 2, "reversion": 0.4, "tie_multiplier": 1.0},
    "qb": {"enabled": True, "k": 6, "new_qb": -50, "annual_reversion": 0.2, "inactivity_penalty": 25, "usual_starter_window": 8},
    "blend": {"model_weight": 0.35, "fitted_model_weight": None, "pinned": True, "fit_seasons": list(BLEND_SEASONS), "fit_metric": "brier"},
    "tier_bounds": dict(schema.TIER_BOUNDS),
    "score": {"points_per_elo": 25, "league_average_total": None, "league_average_season": None},
    "sim": {"runs": 10000, "seed": 2026},
    "books": list(schema.DEFAULT_BOOKS),
}


def with_params(base: dict, **elo_overrides) -> dict:
    """Copy of ``base`` with params.elo fields overridden (pure)."""
    p = copy.deepcopy(base)
    p["elo"].update(elo_overrides)
    return p


def objective(games: list[dict], params: dict, seasons=TUNE_SEASONS) -> tuple[float, int]:
    """Brier of the model over played games in ``seasons`` for one parameter set."""
    rep = elo.replay(games, params)
    return evaluate.season_brier(games, rep, seasons)


def grid_search_elo(games: list[dict], base: dict, grid: dict = GRID, log=None) -> dict:
    """Step 1: best (k, hfa, reversion, rest_per_day) by 2010-2024 Brier with QB off.

    Returns {"best": {...}, "brier": float, "n": int, "results": [[k,hfa,rev,rest,brier], ...]}.
    Ties go to the first combination in grid order (deterministic).
    """
    base = copy.deepcopy(base)
    base["qb"]["enabled"] = False
    results = []
    best = None
    for k, hfa, rev, rest in itertools.product(grid["k"], grid["hfa"], grid["reversion"], grid["rest_per_day"]):
        brier, n = objective(games, with_params(base, k=k, hfa=hfa, reversion=rev, rest_per_day=rest))
        results.append([k, hfa, rev, rest, brier])
        if best is None or brier < best[0]:
            best = (brier, {"k": k, "hfa": hfa, "reversion": rev, "rest_per_day": rest}, n)
        if log:
            log("elo k=%2d hfa=%2d rev=%.2f rest=%d  brier=%.5f" % (k, hfa, rev, rest, brier))
    return {"best": best[1], "brier": best[0], "n": best[2], "results": results}


def grid_search_kq(games: list[dict], base: dict, grid: dict = GRID, log=None) -> dict:
    """Step 2: best KQ with the Elo values in ``base`` held and the QB term on."""
    base = copy.deepcopy(base)
    base["qb"]["enabled"] = True
    results = []
    best = None
    for kq in grid["kq"]:
        p = copy.deepcopy(base)
        p["qb"]["k"] = kq
        brier, n = objective(games, p)
        results.append([kq, brier])
        if best is None or brier < best[0]:
            best = (brier, kq, n)
        if log:
            log("qb  kq=%2d  brier=%.5f" % (kq, brier))
    return {"best_kq": best[1], "brier": best[0], "n": best[2], "results": results}


def qb_gate(games: list[dict], params_with_qb: dict, season: int = HOLDOUT_SEASON) -> dict:
    """Step 3: keep the QB term iff holdout Brier with QB <= without (identical rows)."""
    p_on = copy.deepcopy(params_with_qb)
    p_on["qb"]["enabled"] = True
    ev = evaluate.holdout(games, p_on, season)
    with_qb = ev["model"]["brier"]
    without = ev["model_no_qb"]["brier"]
    return {"season": season, "n": ev["samples"]["model"], "brier_with_qb": with_qb, "brier_without_qb": without, "enabled": with_qb is not None and without is not None and with_qb <= without}


def tune(games: list[dict], base: dict | None = None, log=None) -> dict:
    """Run every step and return the complete params.json object (pure apart from ``log``)."""
    base = copy.deepcopy(base or DEFAULT_PARAMS)
    elo_fit = grid_search_elo(games, base, log=log)
    params = with_params(base, **elo_fit["best"])
    kq_fit = grid_search_kq(games, params, log=log)
    params["qb"]["k"] = kq_fit["best_kq"]
    gate = qb_gate(games, params)
    params["qb"]["enabled"] = gate["enabled"]

    rep = elo.replay(games, params)
    blend_fit = evaluate.fit_blend_weight(games, params, BLEND_SEASONS, replay_result=rep)
    # D3 (amended 2026-10-09): the fit against CLOSING lines prefers ~0.05, which would make the
    # headline a market mirror. The product holds the weight at PINNED_MODEL_WEIGHT and reports
    # the fitted value and its holdout cost alongside, so the choice is visible, not hidden.
    params["blend"]["model_weight"] = PINNED_MODEL_WEIGHT
    params["blend"]["fitted_model_weight"] = blend_fit["model_weight"]
    params["blend"]["pinned"] = True
    params["blend"]["fit_seasons"] = list(BLEND_SEASONS)
    params["blend"]["fit_metric"] = "brier"

    prev = evaluate.previous_full_season(games)
    params["score"]["league_average_season"] = prev
    params["score"]["league_average_total"] = evaluate.league_average_total(games, prev)

    ev = evaluate.holdout(games, params, HOLDOUT_SEASON, replays={"model": rep})
    params["reference"] = {"season": HOLDOUT_SEASON, "brier": ev["model"]["brier"], "logloss": ev["model"]["logloss"], "accuracy": ev["model"]["accuracy"], "n": ev["samples"]["model"]}
    params["evaluation"] = ev
    params["qb_gate"] = gate
    params["tuning"] = {
        "seasons": list(TUNE_SEASONS),
        "metric": "brier",
        "elo": {"best": elo_fit["best"], "brier": elo_fit["brier"], "n": elo_fit["n"], "grid": {k: list(v) for k, v in GRID.items() if k != "kq"}, "results": elo_fit["results"]},
        "qb": {"best_kq": kq_fit["best_kq"], "brier": kq_fit["brier"], "n": kq_fit["n"], "grid": list(GRID["kq"]), "results": kq_fit["results"]},
    }
    params["blend_fit"] = {"seasons": list(BLEND_SEASONS), "metric": "brier", "model_weight": blend_fit["model_weight"], "brier": blend_fit["brier"], "n": blend_fit["n"], "grid": blend_fit["grid"]}
    params["tuned_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return params


def write_params(params: dict, path: str = evaluate.PARAMS_PATH) -> None:
    """Atomic write (temp + os.replace) of params.json with full float precision."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(params, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv or "--verbose" in argv
    t0 = time.time()
    games = data.load_games(evaluate.GAMES_PATH)
    log = (lambda s: print(s, flush=True)) if verbose else None
    params = tune(games, log=log)
    write_params(params)
    print("elo: %s  (2010-2024 brier %.5f, n=%d)" % (params["tuning"]["elo"]["best"], params["tuning"]["elo"]["brier"], params["tuning"]["elo"]["n"]))
    print("qb: kq=%d  gate: with=%.5f without=%.5f -> enabled=%s" % (params["qb"]["k"], params["qb_gate"]["brier_with_qb"], params["qb_gate"]["brier_without_qb"], params["qb"]["enabled"]))
    print("blend: w=%.2f  (2015-2024 brier %.5f, n=%d)" % (params["blend"]["model_weight"], params["blend_fit"]["brier"], params["blend_fit"]["n"]))
    print("score: league_average_total=%.4f (season %d)" % (params["score"]["league_average_total"], params["score"]["league_average_season"]))
    print(evaluate.format_table(params["evaluation"]))
    print("wrote %s in %.1f s" % (evaluate.PARAMS_PATH, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
