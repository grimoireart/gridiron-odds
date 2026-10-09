"""Tests for src/evaluate.py and src/tune.py: no-vig, metrics, holdout shape, blend fit,
previous-full-season helpers, QB gate, and the params.json reference band (+/- 0.003)."""
import copy
import json
import os
import unittest

from src import data, elo, evaluate, tune
from tests.test_elo import game

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GAMES_CSV = os.path.join(ROOT, "data", "games.csv")
PARAMS = os.path.join(ROOT, "params.json")


def params():
    p = copy.deepcopy(tune.DEFAULT_PARAMS)
    p["score"]["league_average_total"] = 45.0
    p["score"]["league_average_season"] = 2020
    return p


class PureTests(unittest.TestCase):
    def test_no_vig(self):
        h = evaluate.no_vig_home(-150, 130)
        self.assertAlmostEqual(h, 0.5797, places=3)  # REVIEW_codex correction: ~0.580, not 0.40-0.45
        self.assertAlmostEqual(h + evaluate.no_vig_home(130, -150), 1.0, places=12)
        self.assertAlmostEqual(evaluate.no_vig_home(-110, -110), 0.5, places=12)
        self.assertAlmostEqual(evaluate.no_vig_home(100, 100), 0.5, places=12)

    def test_metrics(self):
        m = evaluate.metrics([(0.5, 1.0), (0.5, 0.0)])
        self.assertAlmostEqual(m["brier"], 0.25)
        self.assertAlmostEqual(m["accuracy"], 0.5)
        self.assertEqual(m["n"], 2)
        m2 = evaluate.metrics([(0.8, 1.0), (0.3, 0.0), (0.6, 0.5)])
        self.assertAlmostEqual(m2["brier"], (0.04 + 0.09 + 0.01) / 3)
        self.assertAlmostEqual(m2["accuracy"], 1.0)  # the tie is excluded from accuracy
        self.assertEqual(m2["n"], 3)
        self.assertEqual(evaluate.metrics([]), {"brier": None, "logloss": None, "accuracy": None, "n": 0})

    def test_previous_full_season_and_league_average_total(self):
        gs = [
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, season="2020", gameday="2020-09-13"),
            game(home_team="KC", away_team="CHI", home_score=20, away_score=20, season="2020", gameday="2020-09-20", week="2"),
            game(home_team="KC", away_team="DEN", home_score=30, away_score=10, season="2021", gameday="2021-09-12"),
            game(home_team="KC", away_team="CHI", season="2021", gameday="2021-09-19", week="2"),  # unplayed
        ]
        self.assertEqual(evaluate.previous_full_season(gs), 2020)
        self.assertAlmostEqual(evaluate.league_average_total(gs, 2020), 42.0)

    def test_holdout_shape_and_no_qb_equivalence(self):
        gs = [
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, season="2020", gameday="2020-09-13", home_qb_id="x1", away_qb_id="x2"),
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, season="2021", gameday="2021-09-12", home_moneyline="-150", away_moneyline="130", home_qb_id="x1", away_qb_id="x2"),
            game(home_team="DEN", away_team="KC", home_score=10, away_score=17, season="2021", gameday="2021-09-19", week="2", home_qb_id="x2", away_qb_id="x1"),
        ]
        p = params()
        ev = evaluate.holdout(gs, p, 2021)
        self.assertEqual(set(ev), {"season", "samples", "model", "model_no_qb", "market", "blend"})
        self.assertEqual(ev["samples"], {"model": 2, "market_comparable": 1})
        for k in ("model", "model_no_qb", "market", "blend"):
            self.assertEqual(set(ev[k]), {"brier", "logloss", "accuracy"})
        self.assertNotEqual(ev["model"]["brier"], ev["model_no_qb"]["brier"])
        p_off = params()
        p_off["qb"]["enabled"] = False
        ev_off = evaluate.holdout(gs, p_off, 2021)
        self.assertEqual(ev_off["model"], ev_off["model_no_qb"])
        self.assertEqual(ev_off["model"], ev["model_no_qb"])  # same rows, same no-QB replay
        # blend endpoints
        p1 = params()
        p1["blend"]["model_weight"] = 1.0
        ev1 = evaluate.holdout(gs, p1, 2021)
        row = [r for r in evaluate.holdout_rows(gs, elo.replay(gs, p1), 2021) if r["p_market"] is not None][0]
        self.assertAlmostEqual(ev1["blend"]["brier"], (row["p_model"] - row["s"]) ** 2, places=12)
        p0 = params()
        p0["blend"]["model_weight"] = 0.0
        self.assertEqual(evaluate.holdout(gs, p0, 2021)["blend"], evaluate.holdout(gs, p0, 2021)["market"])

    def test_fit_blend_weight_picks_grid_minimum(self):
        gs = [
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, season="2015", gameday="2015-09-13", home_moneyline="-300", away_moneyline="250"),
            game(home_team="DEN", away_team="KC", home_score=10, away_score=17, season="2015", gameday="2015-09-20", week="2", home_moneyline="200", away_moneyline="-240"),
            game(home_team="CHI", away_team="KC", home_score=10, away_score=17, season="2016", gameday="2016-09-11", home_moneyline="300", away_moneyline="-360"),
        ]
        fit = evaluate.fit_blend_weight(gs, params(), (2015, 2024), step=0.05)
        self.assertEqual(len(fit["grid"]), 21)
        self.assertEqual(fit["n"], 3)
        self.assertEqual(fit["brier"], min(b for _, b in fit["grid"]))
        self.assertIn(fit["model_weight"], [w for w, _ in fit["grid"]])
        self.assertEqual(fit["model_weight"], 0.0)  # a confident, correct market wins here

    def test_qb_gate_decision_rule(self):
        gs = [game(home_team="KC", away_team="DEN", home_score=27, away_score=17, season="2021", gameday="2021-09-12", home_qb_id="x1", away_qb_id="x2")]
        gate = tune.qb_gate(gs, params(), 2021)
        self.assertEqual(set(gate), {"season", "n", "brier_with_qb", "brier_without_qb", "enabled"})
        self.assertEqual(gate["enabled"], gate["brier_with_qb"] <= gate["brier_without_qb"])


@unittest.skipUnless(os.path.exists(GAMES_CSV) and os.path.exists(PARAMS), "needs data/games.csv and params.json")
class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = data.load_games(GAMES_CSV)
        with open(PARAMS, encoding="utf-8") as fh:
            cls.params = json.load(fh)

    def test_params_json_has_contract_keys(self):
        for k in ("elo", "qb", "blend", "tier_bounds", "score", "sim", "books", "reference", "qb_gate", "tuned_at"):
            self.assertIn(k, self.params)
        self.assertEqual(self.params["reference"]["season"], 2025)
        self.assertIsNotNone(self.params["score"]["league_average_total"])

    def test_holdout_brier_within_band_of_params_json(self):
        ok, text = evaluate.reference_check(self.params, self.games)
        self.assertTrue(ok, text)

    def test_no_qb_holdout_matches_ground_truth(self):
        ev = evaluate.holdout(self.games, self.params, 2025)
        self.assertEqual(ev["samples"]["model"], 285)
        self.assertAlmostEqual(ev["model_no_qb"]["brier"], 0.2209, delta=0.001)  # D1: ties now mult=1
        self.assertAlmostEqual(ev["market"]["brier"], 0.2104, delta=0.001)

    def test_qb_gate_holds_on_holdout(self):
        ev = evaluate.holdout(self.games, self.params, 2025)
        if self.params["qb"]["enabled"]:
            self.assertLessEqual(ev["model"]["brier"], ev["model_no_qb"]["brier"])
        self.assertEqual(self.params["qb_gate"]["enabled"], self.params["qb"]["enabled"])


if __name__ == "__main__":
    unittest.main()
