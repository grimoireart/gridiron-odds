"""Grid search -> params.json (SPEC "Elo model", D1-D3). ``python -m src.tune``.

Steps (all offline, reads data/games.csv only):
  1. Elo grid on 2010-2024 Brier with the QB term OFF: K x HFA x REV x REST (180 replays).
  2. KQ grid {4,6,8,12} with the four Elo values held, QB ON, same objective.
  3. QB gate: enabled iff 2025 holdout Brier with QB <= without (same rows).
  4. Blend weight fit on 2015-2024 against CSV closing lines (D3) with the gated model.
  5. 2025 holdout evaluation (section 2.4) and the reference block.
  6. Injury stage (D18.6): nflverse injuries + depth charts 2010-2025 (src/injury_history.py;
     downloaded once, cached under data/), coordinate descent over the six position-group
     weights on 2010-2024 Brier with Elo + KQ held, then the 2025 holdout gate
     (enabled iff Brier with <= without - INJURY_IMPROVEMENT). Writes params.injury (the
     exact section-2.3 block) and the diagnostic block params.injury_tuning.
     The term is applied the way predict.py applies it (INJURY_MODE "pregame": added to d at
     forecast time on ratings replayed without it); the "replay" mode (the term injected into
     the replay through elo.replay's ``extra`` hook, so ratings absorb it) is tuned and gated
     as well and recorded under injury_tuning.alternative for comparison.
Writes params.json = section 2.3 keys + reference + qb_gate + tuning + blend_fit + injury_tuning + tuned_at.
Runs in well under two minutes once the injury files are cached (one full replay is ~0.1 s).
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import data, elo, evaluate, injury_history, schema

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
INJURY_GRID = (0, 5, 10, 15, 20, 30)          # Elo points per starter Out, per position group
INJURY_STATUS_MULT = dict(injury_history.STATUS_MULT)  # Out 1, Doubtful 0.75, Questionable 0.25, Probable 0.1
INJURY_IMPROVEMENT = 0.0005                   # the gate: holdout Brier must fall by at least this
INJURY_MAX_SWEEPS = 12
INJURY_MODE = "pregame"                       # how params.injury is tuned/gated; "replay" is the recorded alternative
INJURY_MODES = ("pregame", "replay")

DEFAULT_PARAMS = {
    "elo": {"k": 20, "hfa": 40, "rest_per_day": 2, "reversion": 0.4, "tie_multiplier": 1.0},
    "qb": {"enabled": True, "k": 6, "new_qb": -50, "annual_reversion": 0.2, "inactivity_penalty": 25, "usual_starter_window": 8},
    "blend": {"model_weight": 0.35, "fitted_model_weight": None, "pinned": True, "fit_seasons": list(BLEND_SEASONS), "fit_metric": "brier"},
    "tier_bounds": dict(schema.TIER_BOUNDS),
    "score": {"points_per_elo": 25, "league_average_total": None, "league_average_season": None},
    "sim": {"runs": 10000, "seed": 2026},
    "books": list(schema.DEFAULT_BOOKS),
    "injury": {
        "enabled": False,
        "weights": {g: 0 for g in schema.INJURY_WEIGHT_GROUPS},
        "status_mult": dict(INJURY_STATUS_MULT),
        "gate": {"holdout_brier_with": None, "holdout_brier_without": None, "improvement_required": INJURY_IMPROVEMENT},
    },
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


# ----------------------------------------------------------------------------
# Step 6: injury term (D18.6)
# ----------------------------------------------------------------------------


def injury_objective(games: list[dict], params: dict, features: dict, weights: dict,
                     status_mult: dict | None = None, seasons=TUNE_SEASONS, mode: str = INJURY_MODE,
                     replay_result: dict | None = None) -> tuple[float, int]:
    """Brier over ``seasons`` with the injury term applied in ``mode``.

    "pregame": the plain replay's d plus the term at forecast time (predict.py's rule;
    ``replay_result`` may pass the plain replay so no replay runs per candidate).
    "replay": the term injected through elo.replay's ``extra`` hook (one replay per call)."""
    extra = injury_history.extra_from_features(features, weights, status_mult or INJURY_STATUS_MULT)
    if mode == "replay":
        rep = elo.replay(games, params, extra=extra)
        return evaluate.season_brier(games, rep, seasons)
    if mode != "pregame":
        raise ValueError("mode must be one of %s, got %r" % (INJURY_MODES, mode))
    rep = replay_result or elo.replay(games, params)
    rows = evaluate.holdout_rows_with_extra(games, rep, seasons, extra)
    m = evaluate.metrics([(r["p_model"], r["s"]) for r in rows])
    return m["brier"], m["n"]


def coordinate_descent_injury(games: list[dict], params: dict, features: dict, grid=INJURY_GRID,
                              status_mult: dict | None = None, seasons=TUNE_SEASONS, log=None,
                              max_sweeps: int = INJURY_MAX_SWEEPS, mode: str = INJURY_MODE,
                              replay_result: dict | None = None) -> dict:
    """Coordinate descent over the six position-group weights (all start at 0).

    Each sweep tries every grid value for one group at a time (the others held), keeps a value
    only when it strictly lowers the Brier (ties keep the current, smaller-first value) and
    stops after a sweep that changes nothing. Returns {"mode", "weights", "brier",
    "brier_without", "n", "sweeps", "evaluations", "trace": [[sweep, group, value, brier], ...]}.
    """
    status_mult = status_mult or INJURY_STATUS_MULT
    if mode == "pregame" and replay_result is None:
        replay_result = elo.replay(games, params)
    groups = list(schema.INJURY_WEIGHT_GROUPS)
    weights = {g: 0 for g in groups}
    best, n = injury_objective(games, params, features, weights, status_mult, seasons, mode, replay_result)
    without = best
    trace = []
    evaluations = 1
    sweeps = 0
    for sweep in range(1, max_sweeps + 1):
        sweeps = sweep
        changed = False
        for g in groups:
            current = weights[g]
            for v in grid:
                if v == current:
                    continue
                trial = dict(weights)
                trial[g] = v
                brier, _ = injury_objective(games, params, features, trial, status_mult, seasons, mode, replay_result)
                evaluations += 1
                trace.append([sweep, g, v, brier])
                if log:
                    log("injury[%s] sweep %d %-8s w=%2d  brier=%.6f%s" % (mode, sweep, g, v, brier, "  *" if brier < best else ""))
                if brier < best:
                    best, weights, changed = brier, trial, True
        if not changed:
            break
    return {"mode": mode, "weights": weights, "brier": best, "brier_without": without, "n": n, "sweeps": sweeps,
            "evaluations": evaluations, "trace": trace}


def injury_gate_enabled(brier_with, brier_without, improvement_required: float = INJURY_IMPROVEMENT) -> bool:
    """The D18.6 gate: on iff both holdout Briers exist and with <= without - improvement_required."""
    if brier_with is None or brier_without is None:
        return False
    return (brier_without - brier_with) >= improvement_required - 1e-12


def injury_gate(games: list[dict], params: dict, features: dict, weights: dict, status_mult: dict | None = None,
                improvement_required: float = INJURY_IMPROVEMENT, season: int = HOLDOUT_SEASON,
                replay_result: dict | None = None, mode: str = INJURY_MODE) -> tuple[dict, dict]:
    """Holdout Brier with/without the injury term on identical rows -> (gate block, evaluation).

    The gate block is params.injury.gate plus "enabled", "season", "n" and "mode"; the
    evaluation is evaluate.holdout's object with the model_with_injuries row."""
    extra = injury_history.extra_from_features(features, weights, status_mult or INJURY_STATUS_MULT)
    replays = {"model": replay_result} if replay_result else None
    ev = evaluate.holdout(games, params, season, replays=replays, injury_extra=extra, injury_mode=mode)
    with_inj = ev["model_with_injuries"]["brier"]
    without = ev["model"]["brier"]
    gate = {"holdout_brier_with": with_inj, "holdout_brier_without": without, "improvement_required": improvement_required,
            "enabled": injury_gate_enabled(with_inj, without, improvement_required), "season": season, "n": ev["samples"]["model"],
            "mode": mode}
    return gate, ev


def tune_injury(games: list[dict], params: dict, history: dict | None, log=None,
                replay_result: dict | None = None, seasons=TUNE_SEASONS,
                holdout_season: int = HOLDOUT_SEASON) -> tuple[dict, dict, dict | None]:
    """Step 6 on a params object whose Elo/QB values are final.

    ``history`` is injury_history.build_history(...) (None = unavailable: the term stays off
    with null gate numbers). Returns (params.injury block, params.injury_tuning block,
    evaluation with the model_with_injuries row or None)."""
    status_mult = dict(INJURY_STATUS_MULT)
    block = {"enabled": False, "weights": {g: 0 for g in schema.INJURY_WEIGHT_GROUPS}, "status_mult": status_mult,
             "gate": {"holdout_brier_with": None, "holdout_brier_without": None, "improvement_required": INJURY_IMPROVEMENT}}
    tuned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    features = (history or {}).get("features") or {}
    if not features:
        note = "injury history unavailable: term disabled, nothing tuned"
        if log:
            log(note)
        return block, {"tuned_at": tuned_at, "available": False, "notes": list((history or {}).get("notes") or []) + [note]}, None
    replay_result = replay_result or elo.replay(games, params)
    fit = coordinate_descent_injury(games, params, features, status_mult=status_mult, seasons=seasons, log=log,
                                    mode=INJURY_MODE, replay_result=replay_result)
    gate, ev = injury_gate(games, params, features, fit["weights"], status_mult, season=holdout_season,
                           replay_result=replay_result, mode=INJURY_MODE)
    # The other application mode, tuned and gated the same way, for the record only.
    alt_mode = [m for m in INJURY_MODES if m != INJURY_MODE][0]
    alt_fit = coordinate_descent_injury(games, params, features, status_mult=status_mult, seasons=seasons, log=log,
                                        mode=alt_mode, replay_result=replay_result)
    alt_gate, _ = injury_gate(games, params, features, alt_fit["weights"], status_mult, season=holdout_season,
                              replay_result=replay_result, mode=alt_mode)
    block["enabled"] = gate["enabled"]
    block["weights"] = dict(fit["weights"])
    block["gate"] = {k: gate[k] for k in ("holdout_brier_with", "holdout_brier_without", "improvement_required")}
    coverage = {}
    for season, c in sorted(((int(k), v) for k, v in (history.get("coverage") or {}).items())):
        if c.get("skipped"):
            coverage[str(season)] = {"skipped": True, "notes": c.get("notes", [])}
        else:
            coverage[str(season)] = {"depth_format": c["depth_format"], "games": c["games"], "games_both_filed": c["games_both_filed"],
                                     "starters_per_side": round(c["starters_per_side_sum"] / max(1, 2 * c["games"]), 2),
                                     "starters_out": c["starters_out"], "starters_doubtful": c["starters_doubtful"],
                                     "starters_questionable": c["starters_questionable"], "starters_probable": c["starters_probable"],
                                     "unmapped_starter_slots": c.get("unmapped_starter_slots", {})}
    tuning = {
        "tuned_at": tuned_at,
        "available": True,
        "mode": INJURY_MODE,
        "seasons": list(seasons),
        "metric": "brier",
        "grid": list(INJURY_GRID),
        "status_mult": status_mult,
        "weights": dict(fit["weights"]),
        "brier_with": fit["brier"],
        "brier_without": fit["brier_without"],
        "n": fit["n"],
        "sweeps": fit["sweeps"],
        "evaluations": fit["evaluations"],
        "holdout": gate,
        "alternative": {"mode": alt_mode, "weights": dict(alt_fit["weights"]), "brier_with": alt_fit["brier"],
                        "brier_without": alt_fit["brier_without"], "n": alt_fit["n"], "sweeps": alt_fit["sweeps"],
                        "evaluations": alt_fit["evaluations"], "holdout": alt_gate},
        "coverage": coverage,
        "notes": list(history.get("notes") or []),
        "trace": fit["trace"],
    }
    return block, tuning, ev


def tune(games: list[dict], base: dict | None = None, log=None, injury_history_data: dict | None = None) -> dict:
    """Run every step and return the complete params.json object (pure apart from ``log``).

    ``injury_history_data`` is injury_history.build_history(...) for step 6; None keeps the
    injury term disabled with null gate numbers (main() always supplies it)."""
    base = copy.deepcopy(base or DEFAULT_PARAMS)
    base.setdefault("injury", copy.deepcopy(DEFAULT_PARAMS["injury"]))
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

    # Step 6 (D18.6): the injury term, with the final Elo + KQ values held.
    inj_block, inj_tuning, ev_inj = tune_injury(games, params, injury_history_data, log=log, replay_result=rep)
    params["injury"] = inj_block
    if ev_inj is not None:
        ev = ev_inj  # same rows and numbers as above, plus the model_with_injuries row

    params["evaluation"] = ev
    params["qb_gate"] = gate
    params["tuning"] = {
        "seasons": list(TUNE_SEASONS),
        "metric": "brier",
        "elo": {"best": elo_fit["best"], "brier": elo_fit["brier"], "n": elo_fit["n"], "grid": {k: list(v) for k, v in GRID.items() if k != "kq"}, "results": elo_fit["results"]},
        "qb": {"best_kq": kq_fit["best_kq"], "brier": kq_fit["brier"], "n": kq_fit["n"], "grid": list(GRID["kq"]), "results": kq_fit["results"]},
    }
    params["blend_fit"] = {"seasons": list(BLEND_SEASONS), "metric": "brier", "model_weight": blend_fit["model_weight"], "brier": blend_fit["brier"], "n": blend_fit["n"], "grid": blend_fit["grid"]}
    params["injury_tuning"] = inj_tuning
    params["tuned_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return params


def write_params(params: dict, path: str = evaluate.PARAMS_PATH) -> None:
    """Atomic write (temp + os.replace) of params.json with full float precision."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(params, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def format_injury_table(params: dict) -> str:
    """Plain-text summary of the injury stage (weights, 2010-2024 and holdout Briers, gate)."""
    inj, t = params["injury"], params.get("injury_tuning") or {}
    lines = ["injury term (D18.6; mode=%s: %s)" % (t.get("mode", INJURY_MODE),
             "term added to d at forecast time, as predict.py does" if t.get("mode", INJURY_MODE) == "pregame" else "term injected into the replay")]
    if not t.get("available"):
        lines.append("  history unavailable -> enabled=false; " + "; ".join(t.get("notes") or []))
        return "\n".join(lines)
    lines.append("  %-10s %s" % ("group", " ".join("%7s" % g for g in schema.INJURY_WEIGHT_GROUPS)))
    lines.append("  %-10s %s" % ("weight", " ".join("%7s" % inj["weights"][g] for g in schema.INJURY_WEIGHT_GROUPS)))
    lines.append("  %-10s %s" % ("status", " ".join("%s=%s" % (k, v) for k, v in inj["status_mult"].items())))
    lines.append("  %d-%d brier  without=%.5f  with=%.5f  (n=%d, %d sweeps, %d evaluations)" % (
        t["seasons"][0], t["seasons"][1], t["brier_without"], t["brier_with"], t["n"], t["sweeps"], t["evaluations"]))
    h = t["holdout"]
    lines.append("  %d holdout brier  without=%.5f  with=%.5f  delta=%+.5f  required<=%+.5f  -> enabled=%s" % (
        h["season"], h["holdout_brier_without"], h["holdout_brier_with"], h["holdout_brier_with"] - h["holdout_brier_without"],
        -h["improvement_required"], inj["enabled"]))
    a = t.get("alternative")
    if a:
        ah = a["holdout"]
        lines.append("  for the record, mode=%s: weights %s; %d-%d brier with=%.5f; %d holdout with=%.5f delta=%+.5f -> gate %s" % (
            a["mode"], " ".join("%s=%s" % (g, a["weights"][g]) for g in schema.INJURY_WEIGHT_GROUPS), t["seasons"][0], t["seasons"][1],
            a["brier_with"], ah["season"], ah["holdout_brier_with"], ah["holdout_brier_with"] - ah["holdout_brier_without"],
            "passes" if ah["enabled"] else "fails"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv or "--verbose" in argv
    t0 = time.time()
    games = data.load_games(evaluate.GAMES_PATH)
    log = (lambda s: print(s, flush=True)) if verbose else None
    offline = os.environ.get("GRIDIRON_OFFLINE") == "1"
    history = injury_history.build_history(games, offline=offline, log=log)
    params = tune(games, log=log, injury_history_data=history)
    write_params(params)
    print("elo: %s  (2010-2024 brier %.5f, n=%d)" % (params["tuning"]["elo"]["best"], params["tuning"]["elo"]["brier"], params["tuning"]["elo"]["n"]))
    print("qb: kq=%d  gate: with=%.5f without=%.5f -> enabled=%s" % (params["qb"]["k"], params["qb_gate"]["brier_with_qb"], params["qb_gate"]["brier_without_qb"], params["qb"]["enabled"]))
    print("blend: w=%.2f  (2015-2024 brier %.5f, n=%d)" % (params["blend"]["model_weight"], params["blend_fit"]["brier"], params["blend_fit"]["n"]))
    print("score: league_average_total=%.4f (season %d)" % (params["score"]["league_average_total"], params["score"]["league_average_season"]))
    print(format_injury_table(params))
    print(evaluate.format_table(params["evaluation"]))
    print("wrote %s in %.1f s" % (evaluate.PARAMS_PATH, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
