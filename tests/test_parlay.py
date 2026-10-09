"""Tests for src/parlay.py (D9): the SPEC two-leg test, one book per candidate,
missing legs, duplicate games, leg counts, ordering, eligibility from official
fields, and the shared JavaScript fixture."""
import copy
import json
import os
import unittest

from src import parlay

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def leg(game_id, p, american, side="HOME"):
    return {"game_id": game_id, "side": side, "p": p, "american": american}


class MathTests(unittest.TestCase):
    def test_spec_two_independent_60_percent_legs(self):
        stats = parlay.combo_stats([leg("g1", 0.6, 100), leg("g2", 0.6, 100)])
        self.assertAlmostEqual(stats["hit"], 0.36, places=12)
        self.assertAlmostEqual(stats["decimal_odds"], 4.0)
        self.assertAlmostEqual(stats["payout"], 3.0)
        self.assertAlmostEqual(stats["ev"], 0.36 * 3.0 - 0.64, places=12)
        self.assertIn(stats["fair_american"], (177, 178))  # 1/0.36 = 2.7778 decimal = +177.8
        self.assertEqual(parlay.fair_american(0.36), 178)

    def test_fair_american_both_sides(self):
        self.assertEqual(parlay.fair_american(0.5), 100)
        self.assertEqual(parlay.fair_american(0.6), -150)
        self.assertEqual(parlay.fair_american(0.25), 300)
        self.assertEqual(parlay.fair_american(0.8), -400)
        self.assertEqual(parlay.fair_american(0.51), -104)
        self.assertLessEqual(parlay.fair_american(0.499), -100 * -1)  # positive side
        self.assertGreaterEqual(abs(parlay.fair_american(0.4999)), 100)
        with self.assertRaises(ValueError):
            parlay.fair_american(0.0)
        with self.assertRaises(ValueError):
            parlay.fair_american(1.0)

    def test_decimal_profit(self):
        self.assertAlmostEqual(parlay.decimal_profit(150), 1.5)
        self.assertAlmostEqual(parlay.decimal_profit(-120), 100 / 120)
        with self.assertRaises(ValueError):
            parlay.decimal_profit(-50)

    def test_ev_of_negative_vig_legs_is_negative(self):
        stats = parlay.combo_stats([leg("g1", 0.5, -110), leg("g2", 0.5, -110)])
        self.assertLess(stats["ev"], 0)
        self.assertAlmostEqual(stats["hit"], 0.25)


class RankingTests(unittest.TestCase):
    def legs_by_book(self):
        return {
            "draftkings": [leg("g1", 0.7, -200), leg("g2", 0.6, -130), leg("g3", 0.58, 110)],
            "fanduel": [leg("g1", 0.7, -210), leg("g3", 0.58, 105)],  # g2 missing at fanduel
        }

    def test_one_book_per_candidate_and_missing_leg_excludes(self):
        ranked = parlay.rank_parlays(self.legs_by_book())
        self.assertTrue(ranked)
        for c in ranked:
            self.assertIn(c["book"], ("draftkings", "fanduel"))
            if c["book"] == "fanduel":
                self.assertNotIn("g2", c["game_ids"])
            # every leg priced with that book's own price
            src = {l["game_id"]: l for l in self.legs_by_book()[c["book"]]}
            for l in c["legs"]:
                self.assertEqual(l["american"], src[l["game_id"]]["american"])
        fd = [c for c in ranked if c["book"] == "fanduel"]
        self.assertEqual(len(fd), 1)  # only the g1+g3 pair exists at fanduel
        self.assertEqual(fd[0]["game_ids"], ["g1", "g3"])
        dk = [c for c in ranked if c["book"] == "draftkings"]
        self.assertEqual(len(dk), 4)  # 3 pairs + 1 triple

    def test_no_two_legs_from_one_game(self):
        lb = {"draftkings": [leg("g1", 0.7, -200, "HOME"), leg("g1", 0.55, 170, "AWAY"), leg("g2", 0.6, -130)]}
        ranked = parlay.rank_parlays(lb)
        for c in ranked:
            self.assertEqual(len(set(c["game_ids"])), len(c["game_ids"]))
        self.assertEqual(len(ranked), 2)  # (g1H,g2) and (g1A,g2); never (g1H,g1A) nor the triple

    def test_leg_counts_and_top(self):
        legs = [leg("g%d" % i, 0.6, -110) for i in range(7)]
        ranked = parlay.rank_parlays({"draftkings": legs}, min_legs=2, max_legs=5)
        sizes = sorted(set(c["n_legs"] for c in ranked))
        self.assertEqual(sizes, [2, 3, 4, 5])
        from math import comb
        self.assertEqual(len(ranked), sum(comb(7, n) for n in (2, 3, 4, 5)))
        self.assertEqual(len(parlay.rank_parlays({"draftkings": legs}, top=5)), 5)
        self.assertEqual(parlay.rank_parlays({"draftkings": legs[:1]}), [])  # fewer legs than min_legs
        with self.assertRaises(ValueError):
            parlay.rank_parlays({"draftkings": legs}, min_legs=3, max_legs=2)

    def test_sorted_by_ev_then_hit_then_book_then_games(self):
        ranked = parlay.rank_parlays(self.legs_by_book())
        keys = [(-c["ev"], -c["hit"], c["book"], c["game_ids"]) for c in ranked]
        self.assertEqual(keys, sorted(keys))
        # identical legs at two books: EV ties broken by hit (equal) then book name
        lb = {"zbook": [leg("g1", 0.6, 100), leg("g2", 0.6, 100)], "abook": [leg("g1", 0.6, 100), leg("g2", 0.6, 100)]}
        ranked = parlay.rank_parlays(lb)
        self.assertEqual([c["book"] for c in ranked], ["abook", "zbook"])

    def test_candidate_shape(self):
        c = parlay.rank_parlays(self.legs_by_book())[0]
        self.assertEqual(set(c), {"book", "n_legs", "game_ids", "legs", "hit", "decimal_odds", "payout", "ev", "fair_american"})
        self.assertEqual(c["n_legs"], len(c["legs"]))
        self.assertEqual(c["game_ids"], [l["game_id"] for l in c["legs"]])
        self.assertIsInstance(c["fair_american"], int)
        stats = parlay.combo_stats(c["legs"])
        for k in ("hit", "decimal_odds", "payout", "ev", "fair_american"):
            self.assertEqual(c[k], stats[k])

    def test_pin_best_and_build(self):
        legs = [leg("g%d" % i, 0.6 + 0.02 * i, -110 - 5 * i) for i in range(5)]
        built = parlay.build_parlays({"draftkings": legs}, top=5)
        self.assertEqual(set(built), {"pinned", "ranked", "n_candidates"})
        self.assertEqual(len(built["ranked"]), 5)
        self.assertEqual(built["n_candidates"], 10 + 10 + 5 + 1)
        ranked_all = parlay.rank_parlays({"draftkings": legs})
        for n in (2, 3, 4):
            best = next(c for c in ranked_all if c["n_legs"] == n)
            self.assertEqual(built["pinned"][n], best)
        self.assertEqual(parlay.pin_best([]), {2: None, 3: None, 4: None})


class EligibilityTests(unittest.TestCase):
    def test_eligible_legs_from_model_fixture(self):
        games = load("model_fixture.json")["games"]
        legs = parlay.eligible_legs(games, "draftkings")
        ids = [l["game_id"] for l in legs]
        self.assertEqual(ids, ["2026_05_TB_DAL", "2026_05_PHI_JAX", "2026_05_CHI_GB", "2026_05_LV_NE", "2026_05_DEN_LAC", "2026_05_BUF_LA"])
        self.assertNotIn("2026_05_CLE_NYJ", ids)  # p_pick_blend 0.53 < 0.55
        self.assertNotIn("2026_05_CIN_MIA", ids)  # no market -> no prices
        by_id = {g["game_id"]: g for g in games}
        for l in legs:
            o = by_id[l["game_id"]]["official"]
            self.assertEqual(l["side"], o["pick"])
            self.assertEqual(l["p"], o["p_pick_blend"])
            self.assertEqual(l["american"], o["best_prices"]["draftkings"]["american"])
            self.assertGreaterEqual(l["p"], 0.55)

    def test_missing_book_price_drops_leg(self):
        games = copy.deepcopy(load("model_fixture.json")["games"])
        games[0]["official"]["best_prices"]["betmgm"] = None
        legs = parlay.eligible_legs(games, "betmgm")
        self.assertNotIn(games[0]["game_id"], [l["game_id"] for l in legs])
        self.assertEqual(parlay.eligible_legs(games, "williamhill_us"), [])


class FixtureTests(unittest.TestCase):
    """tests/fixtures/parlay_fixture.json is generated from this module; the
    JavaScript port must reproduce `expected` from `input`."""

    def test_fixture_reproduces_exactly(self):
        fx = load("parlay_fixture.json")
        inp, exp = fx["input"], fx["expected"]
        games = [{"game_id": g["game_id"], "official": {"pick": g["pick"], "p_pick_blend": g["p_pick_blend"], "best_prices": g["best_prices"]}}
                 for g in inp["games"]]
        legs_by_book = {b: parlay.eligible_legs(games, b, inp["min_p"]) for b in inp["books"]}
        self.assertEqual(json.loads(json.dumps(legs_by_book)), exp["legs_by_book"])
        built = parlay.build_parlays(legs_by_book, inp["min_legs"], inp["max_legs"], inp["top"])
        ranked_all = parlay.rank_parlays(legs_by_book, inp["min_legs"], inp["max_legs"])
        self.assertEqual(json.loads(json.dumps(ranked_all)), exp["ranked_all"])
        self.assertEqual(json.loads(json.dumps(built["ranked"])), exp["ranked_top"])
        self.assertEqual(json.loads(json.dumps({str(k): v for k, v in built["pinned"].items()})), exp["pinned"])
        self.assertEqual(built["n_candidates"], exp["n_candidates"])
        self.assertEqual(len(exp["ranked_top"]), inp["top"])

    def test_fixture_properties(self):
        exp = load("parlay_fixture.json")["expected"]
        books = set(exp["legs_by_book"])
        for c in exp["ranked_all"]:
            self.assertIn(c["book"], books)
            self.assertEqual(len(set(c["game_ids"])), len(c["game_ids"]))
            self.assertTrue(2 <= c["n_legs"] <= 5)
            priced = {l["game_id"]: l["american"] for l in exp["legs_by_book"][c["book"]]}
            for l in c["legs"]:
                self.assertEqual(l["american"], priced[l["game_id"]])
        evs = [c["ev"] for c in exp["ranked_all"]]
        self.assertEqual(evs, sorted(evs, reverse=True))
        # a leg missing at one book means that book has fewer candidates
        self.assertIsNotNone(exp["pinned"]["2"])
        self.assertEqual(exp["pinned"]["2"]["n_legs"], 2)


if __name__ == "__main__":
    unittest.main()
