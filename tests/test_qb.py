"""Tests for src/qb.py and the QB term inside elo.replay (D2): keyed by id, new id at Q0,
update direction, lazy inactivity decay, season regression, usual starter, QB swap lowers p."""
import copy
import unittest

from src import elo, qb, tune
from tests.test_elo import game


def params(**over):
    p = copy.deepcopy(tune.DEFAULT_PARAMS)
    p["qb"]["enabled"] = True
    p["score"]["league_average_total"] = 45.0
    p["score"]["league_average_season"] = 2020
    p["qb"].update(over)
    return p


A, B, C = "00-0000001", "00-0000002", "00-0000003"


class PureHelperTests(unittest.TestCase):
    def test_inactivity_is_per_completed_year_and_pure(self):
        self.assertEqual(qb.qb_inactivity(10.0, 0, 25), 10.0)
        self.assertEqual(qb.qb_inactivity(10.0, 364, 25), 10.0)
        self.assertEqual(qb.qb_inactivity(10.0, 365, 25), -15.0)
        self.assertEqual(qb.qb_inactivity(10.0, 729, 25), -15.0)
        self.assertEqual(qb.qb_inactivity(10.0, 730, 25), -40.0)
        self.assertEqual(qb.qb_inactivity(10.0, 400, 25), qb.qb_inactivity(10.0, 400, 25))  # same input, same output
        self.assertEqual(qb.qb_inactivity(-50.0, 365, 25), -75.0)  # D2: subtracts, does not regress toward 0

    def test_season_regress(self):
        self.assertAlmostEqual(qb.season_regress(100.0, 0.2), 80.0)
        self.assertAlmostEqual(qb.season_regress(-50.0, 0.2), -40.0)

    def test_days_between(self):
        self.assertEqual(qb.days_between("2020-09-13", "2021-09-12"), 364)
        self.assertEqual(qb.days_between("2020-09-13", "2021-09-13"), 365)

    def test_effective_q(self):
        p = params()
        self.assertEqual(qb.effective_q(None, "2021-09-12", p), -50.0)
        st = {"q": 20.0, "last_start_date": "2019-09-01"}
        self.assertEqual(qb.effective_q(st, "2021-09-12", p), -30.0)  # two completed years
        self.assertEqual(st["q"], 20.0)  # not mutated

    def test_usual_starter(self):
        starts = [{"id": A, "name": "A"}] * 5 + [{"id": B, "name": "B"}] * 3
        self.assertEqual(qb.usual_starter(starts, 8)["id"], A)
        self.assertEqual(qb.usual_starter(starts, 3)["id"], B)  # only the window counts
        tie = [{"id": A, "name": "A"}, {"id": B, "name": "B"}, {"id": A, "name": "A"}, {"id": B, "name": "B"}]
        self.assertEqual(qb.usual_starter(tie, 8)["id"], B)  # tie -> most recent
        self.assertIsNone(qb.usual_starter([], 8))
        old = [{"id": A, "name": "A"}] * 10 + [{"id": B, "name": "B"}] * 5
        self.assertEqual(qb.usual_starter(old, 8)["id"], B)  # 5 of the last 8 are B


class ReplayQbTests(unittest.TestCase):
    def test_new_id_starts_at_q0_and_update_direction(self):
        g = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B, home_qb_name="Alpha", away_qb_name="Beta")
        rep = elo.replay([g], params())
        pre = rep["pregame"][g["game_id"]]
        self.assertEqual(pre["q_home"], -50.0)
        self.assertEqual(pre["q_away"], -50.0)
        self.assertEqual(pre["qb_home_starts"], 0)
        self.assertEqual(pre["d"], 40)  # equal Q cancels; HFA only
        self.assertGreater(rep["qb"][A]["q"], -50.0)  # winner's QB up
        self.assertLess(rep["qb"][B]["q"], -50.0)  # loser's QB down
        self.assertAlmostEqual(rep["qb"][A]["q"] + 50, -(rep["qb"][B]["q"] + 50), places=12)
        self.assertEqual(rep["qb"][A]["starts"], 1)
        self.assertEqual(rep["qb"][A]["last_start_date"], "2021-09-12")
        self.assertEqual(rep["qb"][A]["team"], "KC")
        self.assertEqual(rep["qb"][A]["name"], "Alpha")
        self.assertEqual(rep["usual_starter"]["KC"], {"id": A, "name": "Alpha"})
        self.assertEqual(rep["usual_starter"]["DEN"], {"id": B, "name": "Beta"})
        self.assertIsNone(rep["usual_starter"]["CHI"])

    def test_qb_update_uses_team_mult_and_kq(self):
        p = params(k=6)
        g = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B)
        rep = elo.replay([g], p)
        pre = rep["pregame"][g["game_id"]]
        team_delta = rep["ratings"]["KC"] - 1500.0
        # team delta = K*mult*(s-p), qb delta = KQ*mult*(s-p) -> ratio KQ/K
        self.assertAlmostEqual(rep["qb"][A]["q"] + 50, team_delta * 6 / 20, places=12)
        self.assertAlmostEqual(pre["p_home"], elo.win_prob(40))

    def test_keyed_by_id_not_name(self):
        g1 = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B, home_qb_name="Same Name")
        g2 = game(home_team="KC", away_team="CHI", home_score=27, away_score=17, home_qb_id=C, away_qb_id=B, home_qb_name="Same Name", gameday="2021-09-19", week="2")
        rep = elo.replay([g1, g2], params())
        self.assertIn(A, rep["qb"])
        self.assertIn(C, rep["qb"])
        self.assertEqual(rep["pregame"][g2["game_id"]]["q_home"], -50.0)  # a new id is a first start
        self.assertEqual(rep["pregame"][g2["game_id"]]["qb_home_starts"], 0)

    def test_swapping_starter_for_backup_lowers_win_prob(self):
        wins = [
            game(home_team="KC", away_team="DEN", home_score=30, away_score=10, home_qb_id=A, away_qb_id=B, gameday="2021-09-12", week="1"),
            game(home_team="KC", away_team="CHI", home_score=30, away_score=10, home_qb_id=A, away_qb_id=C, gameday="2021-09-19", week="2"),
            game(home_team="KC", away_team="SEA", home_score=30, away_score=10, home_qb_id=A, away_qb_id="00-0000009", gameday="2021-09-26", week="3"),
        ]
        usual = game(home_team="KC", away_team="DEN", home_qb_id=A, away_qb_id=B, gameday="2021-10-03", week="4")
        backup = game(home_team="KC", away_team="DEN", home_qb_id="00-0000099", away_qb_id=B, gameday="2021-10-03", week="4")
        p = params()
        rep_u = elo.replay(wins + [usual], p)
        rep_b = elo.replay(wins + [backup], p)
        pu = rep_u["pregame"][usual["game_id"]]
        pb = rep_b["pregame"][backup["game_id"]]
        self.assertEqual(pu["elo_home"], pb["elo_home"])  # team rating identical
        self.assertGreater(pu["q_home"], pb["q_home"])
        self.assertEqual(pb["q_home"], -50.0)
        self.assertLess(pb["p_home"], pu["p_home"])
        self.assertEqual(rep_u["usual_starter"]["KC"]["id"], A)
        # explicit 60-point swap through the pure d function
        self.assertLess(elo.win_prob(elo.pregame_d(1550, 1500, 40, 0, -60, 0)), elo.win_prob(elo.pregame_d(1550, 1500, 40, 0, 0, 0)))

    def test_blank_starter_means_no_qb_term(self):
        played = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B)
        future = game(home_team="KC", away_team="DEN", gameday="2021-09-19", week="2", home_qb_id="", away_qb_id="")
        rep = elo.replay([played, future], params())
        pre = rep["pregame"][future["game_id"]]
        self.assertIsNone(pre["q_home"])
        self.assertIsNone(pre["q_away"])
        self.assertIsNone(pre["qb_home_starts"])
        self.assertAlmostEqual(pre["d"], pre["elo_home"] - pre["elo_away"] + 40, places=12)

    def test_disabled_qb_gives_null_terms_and_identical_team_ratings_to_no_qb_formula(self):
        g = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B)
        p = params()
        p["qb"]["enabled"] = False
        rep = elo.replay([g], p)
        self.assertIsNone(rep["pregame"][g["game_id"]]["q_home"])
        self.assertEqual(rep["qb"], {})

    def test_season_regression_and_lazy_inactivity_decay(self):
        p = params(annual_reversion=0.2, inactivity_penalty=25)
        g1 = game(home_team="KC", away_team="DEN", home_score=27, away_score=17, home_qb_id=A, away_qb_id=B, season="2020", gameday="2020-09-13")
        q_after = elo.replay([g1], p)["qb"][A]["q"]
        # 364 days later: regression only
        g2 = game(home_team="KC", away_team="CHI", home_qb_id=A, away_qb_id=C, season="2021", gameday="2021-09-12")
        self.assertAlmostEqual(elo.replay([g1, g2], p)["pregame"][g2["game_id"]]["q_home"], q_after * 0.8, places=12)
        # 371 days later: regression then one completed-year penalty, applied once
        g3 = game(home_team="KC", away_team="CHI", home_score=20, away_score=10, home_qb_id=A, away_qb_id=C, season="2021", gameday="2021-09-19", week="2")
        rep = elo.replay([g1, g3], p)
        self.assertAlmostEqual(rep["pregame"][g3["game_id"]]["q_home"], q_after * 0.8 - 25, places=12)
        self.assertEqual(rep["qb"][A]["last_start_date"], "2021-09-19")
        self.assertEqual(rep["qb"][A]["starts"], 2)
        # the stored q after g3 is the decayed value plus the game update (decay not re-applied)
        g4 = game(home_team="KC", away_team="SEA", home_qb_id=A, away_qb_id=C, season="2021", gameday="2021-09-26", week="3")
        rep4 = elo.replay([g1, g3, g4], p)
        self.assertAlmostEqual(rep4["pregame"][g4["game_id"]]["q_home"], rep["qb"][A]["q"], places=12)

    def test_qb_contribution_to_d(self):
        g1 = game(home_team="KC", away_team="DEN", home_score=30, away_score=10, home_qb_id=A, away_qb_id=B)
        g2 = game(home_team="DEN", away_team="KC", home_qb_id=B, away_qb_id=A, gameday="2021-09-19", week="2")
        rep = elo.replay([g1, g2], params())
        pre = rep["pregame"][g2["game_id"]]
        self.assertAlmostEqual(pre["d"], elo.pregame_d(pre["elo_home"], pre["elo_away"], 40, 0, pre["q_home"], pre["q_away"]), places=12)
        self.assertLess(pre["q_home"], pre["q_away"])  # DEN's QB lost, KC's won


if __name__ == "__main__":
    unittest.main()
