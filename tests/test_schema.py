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

    def test_injury_constants(self):
        self.assertEqual(schema.RATIONALE_KEYS, ("rating", "home_field", "quarterback", "rest", "injuries", "form", "market", "verdict"))
        self.assertEqual(schema.INJURY_STATUSES, ("Out", "Doubtful", "Questionable", "Probable"))
        self.assertEqual(schema.INJURY_GROUPS, ("OL", "WR_TE", "RB", "DL_EDGE", "LB", "DB", "QB", "ST"))
        self.assertEqual(set(schema.INJURY_WEIGHT_GROUPS), set(schema.INJURY_GROUPS) - {"QB", "ST"})
        self.assertIn("injuries", schema.OFFICIAL_KEYS)
        self.assertEqual(len(schema.OFFICIAL_KEYS), 24)

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

    def test_model_fixture_injury_coverage(self):
        games = self.model["games"]
        p = self.model["params"]["injury"]
        self.assertFalse(p["enabled"])
        self.assertEqual(set(p), {"enabled", "weights", "status_mult", "gate"})
        blocks = [g["official"]["injuries"] for g in games]
        for b in blocks:
            self.assertEqual(set(b), set(schema.INJURIES_KEYS))
            self.assertFalse(b["enabled"])
            self.assertEqual((b["elo_home"], b["elo_away"], b["elo"]), (0, 0, 0))
        reports = [b[s] for b in blocks for s in ("home", "away")]
        self.assertTrue(any(not r["available"] for r in reports))
        self.assertTrue(any(r["available"] and not r["report_filed"] for r in reports))
        self.assertTrue(any(r["report_filed"] and r["total_listed"] == 0 or r["report_filed"] and r["starters_out"] == 0 for r in reports))
        self.assertTrue(any(r["starters_out"] >= 2 for r in reports))
        self.assertTrue(any(r["starters_doubtful"] >= 1 for r in reports))
        self.assertTrue(any(r["qb_listed"] == "Questionable" for r in reports))
        players = [pl for r in reports for pl in r["players"]]
        self.assertTrue(any(pl["covered_by_qb_term"] and pl["starter"] for pl in players))
        self.assertTrue(any(pl["gsis_id"] is None for pl in players))
        self.assertTrue(any(pl["starter"] and pl["status"] == "Questionable" for pl in players))
        with_key = [g["game_id"] for g in games if "injuries" in g["official"]["rationale"]]
        self.assertGreaterEqual(len(with_key), 3)
        self.assertLess(len(with_key), len(games))
        for g in games:
            self.assertEqual(g["official"]["rationale"], schema.expected_rationale_keys(g))
            if "injuries" in g["official"]["rationale"]:
                s = g["official"]["rationale_sentences"][g["official"]["rationale"].index("injuries")]
                self.assertIn("lists", s)
                self.assertTrue(s.endswith("shown for context, not in the number yet."))

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

    def test_injury_rules(self):
        m = copy.deepcopy(self.model)
        g = next(x for x in m["games"] if x["game_id"] == "2026_05_TB_DAL")
        inj = g["official"]["injuries"]
        self.assertTrue(schema.injury_rationale_triggers(inj))
        self.assertFalse(schema.injury_rationale_triggers(next(x for x in m["games"] if x["game_id"] == "2026_05_CLE_NYJ")["official"]["injuries"]))
        # ordering: a non-starter ahead of a starter
        inj["home"]["players"].reverse()
        # counts: starters_out no longer matches the players
        inj["away"]["starters_out"] = 7
        # covered flag must follow the group
        inj["away"]["players"][0]["covered_by_qb_term"] = True
        # elo arithmetic and sign (on a game whose reports are intact, so the arithmetic is reached)
        g5 = next(x for x in m["games"] if x["game_id"] == "2026_05_PHI_JAX")
        g5["official"]["injuries"]["elo"] = 1.5
        g5["official"]["injuries"]["elo_home"] = 2.0
        # unavailable report that is not all zeros
        g2 = next(x for x in m["games"] if x["game_id"] == "2026_05_CHI_GB")
        g2["official"]["injuries"]["home"]["total_listed"] = 1
        # rationale: drop the injuries key where the trigger fires; add it where it does not
        g3 = next(x for x in m["games"] if x["game_id"] == "2026_05_LV_NE")
        i = g3["official"]["rationale"].index("injuries")
        g3["official"]["rationale"].pop(i)
        g3["official"]["rationale_sentences"].pop(i)
        g4 = next(x for x in m["games"] if x["game_id"] == "2026_05_CLE_NYJ")
        g4["official"]["rationale"].insert(2, "injuries")
        g4["official"]["rationale_sentences"].insert(2, "x")
        # params: a bad weight key, a status multiplier out of range, enabled without the gate
        m["params"]["injury"]["weights"]["QB"] = 50
        m["params"]["injury"]["status_mult"]["Out"] = 1.5
        m["params"]["injury"]["enabled"] = True
        joined = "\n".join(schema.validate_model(m))
        self.assertIn("players: must list starters first", joined)
        self.assertIn("starters_out: 7 but the players imply", joined)
        self.assertIn("covered_by_qb_term must equal", joined)
        self.assertIn("elo must equal elo_home - elo_away", joined)
        self.assertIn("elo_home: must be <= 0", joined)
        self.assertIn("unavailable report must be all false/[]/0/null", joined)
        self.assertIn("2026_05_LV_NE).official.rationale: ['rating', 'home_field', 'verdict'] does not match", joined)
        self.assertIn("2026_05_CLE_NYJ).official.rationale: ['rating', 'home_field', 'injuries'", joined)
        self.assertIn("params.injury.weights: unexpected keys ['QB']", joined)
        self.assertIn("params.injury.status_mult.Out: number in [0,1]", joined)
        self.assertIn("params.injury.enabled: true but the gate is not met", joined)
        self.assertIn("official.injuries.enabled: must equal params.injury.enabled", joined)

    def test_injury_term_enabled_when_the_gate_is_met(self):
        from src import injuries
        m = copy.deepcopy(self.model)
        p = m["params"]["injury"]
        p["enabled"] = True
        p["gate"] = {"holdout_brier_with": 0.2100, "holdout_brier_without": 0.2150, "improvement_required": 0.001}
        for g in m["games"]:
            inj = g["official"]["injuries"]
            inj["enabled"] = True
            inj["elo_home"] = injuries.injury_elo(inj["home"], m["params"])
            inj["elo_away"] = injuries.injury_elo(inj["away"], m["params"])
            inj["elo"] = inj["elo_home"] - inj["elo_away"]
        self.assertEqual(schema.validate_model(m), [])
        self.assertTrue(any(g["official"]["injuries"]["elo"] != 0 for g in m["games"]))
        # the formula is checked: a hand-edited term is rejected
        g = next(x for x in m["games"] if x["game_id"] == "2026_05_TB_DAL")
        g["official"]["injuries"]["elo_home"] -= 1.0
        g["official"]["injuries"]["elo"] -= 1.0
        self.assertTrue(any("D18.6 on the home report gives" in x for x in schema.validate_model(m)))
        # the gate itself: an improvement below the threshold cannot enable the term
        p["gate"]["holdout_brier_with"] = 0.2145
        self.assertTrue(any("gate is not met" in x for x in schema.validate_model(m)))
        # a missing params.injury block is a problem on its own
        del m["params"]["injury"]
        self.assertTrue(any("params: missing keys ['injury']" in x for x in schema.validate_model(m)))

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
