"""Tests for src/market.py: price math (Codex-corrected expectations), consensus/best price,
Odds API normalisation of the fixture, overlay matching edge cases, and the single official call."""
import io
import json
import os
import unittest
from contextlib import redirect_stderr

from src import market, schema

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def game(game_id, home, away, kickoff, played=False, status=None):
    g = {"game_id": game_id, "home": home, "away": away, "kickoff_utc": kickoff, "played": played}
    if status:
        g["status"] = status
    return g


class PriceMathTests(unittest.TestCase):
    def test_decimal_profit_both_signs(self):
        self.assertAlmostEqual(market.american_to_decimal_profit(150), 1.5)
        self.assertAlmostEqual(market.american_to_decimal_profit(-120), 100 / 120)
        self.assertAlmostEqual(market.american_to_decimal_profit(100), 1.0)
        self.assertAlmostEqual(market.american_to_decimal_profit(-100), 1.0)
        self.assertAlmostEqual(market.american_to_decimal_profit(-110), 0.9090909090909091)

    def test_rejects_zero_and_short_odds(self):
        for bad in (0, 50, -50, 99, -99, None, "150", True, float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                market.american_to_decimal_profit(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                market.implied_prob(bad)
        with self.assertRaises(ValueError):
            market.no_vig_pair(0, 150)
        with self.assertRaises(ValueError):
            market.no_vig_pair(-150, 80)

    def test_implied_prob(self):
        self.assertAlmostEqual(market.implied_prob(150), 0.4)
        self.assertAlmostEqual(market.implied_prob(-120), 120 / 220)
        self.assertAlmostEqual(market.implied_prob(100), 0.5)
        self.assertAlmostEqual(market.implied_prob(-100), 0.5)

    def test_no_vig_pair_codex_corrected(self):
        # raw implied 0.600 / 0.4348 -> normalised home ~0.580, away ~0.420, sum 1
        ph, pa = market.no_vig_pair(-150, 130)
        self.assertAlmostEqual(ph, 0.6 / (0.6 + 100 / 230), places=12)
        self.assertAlmostEqual(ph, 0.5798, places=3)
        self.assertAlmostEqual(pa, 0.4202, places=3)
        self.assertAlmostEqual(ph + pa, 1.0, places=12)

    def test_no_vig_even_money_and_symmetry(self):
        self.assertEqual(market.no_vig_pair(100, -100), (0.5, 0.5))
        ph, pa = market.no_vig_pair(-110, -110)
        self.assertAlmostEqual(ph, 0.5)
        self.assertAlmostEqual(pa, 0.5)
        a = market.no_vig_pair(-200, 170)
        b = market.no_vig_pair(170, -200)
        self.assertAlmostEqual(a[0], b[1])
        self.assertAlmostEqual(a[1], b[0])

    def test_break_even(self):
        self.assertAlmostEqual(market.break_even(market.american_to_decimal_profit(-110)), 110 / 210)
        self.assertAlmostEqual(market.break_even(1.0), 0.5)

    def test_ev_cases(self):
        self.assertAlmostEqual(market.estimated_ev(0.5, 100), 0.0)
        self.assertAlmostEqual(market.estimated_ev(0.5, -110), -0.045454545454545, places=12)
        self.assertAlmostEqual(market.estimated_ev(0.4, 150), 0.0)  # fair price
        self.assertAlmostEqual(market.estimated_ev(0.45, 150), 0.125)  # positive at a plus price
        self.assertAlmostEqual(market.estimated_ev(0.6, -150), 0.6 * (2 / 3) - 0.4)  # 0.0 at fair minus price
        self.assertAlmostEqual(market.estimated_ev(0.7, -150), 0.7 * (2 / 3) - 0.3)  # positive at a minus price
        self.assertLess(market.estimated_ev(0.5, -150), 0)
        self.assertEqual(market.ev_per_dollar(0.5, -110), market.estimated_ev(0.5, -110))
        with self.assertRaises(ValueError):
            market.estimated_ev(0.5, 0)

    def test_clv_pp(self):
        self.assertAlmostEqual(market.clv_pp(0.58, 0.55), 3.0)
        self.assertAlmostEqual(market.clv_pp(0.49, 0.52), -3.0)  # crossing even money is just arithmetic
        self.assertIsNone(market.clv_pp(None, 0.5))
        self.assertIsNone(market.clv_pp(0.5, None))


class ConsensusAndPriceTests(unittest.TestCase):
    BOOKS = [
        {"key": "draftkings", "home_ml": -150, "away_ml": 130, "home_spread": -3.0, "total": 44.5},
        {"key": "fanduel", "home_ml": -140, "away_ml": 120, "home_spread": -2.5, "total": 45.0},
        {"key": "betmgm", "home_ml": None, "away_ml": 125, "home_spread": -3.0, "total": None},
        {"key": "betrivers", "home_ml": -150, "away_ml": 130, "home_spread": None, "total": 44.0},
    ]

    def test_consensus_mean_and_medians(self):
        c = market.consensus(self.BOOKS)
        p1 = market.no_vig_pair(-150, 130)[0]
        p2 = market.no_vig_pair(-140, 120)[0]
        self.assertAlmostEqual(c["p_home"], (p1 + p2 + p1) / 3)
        self.assertEqual(c["n_ml"], 3)
        self.assertEqual(c["total"], 44.5)
        self.assertEqual(c["n_total"], 3)
        self.assertEqual(c["home_spread"], -3.0)
        self.assertEqual(c["n_spread"], 3)

    def test_consensus_empty(self):
        c = market.consensus([])
        self.assertEqual(c, {"p_home": None, "n_ml": 0, "total": None, "n_total": 0, "home_spread": None, "n_spread": 0})
        c = market.consensus([{"key": "x", "home_ml": -110, "away_ml": None, "home_spread": None, "total": None}])
        self.assertIsNone(c["p_home"])

    def test_prices_by_book_and_best_price(self):
        keys = list(schema.DEFAULT_BOOKS)
        by = market.prices_by_book(self.BOOKS, "home", "KC", keys)
        self.assertEqual(list(by), keys)
        self.assertEqual(by["draftkings"], {"book": "draftkings", "side": "KC", "american": -150})
        self.assertIsNone(by["betmgm"])
        best = market.best_price(self.BOOKS, "home", "KC", keys)
        self.assertEqual(best, {"book": "fanduel", "side": "KC", "american": -140})
        best_away = market.best_price(self.BOOKS, "away", "DEN", keys)
        self.assertEqual(best_away, {"book": "draftkings", "side": "DEN", "american": 130})  # tie -> first in order
        self.assertEqual(market.best_price(self.BOOKS, "away", "DEN", ["betrivers", "draftkings"])["book"], "betrivers")
        self.assertIsNone(market.best_price(self.BOOKS, "home", "KC", ["pinnacle"]))
        self.assertIsNone(market.best_price([], "home", "KC", keys))
        with self.assertRaises(ValueError):
            market.best_price(self.BOOKS, "KC", "KC", keys)

    def test_market_from_books_and_csv(self):
        m = market.market_from_books(self.BOOKS, "odds_api", "2026-10-09T10:00:00Z")
        self.assertEqual(set(m), schema.MARKET_KEYS)
        self.assertEqual(m["n_books"], 4)
        self.assertEqual(m["source"], "odds_api")
        g = {"home_moneyline": -150, "away_moneyline": 130, "spread_line": 3.0, "total_line": 44.5}
        c = market.market_from_csv(g, "2026-10-09T10:00:00Z")
        self.assertEqual(c["source"], "csv")
        self.assertEqual(c["books"], [{"key": "csv", "home_ml": -150, "away_ml": 130, "home_spread": -3.0, "total": 44.5}])
        self.assertAlmostEqual(c["consensus_p_home"], market.no_vig_pair(-150, 130)[0])
        self.assertEqual(c["consensus_home_spread"], -3.0)
        self.assertIsNone(market.market_from_csv({"home_moneyline": None, "away_moneyline": 130, "spread_line": 3.0, "total_line": 44.5}, "x"))
        c2 = market.market_from_csv({"home_moneyline": -150, "away_moneyline": 130, "spread_line": None, "total_line": None}, "x")
        self.assertIsNone(c2["books"][0]["home_spread"])
        self.assertIsNone(c2["consensus_total"])


class NormalizeOddsApiTests(unittest.TestCase):
    def setUp(self):
        self.payload = load("odds_api_sample.json")
        self.teams = schema.TEAM_META

    def test_fixture_normalises_to_valid_canonical_overlay(self):
        ov = market.normalize_odds_api(self.payload, self.teams, fetched_at="2026-10-09T20:25:00Z")
        self.assertEqual(schema.validate_overlay(ov), [])
        self.assertTrue(ov["canonical"])
        self.assertEqual(ov["source"], "build")
        self.assertFalse(ov["degraded"])
        self.assertEqual(ov["fetched_at"], "2026-10-09T20:25:00Z")
        self.assertEqual(len(ov["games"]), 2)
        jax = ov["games"][0]
        self.assertEqual((jax["home"], jax["away"], jax["commence_time"]), ("JAX", "PHI", "2026-10-11T13:30:00Z"))
        self.assertEqual([b["key"] for b in jax["books"]], ["draftkings", "fanduel", "williamhill_us"])
        self.assertEqual(jax["books"][0], {"key": "draftkings", "home_ml": -375, "away_ml": 295, "home_spread": -7.5, "total": 41.5})
        self.assertEqual(jax["books"][2]["total"], 42.0)
        la = ov["games"][1]
        self.assertEqual((la["home"], la["away"]), ("LA", "BUF"))
        self.assertEqual(la["books"][0], {"key": "draftkings", "home_ml": -178, "away_ml": 150, "home_spread": 3.0, "total": 54.5})

    def test_consensus_of_fixture_matches_hand_arithmetic(self):
        ov = market.normalize_odds_api(self.payload, self.teams)
        c = market.consensus(ov["games"][0]["books"])
        expected = sum(market.no_vig_pair(h, a)[0] for h, a in ((-375, 295), (-400, 315), (-380, 300))) / 3
        self.assertAlmostEqual(c["p_home"], expected)
        self.assertEqual(c["total"], 41.5)
        self.assertEqual(c["home_spread"], -7.5)

    def test_offshore_books_dropped_and_unknown_names_skipped(self):
        payload = json.loads(json.dumps(self.payload))
        payload[0]["bookmakers"].append({"key": "bovada", "title": "Bovada", "markets": payload[0]["bookmakers"][0]["markets"]})
        payload[0]["bookmakers"].append({"key": "draftkings", "title": "dup", "markets": []})  # duplicate key dropped
        payload.append({"id": "x", "commence_time": "2026-10-11T17:00:00Z", "home_team": "London Monarchs", "away_team": "Buffalo Bills", "bookmakers": []})
        payload.append({"id": "y", "commence_time": "not a time", "home_team": "Buffalo Bills", "away_team": "Miami Dolphins", "bookmakers": []})
        ov = market.normalize_odds_api(payload, self.teams)
        self.assertEqual(len(ov["games"]), 2)
        self.assertEqual([b["key"] for b in ov["games"][0]["books"]], ["draftkings", "fanduel", "williamhill_us"])
        self.assertEqual(schema.validate_overlay(ov), [])

    def test_aliases_and_missing_markets(self):
        payload = [
            {
                "commence_time": "2026-10-11T20:25:00.000Z",
                "home_team": "Oakland Raiders",  # alias from teams.json
                "away_team": "St. Louis Rams",
                "bookmakers": [
                    {"key": "betmgm", "markets": [{"key": "h2h", "outcomes": [{"name": "Oakland Raiders", "price": -105}, {"name": "St. Louis Rams", "price": -115}]}]},
                    {"key": "fanduel", "markets": [{"key": "totals", "outcomes": [{"name": "Under", "price": -110, "point": 47.5}]}]},
                    {"key": "betrivers", "markets": [{"key": "h2h", "outcomes": [{"name": "Oakland Raiders", "price": 0}, {"name": "St. Louis Rams", "price": 95}]}]},
                ],
            }
        ]
        ov = market.normalize_odds_api(payload, self.teams)
        g = ov["games"][0]
        self.assertEqual((g["home"], g["away"], g["commence_time"]), ("LV", "LA", "2026-10-11T20:25:00Z"))
        self.assertEqual(g["books"][0], {"key": "betmgm", "home_ml": -105, "away_ml": -115, "home_spread": None, "total": None})
        self.assertEqual(g["books"][1], {"key": "fanduel", "home_ml": None, "away_ml": None, "home_spread": None, "total": 47.5})
        self.assertEqual(g["books"][2], {"key": "betrivers", "home_ml": None, "away_ml": None, "home_spread": None, "total": None})
        self.assertEqual(schema.validate_overlay(ov), [])

    def test_name_map_accepts_model_teams_entries(self):
        teams = [{"team": "KC", "full_name": "Kansas City Chiefs", "aliases": ["KC Chiefs"]}]
        nm = market.name_map(teams)
        self.assertEqual(nm["kansas  city chiefs".replace("  ", " ")], "KC")
        self.assertEqual(nm["kc chiefs"], "KC")
        self.assertEqual(nm["kc"], "KC")

    def test_canonicalize_worker_overlay_fixture(self):
        ov = market.canonicalize_overlay(load("overlay_fixture.json"))
        self.assertEqual(schema.validate_overlay(ov), [])
        self.assertTrue(ov["canonical"])
        self.assertEqual(ov["source"], "worker")
        self.assertEqual([(g["home"], g["away"]) for g in ov["games"]], [("JAX", "PHI"), ("GB", "CHI"), ("NE", "LV"), ("LAC", "DEN"), ("LA", "BUF")])
        raw = load("overlay_fixture.json")
        raw["games"].append({"home": "Nowhere FC", "away": "Buffalo Bills", "commence_time": "2026-10-11T17:00:00Z", "books": []})
        self.assertEqual(len(market.canonicalize_overlay(raw)["games"]), 5)
        self.assertFalse(raw["canonical"], "input not mutated")
        self.assertEqual(market.canonicalize_overlay(ov)["games"], ov["games"], "idempotent on codes")


class MatchOverlayTests(unittest.TestCase):
    def overlay(self, *games):
        return {"schema_version": "1.0.0", "fetched_at": None, "degraded": False, "canonical": True, "source": "build", "games": list(games)}

    @staticmethod
    def og(home, away, when):
        return {"home": home, "away": away, "commence_time": when, "books": []}

    def test_exact_match_and_reversed_rejected(self):
        games = [game("2026_05_PHI_JAX", "JAX", "PHI", "2026-10-11T13:30:00Z")]
        m = market.match_overlay(games, self.overlay(self.og("JAX", "PHI", "2026-10-11T13:30:00Z")))
        self.assertEqual(list(m), ["2026_05_PHI_JAX"])
        self.assertEqual(m["2026_05_PHI_JAX"]["home"], "JAX")
        self.assertEqual(market.match_overlay(games, self.overlay(self.og("PHI", "JAX", "2026-10-11T13:30:00Z"))), {})

    def test_aliases_via_canonicalize_then_match(self):
        games = [game("2026_05_BUF_LA", "LA", "BUF", "2026-10-13T00:15:00Z")]
        raw = {"schema_version": "1.0.0", "fetched_at": None, "degraded": False, "canonical": False, "source": "worker",
               "games": [self.og("St. Louis Rams", "Buffalo Bills", "2026-10-13T00:15:00Z")]}
        self.assertEqual(market.match_overlay(games, raw), {}, "non-canonical overlay never matches")
        self.assertEqual(list(market.match_overlay(games, market.canonicalize_overlay(raw))), ["2026_05_BUF_LA"])

    def test_window_boundary_and_international_game(self):
        # London game: CSV says 09:30 ET -> 13:30Z; feed may carry the same instant or a reschedule
        games = [game("2026_05_MIN_NYJ", "NYJ", "MIN", "2026-10-12T13:30:00Z")]
        self.assertTrue(market.match_overlay(games, self.overlay(self.og("NYJ", "MIN", "2026-10-12T13:30:00Z"))))
        self.assertTrue(market.match_overlay(games, self.overlay(self.og("NYJ", "MIN", "2026-10-14T01:30:00Z"))), "exactly 36 h late")
        self.assertTrue(market.match_overlay(games, self.overlay(self.og("NYJ", "MIN", "2026-10-11T01:30:00Z"))), "exactly 36 h early")
        self.assertEqual(market.match_overlay(games, self.overlay(self.og("NYJ", "MIN", "2026-10-14T01:30:01Z"))), {})
        # blank-time CSV row defaulted to 13:00 ET (17:00Z) still matches a real 20:15 ET kickoff
        games = [game("2026_06_KC_DEN", "DEN", "KC", "2026-10-18T17:00:00Z")]
        self.assertTrue(market.match_overlay(games, self.overlay(self.og("DEN", "KC", "2026-10-19T00:15:00Z"))))

    def test_ambiguity_fails_closed(self):
        # two CSV games with the same teams within 36 h (hypothetical doubleheader): no match
        games = [game("A", "DEN", "KC", "2026-10-18T17:00:00Z"), game("B", "DEN", "KC", "2026-10-19T17:00:00Z")]
        self.assertEqual(market.match_overlay(games, self.overlay(self.og("DEN", "KC", "2026-10-18T20:00:00Z"))), {})
        # same teams a week apart: only the one inside the window matches
        games = [game("A", "DEN", "KC", "2026-10-18T17:00:00Z"), game("B", "DEN", "KC", "2026-10-25T17:00:00Z")]
        self.assertEqual(list(market.match_overlay(games, self.overlay(self.og("DEN", "KC", "2026-10-18T20:00:00Z")))), ["A"])
        # two overlay games claiming one CSV game: dropped
        ov = self.overlay(self.og("DEN", "KC", "2026-10-18T17:00:00Z"), self.og("DEN", "KC", "2026-10-18T21:00:00Z"))
        self.assertEqual(market.match_overlay([game("A", "DEN", "KC", "2026-10-18T17:00:00Z")], ov), {})

    def test_finals_never_match_and_no_match_cases(self):
        played = game("A", "DEN", "KC", "2026-10-18T17:00:00Z", played=True)
        final = game("B", "DEN", "KC", "2026-10-18T17:00:00Z", status="final")
        sched = game("C", "DEN", "KC", "2026-10-18T17:00:00Z", status="scheduled")
        ov = self.overlay(self.og("DEN", "KC", "2026-10-18T17:00:00Z"))
        self.assertEqual(market.match_overlay([played], ov), {})
        self.assertEqual(market.match_overlay([final], ov), {})
        self.assertEqual(list(market.match_overlay([sched], ov)), ["C"])
        self.assertEqual(market.match_overlay([], ov), {})
        self.assertEqual(market.match_overlay([sched], self.overlay()), {})
        self.assertEqual(market.match_overlay([sched], self.overlay(self.og("DEN", "KC", "garbage"))), {})

    def test_fixture_end_to_end(self):
        ov = market.normalize_odds_api(load("odds_api_sample.json"), schema.TEAM_META)
        games = [game("2026_05_PHI_JAX", "JAX", "PHI", "2026-10-11T13:30:00Z"), game("2026_05_BUF_LA", "LA", "BUF", "2026-10-13T00:15:00Z"), game("2026_05_CHI_GB", "GB", "CHI", "2026-10-11T17:00:00Z")]
        m = market.match_overlay(games, ov)
        self.assertEqual(sorted(m), ["2026_05_BUF_LA", "2026_05_PHI_JAX"])
        best = market.best_price(m["2026_05_PHI_JAX"]["books"], "home", "JAX", list(schema.DEFAULT_BOOKS))
        self.assertEqual(best, {"book": "draftkings", "side": "JAX", "american": -375})


class _FakeResponse:
    def __init__(self, status, body, headers):
        self.status = status
        self._body = body
        self.headers = headers

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchOfficialOddsTests(unittest.TestCase):
    KEY = "sk-test-SECRET-123"

    def run_fetch(self, status=200, body=None, headers=None, raise_exc=None):
        calls = []
        payload = load("odds_api_sample.json") if body is None else body
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

        def urlopen(req, timeout):
            calls.append((req, timeout))
            if raise_exc:
                raise raise_exc
            return _FakeResponse(status, raw, headers if headers is not None else {"x-requests-remaining": "397", "x-requests-used": "103"})

        err = io.StringIO()
        with redirect_stderr(err):
            out = market.fetch_official_odds(self.KEY, _urlopen=urlopen, _now=lambda: __import__("datetime").datetime(2026, 10, 9, 10, 2, 41, tzinfo=__import__("datetime").timezone.utc))
        return out, calls, err.getvalue()

    def test_success_logs_remaining_and_never_stores_key(self):
        out, calls, log = self.run_fetch()
        self.assertEqual(len(calls), 1)
        req, timeout = calls[0]
        self.assertEqual(timeout, 10.0)
        self.assertTrue(req.full_url.startswith(market.ODDS_API_URL + "?"))
        self.assertIn("apiKey=" + self.KEY, req.full_url)
        self.assertIn("markets=h2h%2Cspreads%2Ctotals", req.full_url)
        self.assertIn("regions=us", req.full_url)
        self.assertNotIn("bookmakers", req.full_url)
        self.assertEqual(out["fetched_at"], "2026-10-09T10:02:41Z")
        self.assertEqual(out["requests_remaining"], 397)
        self.assertEqual(out["requests_used"], 103)
        self.assertEqual(len(out["payload"]), 2)
        self.assertIn("x-requests-remaining=397", log)
        self.assertNotIn(self.KEY, log)
        self.assertNotIn(self.KEY, json.dumps(out))

    def test_failures_return_none_and_redact_key(self):
        import urllib.error

        for kwargs in (
            {"status": 401},
            {"status": 429},
            {"body": b"<html>bad gateway</html>"},
            {"body": {"message": "not a list"}},
            {"raise_exc": TimeoutError("timed out")},
            {"raise_exc": urllib.error.URLError("dns failed")},
            {"raise_exc": urllib.error.HTTPError("https://x/?apiKey=" + self.KEY, 500, "boom " + self.KEY, {}, None)},
        ):
            out, calls, log = self.run_fetch(**kwargs)
            self.assertIsNone(out, kwargs)
            self.assertEqual(len(calls), 1)
            self.assertIn("falling back to CSV lines", log)
            self.assertNotIn(self.KEY, log, kwargs)

    def test_missing_key_makes_no_call(self):
        calls = []
        self.assertIsNone(market.fetch_official_odds("", _urlopen=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_missing_headers_are_none(self):
        out, _, log = self.run_fetch(headers={})
        self.assertIsNone(out["requests_remaining"])
        self.assertIn("x-requests-remaining=None", log)


if __name__ == "__main__":
    unittest.main()
