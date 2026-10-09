"""Tests for src/elo.py: formula acceptance items, fixture symmetry, rollover timing, aliases,
ties (D1), chronology with blank gametime, and full-CSV speed."""
import copy
import os
import time
import unittest

from src import data, elo, tune

HERE = os.path.dirname(os.path.abspath(__file__))
GAMES_CSV = os.path.join(os.path.dirname(HERE), "data", "games.csv")

FIELDS = [
    "game_id", "season", "game_type", "week", "gameday", "weekday", "gametime", "away_team", "away_score",
    "home_team", "home_score", "location", "result", "total", "overtime", "away_rest", "home_rest",
    "away_moneyline", "home_moneyline", "spread_line", "total_line", "div_game", "roof", "surface",
    "away_qb_id", "home_qb_id", "away_qb_name", "home_qb_name", "stadium",
]


def game(**kw):
    """A normalized game built from a raw CSV-shaped row; blanks everywhere not given."""
    base = {k: "" for k in FIELDS}
    base.update(season="2021", game_type="REG", week="1", gameday="2021-09-12", gametime="13:00", location="Home", home_rest="7", away_rest="7")
    base.update({k: ("" if v is None else str(v)) for k, v in kw.items()})
    if not base["game_id"]:
        base["game_id"] = "%s_%02d_%s_%s" % (base["season"], int(base["week"]), base["away_team"], base["home_team"])
    return data.normalize_row(base)


def params(**over):
    p = copy.deepcopy(tune.DEFAULT_PARAMS)
    p["qb"]["enabled"] = False
    p["score"]["league_average_total"] = 45.0
    p["score"]["league_average_season"] = 2020
    for k, v in over.items():
        p["elo"][k] = v
    return p


class FormulaTests(unittest.TestCase):
    def test_win_prob_values_and_symmetry(self):
        self.assertEqual(elo.win_prob(0), 0.5)
        self.assertAlmostEqual(elo.win_prob(400), 0.90909, places=4)
        for d in (-300, -37.5, 12, 250):
            self.assertAlmostEqual(elo.win_prob(d) + elo.win_prob(-d), 1.0, places=12)

    def test_mov_multiplier(self):
        self.assertAlmostEqual(elo.mov_multiplier(10, 0.0, 1.0), __import__("math").log(11))
        self.assertLess(elo.mov_multiplier(10, 200.0, 1.0), elo.mov_multiplier(10, -200.0, 1.0))  # favorites gain less
        self.assertEqual(elo.mov_multiplier(0, 150.0, 1.0), 1.0)  # tie uses the tie multiplier, not ln(1)=0
        self.assertEqual(elo.mov_multiplier(0, 150.0, 0.5), 0.5)

    def test_rollover_arithmetic(self):
        self.assertAlmostEqual(elo.rollover(1600, 0.4), 1560.0)
        self.assertEqual(elo.rollover(1500, 0.4), 1500.0)

    def test_pregame_d(self):
        self.assertEqual(elo.pregame_d(1550, 1500, 40, 4, 10, -20), 50 + 40 + 4 + 30)

    def test_ranks(self):
        self.assertEqual(elo.ranks({"A": 1500, "B": 1600, "C": 1500}), {"B": 1, "A": 2, "C": 3})


class FixtureTests(unittest.TestCase):
    def test_ten_point_home_win_is_symmetric(self):
        g = game(home_team="KC", away_team="DEN", home_score=27, away_score=17)
        rep = elo.replay([g], params())
        self.assertGreater(rep["ratings"]["KC"], 1500)
        self.assertLess(rep["ratings"]["DEN"], 1500)
        self.assertAlmostEqual(rep["ratings"]["KC"] - 1500, 1500 - rep["ratings"]["DEN"], places=12)
        pre = rep["pregame"][g["game_id"]]
        self.assertEqual(pre["elo_home"], 1500)  # recorded BEFORE the update
        self.assertEqual(pre["d"], 40)  # HFA only, rest equal
        self.assertEqual(pre["p_home"], elo.win_prob(40))
        self.assertIsNone(pre["q_home"])  # QB disabled -> null
        self.assertEqual(rep["history"]["KC"], [[2021, 1, g["game_id"], rep["ratings"]["KC"]]])
        self.assertEqual(rep["records"]["KC"], {"w": 1, "l": 0, "t": 0})
        self.assertEqual(rep["records"]["DEN"], {"w": 0, "l": 1, "t": 0})

    def test_away_win_mirrors_home_win(self):
        p = params()
        home = elo.replay([game(home_team="KC", away_team="DEN", home_score=27, away_score=17, location="Neutral")], p)
        away = elo.replay([game(home_team="DEN", away_team="KC", home_score=17, away_score=27, location="Neutral")], p)
        self.assertAlmostEqual(home["ratings"]["KC"], away["ratings"]["KC"], places=12)
        self.assertAlmostEqual(home["ratings"]["DEN"], away["ratings"]["DEN"], places=12)

    def test_rollover_applied_exactly_once_before_first_game_of_season(self):
        initial = {"ratings": {"KC": 1600.0}}  # state at the end of the previous season
        g1 = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-09-12")
        g2 = game(home_team="KC", away_team="CHI", home_score=20, away_score=10, gameday="2021-09-19", week="2")
        g3 = game(home_team="KC", away_team="DEN", home_score=20, away_score=10, season="2022", gameday="2022-09-11")
        rep = elo.replay([g1, g2, g3], params(reversion=0.4), initial=initial)
        self.assertAlmostEqual(rep["pregame"][g1["game_id"]]["elo_home"], 1560.0, places=12)  # 1600 -> 1560 once
        after_g1 = rep["history"]["KC"][0][3]
        self.assertAlmostEqual(rep["pregame"][g2["game_id"]]["elo_home"], after_g1, places=12)  # no second rollover
        after_g2 = rep["history"]["KC"][1][3]
        self.assertAlmostEqual(rep["pregame"][g3["game_id"]]["elo_home"], elo.rollover(after_g2, 0.4), places=12)

    def test_no_rollover_within_a_season_across_calendar_year(self):
        g1 = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-12-26", week="16")
        g2 = game(home_team="KC", away_team="CHI", home_score=20, away_score=10, gameday="2022-01-02", week="17")
        rep = elo.replay([g1, g2], params())
        self.assertAlmostEqual(rep["pregame"][g2["game_id"]]["elo_home"], rep["history"]["KC"][0][3], places=12)

    def test_aliases_feed_one_rating(self):
        g1 = game(home_team="STL", away_team="SEA", home_score=20, away_score=10, season="2015", gameday="2015-09-13")
        g2 = game(home_team="LA", away_team="SEA", home_score=20, away_score=10, season="2015", gameday="2015-09-20", week="2")
        g3 = game(home_team="OAK", away_team="DEN", home_score=20, away_score=10, season="2015", gameday="2015-09-13")
        g4 = game(home_team="LV", away_team="DEN", home_score=20, away_score=10, season="2015", gameday="2015-09-20", week="2")
        rep = elo.replay([g1, g2, g3, g4], params())
        self.assertNotIn("STL", rep["ratings"])
        self.assertNotIn("OAK", rep["ratings"])
        self.assertEqual(len(rep["history"]["LA"]), 2)
        self.assertEqual(len(rep["history"]["LV"]), 2)
        self.assertAlmostEqual(rep["pregame"][g2["game_id"]]["elo_home"], rep["history"]["LA"][0][3], places=12)
        self.assertAlmostEqual(rep["pregame"][g4["game_id"]]["elo_home"], rep["history"]["LV"][0][3], places=12)

    def test_tie_moves_mismatched_teams_toward_each_other(self):
        g = game(home_team="KC", away_team="DEN", home_score=20, away_score=20, location="Neutral")
        rep = elo.replay([g], params(tie_multiplier=1.0), initial={"ratings": {"KC": 1700.0}, "season": 2021})
        self.assertLess(rep["ratings"]["KC"], 1700.0)
        self.assertGreater(rep["ratings"]["DEN"], 1500.0)
        self.assertAlmostEqual(1700.0 - rep["ratings"]["KC"], rep["ratings"]["DEN"] - 1500.0, places=12)
        self.assertEqual(rep["records"]["KC"], {"w": 0, "l": 0, "t": 1})

    def test_tie_at_equal_ratings_changes_nothing(self):
        g = game(home_team="KC", away_team="DEN", home_score=20, away_score=20, location="Neutral")
        rep = elo.replay([g], params())
        self.assertEqual(rep["ratings"]["KC"], 1500.0)
        self.assertEqual(rep["ratings"]["DEN"], 1500.0)

    def test_tie_multiplier_zero_reproduces_prototype_zero_update(self):
        g = game(home_team="KC", away_team="DEN", home_score=20, away_score=20)
        rep = elo.replay([g], params(tie_multiplier=0.0), initial={"ratings": {"KC": 1700.0}, "season": 2021})
        self.assertEqual(rep["ratings"]["KC"], 1700.0)

    def test_chronology_blank_gametime_sorts_last_on_its_day(self):
        timed = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gametime="13:00")
        blank = game(home_team="KC", away_team="CHI", home_score=20, away_score=10, gametime="")
        rep = elo.replay([blank, timed], params())  # input order deliberately reversed
        self.assertEqual(rep["pregame"][timed["game_id"]]["elo_home"], 1500.0)
        self.assertAlmostEqual(rep["pregame"][blank["game_id"]]["elo_home"], rep["history"]["KC"][0][3], places=12)
        self.assertEqual(rep["history"]["KC"][0][2], timed["game_id"])

    def test_shuffled_input_gives_identical_replay(self):
        gs = [
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-09-12"),
            game(home_team="DEN", away_team="CHI", home_score=10, away_score=13, gameday="2021-09-19", week="2"),
            game(home_team="CHI", away_team="KC", home_score=3, away_score=30, gameday="2021-09-26", week="3"),
            game(home_team="KC", away_team="CHI", home_score=3, away_score=30, gameday="2022-09-11", season="2022"),
        ]
        a = elo.replay(gs, params())
        b = elo.replay(list(reversed(gs)), params())
        self.assertEqual(a["ratings"], b["ratings"])
        self.assertEqual(a["pregame"], b["pregame"])

    def test_rest_and_neutral_terms(self):
        g = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_rest="10", away_rest="6")
        pre = elo.replay([g], params())["pregame"][g["game_id"]]
        self.assertEqual(pre["rest_elo"], 8)
        self.assertEqual(pre["hfa_elo"], 40)
        g2 = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_rest="", away_rest="6", location="Neutral")
        pre2 = elo.replay([g2], params())["pregame"][g2["game_id"]]
        self.assertEqual(pre2["rest_elo"], 0)  # a blank rest field => rest term 0
        self.assertEqual(pre2["hfa_elo"], 0)
        self.assertEqual(pre2["d"], 0)

    def test_unplayed_game_gets_pregame_from_final_state(self):
        played = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-09-12")
        future = game(home_team="DEN", away_team="KC", gameday="2021-09-19", week="2")
        rep = elo.replay([future, played], params())
        self.assertFalse(future["played"])
        pre = rep["pregame"][future["game_id"]]
        self.assertAlmostEqual(pre["elo_away"], rep["ratings"]["KC"], places=12)
        self.assertEqual(rep["records"]["KC"], {"w": 1, "l": 0, "t": 0})

    def test_unplayed_next_season_is_rolled_over_once_but_ratings_stay_unrolled(self):
        played = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-09-12")
        future = game(home_team="KC", away_team="DEN", gameday="2022-09-11", season="2022")
        rep = elo.replay([played, future], params(reversion=0.4))
        self.assertAlmostEqual(rep["pregame"][future["game_id"]]["elo_home"], elo.rollover(rep["ratings"]["KC"], 0.4), places=12)
        self.assertAlmostEqual(rep["ratings"]["KC"], rep["history"]["KC"][-1][3], places=12)

    def test_form_delta_spans_rollover(self):
        gs = [
            game(home_team="KC", away_team="DEN", home_score=27, away_score=17, gameday="2021-09-12"),
            game(home_team="KC", away_team="CHI", home_score=27, away_score=17, gameday="2022-09-11", season="2022"),
        ]
        rep = elo.replay(gs, params())
        self.assertIsNone(elo.form_delta(rep["history"], rep["pregame"], "SEA"))
        expected = rep["ratings"]["KC"] - rep["pregame"][gs[0]["game_id"]]["elo_home"]
        self.assertAlmostEqual(elo.form_delta(rep["history"], rep["pregame"], "KC", 3), expected, places=12)
        # window of 1 = change in the last game only
        last = rep["ratings"]["KC"] - rep["pregame"][gs[1]["game_id"]]["elo_home"]
        self.assertAlmostEqual(elo.form_delta(rep["history"], rep["pregame"], "KC", 1), last, places=12)


@unittest.skipUnless(os.path.exists(GAMES_CSV), "data/games.csv not cached")
class FullCsvTests(unittest.TestCase):
    def test_full_replay_is_fast_and_complete(self):
        games = data.load_games(GAMES_CSV)
        p = copy.deepcopy(tune.DEFAULT_PARAMS)
        t0 = time.time()
        rep = elo.replay(games, p)
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(len(rep["pregame"]), len(games))  # every game, played or not
        self.assertEqual(set(rep["ratings"]), set(elo.TEAMS))
        for g in games:
            if not g["played"] and not g["home_qb_id"]:
                self.assertIsNone(rep["pregame"][g["game_id"]]["q_home"])  # blank starter -> no QB term
        self.assertTrue(all(0 < v["p_home"] < 1 for v in rep["pregame"].values()))
        self.assertEqual(sorted(elo.ranks(rep["ratings"]).values()), list(range(1, 33)))


if __name__ == "__main__":
    unittest.main()
