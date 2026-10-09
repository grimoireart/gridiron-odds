"""Tests for src/schema.py: constants, fixtures validate, broken fixtures are reported."""
import copy
import json
import os
import unittest

from src import schema

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class ConstantsTests(unittest.TestCase):
    def test_teams_and_divisions(self):
        self.assertEqual(len(schema.TEAMS), 32)
        self.assertEqual(len(schema.DIVISIONS), 8)
        self.assertTrue(all(len(v) == 4 for v in schema.DIVISIONS.values()))
        self.assertEqual(schema.SCHEMA_VERSION, "1.0.0")
        self.assertEqual(schema.TIERS, ("Toss-up", "Lean", "Strong", "Lock-ish"))
        self.assertEqual(schema.DEFAULT_BOOKS, ("draftkings", "fanduel", "betmgm", "betrivers"))

    def test_aliases(self):
        self.assertEqual(schema.ALIAS["STL"], "LA")
        self.assertEqual(schema.ALIAS["SD"], "LAC")
        self.assertEqual(schema.ALIAS["OAK"], "LV")
        self.assertEqual(schema.ALIAS["KC"], "KC")
        self.assertEqual(schema.code_from_name("Washington Football Team"), "WAS")
        self.assertEqual(schema.code_from_name("Los Angeles Rams"), "LA")
        self.assertEqual(schema.code_from_name("  san diego   chargers "), "LAC")
        self.assertIsNone(schema.code_from_name("Hartford Whalers"))
        for t in schema.TEAM_META:
            self.assertEqual(schema.code_from_name(t["full_name"]), t["code"])

    def test_tier_for(self):
        self.assertEqual(schema.tier_for(0.5), "Toss-up")
        self.assertEqual(schema.tier_for(0.55), "Lean")
        self.assertEqual(schema.tier_for(0.6499), "Lean")
        self.assertEqual(schema.tier_for(0.65), "Strong")
        self.assertEqual(schema.tier_for(0.78), "Lock-ish")


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.model = load("model_fixture.json")

    def test_model_fixture_is_valid(self):
        self.assertEqual(schema.validate_model(self.model), [])

    def test_model_fixture_coverage(self):
        games = self.model["games"]
        self.assertEqual(len(games), 8)
        self.assertEqual(sum(1 for g in games if g["league_date"] == "2026-10-11"), 5)
        self.assertEqual(sum(1 for g in games if g["league_date"] == "2026-10-12"), 1)
        finals = [g for g in games if g["status"] == "final"]
        self.assertEqual(sorted(g["result"]["hit"] for g in finals), [False, True])
        self.assertTrue(any(g["neutral"] for g in games))
        self.assertTrue(any(g["official"]["market"] is None for g in games))
        self.assertTrue(any(g["official"]["edge"] for g in games))
        self.assertTrue(any(
            (g["official"]["factors"]["qb_away"] or {}).get("differs_from_usual") or (g["official"]["factors"]["qb_home"] or {}).get("differs_from_usual")
            for g in games))
        self.assertEqual({g["official"]["tier"] for g in games}, set(schema.TIERS))
        self.assertEqual(len(self.model["teams"]), 32)

    def test_overlay_fixture_is_valid(self):
        ov = load("overlay_fixture.json")
        self.assertEqual(schema.validate_overlay(ov), [])
        self.assertFalse(ov["canonical"])
        self.assertEqual(len(ov["games"]), 5)
        for g in ov["games"]:
            self.assertIsNotNone(schema.code_from_name(g["home"]))
            self.assertEqual(len(g["books"]), 4)

    def test_odds_api_sample_shape(self):
        raw = load("odds_api_sample.json")
        self.assertEqual(len(raw), 2)
        for g in raw:
            self.assertEqual(set(g), {"id", "sport_key", "sport_title", "commence_time", "home_team", "away_team", "bookmakers"})
            self.assertEqual([b["key"] for b in g["bookmakers"]], ["draftkings", "fanduel", "williamhill_us"])
            for b in g["bookmakers"]:
                self.assertEqual([m["key"] for m in b["markets"]], ["h2h", "spreads", "totals"])

    def test_broken_fixture_reports_problems(self):
        # value problems (key set intact)
        m = copy.deepcopy(self.model)
        g = m["games"][2]
        g["official"]["p_home_model"] = 1.7
        g["official"]["tier"] = "Lean+"
        g["official"]["factors"]["home_rank"] = 40
        m["teams"].pop()
        m["track_record"]["units"]["break_even_minus_110"] = 0.5
        problems = schema.validate_model(m)
        joined = "\n".join(problems)
        self.assertIn("p_home_model", joined)
        self.assertIn("Lean+", joined)
        self.assertIn("home_rank", joined)
        self.assertIn("teams: must be a list of exactly 32", joined)
        self.assertIn("break_even_minus_110", joined)
        self.assertGreaterEqual(len(problems), 5)
        # a wrong key set is reported on its own (the object is not checked further)
        m2 = copy.deepcopy(self.model)
        del m2["games"][2]["official"]["parlay_eligible"]
        m2["games"][3]["official"]["live_market"] = {}
        problems = schema.validate_model(m2)
        self.assertTrue(any("missing keys ['parlay_eligible']" in x for x in problems))
        self.assertTrue(any("unexpected keys ['live_market']" in x for x in problems))

    def test_consistency_rules(self):
        m = copy.deepcopy(self.model)
        g = next(x for x in m["games"] if x["official"]["market"] is None)
        g["official"]["p_home_market"] = 0.6  # not allowed without a market
        g2 = next(x for x in m["games"] if x["status"] == "final")
        g2["result"]["hit"] = not g2["result"]["hit"]
        g3 = next(x for x in m["games"] if x["neutral"])
        g3["official"]["factors"]["hfa_elo"] = 40
        problems = schema.validate_model(m)
        joined = "\n".join(problems)
        self.assertIn("p_home_market: must be null when market is null", joined)
        self.assertIn("hit does not match", joined)
        self.assertIn("hfa_elo: must be 0 at a neutral site", joined)

    def test_wrong_major_version_and_extra_key(self):
        m = copy.deepcopy(self.model)
        m["schema_version"] = "2.0.0"
        m["live_market"] = {}
        problems = schema.validate_model(m)
        self.assertTrue(any("unexpected keys ['live_market']" in p for p in problems))
        self.assertEqual(schema.validate_model({"schema_version": "1.0.0"})[0][:6], "model:")

    def test_sim_sums_checked(self):
        m = copy.deepcopy(self.model)
        for t in m["teams"]:
            if t["conference"] == "AFC":
                t["simulation"]["playoffs"] = 0.3
        problems = schema.validate_model(m)
        self.assertTrue(any("AFC playoff probabilities sum" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
