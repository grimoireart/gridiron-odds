"""Tests for src/simulate.py (D11, CONTRACT 6.5)."""

import copy
import json
import os
import random
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import simulate as sim  # noqa: E402
from src.data import load_games  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "model_fixture.json")
GAMES_CSV = os.path.join(ROOT, "data", "games.csv")
TEAMS_JSON = os.path.join(ROOT, "src", "teams.json")

PARAMS = {"elo": {"k": 20, "hfa": 40, "rest_per_day": 2, "reversion": 0.4, "tie_multiplier": 1.0}}


def _fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _teams_json():
    with open(TEAMS_JSON, encoding="utf-8") as fh:
        return json.load(fh)["teams"]


def _conf_of(teams):
    return {(t.get("code") or t["team"]): t["conference"] for t in teams}


def _remaining_2026():
    games = load_games(GAMES_CSV)
    return [g for g in games if g["season"] == 2026 and g["game_type"] == "REG" and not g["played"]]


def _game(home, away, neutral=False, home_rest=7, away_rest=7, game_type="REG", played=False):
    return {
        "game_id": "2026_99_%s_%s" % (away, home),
        "game_type": game_type,
        "played": played,
        "home": home,
        "away": away,
        "neutral": neutral,
        "home_rest": home_rest,
        "away_rest": away_rest,
    }


class TestHelpers(unittest.TestCase):
    def test_win_prob(self):
        self.assertAlmostEqual(sim.win_prob(0), 0.5)
        self.assertAlmostEqual(sim.win_prob(400), 10 / 11, places=9)
        self.assertAlmostEqual(sim.win_prob(123) + sim.win_prob(-123), 1.0, places=12)

    def test_game_probability_terms(self):
        # HFA applied only when not neutral; rest term 0 when either rest is missing (D1)
        p_home = sim.game_probability(1500, 1500, False, 7, 7, 40, 2)
        self.assertAlmostEqual(p_home, sim.win_prob(40))
        self.assertAlmostEqual(sim.game_probability(1500, 1500, True, 7, 7, 40, 2), 0.5)
        self.assertAlmostEqual(sim.game_probability(1500, 1500, True, 10, 7, 40, 2), sim.win_prob(6))
        self.assertAlmostEqual(sim.game_probability(1500, 1500, True, None, 7, 40, 2), 0.5)
        self.assertAlmostEqual(sim.game_probability(1500, 1500, True, 10, None, 40, 2), 0.5)

    def test_prepare_filters_to_unplayed_reg_rows(self):
        fx = _fixture()
        ratings = {t["team"]: t["elo"] for t in fx["teams"]}
        games = [
            _game("KC", "BUF"),
            _game("KC", "BUF", game_type="WC"),
            _game("DET", "GB", played=True),
        ]
        state = sim.prepare(fx["teams"], games, ratings, PARAMS)
        self.assertEqual(len(state["games"]), 1)
        self.assertEqual(state["skipped_rows"], 2)
        self.assertEqual(len(state["codes"]), 32)
        self.assertEqual(len(state["div_members"]), 8)
        self.assertEqual([len(d) for d in state["conf_divisions"]], [4, 4])

    def test_prepare_rejects_missing_rating_or_record(self):
        fx = _fixture()
        ratings = {t["team"]: t["elo"] for t in fx["teams"]}
        bad = dict(ratings)
        del bad["KC"]
        with self.assertRaises(ValueError):
            sim.prepare(fx["teams"], [], bad, PARAMS)
        teams = _teams_json()  # no records attached
        with self.assertRaises(ValueError):
            sim.prepare(teams, [], ratings, PARAMS)
        # records supplied separately (replay["records"] shape) works with bare teams.json
        records = {t["team"]: t["record"] for t in fx["teams"]}
        state = sim.prepare(teams, [], ratings, PARAMS, records=records)
        self.assertEqual(state["base_w"][state["codes"].index("BAL")], 4)


class TestSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = _fixture()
        cls.teams = cls.fx["teams"]
        cls.ratings = {t["team"]: t["elo"] for t in cls.teams}
        cls.conf = _conf_of(cls.teams)
        cls.remaining = _remaining_2026()

    def test_real_schedule_shape(self):
        # Season-agnostic: the remaining slate is the unplayed REG rows; it shrinks every week,
        # so assert shape only (a hard floor of 200 broke the build on 2026-10-11).
        self.assertLessEqual(len(self.remaining), 272)
        self.assertTrue(all(g["game_type"] == "REG" and not g["played"] for g in self.remaining))

    def test_counts_sum_exactly(self):
        runs = 400
        raw = sim.simulate_counts(self.teams, self.remaining, self.ratings, PARAMS, runs=runs, seed=7)
        counts = raw["counts"]
        for conf in ("AFC", "NFC"):
            codes = [c for c in counts if self.conf[c] == conf]
            self.assertEqual(len(codes), 16)
            self.assertEqual(sum(counts[c]["playoffs"] for c in codes), 7 * runs)
            self.assertEqual(sum(counts[c]["division"] for c in codes), 4 * runs)
            self.assertEqual(sum(counts[c]["seed1"] for c in codes), runs)
        self.assertEqual(sum(v["super_bowl"] for v in counts.values()), runs)
        # every simulated game produces exactly one win
        self.assertEqual(sum(v["wins"] for v in counts.values()), runs * len(self.remaining))
        for v in counts.values():
            self.assertLessEqual(v["super_bowl"], v["playoffs"])
            self.assertLessEqual(v["division"], v["playoffs"])
            self.assertLessEqual(v["seed1"], v["division"])

    def test_probabilities_within_monte_carlo_tolerance(self):
        out = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=1000, seed=3)
        for conf in ("AFC", "NFC"):
            total = sum(v["playoffs"] for c, v in out["teams"].items() if self.conf[c] == conf)
            self.assertAlmostEqual(total, 7.0, delta=0.01)
        self.assertAlmostEqual(sum(v["super_bowl"] for v in out["teams"].values()), 1.0, delta=1e-9)
        for v in out["teams"].values():
            for k in ("playoffs", "division", "seed1", "super_bowl"):
                self.assertGreaterEqual(v[k], 0.0)
                self.assertLessEqual(v[k], 1.0)
            self.assertGreaterEqual(v["projected_wins"], 0.0)
            self.assertLessEqual(v["projected_wins"], 17.0)
        self.assertEqual(out["runs"], 1000)
        self.assertEqual(out["seed"], 3)
        self.assertEqual(out["notes"], out["sim_notes"])
        self.assertTrue(any("Ties are not simulated" in n for n in out["notes"]))

    def test_each_run_has_8_division_winners_two_one_seeds_no_duplicates(self):
        state = sim.prepare(self.teams, self.remaining, self.ratings, PARAMS)
        rng = random.Random(11)
        div_of = {t["team"]: t["division"] for t in self.teams}
        for _ in range(50):
            r = sim.run_once(state, rng)
            winners = [state["codes"][i] for i in r["division_winners"]]
            self.assertEqual(len(winners), 8)
            self.assertEqual(len({div_of[w] for w in winners}), 8)
            self.assertEqual(len(r["seeds"]), 2)
            ones = []
            for seeds in r["seeds"]:
                codes = [state["codes"][i] for i in seeds]
                self.assertEqual(len(codes), 7)
                self.assertEqual(len(set(codes)), 7)  # no duplicate qualifiers
                self.assertEqual(len({self.conf[c] for c in codes}), 1)  # one conference
                self.assertTrue(set(codes[:4]).issubset(winners))  # seeds 1-4 are division winners
                self.assertTrue(set(codes[4:]).isdisjoint(winners))  # 5-7 are wild cards
                ones.append(codes[0])
                self.assertIn(state["codes"][r["conference_champions"][len(ones) - 1]], codes)
            self.assertEqual(len(ones), 2)
            self.assertNotEqual(self.conf[ones[0]], self.conf[ones[1]])
            self.assertIn(r["super_bowl"], r["conference_champions"])
            self.assertEqual(sum(r["wins"]), len(self.remaining))

    def test_seeding_follows_record_then_elo(self):
        # 2 wild cards must each have at least as many standings points as every excluded team;
        # seeds 1-4 are in descending record order (ties broken by Elo + jitter).
        state = sim.prepare(self.teams, self.remaining, self.ratings, PARAMS)
        rng = random.Random(5)
        for _ in range(20):
            r = sim.run_once(state, rng)
            pts = [state["base_pts"][i] + 2 * r["wins"][i] for i in range(32)]
            for c, seeds in enumerate(r["seeds"]):
                for a, b in zip(seeds[:4], seeds[1:4]):
                    self.assertGreaterEqual(pts[a], pts[b])
                for a, b in zip(seeds[4:], seeds[5:]):
                    self.assertGreaterEqual(pts[a], pts[b])
                excluded = [i for i in state["conf_members"][c] if i not in seeds]
                worst_wc = min(pts[i] for i in seeds[4:])
                for i in excluded:
                    self.assertLessEqual(pts[i], worst_wc)

    def test_reproducible_by_seed(self):
        a = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=300, seed=2026)
        b = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=300, seed=2026)
        c = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=300, seed=2027)
        self.assertEqual(a, b)
        self.assertNotEqual(a["teams"], c["teams"])

    def test_inputs_not_mutated(self):
        ratings = dict(self.ratings)
        teams = copy.deepcopy(self.teams)
        games = copy.deepcopy(self.remaining)
        params = copy.deepcopy(PARAMS)
        sim.simulate(teams, games, ratings, params, runs=200, seed=1)
        self.assertEqual(ratings, self.ratings)
        self.assertEqual(teams, self.teams)
        self.assertEqual(games, self.remaining)
        self.assertEqual(params, PARAMS)

    def test_clinched_team_is_certain(self):
        # KC already 17-0 with no games left, every other AFC West team 0-17: division and
        # playoffs are 100% in every run; a 0-17 team with no games left never makes it.
        teams = copy.deepcopy(self.teams)
        for t in teams:
            if t["team"] == "KC":
                t["record"] = {"w": 17, "l": 0, "t": 0}
            elif t["division"] == "AFC West":
                t["record"] = {"w": 0, "l": 17, "t": 0}
            elif t["team"] == "CAR":
                t["record"] = {"w": 0, "l": 17, "t": 0}
        remaining = [g for g in self.remaining if "KC" not in (g["home"], g["away"]) and "CAR" not in (g["home"], g["away"])]
        out = sim.simulate(teams, remaining, self.ratings, PARAMS, runs=500, seed=9)
        self.assertEqual(out["teams"]["KC"]["division"], 1.0)
        self.assertEqual(out["teams"]["KC"]["playoffs"], 1.0)
        self.assertEqual(out["teams"]["KC"]["seed1"], 1.0)  # 17-0 beats any 13-game max elsewhere
        self.assertEqual(out["teams"]["KC"]["projected_wins"], 17.0)
        self.assertEqual(out["teams"]["CAR"]["playoffs"], 0.0)
        self.assertEqual(out["teams"]["CAR"]["projected_wins"], 0.0)

    def test_projected_wins_adds_simulated_to_real(self):
        teams = copy.deepcopy(self.teams)
        for t in teams:
            t["record"] = {"w": 3, "l": 1, "t": 1} if t["team"] == "KC" else {"w": 2, "l": 2, "t": 0}
        # one certain game: KC (1900) at home vs a 1100 team is ~0.99
        ratings = dict(self.ratings)
        ratings["KC"] = 1900.0
        ratings["CAR"] = 1100.0
        out = sim.simulate(teams, [_game("KC", "CAR")], ratings, PARAMS, runs=200, seed=4)
        self.assertGreater(out["teams"]["KC"]["projected_wins"], 3.95)
        self.assertLessEqual(out["teams"]["KC"]["projected_wins"], 4.0)
        self.assertAlmostEqual(out["teams"]["KC"]["projected_wins"] + out["teams"]["CAR"]["projected_wins"], 6.0)

    def test_neutral_and_rest_change_game_probability(self):
        ratings = {t["team"]: 1500.0 for t in self.teams}
        home_edge = sim.prepare(self.teams, [_game("KC", "CAR")], ratings, PARAMS)["games"][0][2]
        neutral = sim.prepare(self.teams, [_game("KC", "CAR", neutral=True)], ratings, PARAMS)["games"][0][2]
        rested = sim.prepare(self.teams, [_game("KC", "CAR", home_rest=10)], ratings, PARAMS)["games"][0][2]
        self.assertAlmostEqual(neutral, 0.5)
        self.assertAlmostEqual(home_edge, sim.win_prob(40))
        self.assertAlmostEqual(rested, sim.win_prob(46))

    def test_attach_fills_contract_simulation_block(self):
        out = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=50, seed=2)
        entries = sim.attach(self.teams, out)
        self.assertEqual(len(entries), 32)
        for e in entries:
            self.assertEqual(
                set(e["simulation"]), {"runs", "projected_wins", "playoffs", "division", "seed1", "super_bowl"}
            )
            self.assertEqual(e["simulation"]["runs"], 50)
            self.assertEqual(e["simulation"], {"runs": 50, **out["teams"][e["team"]]})
        # original entries untouched
        self.assertEqual(self.teams[0]["simulation"]["runs"], 10000)

    def test_timing_10k_runs_under_5s(self):
        t0 = time.perf_counter()
        out = sim.simulate(self.teams, self.remaining, self.ratings, PARAMS, runs=10000, seed=2026)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0, "10k-run simulation took %.2fs" % elapsed)
        self.assertEqual(out["runs"], 10000)
        self.assertAlmostEqual(sum(v["super_bowl"] for v in out["teams"].values()), 1.0, delta=1e-9)


if __name__ == "__main__":
    unittest.main()
